#!/usr/bin/env python3
"""Local free image toolkit for the Cortex/Codex server agent.

The script intentionally keeps heavy AI generation out of the VPS path.  This
server has no visible GPU and limited free disk, so the useful local layer is
inspection, cleanup, conversion, cutouts, OCR, and post-processing around the
agent's image-generation/editing tools.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError


COMMANDS = [
    "convert",
    "identify",
    "ffmpeg",
    "vipsthumbnail",
    "cwebp",
    "dwebp",
    "jpegoptim",
    "optipng",
    "pngquant",
    "gifsicle",
    "exiftool",
    "tesseract",
    "potrace",
]


def _json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _open_image(path: Path) -> Image.Image:
    try:
        return ImageOps.exif_transpose(Image.open(path))
    except UnidentifiedImageError as exc:
        raise SystemExit(f"Not an image: {path}") from exc


def _save_image(image: Image.Image, out: Path, quality: int = 92) -> None:
    _ensure_parent(out)
    suffix = out.suffix.lower()
    params: dict[str, object] = {}

    if suffix in {".jpg", ".jpeg"}:
        if image.mode in {"RGBA", "LA", "P"}:
            background = Image.new("RGB", image.size, "white")
            if image.mode == "P":
                image = image.convert("RGBA")
            background.paste(image, mask=image.getchannel("A") if "A" in image.getbands() else None)
            image = background
        else:
            image = image.convert("RGB")
        params.update({"quality": quality, "optimize": True, "progressive": True})
    elif suffix == ".png":
        params.update({"optimize": True})
    elif suffix == ".webp":
        params.update({"quality": quality, "method": 6})

    image.save(out, **params)


def _info(path: Path) -> dict[str, object]:
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original)
        fmt = original.format
        exif_tags = len(original.getexif() or {})
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "format": fmt,
        "mode": image.mode,
        "width": image.width,
        "height": image.height,
        "has_alpha": "A" in image.getbands(),
        "exif_tags": exif_tags,
    }


def _module_status(module: str, env: dict[str, str] | None = None) -> dict[str, object]:
    code = (
        "import importlib, json; "
        f"m=importlib.import_module({module!r}); "
        "print(json.dumps({'ok': True, 'version': getattr(m, '__version__', 'unknown')}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"ok": True, "version": "unknown"}
    return {"ok": False, "error": result.stderr.strip() or result.stdout.strip()}


def cmd_doctor(_: argparse.Namespace) -> None:
    env = os.environ.copy()
    env.setdefault("NUMBA_DISABLE_JIT", "1")
    _json(
        {
            "python": sys.executable,
            "commands": {name: shutil.which(name) for name in COMMANDS},
            "modules": {
                "PIL": _module_status("PIL"),
                "cv2": _module_status("cv2"),
                "numpy": _module_status("numpy"),
                "onnxruntime": _module_status("onnxruntime"),
                "rembg": _module_status("rembg", env=env),
            },
            "rembg_runtime_env": {"NUMBA_DISABLE_JIT": env["NUMBA_DISABLE_JIT"]},
        }
    )


def cmd_inspect(args: argparse.Namespace) -> None:
    _json(_info(_path(args.input)))


def cmd_resize(args: argparse.Namespace) -> None:
    image = _open_image(_path(args.input))
    out = _path(args.output)

    if args.width or args.height:
        width = args.width or image.width
        height = args.height or image.height
        if args.cover:
            image = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)
        else:
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
    elif args.max_edge:
        image.thumbnail((args.max_edge, args.max_edge), Image.Resampling.LANCZOS)

    _save_image(image, out, args.quality)
    _json({"ok": True, "output": str(out), "info": _info(out)})


def cmd_autofix(args: argparse.Namespace) -> None:
    image = _open_image(_path(args.input))
    out = _path(args.output)

    has_alpha = "A" in image.getbands()
    alpha = image.getchannel("A") if has_alpha else None
    base = image.convert("RGB")
    base = ImageOps.autocontrast(base, cutoff=args.cutoff)
    base = ImageEnhance.Color(base).enhance(args.color)
    base = ImageEnhance.Contrast(base).enhance(args.contrast)
    base = ImageEnhance.Sharpness(base).enhance(args.sharpness)

    if alpha is not None and out.suffix.lower() not in {".jpg", ".jpeg"}:
        base.putalpha(alpha)

    _save_image(base, out, args.quality)
    _json({"ok": True, "output": str(out), "info": _info(out)})


def cmd_upscale(args: argparse.Namespace) -> None:
    image = _open_image(_path(args.input))
    out = _path(args.output)
    if args.width and args.height:
        size = (args.width, args.height)
    else:
        size = (max(1, round(image.width * args.factor)), max(1, round(image.height * args.factor)))
    image = image.resize(size, Image.Resampling.LANCZOS)
    if args.sharpness != 1:
        alpha = image.getchannel("A") if "A" in image.getbands() else None
        base = ImageEnhance.Sharpness(image.convert("RGB")).enhance(args.sharpness)
        if alpha is not None and out.suffix.lower() not in {".jpg", ".jpeg"}:
            base.putalpha(alpha)
        image = base
    _save_image(image, out, args.quality)
    _json({"ok": True, "output": str(out), "info": _info(out)})


def cmd_denoise(args: argparse.Namespace) -> None:
    import cv2

    image = _open_image(_path(args.input)).convert("RGBA")
    alpha = image.getchannel("A")
    rgb = np.asarray(image.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    denoised = cv2.fastNlMeansDenoisingColored(
        bgr,
        None,
        h=args.luminance,
        hColor=args.color,
        templateWindowSize=args.template_window,
        searchWindowSize=args.search_window,
    )
    result = Image.fromarray(cv2.cvtColor(denoised, cv2.COLOR_BGR2RGB), "RGB")
    out = _path(args.output)
    if out.suffix.lower() not in {".jpg", ".jpeg"}:
        result.putalpha(alpha)
    _save_image(result, out, args.quality)
    _json({"ok": True, "output": str(out), "info": _info(out)})


def cmd_chroma_key(args: argparse.Namespace) -> None:
    image = _open_image(_path(args.input)).convert("RGBA")
    arr = np.asarray(image).astype(np.float32)

    if args.key == "auto":
        border = np.concatenate(
            [
                arr[0, :, :3],
                arr[-1, :, :3],
                arr[:, 0, :3],
                arr[:, -1, :3],
            ],
            axis=0,
        )
        key = np.median(border, axis=0)
    else:
        value = args.key.lstrip("#")
        if len(value) != 6:
            raise SystemExit("--key must be auto or a hex color like #00ff00")
        key = np.array([int(value[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)

    dist = np.linalg.norm(arr[:, :, :3] - key, axis=2)
    alpha = np.clip((dist - args.transparent_threshold) / max(args.softness, 1), 0, 1) * 255
    alpha = np.minimum(alpha, arr[:, :, 3])
    arr[:, :, 3] = alpha

    output = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")
    out = _path(args.output)
    _save_image(output, out, args.quality)
    _json({"ok": True, "output": str(out), "key_rgb": [round(float(x), 2) for x in key], "info": _info(out)})


def cmd_bg_remove(args: argparse.Namespace) -> None:
    os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
    from rembg import new_session, remove

    src = _path(args.input)
    out = _path(args.output)
    _ensure_parent(out)

    session = new_session(args.model)
    data = src.read_bytes()
    result = remove(
        data,
        session=session,
        alpha_matting=args.alpha_matting,
        alpha_matting_foreground_threshold=args.foreground_threshold,
        alpha_matting_background_threshold=args.background_threshold,
        alpha_matting_erode_size=args.erode_size,
    )
    out.write_bytes(result)
    _json({"ok": True, "output": str(out), "model": args.model, "info": _info(out)})


def cmd_ocr(args: argparse.Namespace) -> None:
    image = _path(args.input)
    if not shutil.which("tesseract"):
        raise SystemExit("tesseract is not installed")
    command = ["tesseract", str(image), "stdout", "-l", args.lang, "--psm", str(args.psm)]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip())
    if args.json:
        _json({"ok": True, "text": result.stdout})
    else:
        print(result.stdout, end="")


def cmd_strip_metadata(args: argparse.Namespace) -> None:
    image = _open_image(_path(args.input))
    out = _path(args.output)
    _save_image(image.copy(), out, args.quality)
    _json({"ok": True, "output": str(out), "info": _info(out)})


def _grid_size(count: int, cols: int | None) -> tuple[int, int]:
    if cols and cols > 0:
        return cols, math.ceil(count / cols)
    cols = math.ceil(math.sqrt(count))
    return cols, math.ceil(count / cols)


def cmd_contact_sheet(args: argparse.Namespace) -> None:
    paths = [_path(value) for value in args.inputs]
    if not paths:
        raise SystemExit("No input images")

    thumbs: list[Image.Image] = []
    for path in paths:
        image = _open_image(path).convert("RGB")
        image.thumbnail((args.thumb, args.thumb), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (args.thumb, args.thumb), "white")
        canvas.paste(image, ((args.thumb - image.width) // 2, (args.thumb - image.height) // 2))
        thumbs.append(canvas)

    cols, rows = _grid_size(len(thumbs), args.cols)
    sheet = Image.new("RGB", (cols * args.thumb, rows * args.thumb), "white")
    for index, thumb in enumerate(thumbs):
        x = (index % cols) * args.thumb
        y = (index // cols) * args.thumb
        sheet.paste(thumb, (x, y))

    out = _path(args.output)
    _save_image(sheet, out, args.quality)
    _json({"ok": True, "output": str(out), "count": len(paths), "cols": cols, "rows": rows, "info": _info(out)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Free local image toolkit for Cortex/Codex agent workflows.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="Check installed image tools and Python modules.")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("inspect", help="Return compact JSON image metadata.")
    p.add_argument("input")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("resize", help="Resize or crop an image.")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--max-edge", type=int)
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--cover", action="store_true", help="Center-crop to exact width/height instead of fitting inside.")
    p.add_argument("--quality", type=int, default=92)
    p.set_defaults(func=cmd_resize)

    p = sub.add_parser("autofix", help="Mild automatic contrast/color/sharpness cleanup.")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--cutoff", type=float, default=0.5)
    p.add_argument("--color", type=float, default=1.03)
    p.add_argument("--contrast", type=float, default=1.04)
    p.add_argument("--sharpness", type=float, default=1.15)
    p.add_argument("--quality", type=int, default=92)
    p.set_defaults(func=cmd_autofix)

    p = sub.add_parser("upscale", help="Deterministic CPU upscaling with LANCZOS resampling.")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--factor", type=float, default=2)
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--sharpness", type=float, default=1.08)
    p.add_argument("--quality", type=int, default=92)
    p.set_defaults(func=cmd_upscale)

    p = sub.add_parser("denoise", help="CPU photo denoise with OpenCV non-local means.")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--luminance", type=float, default=5)
    p.add_argument("--color", type=float, default=5)
    p.add_argument("--template-window", type=int, default=7)
    p.add_argument("--search-window", type=int, default=21)
    p.add_argument("--quality", type=int, default=92)
    p.set_defaults(func=cmd_denoise)

    p = sub.add_parser("chroma-key", help="Remove a flat chroma-key background to alpha.")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--key", default="auto", help="auto or hex color, for example #00ff00")
    p.add_argument("--transparent-threshold", type=float, default=18)
    p.add_argument("--softness", type=float, default=36)
    p.add_argument("--quality", type=int, default=92)
    p.set_defaults(func=cmd_chroma_key)

    p = sub.add_parser("bg-remove", help="Remove photo background with rembg/ONNX CPU model.")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--model", default="u2netp", help="rembg model name; u2netp is smaller and good for server use.")
    p.add_argument("--alpha-matting", action="store_true")
    p.add_argument("--foreground-threshold", type=int, default=240)
    p.add_argument("--background-threshold", type=int, default=10)
    p.add_argument("--erode-size", type=int, default=10)
    p.set_defaults(func=cmd_bg_remove)

    p = sub.add_parser("ocr", help="OCR image text with Tesseract.")
    p.add_argument("input")
    p.add_argument("--lang", default="rus+eng")
    p.add_argument("--psm", type=int, default=6)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("strip-metadata", help="Rewrite image without EXIF metadata.")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument("--quality", type=int, default=92)
    p.set_defaults(func=cmd_strip_metadata)

    p = sub.add_parser("contact-sheet", help="Create a grid preview from multiple images.")
    p.add_argument("output")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--thumb", type=int, default=320)
    p.add_argument("--cols", type=int)
    p.add_argument("--quality", type=int, default=92)
    p.set_defaults(func=cmd_contact_sheet)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
