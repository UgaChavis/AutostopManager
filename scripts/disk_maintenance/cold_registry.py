"""Explicit historical work-Telegram cold keepers, never current/previous hot."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from . import cold_cas
from .util import MaintenanceError, read_json, sha256_file


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise MaintenanceError(code)


def records(policy: dict) -> dict[str, dict]:
    inventory = policy["inventory"]
    configured = inventory.get("cold_tg_runtimes", [])
    _require(isinstance(configured, list) and len(configured) <= 8, "cold_registry_invalid")
    if not configured:
        return {}
    coherent = inventory.get("coherent_keep_paths", [])
    hot = inventory.get("hot_coherent_keep_paths", [])
    _require(isinstance(hot, list) and len(hot) == 2 and coherent and hot[0] == coherent[0], "cold_hot_tuples_invalid")
    runtime_root = Path(inventory["roots"].get("tg_runtimes", ""))
    _require(runtime_root.is_absolute(), "cold_runtime_root_invalid")
    hot_paths = set()
    for item in hot:
        _require(isinstance(item, list) and len(item) == 3 and item in coherent, "cold_hot_tuples_invalid")
        _require(all(isinstance(value, str) for value in item), "cold_hot_tuples_invalid")
        _require(Path(item[2]).parent == runtime_root, "cold_hot_tuples_invalid")
        hot_paths.update(item)
    _require(hot[0][0] != hot[1][0] and hot[0][1] != hot[1][1], "cold_hot_tuples_invalid")
    for key, position in (("manager_releases", 0), ("work_tg_releases", 1)):
        current = Path(inventory["roots"].get(key, "")) / "current"
        if current.is_symlink():
            _require(str(current.resolve()) == hot[0][position], "cold_current_changed")
    historical = {row[2]: row for row in coherent if isinstance(row, list) and len(row) == 3}
    result = {}
    for record in configured:
        _require(isinstance(record, dict), "cold_registry_invalid")
        keys = {"path", "revision", "root_id", "bundle_dir", "manifest_sha256", "rehearsal_path", "rehearsal_sha256"}
        _require(
            set(record) == keys and all(isinstance(value, str) for value in record.values()), "cold_registry_invalid"
        )
        path = Path(record["path"])
        _require(
            path.parent == runtime_root and cold_cas.REVISION.fullmatch(path.name) is not None,
            "cold_runtime_path_invalid",
        )
        _require(record["revision"] == path.name and record["path"] in historical, "cold_runtime_binding_invalid")
        _require(record["path"] not in hot_paths and record["path"] not in result, "cold_hot_runtime_forbidden")
        for key in ("bundle_dir", "rehearsal_path"):
            value = Path(record[key])
            _require(value.is_absolute() and ".." not in value.parts, "cold_registry_path_invalid")
        result[record["path"]] = record
    return result


def attest(record: dict, *, full: bool = False) -> tuple[dict, dict]:
    bundle = Path(record["bundle_dir"])
    manifest = cold_cas.load_bundle(bundle, record["manifest_sha256"])
    roots = [row for row in manifest["roots"] if row["id"] == record["root_id"]]
    _require(len(roots) == 1, "cold_registry_root_invalid")
    root = roots[0]
    _require(
        root["kind"] == "tg_runtime"
        and root["source_path"] == record["path"]
        and root["revision"] == record["revision"],
        "cold_runtime_binding_invalid",
    )
    receipt_path = Path(record["rehearsal_path"])
    receipt = read_json(receipt_path, private=True)
    _require(sha256_file(receipt_path) == record["rehearsal_sha256"], "cold_rehearsal_hash_mismatch")
    _require(
        receipt.get("schema") == "autostop.tg-cold-rehearsal.v1" and receipt.get("ok") is True, "cold_rehearsal_invalid"
    )
    for field in ("root_id", "revision", "manifest_sha256"):
        _require(receipt.get(field) == record[field], "cold_rehearsal_binding_invalid")
    _require(receipt.get("source_path") == record["path"], "cold_rehearsal_binding_invalid")
    required = {
        "full_tree_sha_metadata",
        "separate_inode_groups",
        "package_pins",
        "python_abi",
        "model_manifest",
        "native_readiness",
        "network_isolated",
        "originals_unchanged",
    }
    _require(
        isinstance(receipt.get("checks"), dict)
        and set(receipt["checks"]) == required
        and all(value is True for value in receipt["checks"].values()),
        "cold_rehearsal_incomplete",
    )
    _require(
        receipt.get("platform") == manifest["platform"]
        and receipt.get("provenance_sha256") == hashlib.sha256(cold_cas.canonical(root["provenance"])).hexdigest(),
        "cold_rehearsal_provenance_invalid",
    )
    if full:
        cold_cas.verify_bundle(bundle, record["manifest_sha256"])
    return manifest, root


def verify(policy: dict, *, full: bool) -> list[dict]:
    result = []
    carriers = set()
    for path, record in records(policy).items():
        key = (record["bundle_dir"], record["manifest_sha256"])
        _, root = attest(record, full=full and key not in carriers)
        carriers.add(key)
        runtime = Path(path)
        try:
            info = runtime.lstat()
        except FileNotFoundError:
            pass
        else:
            _require(stat.S_ISDIR(info.st_mode), "cold_tree_kind_mismatch")
            cold_cas.verify_tree(runtime, root)
        result.append(
            {
                "component": "coherent_runtime",
                "path": path,
                "storage": "attested_cold",
                "ok": True,
                "manifest_sha256": record["manifest_sha256"],
                "rehearsal_sha256": record["rehearsal_sha256"],
            }
        )
    return result
