"""Manual registry behavior and one-way retirement of legacy role executors."""

from __future__ import annotations
import json
import sqlite3
from uuid import uuid4
import pytest
from autostop_manager.role_coordination import RoleError, RoleStore
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import AUTOMATION_TEMPLATES, AutomationStore


@pytest.fixture
def store(tmp_path):
    return RoleStore(tmp_path / "registry.sqlite3")


def action(store, operation, role="M2", payload=None, *, actor="owner", revision=None, key=None):
    return store.action(
        operation, role, payload, idempotency_key=key or str(uuid4()), expected_revision=revision, actor=actor
    )


def task(store, resource="repo:one"):
    return action(
        store,
        "task_create",
        payload={
            "title": "Inspect code",
            "objective": "Fix issue",
            "resource": resource,
            "acceptance": "Verified result",
        },
    )["item"]


def test_read_is_nonmutating_and_has_no_runtime_bindings(store):
    assert store.read()["roles"] == [
        {"role": role, "revision": 0, "context": "", "updated_at": None} for role in ("M1", "M2")
    ]
    assert not store.path.exists()


@pytest.mark.parametrize("operation", ["bind", "start", "stop", "stop_all", "sleep", "reconcile_turn"])
def test_retired_actions_fail_before_storage_or_effects(store, operation):
    with pytest.raises(RoleError, match="role_action_invalid"):
        action(store, operation)
    assert not store.path.exists()


def test_manual_tasks_work_without_duty_and_keep_resource_claim_guard(store):
    item = task(store)
    with pytest.raises(RoleError, match="role_task_resource_busy"):
        task(store)
    claimed = action(store, "task_claim", payload={"id": item["id"]}, revision=item["revision"], actor="M2")["item"]
    assert claimed["status"] == "running"
    result = action(
        store,
        "task_result",
        payload={"id": item["id"], "result": "Fixed", "verification": "Tests passed"},
        revision=claimed["revision"],
        actor="M2",
    )["item"]
    assert result["status"] == "review"
    accepted = action(
        store,
        "task_accept",
        role="M1",
        payload={"id": item["id"], "verification": "Accepted"},
        revision=result["revision"],
        actor="M1",
    )["item"]
    assert accepted["status"] == "completed"
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM role_items WHERE kind='task_notice'").fetchone()[0] == 0
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='role_dispatches'").fetchone() is None


def test_task_cas_idempotency_and_claim_priority(store):
    item = task(store)
    a = action(store, "task_claim", payload={"id": item["id"]}, revision=item["revision"], key="claim")
    assert action(store, "task_claim", payload={"id": item["id"]}, revision=item["revision"], key="claim") == a
    with pytest.raises(RoleError, match="role_idempotency_conflict"):
        action(store, "task_claim", payload={"id": "different"}, revision=item["revision"], key="claim")
    with pytest.raises(RoleError, match="role_revision_conflict"):
        action(store, "task_result", payload={"id": item["id"]}, revision=item["revision"])


def test_manual_checkpoint_is_revisioned_and_preserves_technical_journal(store):
    d = store.views_root / "M2"
    d.mkdir(parents=True)
    journal = d / "current-state.md"
    journal.write_text("technical context")
    role = action(store, "checkpoint", payload={"text": "Manual context"}, revision=0)["role"]
    assert set(role) == {"role", "revision", "context", "updated_at"}
    assert role["context"] == "Manual context"
    assert journal.read_text() == "technical context"
    assert (d / "context.md").is_file()
    assert not (d / "duty-state.md").exists()


def test_approval_requires_owner_exact_proposal_and_one_consumption(store):
    data = {
        "action": "release",
        "target": "manager",
        "target_revision": "one",
        "proposal_ref": "plan:one",
        "proposal_hash": "a" * 64,
    }
    item = action(store, "approval_create", payload=data)["item"]
    with pytest.raises(RoleError, match="role_approval_decision_denied"):
        action(
            store,
            "approval_decide",
            payload={"id": item["id"], "decision": "approved", "decision_ref": "owner:yes"},
            actor="M2",
            revision=item["revision"],
        )
    item = action(
        store,
        "approval_decide",
        payload={"id": item["id"], "decision": "approved", "decision_ref": "owner:yes"},
        revision=item["revision"],
    )["item"]
    with pytest.raises(RoleError, match="role_approval_stale"):
        action(
            store,
            "approval_consume",
            payload={**data, "id": item["id"], "target_revision": "two"},
            revision=item["revision"],
        )
    item = action(store, "approval_consume", payload={**data, "id": item["id"]}, revision=item["revision"])["item"]
    assert item["status"] == "used"
    with pytest.raises(RoleError, match="role_approval_not_available"):
        action(store, "approval_consume", payload={**data, "id": item["id"]}, revision=item["revision"])


def legacy(store, status="completed"):
    db = sqlite3.connect(store.path)
    db.executescript("""
      CREATE TABLE role_state(role TEXT PRIMARY KEY,revision INTEGER,context TEXT,updated_at TEXT,
      thread_id TEXT,model TEXT,goal_owned INTEGER,desired_state TEXT);
      INSERT INTO role_state VALUES('M2',17,'preserved context','before','old-chat','old-model',1,'on');
      CREATE TABLE role_items(id TEXT PRIMARY KEY,role TEXT,kind TEXT,status TEXT,revision INTEGER,data TEXT,created_at TEXT,updated_at TEXT);
      INSERT INTO role_items VALUES('task-old','M2','task','running',4,'{"source":"owner","resource":"repo:old","claimed_by":"M2"}','before','before');
      CREATE TABLE role_commands(key TEXT PRIMARY KEY,request_hash TEXT,response TEXT,created_at TEXT);
      INSERT INTO role_commands VALUES('receipt-old','hash','{"ok":true,"item":{"id":"task-old"}}','before');
      CREATE TABLE role_dispatches(id TEXT PRIMARY KEY,role TEXT,kind TEXT,item_id TEXT,thread_id TEXT,
      turn_id TEXT,before_turn_id TEXT,status TEXT,started_at TEXT,updated_at TEXT,error TEXT);
    """)
    db.execute(
        "INSERT INTO role_dispatches VALUES('dispatch-old','M2','duty',NULL,'old-chat','old-turn','prior-turn',?,'before','before',NULL)",
        (status,),
    )
    db.commit()
    db.close()


def test_legacy_migration_preserves_manual_records_receipts_and_never_replays(store):
    legacy(store)
    with sqlite3.connect(store.path) as db:
        before = db.execute("SELECT * FROM role_items").fetchall()
        commands = db.execute("SELECT * FROM role_commands").fetchall()
    store.initialize()
    store.initialize()
    with store.connection() as db:
        item = db.execute("SELECT * FROM role_items").fetchone()
        assert item["id"] == before[0][0] and item["status"] == "review" and item["revision"] == 5
        data = json.loads(item["data"])
        old_data = json.loads(before[0][5])
        assert {k: v for k, v in data.items() if k != "retirement_review"} == old_data
        assert data["retirement_review"] == {"reason": "legacy_executor_retired", "original_status": "running"}
        assert [tuple(x) for x in db.execute("SELECT * FROM role_commands")] == commands
        assert {x[1] for x in db.execute("PRAGMA table_info(role_state)")} == {
            "role",
            "revision",
            "context",
            "updated_at",
        }
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='role_dispatches'").fetchone() is None
        receipt = json.loads(db.execute("SELECT data FROM role_receipts WHERE id='dispatch-old'").fetchone()[0])
        assert receipt["status"] == "completed" and receipt["turn_id"] == "old-turn"
        assert receipt["thread_id"] == "old-chat" and receipt["before_turn_id"] == "prior-turn"
        assert "goal_owned" not in receipt
        assert "thread_id" not in store.role_state("M2")
        assert db.execute("SELECT version FROM role_registry_schema").fetchone()[0] == 2
    assert store.role_state("M2")["revision"] == 17


@pytest.mark.parametrize("status", ["starting", "running", "unknown"])
def test_pending_legacy_dispatch_blocks_atomic_migration(store, status):
    legacy(store, status)
    with pytest.raises(RoleError, match="role_retirement_requires_review"):
        store.initialize()
    with sqlite3.connect(store.path) as db:
        assert "thread_id" in {x[1] for x in db.execute("PRAGMA table_info(role_state)")}
        assert db.execute("SELECT status FROM role_dispatches").fetchone()[0] == status
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='role_registry_schema'").fetchone() is None


def test_scheduler_templates_cannot_recreate_duties():
    assert set(AUTOMATION_TEMPLATES) == {"crm_digest_v1"}


def test_removed_owner_submit_transport_is_rejected(tmp_path):
    service = AutomationControlService(store=AutomationStore(tmp_path / "registry.sqlite3"))
    with pytest.raises(Exception, match="automation_operation_invalid"):
        service.handle(
            {
                "protocol": AUTOMATION_CONTROL_PROTOCOL,
                "request_id": str(uuid4()),
                "operation": "role_owner_submit",
                "actor": {"kind": "system", "id": "test", "is_admin": True},
                "payload": {},
            }
        )


def test_manual_claims_allow_independent_resources(store):
    one = task(store, "repo:one")
    two = task(store, "repo:two")
    for item in (one, two):
        result = action(store, "task_claim", payload={"id": item["id"]}, revision=item["revision"])["item"]
        assert result["status"] == "running"
    assert len([x for x in store.read("tasks", "M2")["items"] if x["status"] == "running"]) == 2


def test_old_running_claim_becomes_review_without_blocking_new_manual_work(store):
    legacy(store)
    store.initialize()
    old = store.read("tasks", "M2", reference_id="task-old")["items"][0]
    assert old["status"] == "review" and "result" not in old["data"]
    item = task(store, "repo:new")
    assert (
        action(store, "task_claim", payload={"id": item["id"]}, revision=item["revision"])["item"]["status"]
        == "running"
    )


def test_retirement_archives_duty_jobs_and_preserves_terminal_run_receipts(store):
    auto = AutomationStore(store.path)
    auto.initialize()
    with auto.connect() as db:
        original = auto.create_from_template(
            db, template_id="crm_digest_v1", payload={"template_id": "crm_digest_v1"}, actor_hash="a" * 64
        )["job"]
        ident = original["job_id"]
        db.execute(
            "UPDATE manager_automations SET template_id='m2_duty_v1',desired_state='on' WHERE job_id=?", (ident,)
        )
        db.execute(
            "INSERT INTO manager_automation_runs(run_id,job_id,trigger_kind,status,scheduled_for,requested_at,result_code) VALUES ('old-run',?,'schedule','succeeded','before','before','ok')",
            (ident,),
        )
        # Recreate the old schema marker to exercise a deployed pre-migration DB.
        db.execute("DELETE FROM role_registry_schema")
    store.initialize()
    with auto.connect() as db:
        job = db.execute("SELECT * FROM manager_automations WHERE job_id=?", (ident,)).fetchone()
        assert job["archived_at"] and job["desired_state"] == "off"
        assert db.execute("SELECT status,result_code FROM manager_automation_runs WHERE run_id='old-run'").fetchone()[
            :
        ] == ("succeeded", "ok")
    assert auto.status()["jobs"] == []
