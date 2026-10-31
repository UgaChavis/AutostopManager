import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("recover", Path(__file__).parents[1] / "scripts/crm_backup_recover.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Fake:
    def __init__(self):
        self.ids = {"crm": "c" * 64, "scheduler": "s" * 64}
        self.states = {"crm": "inactive", "scheduler": "inactive"}
        self.starts = []
        self.fail = False

    def identity(self, name):
        return self.ids[name]

    def state(self, name):
        return self.states[name]

    def healthy(self, name):
        return self.states[name] == "active"

    def start(self, name):
        self.starts.append(name)
        if self.fail:
            raise m.Uncertain("unknown")
        self.states[name] = "active"


@pytest.fixture
def case(tmp_path):
    p = tmp_path / "journal.json"
    data = {
        name: {"identity": ident, "initially_active": True, "phase": "stopped"}
        for name, ident in [("crm", "c" * 64), ("scheduler", "s" * 64)]
    }
    m._save(p, data)
    return p, data, Fake()


def test_returns_in_order_and_retains_journal(case):
    p, _, fake = case
    out = m.recover(p, fake)
    assert fake.starts == ["crm", "scheduler"]
    assert all(x["phase"] == "restored" for x in out.values())
    assert json.loads(p.read_text()) == out


def test_changed_identity_rejects_before_start(case):
    p, data, fake = case
    fake.ids["scheduler"] = "changed"
    with pytest.raises(m.Uncertain):
        m.recover(p, fake)
    assert not fake.starts and json.loads(p.read_text()) == data


def test_unknown_stop_does_not_start(case):
    p, data, fake = case
    data["crm"]["phase"] = "stop_intent"
    m._save(p, data)
    with pytest.raises(m.Uncertain):
        m.recover(p, fake)
    assert not fake.starts and json.loads(p.read_text()) == data


def test_unknown_start_is_preserved_and_not_replayed(case):
    p, _, fake = case
    fake.fail = True
    with pytest.raises(m.Uncertain):
        m.recover(p, fake)
    record = json.loads(p.read_text())
    assert record["crm"]["phase"] == "start_intent"
    fake.fail = False
    with pytest.raises(m.Uncertain):
        m.recover(p, fake)
    assert fake.starts == ["crm"] and json.loads(p.read_text()) == record


def test_initially_inactive_never_started(case):
    p, data, fake = case
    data["scheduler"] = {"identity": "s" * 64, "initially_active": False, "phase": "untouched"}
    m._save(p, data)
    m.recover(p, fake)
    assert fake.starts == ["crm"]


def test_active_after_simulated_restart_is_not_modified(case):
    p, data, fake = case
    fake.states["crm"] = "active"
    with pytest.raises(m.Uncertain):
        m.recover(p, fake)
    assert not fake.starts and json.loads(p.read_text()) == data
