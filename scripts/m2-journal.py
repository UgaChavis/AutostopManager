"""Rotate M2 journals and recover an interrupted technical record transaction."""

from __future__ import annotations

import argparse
from datetime import datetime, UTC
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import UUID

ROOT = Path("/var/lib/autostop-manager/roles/M2")
TEXT_FIELDS = ("task", "before", "actions", "checks", "result", "state")
FIELDS = {"id", "git_sha", "release_required", *TEXT_FIELDS}
RECORD = re.compile(r"(?m)^```m2-record\n(.*?)\n```$", re.S)
SUMMARY_LIMIT = 10
SUMMARY_RESULT_CHARS = 300


def atomic_write(path, text):
    if path.is_symlink():
        raise ValueError("journal_symlink")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate(record):
    if set(record) != FIELDS:
        raise ValueError("journal_record_fields")
    UUID(record["id"])
    for field in TEXT_FIELDS:
        text = record[field]
        if not isinstance(text, str) or not text.strip() or len(text) > (8000 if field == "state" else 2000):
            raise ValueError("journal_record_text")
        if "\x00" in text or "```m2-record" in text:
            raise ValueError("journal_record_text")
    sha = record["git_sha"]
    if sha is not None and (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)):
        raise ValueError("journal_record_sha")
    if type(record["release_required"]) is not bool:
        raise ValueError("journal_record_release")


def validate_state_precondition(token, record):
    if token is None:
        return
    if not isinstance(token, str) or (token != "absent" and re.fullmatch(r"[0-9a-f]{64}", token) is None):
        raise ValueError("journal_state_precondition_invalid")
    if record is None:
        raise ValueError("journal_state_precondition_requires_record")


def check_state_precondition(root, token):
    path = root / "current-state.md"
    if path.is_symlink():
        raise ValueError("journal_symlink")
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        current = "absent"
    else:
        current = hashlib.sha256(content).hexdigest()
    if current != token:
        raise ValueError("journal_state_conflict")


def week_for(at):
    if at.tzinfo is None:
        raise ValueError("journal_utc_required")
    year, week, _day = at.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def ensure_week(root, week):
    for directory in (root, root / "journal", root / "summaries"):
        if directory.is_symlink():
            raise ValueError("journal_symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "journal" / (week + ".md")
    if path.is_symlink():
        raise ValueError("journal_symlink")
    if not path.exists():
        atomic_write(path, f"# M2 — журнал {week}\n\nДаты UTC. Только технические сведения.\n")
    return path


def records(path):
    if path.is_symlink():
        raise ValueError("journal_symlink")
    return [json.loads(block) for block in RECORD.findall(path.read_text())]


def canonical_record(record):
    """Compare UUID aliases without changing the stored record spelling."""
    return {**record, "id": str(UUID(record["id"]))}


def publish_pointers(root, week):
    path = ensure_week(root, week)
    entries = records(path)
    commits = {entry["git_sha"] for entry in entries if entry["git_sha"]}
    release_records = sum(entry["release_required"] for entry in entries)
    results = (
        "\n".join(
            f"- {entry['at']}: {' '.join(entry['result'].split())[:SUMMARY_RESULT_CHARS]}"
            for entry in entries[-SUMMARY_LIMIT:]
        )
        or "Записей пока нет."
    )
    atomic_write(
        root / "summaries" / (week + ".md"),
        f"# M2 — сводка {week}\n\nПо записям недели: {len(entries)} результатов, {len(commits)} коммитов, "
        f"{release_records} записей с отметкой о необходимости выпуска.\n\nПоследние {SUMMARY_LIMIT} результатов; полные записи — "
        f"[в журнале](../journal/{week}.md).\n\n{results}\n",
    )
    history = "\n".join(f"- [{p.stem}]({p.name})" for p in sorted((root / "journal").glob("????-W??.md")))
    completed = [p.stem for p in sorted((root / "journal").glob("????-W??.md")) if records(p)]
    latest = completed[-1] if completed else week
    atomic_write(
        root / "journal/INDEX.md",
        f"# M2 — индекс\n\n[Текущее состояние](../current-state.md)\n\n"
        f"Текущая ISO-неделя UTC: [{week}]({week}.md). "
        f"[Сводка недели](../summaries/{week}.md); [последняя сводка с результатами](../summaries/{latest}.md).\n\n## История\n\n{history}\n",
    )
    return {"week": week, "entries": len(entries), "commits": len(commits), "release_records": release_records}


def recover(root):
    pending = root / ".pending-record.json"
    if pending.is_symlink():
        raise ValueError("journal_symlink")
    if not pending.exists():
        return
    payload = json.loads(pending.read_text())
    record = {key: value for key, value in payload.items() if key != "at"}
    validate(record)
    week = week_for(datetime.fromisoformat(payload["at"]))
    path = ensure_week(root, week)
    normalized = canonical_record(payload)
    existing = [entry for entry in records(path) if str(UUID(entry["id"])) == normalized["id"]]
    if any(canonical_record(entry) != normalized for entry in existing):
        raise ValueError("journal_record_conflict")
    if not existing:
        block = (
            "\n## "
            + payload["at"]
            + "\n\n```m2-record\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n```\n"
        )
        atomic_write(path, path.read_text().rstrip() + "\n" + block)
    atomic_write(root / "current-state.md", "# M2 — текущее состояние\n\n" + record["state"].strip() + "\n")
    publish_pointers(root, week)
    pending.unlink()


def run(root=ROOT, record=None, at=None, *, expected_state_sha256=None):
    validate_state_precondition(expected_state_sha256, record)
    if record is not None:
        validate(record)
    at = at or datetime.now(UTC)
    week = week_for(at)
    if root.is_symlink():
        raise ValueError("journal_symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = root / ".journal.lock"
    if lock.is_symlink():
        raise ValueError("journal_symlink")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        ensure_week(root, week)
        recover(root)
        if record is not None:
            normalized = canonical_record(record)
            existing = [
                entry
                for path in (root / "journal").glob("????-W??.md")
                for entry in records(path)
                if str(UUID(entry["id"])) == normalized["id"]
            ]
            if existing:
                if any(
                    canonical_record({k: v for k, v in entry.items() if k != "at"}) != normalized for entry in existing
                ):
                    raise ValueError("journal_record_conflict")
            else:
                if expected_state_sha256 is not None:
                    check_state_precondition(root, expected_state_sha256)
                payload = {**record, "at": at.astimezone(UTC).isoformat()}
                atomic_write(root / ".pending-record.json", json.dumps(payload, ensure_ascii=False))
                recover(root)
        if not (root / "current-state.md").exists():
            atomic_write(root / "current-state.md", "# M2 — текущее состояние\n\nНужна первая техническая проверка.\n")
        return publish_pointers(root, week)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("start", "append"))
    parser.add_argument(
        "--record",
        type=Path,
        help="Private JSON with id, task, before, actions, checks, result, git_sha, release_required, state",
    )
    parser.add_argument(
        "--expected-state-sha256",
        help="Expected current-state.md SHA-256 (64 lowercase hex) or absent; append only",
    )
    args = parser.parse_args()
    if (args.operation == "append") != (args.record is not None):
        parser.error("append requires --record; start has no record")
    if args.expected_state_sha256 is not None and args.operation != "append":
        parser.error("--expected-state-sha256 requires append")
    record = json.loads(args.record.read_text()) if args.record else None
    print(json.dumps(run(record=record, expected_state_sha256=args.expected_state_sha256)))


if __name__ == "__main__":
    main()
