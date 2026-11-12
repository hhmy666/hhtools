import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.tests.test_rewrite import synthetic
from scripts.tests.test_storage import HookOps
from scripts.tower_reducer.path_binding import TEMPLATE_SUFFIX, TILE_SUFFIX
from scripts.tower_reducer.rewrite import rewrite_template
from scripts.tower_reducer.settings import LocalSettings, save_settings
from scripts.tower_reducer.storage import FileOps, InputSnapshot, JobRepository
from scripts.tower_reducer import sync_service as sync_module
from scripts.tower_reducer.sync_service import SyncService, commit_sync, create_sync_plan


class SimulatedCrash(BaseException):
    pass


class SyncServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tile_root = self.root / "tile-checkout" / TILE_SUFFIX
        self.template_root = self.root / "template-checkout" / TEMPLATE_SUFFIX
        self.tile_root.mkdir(parents=True)
        self.template_root.mkdir(parents=True)
        self.settings_path = self.root / "runtime" / "settings.json"
        self.save_bindings()
        self.raw, self.rewrite_plan = synthetic()
        self.repo = JobRepository(self.root / "jobs")
        self.job = self.repo.create(
            [InputSnapshot(self.raw, "source_m.bytes", self.root / "inputs")]
        )
        self.key = next(iter(self.job.snapshot()["templates"]))
        self.output = rewrite_template(self.raw, self.rewrite_plan, "new")
        self.artifact = self.job.register_output(
            self.key, self.output, self.rewrite_plan, "new"
        )
        self.service = SyncService(self.repo, self.settings_path)

    def save_bindings(self, tile=True, template=True, tile_root=None, template_root=None):
        save_settings(
            LocalSettings(
                tile_root=str(tile_root or self.tile_root),
                template_root=str(template_root or self.template_root),
                tile_confirmed=tile,
                template_confirmed=template,
            ),
            self.settings_path,
        )

    def add_second_template(self):
        raw2 = self.raw + b"\x78\x01"
        job = self.repo.create(
            [
                InputSnapshot(self.raw, "a_m.bytes", self.root / "inputs"),
                InputSnapshot(raw2, "b_m.bytes", self.root / "inputs"),
            ]
        )
        first, second = list(job.snapshot()["templates"])
        first_artifact = job.register_output(
            first,
            rewrite_template(self.raw, self.rewrite_plan, "first"),
            self.rewrite_plan,
            "first",
        )
        second_artifact = job.register_output(
            second,
            rewrite_template(raw2, self.rewrite_plan, "second"),
            self.rewrite_plan,
            "second",
        )
        return job, first, second, first_artifact, second_artifact

    def confirmations(self, plan):
        return {
            target["conflict_id"]: "backup_and_replace"
            for row in plan.rows
            for target in row["targets"]
            if target["classification"] == "conflict"
        }

    def test_accepts_only_saved_confirmed_logical_bindings(self):
        self.save_bindings(tile=False, template=True)
        service = SyncService(self.repo, self.settings_path)
        for selected in (
            ("tile",),
            (str(self.tile_root),),
            ("template", "template"),
            {"template": str(self.template_root)},
            (),
        ):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                service.create_sync_plan(self.job, selected)

        plan = service.create_sync_plan(self.job, ("template",))
        self.assertEqual(plan.selected_bindings, ("template",))
        self.assertEqual(plan.rows[0]["targets"][0]["binding_key"], "template")

    def test_documented_function_interface_commits_its_persisted_plan(self):
        plan = create_sync_plan(self.job, ("template",), self.settings_path)
        report = commit_sync(plan.plan_id, {})
        self.assertTrue(report.ok)
        self.assertEqual((self.template_root / "new_m.bytes").read_bytes(), self.output)

    def test_single_and_dual_preview_classifies_without_writing(self):
        (self.template_root / "new_m.bytes").write_bytes(self.output)
        (self.tile_root / "new_m.bytes").write_bytes(b"old")

        plan = self.service.create_sync_plan(self.job, ("template", "tile"))
        targets = {item["binding_key"]: item for item in plan.rows[0]["targets"]}
        self.assertEqual(targets["template"]["classification"], "same_sha")
        self.assertEqual(targets["tile"]["classification"], "conflict")
        self.assertTrue(targets["tile"]["conflict_id"].startswith("conflict-"))
        self.assertFalse(list(self.template_root.glob(".tmp-*")))
        self.assertFalse(list(self.tile_root.glob(".tmp-*")))

        (self.tile_root / "new_m.bytes").unlink()
        one = self.service.create_sync_plan(self.job, ("tile",))
        self.assertEqual(one.rows[0]["targets"][0]["classification"], "create")

    def test_same_sha_is_true_noop(self):
        target = self.template_root / "new_m.bytes"
        target.write_bytes(self.output)
        before = target.stat()
        plan = self.service.create_sync_plan(self.job, ("template",))

        report = self.service.commit_sync(plan.plan_id, {})

        after = target.stat()
        self.assertTrue(report.ok)
        self.assertEqual(report.rows[0]["status"], "skipped_same_sha")
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertFalse((self.job.directory / "backups").exists())

    def test_conflict_requires_plan_bound_confirmation_then_backs_up_and_replaces(self):
        target = self.template_root / "new_m.bytes"
        target.write_bytes(b"old")
        plan = self.service.create_sync_plan(self.job, ("template",))
        conflict_id = plan.rows[0]["targets"][0]["conflict_id"]

        blocked = self.service.commit_sync(plan.plan_id, {})
        self.assertEqual(blocked.rows[0]["status"], "blocked")
        self.assertEqual(blocked.rows[0]["error"]["code"], "conflict_confirmation_required")
        self.assertEqual(target.read_bytes(), b"old")
        with self.assertRaises(ValueError):
            self.service.commit_sync(plan.plan_id, {"conflict-" + "0" * 32: "backup_and_replace"})

        report = self.service.commit_sync(plan.plan_id, {conflict_id: "backup_and_replace"})
        self.assertTrue(report.ok)
        self.assertEqual(target.read_bytes(), self.output)
        backup = report.rows[0]["targets"][0]["backup"]
        self.assertEqual((self.job.directory / backup["relative_path"]).read_bytes(), b"old")
        self.assertEqual(backup["sha256"], hashlib.sha256(b"old").hexdigest())

    def test_preview_is_invalidated_by_settings_or_target_change(self):
        plan = self.service.create_sync_plan(self.job, ("template",))
        (self.template_root / "new_m.bytes").write_bytes(b"concurrent")
        report = self.service.commit_sync(plan.plan_id, {})
        self.assertEqual(report.rows[0]["status"], "failed")
        self.assertEqual(report.rows[0]["error"]["code"], "target_changed")
        self.assertEqual((self.template_root / "new_m.bytes").read_bytes(), b"concurrent")

        (self.template_root / "new_m.bytes").unlink()
        plan = self.service.create_sync_plan(self.job, ("template",))
        replacement = self.root / "other" / TEMPLATE_SUFFIX
        replacement.mkdir(parents=True)
        self.save_bindings(template_root=replacement)
        report = self.service.commit_sync(plan.plan_id, {})
        self.assertEqual(report.status, "invalidated")
        self.assertTrue(all(row["error"]["code"] == "settings_changed" for row in report.rows))
        self.assertFalse((self.template_root / "new_m.bytes").exists())
        self.assertFalse((replacement / "new_m.bytes").exists())

    def test_target_changed_to_symlink_after_preview_is_a_target_change(self):
        plan = self.service.create_sync_plan(self.job, ("template",))
        outside = self.root / "outside.bytes"
        outside.write_bytes(b"outside")
        (self.template_root / "new_m.bytes").symlink_to(outside)

        report = self.service.commit_sync(plan.plan_id, {})

        self.assertEqual(report.rows[0]["status"], "failed")
        self.assertEqual(report.rows[0]["error"]["code"], "target_changed")
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_symlink_special_and_unwritable_targets_are_rejected(self):
        outside = self.root / "outside.bytes"
        outside.write_bytes(b"outside")
        target = self.template_root / "new_m.bytes"
        target.symlink_to(outside)
        plan = self.service.create_sync_plan(self.job, ("template",))
        self.assertEqual(plan.rows[0]["targets"][0]["classification"], "blocked")
        target.unlink()

        target.mkdir()
        plan = self.service.create_sync_plan(self.job, ("template",))
        self.assertEqual(plan.rows[0]["targets"][0]["classification"], "blocked")
        target.rmdir()

        self.template_root.chmod(0o500)
        try:
            with self.assertRaises(ValueError):
                self.service.create_sync_plan(self.job, ("template",))
        finally:
            self.template_root.chmod(0o700)

    def test_all_targets_prepare_before_replace_and_second_failure_rolls_back_first(self):
        job, first, second, first_artifact, second_artifact = self.add_second_template()
        observed = []

        def fail_second_root_once(stage, path):
            if stage == "replace" and path.parent == self.template_root and path.name == "first_m.bytes":
                observed.append(
                    bool(list(self.template_root.glob(".tmp-*")))
                    and bool(list(self.tile_root.glob(".tmp-*")))
                )
            if stage == "replace" and path.parent == self.tile_root and path.name == "first_m.bytes":
                raise OSError("second binding failed")

        self.repo.file_ops = HookOps(fail_second_root_once)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(job, ("template", "tile"))
        report = service.commit_sync(plan.plan_id, {})
        rows = {row["template_key"]: row for row in report.rows}

        self.assertEqual(observed, [True])
        self.assertEqual(rows[first]["status"], "rolled_back")
        self.assertFalse((self.template_root / "first_m.bytes").exists())
        self.assertFalse((self.tile_root / "first_m.bytes").exists())
        self.assertEqual(rows[second]["status"], "synced")
        self.assertEqual((self.template_root / "second_m.bytes").read_bytes(), job.read_artifact(second_artifact["id"]))
        self.assertEqual((self.tile_root / "second_m.bytes").read_bytes(), job.read_artifact(second_artifact["id"]))

    def test_rollback_failure_is_explicit_and_does_not_block_sibling(self):
        job, first, second, _, second_artifact = self.add_second_template()

        def fail_apply_and_rollback(stage, path):
            if stage == "replace" and path.parent == self.tile_root and path.name == "first_m.bytes":
                raise OSError("second binding failed")
            if stage == "sync_rollback_delete" and path.parent == self.template_root and path.name == "first_m.bytes":
                raise OSError("rollback delete failed")

        self.repo.file_ops = HookOps(fail_apply_and_rollback)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(job, ("template", "tile"))
        report = service.commit_sync(plan.plan_id, {})
        rows = {row["template_key"]: row for row in report.rows}

        self.assertEqual(rows[first]["status"], "recovery_required")
        self.assertEqual(rows[first]["error"]["code"], "rollback_incomplete")
        self.assertEqual((self.template_root / "first_m.bytes").read_bytes(), job.read_artifact(rows[first]["artifact_id"]))
        self.assertEqual(rows[second]["status"], "synced")
        self.assertEqual((self.tile_root / "second_m.bytes").read_bytes(), job.read_artifact(second_artifact["id"]))
        unresolved = SyncService(JobRepository(self.root / "jobs"), self.settings_path).recover_pending()
        self.assertTrue(any(item["status"] == "recovery_required" for item in unresolved))

    def test_second_repository_failure_restores_first_repository_conflict_bytes(self):
        first = self.template_root / "new_m.bytes"
        second = self.tile_root / "new_m.bytes"
        first.write_bytes(b"old-template")
        second.write_bytes(b"old-tile")

        def fail_second(stage, path):
            if stage == "replace" and path == second:
                raise OSError("second repository unavailable")

        self.repo.file_ops = HookOps(fail_second)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template", "tile"))
        report = service.commit_sync(plan.plan_id, self.confirmations(plan))

        self.assertEqual(report.rows[0]["status"], "rolled_back")
        self.assertEqual(first.read_bytes(), b"old-template")
        self.assertEqual(second.read_bytes(), b"old-tile")
        self.assertTrue(all(target["backup"] for target in report.rows[0]["targets"]))

    def test_one_templates_journal_failure_does_not_block_sibling_template(self):
        job, first, second, _, second_artifact = self.add_second_template()
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(job, ("template",))
        first_transaction = next(row["transaction_id"] for row in plan.rows if row["template_key"] == first)
        failed = []

        def fail_first_journal_once(stage, path):
            if stage == "fsync_file" and path.parent.name == first_transaction and not failed:
                failed.append(True)
                raise OSError("journal storage unavailable")

        self.repo.file_ops = HookOps(fail_first_journal_once)
        report = service.commit_sync(plan.plan_id, {})
        rows = {row["template_key"]: row for row in report.rows}

        self.assertEqual(rows[first]["status"], "rolled_back")
        self.assertEqual(rows[second]["status"], "synced")
        self.assertFalse((self.template_root / "first_m.bytes").exists())
        self.assertEqual((self.template_root / "second_m.bytes").read_bytes(), job.read_artifact(second_artifact["id"]))

    def test_persistent_journal_failure_after_apply_does_not_block_sibling(self):
        job, first, second, first_artifact, second_artifact = self.add_second_template()
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(job, ("template",))
        first_transaction = next(row["transaction_id"] for row in plan.rows if row["template_key"] == first)
        applied = []

        def fail_first_audit_after_apply(stage, path):
            if stage == "fsync_dir" and path == self.template_root and path.joinpath("first_m.bytes"):
                applied.append(True)
            if stage == "fsync_file" and path.parent.name == first_transaction and applied:
                raise OSError("journal remains unavailable")

        self.repo.file_ops = HookOps(fail_first_audit_after_apply)
        report = service.commit_sync(plan.plan_id, {})
        rows = {row["template_key"]: row for row in report.rows}

        self.assertEqual(rows[first]["status"], "recovery_required")
        self.assertEqual(rows[second]["status"], "synced")
        self.assertEqual((self.template_root / "first_m.bytes").read_bytes(), job.read_artifact(first_artifact["id"]))
        self.assertEqual((self.template_root / "second_m.bytes").read_bytes(), job.read_artifact(second_artifact["id"]))

    def test_crash_journal_recovers_idempotently_after_restart(self):
        plan = self.service.create_sync_plan(self.job, ("template", "tile"))

        def crash_after_first_replace(stage, path):
            if stage == "fsync_dir" and path == self.template_root:
                raise SimulatedCrash("process interrupted after replace")

        self.repo.file_ops = HookOps(crash_after_first_replace)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        self.assertEqual((self.template_root / "new_m.bytes").read_bytes(), self.output)
        self.assertFalse((self.tile_root / "new_m.bytes").exists())

        fresh_repo = JobRepository(self.root / "jobs")
        fresh = SyncService(fresh_repo, self.settings_path)
        first = fresh.recover_pending()
        self.assertTrue(any(row["status"] == "rolled_back" for row in first))
        self.assertFalse((self.template_root / "new_m.bytes").exists())
        self.assertFalse((self.tile_root / "new_m.bytes").exists())
        second = fresh.recover_pending()
        self.assertEqual(second, ())

    def test_crash_during_preparation_recovers_recorded_temporary_files(self):
        plan = self.service.create_sync_plan(self.job, ("template", "tile"))

        def crash_before_prepared_journal(stage, path):
            if stage == "sync_prepare_dir_fsync" and path == self.template_root:
                raise SimulatedCrash("process interrupted while preparing")

        self.repo.file_ops = HookOps(crash_before_prepared_journal)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        self.assertFalse((self.template_root / "new_m.bytes").exists())
        self.assertFalse((self.tile_root / "new_m.bytes").exists())
        self.assertTrue(list(self.template_root.glob(".tmp-*")))
        self.assertTrue(list(self.tile_root.glob(".tmp-*")))

        fresh_repo = JobRepository(self.root / "jobs")
        fresh = SyncService(fresh_repo, self.settings_path)
        result = fresh.recover_pending()
        self.assertTrue(any(row["status"] == "rolled_back" for row in result))
        self.assertFalse(list(self.template_root.glob(".tmp-*")))
        self.assertFalse(list(self.tile_root.glob(".tmp-*")))

    def test_recovery_revalidates_historical_artifact_after_attempt_advances(self):
        plan = self.service.create_sync_plan(self.job, ("template",))

        def crash(stage, path):
            if stage == "sync_prepare_dir_fsync":
                raise SimulatedCrash("leave prepared temp")

        self.repo.file_ops = HookOps(crash)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        self.repo.file_ops = FileOps()
        self.job.start_attempt(self.key)

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        recovered = fresh.recover_pending()

        self.assertTrue(any(item["status"] == "rolled_back" for item in recovered))
        self.assertFalse(list(self.template_root.glob(".tmp-*")))

    def test_corrupt_journal_cannot_turn_recovery_cleanup_into_path_traversal(self):
        plan = self.service.create_sync_plan(self.job, ("template",))

        def crash(stage, path):
            if stage == "sync_prepare_dir_fsync":
                raise SimulatedCrash("leave a pending preparation journal")

        self.repo.file_ops = HookOps(crash)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        journal_path = next((self.job.directory / "sync" / plan.plan_id / "transactions").glob("*/journal.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        victim = self.template_root.parent.parent / "victim"
        victim.write_bytes(b"must remain")
        info = victim.stat()
        journal["targets"][0]["temp_name"] = "../../victim"
        journal["targets"][0]["temp_identity"] = [info.st_dev, info.st_ino, info.st_size]
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        result = fresh.recover_pending()
        self.assertEqual(result, ())
        self.assertTrue(any(item["code"] == "invalid_sync_journal" for item in fresh.diagnostics))
        self.assertEqual(victim.read_bytes(), b"must remain")

    def test_ancestor_symlink_swap_never_writes_replacement_directory(self):
        checkout = self.template_root.parents[len(TEMPLATE_SUFFIX.parts) - 1]
        outside = self.root / "outside-checkout"
        (outside / TEMPLATE_SUFFIX).mkdir(parents=True)
        swapped = []

        def swap(stage, path):
            if stage == "replace" and path.parent == self.template_root and not swapped:
                swapped.append(True)
                checkout.rename(self.root / "original-checkout")
                checkout.symlink_to(outside, target_is_directory=True)

        self.repo.file_ops = HookOps(swap)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template",))
        report = service.commit_sync(plan.plan_id, {})
        self.assertTrue(swapped)
        self.assertIn(report.rows[0]["status"], ("rolled_back", "recovery_required"))
        self.assertFalse((outside / TEMPLATE_SUFFIX / "new_m.bytes").exists())

    def test_create_rollback_never_deletes_file_replaced_at_final_checkpoint(self):
        target = self.template_root / "new_m.bytes"
        displaced = self.root / "transaction-created.bytes"

        def fail_second_and_swap_first(stage, path):
            if stage == "replace" and path == self.tile_root / "new_m.bytes":
                raise OSError("force rollback")
            if stage == "sync_rollback_delete" and path == target:
                target.rename(displaced)
                target.write_bytes(b"third-party")

        self.repo.file_ops = HookOps(fail_second_and_swap_first)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template", "tile"))
        report = service.commit_sync(plan.plan_id, {})

        self.assertEqual(report.rows[0]["status"], "recovery_required")
        self.assertEqual(target.read_bytes(), b"third-party")
        self.assertEqual(displaced.read_bytes(), self.output)

    def test_quarantine_name_collision_never_overwrites_third_party_file(self):
        target = self.template_root / "new_m.bytes"
        collision = []

        def fail_second_and_occupy_quarantine(stage, path):
            if stage == "replace" and path == self.tile_root / "new_m.bytes":
                raise OSError("force rollback")
            if stage == "sync_rollback_delete" and path == target and not collision:
                journal_path = next(
                    (self.job.directory / "sync").glob("plan-*/transactions/txn-*/journal.json")
                )
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                planned = next(
                    item for item in journal["targets"] if item["binding_key"] == "template"
                )
                occupied = self.template_root / planned["rollback_quarantine_name"]
                occupied.write_bytes(b"third-party-quarantine")
                collision.append(occupied)

        self.repo.file_ops = HookOps(fail_second_and_occupy_quarantine)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template", "tile"))
        report = service.commit_sync(plan.plan_id, {})

        self.assertEqual(report.rows[0]["status"], "recovery_required")
        self.assertEqual(len(collision), 1)
        self.assertEqual(collision[0].read_bytes(), b"third-party-quarantine")

    def test_conflict_rollback_never_loses_replacement_after_final_validation(self):
        first = self.template_root / "new_m.bytes"
        second = self.tile_root / "new_m.bytes"
        first.write_bytes(b"old-template")
        second.write_bytes(b"old-tile")
        raced = []

        def fail_second_and_race_restore(stage, path):
            if stage == "sync_before_atomic_restore_exchange" and path == first and not raced:
                first.write_bytes(b"third-party-after-final-check")
                raced.append(True)
            if stage in ("replace", "sync_before_atomic_install_exchange") and path == second:
                raise OSError("force second target failure")

        self.repo.file_ops = HookOps(fail_second_and_race_restore)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template", "tile"))
        report = service.commit_sync(plan.plan_id, self.confirmations(plan))

        preserved = [
            path.read_bytes()
            for path in self.template_root.iterdir()
            if path.is_file() and not path.is_symlink()
        ]
        self.assertEqual(raced, [True])
        self.assertEqual(report.rows[0]["status"], "recovery_required")
        self.assertIn(b"third-party-after-final-check", preserved)

    def test_forward_conflict_exchange_preserves_last_moment_replacement(self):
        target = self.template_root / "new_m.bytes"
        target.write_bytes(b"old-template")
        raced = []

        def race_forward_exchange(stage, path):
            if stage == "sync_before_atomic_install_exchange" and path == target and not raced:
                target.write_bytes(b"third-party-forward")
                raced.append(True)

        self.repo.file_ops = HookOps(race_forward_exchange)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template",))
        report = service.commit_sync(plan.plan_id, self.confirmations(plan))

        preserved = [
            path.read_bytes()
            for path in self.template_root.iterdir()
            if path.is_file() and not path.is_symlink()
        ]
        self.assertEqual(raced, [True])
        self.assertEqual(report.rows[0]["status"], "recovery_required")
        self.assertIn(b"third-party-forward", preserved)

    def test_forward_create_is_atomic_no_replace(self):
        target = self.template_root / "new_m.bytes"
        raced = []

        def occupy_create_target(stage, path):
            if stage == "sync_before_atomic_create" and path == target and not raced:
                target.write_bytes(b"third-party-create")
                raced.append(True)

        self.repo.file_ops = HookOps(occupy_create_target)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template",))
        report = service.commit_sync(plan.plan_id, {})

        self.assertEqual(raced, [True])
        self.assertNotEqual(report.rows[0]["status"], "synced")
        self.assertEqual(target.read_bytes(), b"third-party-create")

    def test_missing_native_atomic_rename_fails_before_external_write(self):
        original = sync_module._ATOMIC_RENAME
        sync_module._ATOMIC_RENAME = None
        try:
            plan = self.service.create_sync_plan(self.job, ("template",))
            report = self.service.commit_sync(plan.plan_id, {})
        finally:
            sync_module._ATOMIC_RENAME = original

        self.assertEqual(report.status, "invalidated")
        self.assertFalse((self.template_root / "new_m.bytes").exists())
        self.assertFalse(list(self.template_root.glob(".tmp-*")))

    def test_conflict_exchange_crash_recovers_original_bytes_idempotently(self):
        target = self.template_root / "new_m.bytes"
        target.write_bytes(b"old-template")
        plan = self.service.create_sync_plan(self.job, ("template",))

        def crash_after_exchange(stage, path):
            if stage == "fsync_dir" and path == self.template_root:
                raise SimulatedCrash("exchange completed before directory fsync")

        self.repo.file_ops = HookOps(crash_after_exchange)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, self.confirmations(plan))
        self.assertEqual(target.read_bytes(), self.output)

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        recovered = fresh.recover_pending()

        self.assertTrue(any(item["status"] == "rolled_back" for item in recovered))
        self.assertEqual(target.read_bytes(), b"old-template")
        self.assertFalse(list(self.template_root.glob(".tmp-*")))
        self.assertEqual(fresh.recover_pending(), ())

    def test_conflict_rollback_never_overwrites_file_replaced_at_checkpoint(self):
        first = self.template_root / "new_m.bytes"
        second = self.tile_root / "new_m.bytes"
        first.write_bytes(b"old-template")
        second.write_bytes(b"old-tile")

        def fail_second_and_swap_first(stage, path):
            if stage == "replace" and path == second:
                raise OSError("force rollback")
            if stage == "sync_rollback_replace" and path == first:
                first.write_bytes(b"third-party")

        self.repo.file_ops = HookOps(fail_second_and_swap_first)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template", "tile"))
        report = service.commit_sync(plan.plan_id, self.confirmations(plan))

        self.assertEqual(report.rows[0]["status"], "recovery_required")
        self.assertEqual(first.read_bytes(), b"third-party")
        self.assertEqual(second.read_bytes(), b"old-tile")

    def test_unknown_journal_state_never_rolls_back_committed_target(self):
        plan = self.service.create_sync_plan(self.job, ("template",))
        self.assertTrue(self.service.commit_sync(plan.plan_id, {}).ok)
        target = self.template_root / "new_m.bytes"
        journal_path = next((self.job.directory / "sync" / plan.plan_id / "transactions").glob("*/journal.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["state"] = "committted"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        recovered = fresh.recover_pending()

        self.assertEqual(target.read_bytes(), self.output)
        self.assertTrue(any(item["code"] == "invalid_sync_journal" for item in fresh.diagnostics))
        self.assertFalse(any(item.get("status") == "rolled_back" for item in recovered))

    def test_journal_plan_field_mismatch_has_zero_external_writes(self):
        plan = self.service.create_sync_plan(self.job, ("template", "tile"))

        def crash_after_first_replace(stage, path):
            if stage == "fsync_dir" and path == self.template_root:
                raise SimulatedCrash("stop after first replace")

        self.repo.file_ops = HookOps(crash_after_first_replace)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        target = self.template_root / "new_m.bytes"
        journal_path = next((self.job.directory / "sync" / plan.plan_id / "transactions").glob("*/journal.json"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["artifact_id"] = "art-" + "f" * 32
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        fresh.recover_pending()

        self.assertEqual(target.read_bytes(), self.output)
        self.assertTrue(any(item["code"] == "invalid_sync_journal" for item in fresh.diagnostics))

    def test_rolled_back_plan_and_conflict_confirmations_cannot_be_replayed(self):
        first = self.template_root / "new_m.bytes"
        second = self.tile_root / "new_m.bytes"
        first.write_bytes(b"old-template")
        second.write_bytes(b"old-tile")

        def fail_second(stage, path):
            if stage == "replace" and path == second:
                raise OSError("second repository unavailable")

        self.repo.file_ops = HookOps(fail_second)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template", "tile"))
        confirmations = self.confirmations(plan)
        first_report = service.commit_sync(plan.plan_id, confirmations)
        self.assertEqual(first_report.rows[0]["status"], "rolled_back")

        self.repo.file_ops = FileOps()
        second_report = service.commit_sync(plan.plan_id, confirmations)

        self.assertEqual(second_report.rows[0]["status"], "rolled_back")
        self.assertEqual(first.read_bytes(), b"old-template")
        self.assertEqual(second.read_bytes(), b"old-tile")

    def test_prepare_barrier_detects_any_target_change_before_first_replace(self):
        first = self.template_root / "new_m.bytes"
        second = self.tile_root / "new_m.bytes"
        first.write_bytes(b"old-template")
        second.write_bytes(b"old-tile")
        external_replaces = []
        changed = []

        def change_second_at_barrier(stage, path):
            if stage == "sync_prepare_dir_fsync" and path == self.template_root and not changed:
                changed.append(True)
                second.write_bytes(b"concurrent")
            if stage == "replace" and path.parent in (self.template_root, self.tile_root) and path.name == "new_m.bytes":
                external_replaces.append(path)

        self.repo.file_ops = HookOps(change_second_at_barrier)
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(self.job, ("template", "tile"))
        report = service.commit_sync(plan.plan_id, self.confirmations(plan))

        self.assertEqual(report.rows[0]["status"], "rolled_back")
        self.assertEqual(external_replaces, [])
        self.assertEqual(first.read_bytes(), b"old-template")
        self.assertEqual(second.read_bytes(), b"concurrent")

    def test_temp_intent_survives_crash_before_inode_record_and_recovers(self):
        plan = self.service.create_sync_plan(self.job, ("template",))

        def crash_after_temp_created(stage, path):
            if stage == "sync_temp_created":
                raise SimulatedCrash("created before inode journal")

        self.repo.file_ops = HookOps(crash_after_temp_created)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        self.assertTrue(list(self.template_root.glob(".tmp-*")))

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        recovered = fresh.recover_pending()

        self.assertTrue(any(item["status"] == "rolled_back" for item in recovered))
        self.assertFalse(list(self.template_root.glob(".tmp-*")))
        self.assertFalse((self.template_root / "new_m.bytes").exists())

    def test_temp_intent_without_created_file_recovers_idempotently(self):
        plan = self.service.create_sync_plan(self.job, ("template",))

        def crash_after_intent(stage, path):
            if stage == "sync_before_temp_create":
                raise SimulatedCrash("intent durable, file not created")

        self.repo.file_ops = HookOps(crash_after_intent)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        self.assertFalse(list(self.template_root.glob(".tmp-*")))

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        recovered = fresh.recover_pending()

        self.assertTrue(any(item["status"] == "rolled_back" for item in recovered))
        self.assertEqual(fresh.recover_pending(), ())

    def test_crash_during_temp_cleanup_remains_recoverable(self):
        plan = self.service.create_sync_plan(self.job, ("template",))
        states = []

        def fail_prepare_then_crash_cleanup(stage, path):
            if stage == "sync_prepare_dir_fsync":
                states.append("prepare_failed")
                raise OSError("fail after temp is recorded")
            if stage == "sync_before_temp_cleanup":
                raise SimulatedCrash("cleanup interrupted")

        self.repo.file_ops = HookOps(fail_prepare_then_crash_cleanup)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        self.assertEqual(states, ["prepare_failed"])
        self.assertTrue(list(self.template_root.glob(".tmp-*")))

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        recovered = fresh.recover_pending()

        self.assertTrue(any(item["status"] == "rolled_back" for item in recovered))
        self.assertFalse(list(self.template_root.glob(".tmp-*")))

    def test_committed_plan_retry_returns_persisted_report_without_writes(self):
        target = self.template_root / "new_m.bytes"
        target.write_bytes(b"old")
        plan = self.service.create_sync_plan(self.job, ("template",))
        confirmation = self.confirmations(plan)
        first = self.service.commit_sync(plan.plan_id, confirmation)
        before = target.stat()

        calls = []
        self.repo.file_ops = HookOps(lambda stage, path: calls.append((stage, path)))
        second = self.service.commit_sync(plan.plan_id, {})

        after = target.stat()
        self.assertEqual(second.to_dict(), first.to_dict())
        self.assertEqual(calls, [])
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_plan_lifecycle_tamper_is_rejected_without_external_write(self):
        plan = self.service.create_sync_plan(self.job, ("template",))
        self.assertTrue(self.service.commit_sync(plan.plan_id, {}).ok)
        target = self.template_root / "new_m.bytes"
        plan_path = self.job.directory / "sync" / plan.plan_id / "plan.json"
        persisted = json.loads(plan_path.read_text(encoding="utf-8"))
        persisted["status"] = "ready"
        plan_path.write_text(json.dumps(persisted), encoding="utf-8")

        calls = []
        fresh_repo = JobRepository(self.root / "jobs", HookOps(lambda stage, path: calls.append((stage, path))))
        fresh = SyncService(fresh_repo, self.settings_path)
        with self.assertRaises((KeyError, ValueError)):
            fresh.commit_sync(plan.plan_id, {})

        self.assertEqual(target.read_bytes(), self.output)
        self.assertEqual(calls, [])

    def test_crash_after_plan_is_consumed_but_before_first_journal_recovers(self):
        plan = self.service.create_sync_plan(self.job, ("template",))

        def crash_before_journal(*args, **kwargs):
            raise SimulatedCrash("plan consumed before first journal")

        self.service._prepare_transaction = crash_before_journal
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        fresh.recover_pending()
        plan_path = self.job.directory / "sync" / plan.plan_id / "plan.json"
        persisted = json.loads(plan_path.read_text(encoding="utf-8"))

        self.assertEqual(persisted["status"], "rolled_back")
        self.assertFalse((self.template_root / "new_m.bytes").exists())
        self.assertEqual(fresh.recover_pending(), ())

    def test_abandoned_preview_does_not_lock_recovery_after_another_plan_syncs(self):
        old = self.service.create_sync_plan(self.job, ("template", "tile"))
        latest = self.service.create_sync_plan(self.job, ("template", "tile"))
        self.assertTrue(self.service.commit_sync(latest.plan_id, {}).ok)
        old_path = self.job.directory / "sync" / old.plan_id / "plan.json"
        before_plan = old_path.read_bytes()
        targets = (self.template_root / "new_m.bytes", self.tile_root / "new_m.bytes")
        before_targets = [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in targets]

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)

        self.assertEqual(fresh.recover_pending(), ())
        self.assertEqual(fresh.diagnostics, [])
        self.assertEqual(old_path.read_bytes(), before_plan)
        self.assertEqual(
            [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in targets],
            before_targets,
        )
        # Unlocking recovery does not make an old preview valid for writing.
        rejected = fresh.commit_sync(old.plan_id, {})
        self.assertEqual(rejected.rows[0]["error"]["code"], "target_changed")

    def test_invalidated_unconsumed_preview_does_not_require_transaction_journal(self):
        old = self.service.create_sync_plan(self.job, ("template",))
        self.save_bindings(template=False)
        self.assertEqual(self.service.commit_sync(old.plan_id, {}).status, "invalidated")
        self.save_bindings()
        latest = self.service.create_sync_plan(self.job, ("template",))
        self.assertTrue(self.service.commit_sync(latest.plan_id, {}).ok)
        old_path = self.job.directory / "sync" / old.plan_id / "plan.json"
        before = old_path.read_bytes()
        self.assertIsNone(json.loads(before)["commit_plan_digest"])

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)

        self.assertEqual(fresh.recover_pending(), ())
        self.assertEqual(fresh.diagnostics, [])
        self.assertEqual(old_path.read_bytes(), before)
        self.assertEqual((self.template_root / "new_m.bytes").read_bytes(), self.output)

    def test_unconsumed_preview_with_existing_journal_is_not_treated_as_committed(self):
        old = self.service.create_sync_plan(self.job, ("template",))
        latest = self.service.create_sync_plan(self.job, ("template",))
        self.assertTrue(self.service.commit_sync(latest.plan_id, {}).ok)
        latest_root = self.job.directory / "sync" / latest.plan_id
        journal = json.loads(next(latest_root.glob("transactions/*/journal.json")).read_text())
        old_root = self.job.directory / "sync" / old.plan_id
        old_data = json.loads((old_root / "plan.json").read_text())
        row = old_data["rows"][0]
        journal.update(
            plan_id=old.plan_id,
            plan_digest=None,
            preview_digest=old_data["preview_digest"],
            transaction_id=row["transaction_id"],
        )
        journal_root = old_root / "transactions" / row["transaction_id"]
        journal_root.mkdir(parents=True)
        journal_path = journal_root / "journal.json"
        journal_path.write_text(json.dumps(journal))
        before = journal_path.read_bytes()

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        fresh.recover_pending()

        self.assertTrue(any(item["code"] == "invalid_sync_journal" for item in fresh.diagnostics))
        self.assertEqual(journal_path.read_bytes(), before)
        self.assertEqual((self.template_root / "new_m.bytes").read_bytes(), self.output)

    def test_invalidated_consumed_plan_still_requires_its_missing_journal(self):
        job, first, second, _, _ = self.add_second_template()
        outside = self.root / "outside.bytes"
        outside.write_bytes(b"outside")
        (self.template_root / "second_m.bytes").symlink_to(outside)
        plan = self.service.create_sync_plan(job, ("template",))
        self.assertEqual(self.service.commit_sync(plan.plan_id, {}).status, "partial_failed")
        plan_root = job.directory / "sync" / plan.plan_id
        persisted = json.loads((plan_root / "plan.json").read_text())
        self.assertEqual(persisted["status"], "invalidated")
        self.assertIsNotNone(persisted["commit_plan_digest"])
        transaction = next((plan_root / "transactions").glob("txn-*"))
        transaction.rename(transaction.with_name("lost-" + transaction.name))

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        fresh.recover_pending()

        self.assertTrue(any(item["code"] == "missing_expected_sync_journal" for item in fresh.diagnostics))
        self.assertEqual((self.template_root / "first_m.bytes").read_bytes(), job.read_artifact(job.artifact_records()[0]["id"]))
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_application_startup_allows_new_preview_after_abandoned_older_preview(self):
        from scripts.tower_reducer.api import Application

        self.service.create_sync_plan(self.job, ("template",))
        latest = self.service.create_sync_plan(self.job, ("template",))
        self.assertTrue(self.service.commit_sync(latest.plan_id, {}).ok)
        self.job.set_status("succeeded")
        before_target = (self.template_root / "new_m.bytes").stat()

        app = Application(
            tool_root=self.root / "tool",
            repository=JobRepository(self.root / "jobs"),
            settings_path=self.settings_path,
            discovery_root=self.root,
        )
        boot = app.bootstrap()
        self.assertFalse(boot["recovery"]["sync_locked"])
        self.assertEqual(boot["recovery"]["sync_diagnostic_count"], 0)
        preview = app.sync_plan({"job_id": self.job.job_id, "bindings": ["template"]})
        self.assertEqual(preview["rows"][0]["targets"][0]["classification"], "same_sha")
        self.assertEqual((self.template_root / "new_m.bytes").read_bytes(), self.output)
        after_target = (self.template_root / "new_m.bytes").stat()
        self.assertEqual(
            (after_target.st_ino, after_target.st_mtime_ns),
            (before_target.st_ino, before_target.st_mtime_ns),
        )

    def test_missing_expected_journal_with_changed_target_is_recovery_required(self):
        plan = self.service.create_sync_plan(self.job, ("template", "tile"))

        def crash_after_first_install(stage, path):
            if stage == "fsync_dir" and path == self.template_root:
                raise SimulatedCrash("first target installed")

        self.repo.file_ops = HookOps(crash_after_first_install)
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        target = self.template_root / "new_m.bytes"
        self.assertEqual(target.read_bytes(), self.output)
        transaction_root = self.job.directory / "sync" / plan.plan_id / "transactions"
        transaction = next(transaction_root.glob("txn-*"))
        transaction.rename(transaction_root / ("lost-" + transaction.name))

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        fresh.recover_pending()
        plan_path = self.job.directory / "sync" / plan.plan_id / "plan.json"
        persisted = json.loads(plan_path.read_text(encoding="utf-8"))

        self.assertEqual(persisted["status"], "recovery_required")
        self.assertEqual(target.read_bytes(), self.output)
        self.assertTrue(any(item["code"] == "missing_expected_sync_journal" for item in fresh.diagnostics))

    def test_blocked_row_nested_schema_is_rejected_before_batch_consumption(self):
        target = self.template_root / "new_m.bytes"
        outside = self.root / "outside.bytes"
        outside.write_bytes(b"outside")
        target.symlink_to(outside)
        plan = self.service.create_sync_plan(self.job, ("template",))
        plan_path = self.job.directory / "sync" / plan.plan_id / "plan.json"
        persisted = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["rows"][0]["status"], "blocked")
        persisted["rows"][0]["targets"] = [1]
        persisted["preview_digest"] = sync_module._preview_digest(persisted)
        persisted["plan_digest"] = sync_module._plan_digest(persisted)
        plan_path.write_text(json.dumps(persisted), encoding="utf-8")

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        with self.assertRaises((KeyError, ValueError)):
            fresh.commit_sync(plan.plan_id, {})

        self.assertEqual(outside.read_bytes(), b"outside")

    def test_artifact_attempt_change_invalidates_ready_plan_without_external_write(self):
        plan = self.service.create_sync_plan(self.job, ("template",))
        self.job.start_attempt(self.key)

        report = self.service.commit_sync(plan.plan_id, {})

        self.assertEqual(report.rows[0]["status"], "failed")
        self.assertFalse((self.template_root / "new_m.bytes").exists())

    def test_hardlinked_dual_targets_are_blocked_during_preview(self):
        first = self.template_root / "new_m.bytes"
        second = self.tile_root / "new_m.bytes"
        first.write_bytes(b"old")
        os.link(first, second)

        plan = self.service.create_sync_plan(self.job, ("template", "tile"))

        self.assertEqual(plan.rows[0]["status"], "blocked")
        self.assertEqual(first.read_bytes(), b"old")
        self.assertEqual(second.read_bytes(), b"old")

    def test_crash_after_all_replaces_before_committed_journal_rolls_back(self):
        plan = self.service.create_sync_plan(self.job, ("template", "tile"))
        original_persist = self.service._persist_journal

        def crash_on_committed(job, journal):
            if journal.get("state") == "committed":
                raise SimulatedCrash("committed state not durable")
            return original_persist(job, journal)

        self.service._persist_journal = crash_on_committed
        with self.assertRaises(SimulatedCrash):
            self.service.commit_sync(plan.plan_id, {})
        self.assertEqual((self.template_root / "new_m.bytes").read_bytes(), self.output)
        self.assertEqual((self.tile_root / "new_m.bytes").read_bytes(), self.output)

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        recovered = fresh.recover_pending()

        self.assertTrue(any(item["status"] == "rolled_back" for item in recovered))
        self.assertFalse((self.template_root / "new_m.bytes").exists())
        self.assertFalse((self.tile_root / "new_m.bytes").exists())

    def test_persistent_report_failure_occurs_after_all_sibling_templates_run(self):
        job, first, second, first_artifact, second_artifact = self.add_second_template()
        service = SyncService(self.repo, self.settings_path)
        plan = service.create_sync_plan(job, ("template",))

        def always_fail_report(job_arg, plan_id, report):
            raise OSError("report storage unavailable")

        service._persist_report = always_fail_report
        report = service.commit_sync(plan.plan_id, {})

        self.assertEqual(report.status, "audit_failed")
        self.assertEqual((self.template_root / "first_m.bytes").read_bytes(), job.read_artifact(first_artifact["id"]))
        self.assertEqual((self.template_root / "second_m.bytes").read_bytes(), job.read_artifact(second_artifact["id"]))
        self.assertTrue(any(item["code"] == "sync_report_persist_failed" for item in service.diagnostics))

    def test_bad_plan_is_diagnosed_without_blocking_healthy_plan_recovery(self):
        bad_plan = self.service.create_sync_plan(self.job, ("template",))
        bad_plan_path = self.job.directory / "sync" / bad_plan.plan_id / "plan.json"
        bad_plan_path.write_text("{broken", encoding="utf-8")

        other_repo = JobRepository(self.root / "jobs")
        other_job = other_repo.create([InputSnapshot(self.raw, "other-plan_m.bytes", self.root / "inputs")])
        other_key = next(iter(other_job.snapshot()["templates"]))
        other_job.register_output(other_key, rewrite_template(self.raw, self.rewrite_plan, "otherplan"), self.rewrite_plan, "otherplan")
        service = SyncService(other_repo, self.settings_path)
        healthy_plan = service.create_sync_plan(other_job, ("template",))

        def crash(stage, path):
            if stage == "sync_prepare_dir_fsync":
                raise SimulatedCrash("leave recoverable transaction")

        other_repo.file_ops = HookOps(crash)
        with self.assertRaises(SimulatedCrash):
            service.commit_sync(healthy_plan.plan_id, {})

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        recovered = fresh.recover_pending()

        self.assertTrue(any(item["status"] == "rolled_back" for item in recovered))
        self.assertTrue(any(item["code"] == "invalid_sync_plan" for item in fresh.diagnostics))

    def test_bad_journal_is_diagnosed_and_does_not_block_healthy_journal(self):
        bad_plan = self.service.create_sync_plan(self.job, ("template",))
        self.assertTrue(self.service.commit_sync(bad_plan.plan_id, {}).ok)
        bad_journal_path = next((self.job.directory / "sync" / bad_plan.plan_id / "transactions").glob("*/journal.json"))
        bad_journal_path.write_text("{broken", encoding="utf-8")

        other_repo = JobRepository(self.root / "jobs")
        other_job = other_repo.create([InputSnapshot(self.raw, "other_m.bytes", self.root / "inputs")])
        other_key = next(iter(other_job.snapshot()["templates"]))
        other_job.register_output(other_key, rewrite_template(self.raw, self.rewrite_plan, "other"), self.rewrite_plan, "other")
        service = SyncService(other_repo, self.settings_path)
        healthy_plan = service.create_sync_plan(other_job, ("template",))

        def crash(stage, path):
            if stage == "sync_prepare_dir_fsync":
                raise SimulatedCrash("pending healthy journal")

        other_repo.file_ops = HookOps(crash)
        with self.assertRaises(SimulatedCrash):
            service.commit_sync(healthy_plan.plan_id, {})

        fresh = SyncService(JobRepository(self.root / "jobs"), self.settings_path)
        recovered = fresh.recover_pending()

        self.assertTrue(any(item["status"] == "rolled_back" for item in recovered))
        self.assertTrue(any(item["code"] == "invalid_sync_journal" for item in fresh.diagnostics))


if __name__ == "__main__":
    unittest.main()
