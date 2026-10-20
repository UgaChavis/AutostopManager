"""Private byte/ledger tests; privileged source/unit/UID attestors are explicit fakes."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


def bootstrap():
    path = Path(__file__).parents[1] / "scripts/component-role-bootstrap.py"
    spec = importlib.util.spec_from_file_location("component_bootstrap_target", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComponentBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.module = bootstrap()
        self.tmp = tempfile.TemporaryDirectory(prefix="component-bootstrap-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def private_writer(self):
        stack = ExitStack()
        # Caller-owned temporary files exercise O_EXCL/fsync. This deliberately
        # does not pretend a UID65534 directory is protected production custody.
        stack.enter_context(
            patch.object(
                self.module, "parent_fd", side_effect=lambda path: os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            )
        )
        original_chown = os.fchown
        stack.enter_context(
            patch.object(self.module.os, "fchown", side_effect=lambda fd, uid, gid: original_chown(fd, -1, -1))
        )
        return stack

    def test_actual_private_o_excl_claim_and_interrupted_claim_never_overwrite_or_retry(self):
        target = self.root / "claim.json"
        with self.private_writer():
            self.module.write_once(target, b"first-exact-claim\n")
            with self.assertRaises(FileExistsError):
                self.module.write_once(target, b"second-claim\n")
        self.assertEqual(target.read_bytes(), b"first-exact-claim\n")
        self.assertEqual(target.stat().st_nlink, 1)
        interrupted = self.root / "interrupted.json"
        with (
            self.private_writer(),
            patch.object(self.module.os, "fsync", side_effect=OSError("artificial fsync failure")),
        ):
            with self.assertRaises(OSError):
                self.module.write_once(interrupted, b"unknown-durability\n")
        self.assertEqual(interrupted.read_bytes(), b"unknown-durability\n")
        with self.private_writer(), self.assertRaises(FileExistsError):
            self.module.write_once(interrupted, b"blind-repeat\n")

    def source_fixture(self):
        source = self.root / "source"
        source.mkdir()
        rows = {
            "REVISION": ("a" * 40 + "\n").encode(),
            "scripts/component-role-bootstrap.py": b"# bootstrap\n",
            "scripts/cold-role-start.py": b"# consumer bootstrap\n",
            "autostop_manager/cold_runtime.py": b"# guard\n",
            "README.md": b"# fixture source\n",
        }
        for name, raw in rows.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        manifest = "".join(
            hashlib.sha256(raw).hexdigest() + "  " + name + "\n" for name, raw in sorted(rows.items())
        ).encode()
        (source / "MANIFEST.sha256").write_bytes(manifest)
        return source, {"path": str(source / "MANIFEST.sha256"), "sha256": hashlib.sha256(manifest).hexdigest()}

    def source_attestor_fixture(self, *, mutate_after_read=None):
        stack = ExitStack()
        original_glob = Path.rglob
        # Root directory ownership qualification is a substituted native seam;
        # every selected/unselected actual regular file still participates.
        stack.enter_context(
            patch.object(
                Path, "rglob", lambda path, pattern: (p for p in original_glob(path, pattern) if not p.is_dir())
            )
        )

        def reader(path, limit=2 * 1024 * 1024, *, with_version=False):
            raw, observed = path.read_bytes(), self.module.version(path.stat())
            self.assertLessEqual(len(raw), limit)
            if mutate_after_read is not None and path.name == "README.md":
                path.write_bytes(b"changed-between-byte-verification-and-metadata-capture\n")
            return (raw, observed) if with_version else raw

        stack.enter_context(patch.object(self.module, "protected_bytes", side_effect=reader))
        return stack

    def test_full_source_bytes_extra_member_and_post_hash_mutation_deny(self):
        source, reference = self.source_fixture()
        with self.source_attestor_fixture():
            snapshot = self.module.qualified_source(source, reference, "a" * 40)
            self.module.recheck_source(source, snapshot)
            (source / "README.md").write_bytes(b"mutated after claim\n")
            with self.assertRaisesRegex(RuntimeError, "source_changed_after_claim"):
                self.module.recheck_source(source, snapshot)
            with self.assertRaisesRegex(RuntimeError, "source_hash"):
                self.module.qualified_source(source, reference, "a" * 40)
            (source / "unselected.py").write_bytes(b"# must not import\n")
            with self.assertRaisesRegex(RuntimeError, "source_inventory_changed"):
                self.module.qualified_source(source, reference, "a" * 40)

    def test_metadata_is_bound_to_the_same_hash_read_not_a_later_stat(self):
        source, reference = self.source_fixture()
        with self.source_attestor_fixture(mutate_after_read=True):
            snapshot = self.module.qualified_source(source, reference, "a" * 40)
            with self.assertRaisesRegex(RuntimeError, "source_changed_after_claim"):
                self.module.recheck_source(source, snapshot)

    def test_invalid_manifest_and_launch_never_import_application_or_accept_arbitrary_command(self):
        with patch.object(self.module.importlib, "import_module", side_effect=AssertionError("application imported")):
            with self.assertRaises(RuntimeError):
                self.module.main(["--role", "wake", "--launch", "/tmp/untrusted.json", "--sha256", "a" * 64])
            with self.assertRaises(SystemExit):
                self.module.main(["--role", "crm-embedded", "--launch", "/tmp/x", "--sha256", "a" * 64])
        source, reference = self.source_fixture()
        original = (source / "MANIFEST.sha256").read_bytes()
        (source / "MANIFEST.sha256").write_bytes(original + b"a" * 64 + b"  ../outside.py\n")
        reference["sha256"] = hashlib.sha256((source / "MANIFEST.sha256").read_bytes()).hexdigest()
        with self.source_attestor_fixture(), self.assertRaisesRegex(RuntimeError, "manifest_path"):
            self.module.qualified_source(source, reference, "a" * 40)

    def test_component_role_entry_loads_protected_environment_before_any_consumer_import(self):
        spec = importlib.util.spec_from_file_location(
            "component_role_entry", Path(__file__).parents[1] / "scripts/cold-role-start.py"
        )
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        source = Path("/opt/autostop-manager-cold-releases/" + "a" * 40 + "/source")
        for fail_env in (False, True):
            events = []
            current = SimpleNamespace(
                document={
                    "schema": "autostop.manager.component-runtime-binding.v1",
                    "paths": {"source_root": str(source)},
                }
            )

            def load_env(events=events, fail_env=fail_env):
                events.append("protected-env")
                if fail_env:
                    raise RuntimeError("artificial invalid role environment")

            def imported(name, events=events, current=current, load_env=load_env):
                if name == "autostop_manager.cold_runtime":
                    return SimpleNamespace(
                        claim_runtime_start=lambda: events.append("claim") or current,
                        require_role=lambda role: events.append("role:" + role),
                    )
                if name == "autostop_manager.config":
                    self.assertEqual(events, ["claim", "role:work-telegram"])
                    return SimpleNamespace(load_runtime_env=load_env)
                self.assertEqual(name, "autostop_manager.telegram_bridge")
                self.assertEqual(events, ["claim", "role:work-telegram", "protected-env"])
                return SimpleNamespace(main=lambda: events.append("consumer"))

            with (
                patch.object(entry, "_source_root", return_value=source),
                patch.object(entry.importlib, "import_module", side_effect=imported),
                patch.object(entry, "sys", SimpleNamespace(modules={}, path=[], argv=[])),
            ):
                if fail_env:
                    with self.assertRaisesRegex(RuntimeError, "invalid role environment"):
                        entry.main(["--role", "work-telegram"])
                    self.assertNotIn("consumer", events)
                else:
                    self.assertEqual(entry.main(["--role", "work-telegram"]), 0)
                    self.assertEqual(events[-1], "consumer")

    def test_revocation_is_permanent_and_unknown_file_denies_without_repair(self):
        self.module.not_revoked(self.root)
        path = self.root / "revoked.json"
        path.write_bytes(b"partial root revocation intent")
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            self.module.not_revoked(self.root)
        self.assertEqual(path.read_bytes(), b"partial root revocation intent")

    def claim_fixture(self):
        from copy import deepcopy
        from test_component_runtime import ATTEMPT, CONTROL, DISPATCH, SOURCE, ComponentRuntimeTests
        from autostop_manager import cold_runtime as cold, component_runtime as component

        fixture = ComponentRuntimeTests(
            "test_six_roles_require_actual_same_process_claim_and_keep_retained_crm_private"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.put(
            DISPATCH + "/state.json",
            {**fixture.state, "state": "closed", "commit_ref": None, "admission_ref": None, "store_effects_ref": None},
        )
        template = deepcopy(fixture.documents["manager-mcp"])
        template.pop("claim_ref")
        launch = {
            name: template[name]
            for name in (
                "mode",
                "source_revision",
                "attempt_id",
                "boot_id",
                "generation",
                "epoch_id",
                "role",
                "instance_id",
                "runtime",
                "not_before_ns",
                "expires_at_ns",
            )
        }
        launch.update(binding_template=template)
        launch_ref = {"path": CONTROL + "/launch/manager-mcp.json", "sha256": "a" * 64}
        self.assertEqual(launch["attempt_id"], ATTEMPT)
        return fixture, launch, launch_ref, Path(SOURCE), cold, component

    def test_same_actual_process_claim_holds_ex_and_is_published_once(self):
        fixture, launch, launch_ref, source, cold, component = self.claim_fixture()
        original_write = self.module.write_once
        artifacts = {}

        def writer(path, raw, **kwargs):
            import fcntl

            other = os.open(fixture.lock, os.O_RDONLY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(other)
            actual = self.root / "virtual" / str(path).lstrip("/")
            actual.parent.mkdir(parents=True, exist_ok=True)
            original_write(actual, raw, **kwargs)
            artifacts[str(path)] = actual
            fixture.files[str(path)] = raw

        with (
            self.private_writer(),
            patch.object(self.module, "context"),
            patch.object(self.module, "not_revoked"),
            patch.object(self.module, "write_once", side_effect=writer),
        ):
            current, reference = self.module.claim(launch, launch_ref, source, component, cold)
            claim = cold._document(fixture.files[current.document["claim_ref"]["path"]])
            self.assertEqual({name: claim[name] for name in component.process_identity()}, component.process_identity())
            self.assertEqual(reference["sha256"], hashlib.sha256(fixture.files[reference["path"]]).hexdigest())
            with self.assertRaises(FileExistsError):
                self.module.claim(launch, launch_ref, source, component, cold)
        ledger = next(path for logical, path in artifacts.items() if "/claims/" in logical)
        self.assertEqual(cold._document(ledger.read_bytes())["binding_ref"], reference)
        self.assertEqual(len(artifacts), 3)

    def test_crash_after_durable_claim_before_publication_cannot_reissue(self):
        fixture, launch, launch_ref, source, cold, component = self.claim_fixture()
        original_write = self.module.write_once
        ledger = self.root / "one-shot.json"

        def writer(path, raw, **kwargs):
            if "/claims/" not in str(path):
                raise OSError("artificial crash before claim/binding publication")
            original_write(ledger, raw, **kwargs)

        with (
            self.private_writer(),
            patch.object(self.module, "context"),
            patch.object(self.module, "not_revoked"),
            patch.object(self.module, "write_once", side_effect=writer),
        ):
            with self.assertRaisesRegex(OSError, "artificial crash"):
                self.module.claim(launch, launch_ref, source, component, cold)
            retained = ledger.read_bytes()
            with self.assertRaises(FileExistsError):
                self.module.claim(launch, launch_ref, source, component, cold)
        self.assertEqual(ledger.read_bytes(), retained)
        self.assertEqual(cold._document(retained)["pid"], os.getpid())
        fence = cold._document(fixture.files[launch["binding_template"]["ingress_fence_ref"]["path"]])
        state = cold._document(fixture.files[fence["state_path"]])
        self.assertEqual(state["state"], "closed")
        self.assertIsNone(state["admission_ref"])

    def test_context_preserves_role_identity_and_rejects_wrong_unit_and_loader_env(self):
        revision, epoch = "a" * 40, "11111111-1111-4111-8111-111111111111"
        source = Path(f"/opt/autostop-manager-cold-releases/{revision}/source")
        runtime = source.parent / "venv/bin/python3.12"
        raw = b"artificial physical interpreter"
        launch = {
            "role": "work-telegram",
            "boot_id": epoch,
            "source_revision": revision,
            "generation": epoch,
            "not_before_ns": 10,
            "expires_at_ns": 30,
            "runtime": {"path": str(runtime), "sha256": hashlib.sha256(raw).hexdigest(), "version": [1] * 9},
            "identity": {"uid": 987, "gid": 982, "supplementary_groups": [982, 10001]},
        }
        component = SimpleNamespace(runtime_identity=lambda path: (path, path, [1] * 9))
        cold = SimpleNamespace(_boot_id=lambda: epoch)
        unit = "0::/system.slice/autostop-manager-component-" + epoch + "-work-telegram.service\n"
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            stack.enter_context(patch.object(self.module.os, "geteuid", return_value=0))
            stack.enter_context(patch.object(self.module.os, "getegid", return_value=0))
            stack.enter_context(
                patch.object(
                    self.module,
                    "sys",
                    SimpleNamespace(flags=SimpleNamespace(isolated=1, no_site=1), dont_write_bytecode=True),
                )
            )
            stack.enter_context(patch.object(self.module.time, "monotonic_ns", return_value=20))
            stack.enter_context(patch.object(self.module, "protected_bytes", return_value=raw))
            stack.enter_context(patch.object(Path, "read_text", return_value=unit))
            self.module.context(launch, source, component, cold)
            launch["identity"]["uid"] = 0
            with self.assertRaisesRegex(RuntimeError, "role_identity"):
                self.module.context(launch, source, component, cold)
            launch["identity"]["uid"] = 987
            with patch.object(Path, "read_text", return_value="0::/system.slice/old.service\n"):
                with self.assertRaisesRegex(RuntimeError, "unit_context"):
                    self.module.context(launch, source, component, cold)
            os.environ["LD_PRELOAD"] = "/synthetic/untrusted.so"
            with self.assertRaisesRegex(RuntimeError, "loader_environment"):
                self.module.context(launch, source, component, cold)


if __name__ == "__main__":
    unittest.main()
