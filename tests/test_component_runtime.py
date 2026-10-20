"""Component wire and effect boundary; artificial authority, actual own flock.

These tests deliberately substitute the privileged filesystem/runtime attestor.
They are not production source, container, UID or startup qualification.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID, uuid5

from autostop_manager import cold_runtime as cold
from autostop_manager import component_runtime as component


EPOCH = "11111111-1111-4111-8111-111111111111"
ATTEMPT = "22222222-2222-4222-8222-222222222222"
BOOT = "33333333-3333-4333-8333-333333333333"
PREVIOUS = "44444444-4444-4444-8444-444444444444"
REVISION = "a" * 40
COMMON = {"attempt_id": ATTEMPT, "boot_id": BOOT, "generation": EPOCH, "epoch_id": EPOCH}
SOURCE = f"/opt/autostop-manager-cold-releases/{REVISION}/source"
CONTROL = f"/var/lib/autostop-component-release/{ATTEMPT}"
DISPATCH = f"/var/lib/autostop-manager-cold/{EPOCH}/dispatch"
PROOFS = DISPATCH + "/proofs"
ETC = f"/etc/autostop-manager-cold/{EPOCH}"


@unittest.skipUnless(os.name == "posix" and Path("/proc/self/stat").exists(), "Linux component process/lock contract")
class ComponentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="manager-component-")
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "dispatch.lock"
        self.lock.write_bytes(b"")
        self.files = {}
        self.now = 200
        self.mode = component.EXTERNAL
        self.documents, self.refs, self.claims = {}, {}, {}
        self._build()
        self._patch(cold, "SOURCE_ROOT", Path(SOURCE))
        self._patch(cold, "_boot_id", lambda: BOOT)
        self._patch(cold, "_read_protected", lambda path: self.files[str(path)])
        self._patch(cold, "_open_protected", lambda path: os.open(self.lock, os.O_RDONLY))
        self._patch(cold, "_lock_link", lambda path: self.lock.stat())
        self._patch(component.time, "monotonic_ns", lambda: self.now)
        self._patch(component, "runtime_identity", lambda path: (path, path, self.runtime["version"]))
        self._patch(component, "ensure_not_revoked", lambda current: None)
        self._patch(component, "validate_role_identity", lambda current: None)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.select("manager-mcp")

    def _patch(self, target, name, value):
        selected = patch.object(target, name, value)
        selected.start()
        self.addCleanup(selected.stop)

    def put(self, path, document):
        raw = cold.canonical(document)
        self.files[path] = raw
        return {"path": path, "sha256": hashlib.sha256(raw).hexdigest()}

    def header(self, kind, **fields):
        return {"schema": "autostop.manager.component-" + kind + ".v1", "mode": self.mode, **COMMON, **fields}

    def _build(self):
        self.runtime = {
            "path": SOURCE.removesuffix("source") + "venv/bin/python3.12",
            "sha256": "b" * 64,
            "version": [1, 2, 31, 4, 5, stat.S_IFREG | 0o755, 0, 0, 1],
        }
        info = self.lock.stat()
        fence = {
            "schema": "autostop.j1.cold-dispatch-fence.v1",
            **COMMON,
            "state_path": DISPATCH + "/state.json",
            "dispatch_lock_path": DISPATCH + "/dispatch.lock",
            "dispatch_lock_identity": {"device": info.st_dev, "inode": info.st_ino, "ctime_ns": info.st_ctime_ns},
        }
        fence_ref = self.put(ETC + "/dispatch/fence.json", fence)
        tombstone_ref = self.put(
            ETC + "/dispatch/tombstones.json",
            {
                "schema": "autostop.j1.cold-dispatch-tombstones.v2",
                **COMMON,
                "origin_attempt_id": PREVIOUS,
                "rows": [
                    {
                        "op_id": "old-unknown",
                        "fingerprint": {"kind": "observed_record_sha256", "sha256": "c" * 64},
                        "outcome": "unknown",
                        "retry": "forbidden",
                    }
                ],
            },
        )
        retained = self.header(
            "retained-crm",
            container_id="d" * 64,
            image_id="sha256:" + "e" * 64,
            app_revision="f" * 40,
            embedded_revision=component.EMBEDDED_REVISION,
            manager_source_root="/opt/old/manager",
            manager_source_sha256="c" * 64,
            environment_sha256="d" * 64,
            manager_mount={
                "source": "/opt/old/manager",
                "destination": "/opt/AutostopManager",
                "read_only": True,
                "data_source": "/opt/AutostopManager/data",
                "data_destination": "/opt/AutostopManager/data",
            },
            owned_paths=["/opt/AutostopManager/data", "/opt/autostopcrm/data"],
        )
        retained_ref = self.put(CONTROL + "/retained-crm.json", retained)
        self.put(PROOFS + "/retained-crm.json", retained)
        ownership = self.header(
            "ownership",
            source_revision=REVISION,
            roles=sorted(cold.NATIVE_ROLES),
            original_units_denied=sorted(component.ORIGINAL_UNITS),
            hold_ref={"path": CONTROL + "/hold.json", "sha256": "a" * 64},
            retirement_ref={"path": CONTROL + "/retirement.json", "sha256": "b" * 64},
            retained_operator_ref={"path": CONTROL + "/operator.json", "sha256": "c" * 64},
            old_writer_allowed=False,
            candidate_may_have_written=True,
        )
        ownership_ref = self.put(PROOFS + "/ownership.json", ownership)
        identity = component.process_identity()
        instances = {}
        for index, role in enumerate(sorted(cold.NATIVE_ROLES)):
            actor = {**identity, "pid": identity["pid"] + (0 if role == "manager-mcp" else index + 1)}
            instance = str(uuid5(UUID(ATTEMPT), role))
            claim = self.header(
                "claim",
                source_revision=REVISION,
                role=role,
                instance_id=instance,
                runtime=self.runtime,
                **actor,
                claimed_ns=150,
            )
            claim_ref = self.put(ETC + f"/{role}/claim.json", claim)
            self.claims[role] = claim_ref
            document = {
                "schema": component.SCHEMA,
                "mode": self.mode,
                **COMMON,
                "source_revision": REVISION,
                "role": role,
                "paths": cold.expected_paths(EPOCH, REVISION, role),
                "tombstones_ref": tombstone_ref,
                "ingress_fence_ref": fence_ref,
                "instance_id": instance,
                "runtime": self.runtime,
                "claim_ref": claim_ref,
                "not_before_ns": 100,
                "expires_at_ns": 300,
                "retained_crm_ref": retained_ref,
            }
            self.documents[role] = document
            self.refs[role] = self.put(ETC + f"/{role}/binding.json", document)
            instances[role] = {"instance_id": instance, **actor, "runtime_sha256": self.runtime["sha256"]}
        self.admission = self.header(
            "admission",
            source_revision=REVISION,
            not_before_ns=100,
            expires_at_ns=300,
            runtime_bindings=self.refs,
            crm_urls={"board": "http://127.0.0.1:8000", "mcp": "http://127.0.0.1:8001/mcp"},
            ownership_ref=ownership_ref,
            retained_crm_ref=retained_ref,
        )
        self.admission_ref = self.put(PROOFS + "/admission.json", self.admission)
        self.commit = self.header(
            "commit",
            source_revision=REVISION,
            committed_ns=180,
            admission_ref={"path": CONTROL + "/admission.json", "sha256": self.admission_ref["sha256"]},
            claim_refs=self.claims,
            instances=instances,
        )
        commit_ref = self.put(PROOFS + "/commit.json", self.commit)
        self.state = self.header(
            "dispatch-state",
            state="open",
            admission_ref=self.admission_ref,
            commit_ref=commit_ref,
            store_effects_ref=None,
        )
        self.put(DISPATCH + "/state.json", self.state)
        self.files[SOURCE + "/REVISION"] = (REVISION + "\n").encode()

    def select(self, role):
        os.environ[cold.BINDING_ENV] = self.refs[role]["path"]
        os.environ[cold.SHA_ENV] = self.refs[role]["sha256"]

    def crm_mode(self, selected="crm-embedded"):
        old_epoch = "55555555-5555-4555-8555-555555555555"
        external = {
            **deepcopy(self.commit),
            "attempt_id": PREVIOUS,
            "generation": old_epoch,
            "epoch_id": old_epoch,
            "admission_ref": {
                "path": f"/var/lib/autostop-component-release/{PREVIOUS}/admission.json",
                "sha256": "c" * 64,
            },
            "claim_refs": {
                role: {"path": f"/etc/autostop-manager-cold/{old_epoch}/{role}/claim.json", "sha256": "d" * 64}
                for role in cold.NATIVE_ROLES
            },
        }
        projection = self.put(PROOFS + "/external-commit.json", external)
        external_ref = {**projection, "path": f"/var/lib/autostop-component-release/{PREVIOUS}/commit.json"}
        old = deepcopy(self.documents["manager-mcp"])
        self.mode = component.CRM
        self.documents, self.refs, self.claims = {}, {}, {}
        identity, instances = component.process_identity(), {}
        for index, role in enumerate(sorted(cold.ROLES)):
            actor = {**identity, "pid": identity["pid"] + (0 if role == selected else index + 1)}
            runtime = {**self.runtime, "path": "/usr/local/bin/python3.12"} if role == "crm-embedded" else self.runtime
            instance = str(uuid5(UUID(ATTEMPT), "crm:" + role))
            claim = self.header(
                "claim",
                source_revision=REVISION,
                role=role,
                instance_id=instance,
                runtime=runtime,
                **actor,
                claimed_ns=150,
            )
            claim_ref = self.put(ETC + f"/{role}/claim.json", claim)
            self.claims[role] = claim_ref
            doc = {key: value for key, value in old.items() if key != "retained_crm_ref"}
            doc.update(
                mode=self.mode,
                role=role,
                instance_id=instance,
                runtime=runtime,
                claim_ref=claim_ref,
                paths=cold.expected_paths(EPOCH, REVISION, role),
                external_commit_ref=external_ref,
                candidate_crm_sha="f" * 40,
            )
            self.documents[role] = doc
            self.refs[role] = self.put(ETC + f"/{role}/binding.json", doc)
            instances[role] = {"instance_id": instance, **actor, "runtime_sha256": runtime["sha256"]}
        owner = cold._document(self.files[PROOFS + "/ownership.json"])
        owner.update(mode=self.mode, roles=sorted(cold.ROLES))
        self.admission = self.header(
            "admission",
            source_revision=REVISION,
            not_before_ns=100,
            expires_at_ns=300,
            runtime_bindings=self.refs,
            crm_urls={
                "board": "http://127.0.0.1:8000/__cold/" + "a" * 64,
                "mcp": "http://127.0.0.1:8001/mcp/__cold/" + "b" * 64 + "/mcp",
            },
            ownership_ref=self.put(PROOFS + "/ownership.json", owner),
            external_commit_ref=external_ref,
            candidate_crm_sha="f" * 40,
        )
        self.admission_ref = self.put(PROOFS + "/admission.json", self.admission)
        self.commit = self.header(
            "commit",
            source_revision=REVISION,
            committed_ns=180,
            claim_refs=self.claims,
            instances=instances,
            admission_ref={"path": CONTROL + "/admission.json", "sha256": self.admission_ref["sha256"]},
        )
        self.state = self.header(
            "dispatch-state",
            state="open",
            admission_ref=self.admission_ref,
            commit_ref=self.put(PROOFS + "/commit.json", self.commit),
            store_effects_ref=None,
        )
        self.put(DISPATCH + "/state.json", self.state)
        self.select(selected)

    def test_six_roles_require_actual_same_process_claim_and_keep_retained_crm_private(self):
        for role in cold.NATIVE_ROLES:
            self.assertEqual(cold.validate_binding_document(self.documents[role]).role, role)
        self.select("manager-mcp")
        del self.files[CONTROL + "/retained-crm.json"]
        with cold.dispatch_guard("new-request", "manager-crm"):
            pass
        self.assertEqual(component.crm_urls(cold.binding())["mcp"], "http://127.0.0.1:8001/mcp")

    def test_expiry_denies_new_start_but_does_not_renew_or_revoke_durable_commit(self):
        self.now = 300
        with cold.dispatch_guard("new-request", "manager-crm"):
            pass
        with self.assertRaisesRegex(cold.ColdRuntimeDenied, "start_expired"):
            cold.claim_runtime_start()
        self.now = 200
        with self.assertRaisesRegex(cold.ColdRuntimeDenied, "start_after_commit"):
            cold.claim_runtime_start()
        self.put(DISPATCH + "/state.json", {**self.state, "state": "closed", "commit_ref": None})
        self.assertEqual(cold.claim_runtime_start().role, "manager-mcp")

    def test_old_unknown_replayed_with_changed_payload_and_missing_identity_denied(self):
        for operation_id in ("old-unknown", None, ""):
            with self.assertRaises(cold.ColdRuntimeDenied):
                with cold.dispatch_guard(operation_id, "manager-crm", payload_sha256="f" * 64):
                    self.fail("unknown/reused operation entered")

    def test_exclusive_revoke_excludes_actual_effect_and_unknown_is_not_retried(self):
        import fcntl

        calls = []

        @cold.guarded_effect("manager-crm", lambda args: args["operation_id"])
        def effect(operation_id):
            calls.append(operation_id)
            other = os.open(self.lock, os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(other)
            raise ConnectionError("artificial unknown")

        with self.assertRaises(ConnectionError):
            effect("new-request")
        self.assertEqual(calls, ["new-request"])
        self.put(DISPATCH + "/state.json", {**self.state, "state": "closed"})
        with self.assertRaisesRegex(cold.ColdRuntimeDenied, "dispatch_closed"):
            effect("second-request")
        self.assertEqual(calls, ["new-request"])

    def test_changed_process_namespace_runtime_or_claim_time_denies(self):
        original = cold._document(self.files[self.claims["manager-mcp"]["path"]])
        for field, value in (
            ("pid", 1),
            ("start_ticks", 1),
            ("pid_namespace", 1),
            ("claimed_ns", True),
            ("claimed_ns", 300),
        ):
            with self.subTest(field=field, value=value):
                changed = self.put(self.claims["manager-mcp"]["path"], {**original, field: value})
                # Rehashing only the claim cannot update the immutable binding.
                self.assertNotEqual(changed["sha256"], self.claims["manager-mcp"]["sha256"])
                with self.assertRaises(cold.ColdRuntimeDenied):
                    cold.binding()
        self.put(self.claims["manager-mcp"]["path"], original)
        for field in ("pid", "start_ticks", "pid_namespace"):
            identity = component.process_identity()
            identity[field] += 1
            with patch.object(component, "process_identity", return_value=identity):
                with self.assertRaisesRegex(cold.ColdRuntimeDenied, "process_changed"):
                    cold.binding()
        changed = list(self.runtime["version"])
        changed[4] += 1
        with patch.object(
            component,
            "runtime_identity",
            return_value=(Path(self.runtime["path"]), Path(self.runtime["path"]), changed),
        ):
            with self.assertRaisesRegex(cold.ColdRuntimeDenied, "runtime_changed"):
                cold.binding()

    def test_binding_schema_role_runtime_window_and_extra_fields_strict(self):
        base = self.documents["manager-mcp"]
        for field, value in (
            ("mode", "other"),
            ("mode", []),
            ("role", "crm-embedded"),
            ("expires_at_ns", True),
            ("expires_at_ns", 100),
            ("expires_at_ns", component.MAX_WINDOW_NS + 101),
            ("instance_id", "missing"),
            ("permission", True),
        ):
            with self.subTest(field=field):
                with self.assertRaises(cold.ColdRuntimeDenied):
                    cold.validate_binding_document({**base, field: value})
        with self.assertRaises(cold.ColdRuntimeDenied):
            cold.validate_binding_document({k: v for k, v in base.items() if k != "claim_ref"})
        with self.assertRaises(cold.ColdRuntimeDenied):
            cold.validate_binding_document({**base, "schema": cold.SCHEMA})

    def test_wrong_old_crm_mount_or_incomplete_roles_and_joint_state_never_admit(self):
        reference = PROOFS + "/retained-crm.json"
        self.files[reference] = cold.canonical({"schema": "wrong"})
        with self.assertRaises(cold.ColdRuntimeDenied):
            with cold.dispatch_guard("new-request", "manager-crm"):
                self.fail("changed CRM admitted")
        self._build()
        changed = deepcopy(self.admission)
        changed["runtime_bindings"].pop("wake")
        reference = self.put(PROOFS + "/admission.json", changed)
        self.put(DISPATCH + "/state.json", {**self.state, "admission_ref": reference})
        with self.assertRaisesRegex(cold.ColdRuntimeDenied, "admission_roles"):
            component.crm_urls(cold.binding())
        self.put(DISPATCH + "/state.json", {**self.state, "schema": "autostop.j1.cold-dispatch-state.v1"})
        with self.assertRaisesRegex(cold.ColdRuntimeDenied, "dispatch-state_schema"):
            with cold.dispatch_guard("new-request", "manager-crm"):
                self.fail("joint state reused")

    def test_exact_legacy_urls_only_and_explicit_independent_board_required(self):
        from autostop_manager import config

        os.environ[config.CRM_MCP_BEARER_TOKEN_ENV] = "synthetic-test-token"
        os.environ[config.CRM_MCP_URL_ENV] = "http://127.0.0.1:8001/mcp"
        os.environ["AUTOSTOP_AUTOMATION_CRM_API_URL"] = "http://127.0.0.1:8000"
        self.assertIsNone(config.get_crm_mcp_connection_config().error_code)
        self.assertIsNone(config.get_automation_crm_connection_config().error_code)
        os.environ[config.CRM_MCP_URL_ENV] = "http://127.0.0.2:8001/mcp"
        self.assertIsNotNone(config.get_crm_mcp_connection_config().error_code)
        os.environ[config.CRM_MCP_URL_ENV] = "http://127.0.0.1:8001/mcp/__cold/" + "a" * 64 + "/mcp"
        self.assertIsNotNone(config.get_crm_mcp_connection_config().error_code)
        del os.environ["AUTOSTOP_AUTOMATION_CRM_API_URL"]
        self.assertIsNotNone(config.get_automation_crm_connection_config().error_code)

    def test_component_mcp_ingress_requires_fresh_exact_epoch_path(self):
        from autostop_manager import config

        for path in (None, "/mcp", "/mcp/__component/" + PREVIOUS, "mcp/__component/" + EPOCH):
            if path is None:
                os.environ.pop("AUTOSTOP_MANAGER_MCP_PATH", None)
            else:
                os.environ["AUTOSTOP_MANAGER_MCP_PATH"] = path
            with self.assertRaisesRegex(cold.ColdRuntimeDenied, "component_mcp_ingress_path"):
                config.get_mcp_path()
        os.environ["AUTOSTOP_MANAGER_MCP_PATH"] = "/mcp/__component/" + EPOCH
        self.assertEqual(config.get_mcp_path(), "/mcp/__component/" + EPOCH)
        self.crm_mode()
        self.assertEqual(config.get_mcp_path(), "/mcp/__component/" + EPOCH)

    def test_retained_crm_owned_paths_cannot_cover_new_state_through_an_ancestor(self):
        original = cold._document(self.files[PROOFS + "/retained-crm.json"])
        for overlapping in ("/var/lib", "/var/lib/autostop-manager-cold", DISPATCH, "/run"):
            document = {**original, "owned_paths": sorted({original["manager_mount"]["data_source"], overlapping})}
            projected = self.put(PROOFS + "/retained-crm.json", document)
            reference = {**projected, "path": CONTROL + "/retained-crm.json"}
            selected = self.documents["manager-mcp"]
            selected["retained_crm_ref"] = reference
            self.refs["manager-mcp"] = self.put(ETC + "/manager-mcp/binding.json", selected)
            self.select("manager-mcp")
            self.admission["retained_crm_ref"] = reference
            admission_ref = self.put(PROOFS + "/admission.json", self.admission)
            self.put(DISPATCH + "/state.json", {**self.state, "admission_ref": admission_ref})
            with self.assertRaisesRegex(cold.ColdRuntimeDenied, "crm_state_overlap"):
                component.crm_urls(cold.binding())

    def test_store_mutations_require_separate_reviewed_record_and_payment_scope(self):
        with self.assertRaises(cold.ColdRuntimeDenied):
            with cold.dispatch_guard("new-request", "manager-store", operation="mark_order_ready"):
                self.fail("Store mutation escaped without admission")
        record = self.header(
            "store-effects",
            source_revision=REVISION,
            admission_ref=self.commit["admission_ref"],
            allowed_operations={
                "manager-store": ["mark_order_ready", "set_order_payment_status"],
                "manager-store-owner": [],
            },
            payment_scope_ref=None,
        )
        reference = self.put(PROOFS + "/store-effects.json", record)
        self.put(DISPATCH + "/state.json", {**self.state, "store_effects_ref": reference})
        with cold.dispatch_guard("new-request", "manager-store", operation="mark_order_ready"):
            pass
        with self.assertRaises(cold.ColdRuntimeDenied):
            with cold.dispatch_guard("new-request", "manager-store", operation="set_order_payment_status"):
                self.fail("payment escaped")
        scope = self.header(
            "store-scope",
            source_revision=REVISION,
            store_revision=component.STORE_REVISION,
            principal_id="00000000-0000-4000-8000-000000000102",
            scope=component.PAYMENT_SCOPE,
            amendment_receipt_ref={"path": CONTROL + "/store-scope/receipt.json", "sha256": "d" * 64},
        )
        record["payment_scope_ref"] = self.put(PROOFS + "/store-scope.json", scope)
        reference = self.put(PROOFS + "/store-effects.json", record)
        self.put(DISPATCH + "/state.json", {**self.state, "store_effects_ref": reference})
        with cold.dispatch_guard("new-request", "manager-store", operation="set_order_payment_status"):
            pass

    def test_crm_mode_links_six_role_commit_but_requires_seven_new_instances_and_cold_urls(self):
        from autostop_manager import config

        self.crm_mode()
        self.assertEqual(len(self.refs), 7)
        for document in self.documents.values():
            cold.validate_binding_document(document)
        with cold.dispatch_guard("new-request", "manager-crm"):
            pass
        os.environ[config.CRM_MCP_BEARER_TOKEN_ENV] = "synthetic-test-token"
        os.environ[config.CRM_MCP_URL_ENV] = self.admission["crm_urls"]["mcp"]
        os.environ["AUTOSTOP_AUTOMATION_CRM_API_URL"] = self.admission["crm_urls"]["board"]
        self.assertIsNone(config.get_crm_mcp_connection_config().error_code)
        self.assertIsNone(config.get_automation_crm_connection_config().error_code)
        os.environ[config.CRM_MCP_URL_ENV] = "http://127.0.0.1:8001/mcp"
        self.assertIsNotNone(config.get_crm_mcp_connection_config().error_code)
        bad = {**self.admission, "runtime_bindings": {"crm-embedded": self.refs["crm-embedded"]}}
        ref = self.put(PROOFS + "/admission.json", bad)
        self.put(DISPATCH + "/state.json", {**self.state, "admission_ref": ref})
        with self.assertRaisesRegex(cold.ColdRuntimeDenied, "admission_roles"):
            component.crm_urls(cold.binding())

    def test_current_and_linked_commit_cannot_substitute_or_duplicate_process_ownership(self):
        bad = deepcopy(self.commit)
        bad["instances"]["j1"] = deepcopy(bad["instances"]["manager-mcp"])
        self.put(DISPATCH + "/state.json", {**self.state, "commit_ref": self.put(PROOFS + "/commit.json", bad)})
        with self.assertRaisesRegex(cold.ColdRuntimeDenied, "duplicate_instance"):
            with cold.dispatch_guard("new-request", "manager-crm"):
                self.fail("same process adopted twice")
        self.crm_mode()
        external = cold._document(self.files[PROOFS + "/external-commit.json"])
        external["mode"] = component.CRM
        # Keeping the old pinned digest must fail even for otherwise canonical JSON.
        self.put(PROOFS + "/external-commit.json", external)
        with self.assertRaises(cold.ColdRuntimeDenied):
            with cold.dispatch_guard("new-request", "manager-crm"):
                self.fail("substituted linked commit")


if __name__ == "__main__":
    unittest.main()
