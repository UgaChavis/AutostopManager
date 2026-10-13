from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("kind", "suffix", "expected"),
    [
        ("download", ".jpg", "download_permissions_invalid"),
        ("photo", ".jpg", "photo_invalid"),
        ("document", ".pdf", "document_invalid"),
        ("audio", ".ogg", "audio_permissions_or_size_invalid"),
        ("video", ".mp4", "video_permissions_or_size_invalid"),
    ],
)
def test_private_media_fifo_is_rejected_before_it_can_block(tmp_path: Path, kind: str, suffix: str, expected: str):
    inbox = tmp_path / "private-media"
    inbox.mkdir(mode=0o700)
    path = inbox / (kind + suffix)
    os.mkfifo(path, mode=0o600)
    script = """
import json
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from autostop_manager import telegram_bridge as b, telegram_transcribe as a, telegram_video_preview as v
path = Path(sys.argv[3])
operations = {
    'download': lambda: b._read_private_download(path, inbox_dir=path.parent),
    'photo': lambda: b._load_validated_photo_file(path, outbox_dir=path.parent),
    'document': lambda: b._load_validated_document_file(path, outbox_dir=path.parent),
    'audio': lambda: a._validate_private_audio(path, inbox_dir=path.parent),
    'video': lambda: v._validate_private_video(path, inbox_dir=path.parent),
}
try:
    operations[sys.argv[2]]()
except (b.BridgeError, a.TranscriptionError, v.VideoPreviewError) as error:
    print(json.dumps({'rejected': True, 'code': error.code}))
else:
    print(json.dumps({'rejected': False}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]), kind, str(path)],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"rejected": True, "code": expected}
    assert path.exists()
