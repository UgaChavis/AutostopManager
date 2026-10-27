"""Unwritable Unicode records fail validation before private journal creation."""

from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
import json
from pathlib import Path
import runpy
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/m2-journal.py"
AT = datetime(2026, 10, 26, tzinfo=UTC)
TEXT_FIELDS = ("task", "before", "actions", "checks", "result", "state")


@pytest.fixture
def journal():
    return runpy.run_path(str(SCRIPT))


def record(**changes):
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "task": "synthetic task",
        "before": "synthetic baseline",
        "actions": "private UTF8 persistence",
        "checks": "synthetic readback",
        "result": "synthetic result",
        "git_sha": None,
        "release_required": False,
        "state": "synthetic state",
        **changes,
    }


def escaped_input(field, escape):
    value = record()
    value[field] = json.loads('"' + escape + '"')
    # Exercise the value produced by an actual ASCII JSON record.
    return json.loads(json.dumps(value, ensure_ascii=True))


@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("escape", [r"\ud800", r"\udc00"], ids=["lone-high", "lone-low"])
def test_lone_surrogate_is_rejected_by_public_validation(journal, field, escape):
    value = escaped_input(field, escape)
    original = deepcopy(value)

    with pytest.raises(ValueError, match=r"^journal_record_text$") as error:
        journal["validate"](value)

    assert type(error.value) is ValueError
    assert value == original


@pytest.mark.parametrize("field,escape", [("task", r"\ud800"), ("state", r"\udc00")])
def test_invalid_utf8_append_never_creates_role_parent(journal, tmp_path, field, escape):
    role = tmp_path / "absent" / "M2"

    with pytest.raises(ValueError, match=r"^journal_record_text$") as error:
        journal["run"](root=role, record=escaped_input(field, escape), at=AT)

    assert type(error.value) is ValueError
    assert not role.parent.exists()  # No root, lock, weekly or pending writes.


@pytest.mark.parametrize(
    "text", [json.loads(r'"\ud83d\ude97"'), "Синтетическая задача"], ids=["decoded-emoji", "cyrillic"]
)
def test_valid_unicode_all_text_fields_append_and_roundtrip(journal, tmp_path, text):
    value = record(**dict.fromkeys(TEXT_FIELDS, text))
    value = json.loads(json.dumps(value, ensure_ascii=True))
    original = deepcopy(value)
    role = tmp_path / "M2"
    assert journal["validate"](value) is None

    report = journal["run"](root=role, record=value, at=AT)

    assert report == {"week": "2026-W44", "entries": 1, "commits": 0, "release_records": 0}
    assert journal["records"](role / "journal/2026-W44.md") == [{**original, "at": AT.isoformat()}]
    assert (role / "current-state.md").read_text(encoding="utf-8") == "# M2 — текущее состояние\n\n" + text + "\n"
    assert text.encode("utf-8") in (role / "journal/2026-W44.md").read_bytes()
    assert not (role / ".pending-record.json").exists()
    assert value == original


def test_cli_append_uses_validated_utf8_record_and_can_be_read_back(journal, tmp_path, monkeypatch, capsys):
    role = tmp_path / "M2"
    value = record(task="Синтетическая задача 🚗", state="Синтетическое состояние")
    supplied = tmp_path / "record.json"
    supplied.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
    supplied.chmod(0o600)
    actual_run = journal["run"]
    monkeypatch.setitem(journal["main"].__globals__, "run", partial(actual_run, root=role, at=AT))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "append", "--record", str(supplied)])

    journal["main"]()

    assert json.loads(capsys.readouterr().out)["entries"] == 1
    assert journal["records"](role / "journal/2026-W44.md") == [{**value, "at": AT.isoformat()}]
    assert not (role / ".pending-record.json").exists()


@pytest.mark.parametrize(
    "field,text",
    [("task", "synthetic\x00text"), ("state", "synthetic```m2-record"), ("task", "x" * 2001), ("state", "x" * 8001)],
    ids=["nul", "record-marker", "text-limit", "state-limit"],
)
def test_existing_forbidden_text_and_character_limits_remain(journal, field, text):
    with pytest.raises(ValueError, match=r"^journal_record_text$"):
        journal["validate"](record(**{field: text}))


@pytest.mark.parametrize(
    "field,text", [("task", "я" * 2000), ("state", "🚗" * 8000)], ids=["text-limit", "state-limit"]
)
def test_valid_multibyte_text_keeps_character_rather_than_byte_limit(journal, field, text):
    assert journal["validate"](record(**{field: text})) is None
