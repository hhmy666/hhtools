from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.config_io import loads_batch_request, resolve_batch_config  # noqa: E402
from puzzle_core.errors import (  # noqa: E402
    CandidateValidationError,
    ExportConflictError,
    InputValidationError,
    OperationCancelledError,
    ReconciliationRequiredError,
)
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from puzzle_core.planning import config_hash  # noqa: E402
from puzzle_core.template_reader import parse_level_bytes  # noqa: E402
from services.export_service import (  # noqa: E402
    CONTROL_DIRECTORY,
    REPORTS_DIRECTORY,
    ExportRequest,
    ExportSelection,
    export_selected,
    new_idempotency_key,
    inconclusive_evidence_record,
)
import services.export_service as export_service_module  # noqa: E402
from services.job_service import generate_batch  # noqa: E402
from services.profile_service import Profile  # noqa: E402
from services.task_control import CooperativeTaskControl  # noqa: E402


def _varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def _chain_bytes(template_id: str) -> bytes:
    payload = [
        _bytes_field(2, template_id.encode()),
        _varint_field(4, 12),
        _varint_field(5, 4),
    ]
    for index in range(9):
        tile = b"".join(
            (
                _varint_field(1, index),
                _varint_field(2, 1),
                _varint_field(3, index),
                _varint_field(4, -10 if index % 2 == 0 else -11),
            )
        )
        payload.append(_bytes_field(6, tile))
    return b"".join(payload)


def _resolved_config(template_id: str, *, candidate_count: int = 1):
    return resolve_batch_config(
        loads_batch_request(
            json.dumps(
                {
                    "schema_version": 1,
                    "template_keys": [template_id],
                    "batch_seed": 99,
                    "generation_defaults": {
                        "replacement_sources": ["A", "B"],
                        "candidate_count": candidate_count,
                        "attempt_budget": 30,
                        "assignment_attempts": 500,
                        "unit_defaults": {"type_count": 3},
                    },
                    "batch_plans": [
                        {
                            "plan_id": "normal-s4",
                            "units": [{"unit_id": "U1", "step": 4}],
                        }
                    ],
                }
            )
        )
    )


class ExportServiceTests(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 9, 8, 30, tzinfo=dt.timezone.utc)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.source_path = self.root / "source001_m.bytes"
        self.source_path.write_bytes(_chain_bytes("source001"))
        self.ulinkconfig = self.root / "ulinkconfig"
        self.ulinkconfig.mkdir()
        with (self.ulinkconfig / "config_level.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.writer(stream)
            writer.writerow(
                ["LevelId", "TemplateId", *(f"DupPara{i}" for i in range(1, 11))]
            )
            writer.writerow(["string"] * 12)
            writer.writerow(["说明"] * 12)
            writer.writerow(
                ["300", "source001", *(["12|17|100|6"] * 10)]
            )
        self.profile = Profile(
            "profile-1",
            "测试环境",
            None,
            None,
            str(self.output),
            str(self.ulinkconfig),
        )
        self.config = _resolved_config("source001")
        batch = generate_batch(
            self.config,
            self.profile,
            file_inputs={"source001": self.source_path},
        )
        self.assertEqual(batch.succeeded, 1)
        item = batch.items[0]
        self.assertTrue(item.source)
        self.assertTrue(item.candidates)
        default_override = inconclusive_evidence_record(
            item.candidates[0].candidate.difficulty_simulation,
            item.candidates[0].candidate.experience_analysis,
        )
        self.assertTrue(default_override["acknowledgement_required"])
        self.selection = ExportSelection(
            source=item.source,
            plan=self.config.work_items[0].plan,
            generation_options=self.config.work_items[0].generation_options,
            generated=item.candidates[0],
            generation_seed=item.generation_seed,
            candidate_ordinal=1,
            original_plan_id="normal-s4",
            online_level_number=300,
            level_id="level-300",
            manual_evidence_override=default_override,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _export(self, *, key: str | None = None, duplicate: bool = False):
        return export_selected(
            ExportRequest(
                profile=self.profile,
                selections=(self.selection,),
                idempotency_key=key or new_idempotency_key(),
                allow_duplicate_as_new_version=duplicate,
            ),
            now=self.NOW,
        )

    def test_precommit_cancel_writes_nothing_and_same_request_can_retry(self) -> None:
        key = new_idempotency_key()
        request = ExportRequest(
            profile=self.profile,
            selections=(self.selection,),
            idempotency_key=key,
        )
        control = CooperativeTaskControl()
        control.request_cancel()
        with self.assertRaises(OperationCancelledError):
            export_selected(request, now=self.NOW, task_control=control)

        self.assertEqual(list(self.output.iterdir()), [])
        retried = export_selected(
            request,
            now=self.NOW,
            task_control=CooperativeTaskControl(),
        )
        self.assertEqual(retried.state, "completed")

    def test_first_export_then_v2_without_overwrite(self) -> None:
        first = self._export()
        first_bytes = (self.output / first.outputs[0].output_relative_path).read_bytes()
        second = self._export(duplicate=True)

        self.assertEqual(first.batch_id, "20260909")
        self.assertEqual(second.batch_id, "20260909_01")
        self.assertEqual(first.outputs[0].output_template_id, "aipuzzlesource001s4")
        self.assertEqual(second.outputs[0].output_template_id, "aipuzzlesource001s4v2")
        self.assertEqual(
            (self.output / first.outputs[0].output_relative_path).read_bytes(),
            first_bytes,
        )
        self.assertTrue((self.output / second.outputs[0].output_relative_path).is_file())

    def test_multiple_candidates_from_one_work_item_receive_sequential_versions(self) -> None:
        config = _resolved_config("source001", candidate_count=3)
        batch = generate_batch(
            config,
            self.profile,
            file_inputs={"source001": self.source_path},
        )
        item = batch.items[0]
        self.assertEqual(len(item.candidates), 3)
        selections = tuple(
            ExportSelection(
                source=item.source,
                plan=config.work_items[0].plan,
                generation_options=generated.generation_options,
                generated=generated,
                generation_seed=generated.candidate.candidate_seed,
                candidate_ordinal=generated.candidate.candidate_index,
                original_plan_id="normal-s4",
                manual_evidence_override=inconclusive_evidence_record(
                    generated.candidate.difficulty_simulation,
                    generated.candidate.experience_analysis,
                ),
            )
            for generated in item.candidates
        )

        result = export_selected(
            ExportRequest(
                profile=self.profile,
                selections=selections,
                idempotency_key=new_idempotency_key(),
            ),
            now=self.NOW,
        )

        self.assertEqual(
            [output.output_template_id for output in result.outputs],
            [
                "aipuzzlesource001s4",
                "aipuzzlesource001s4v2",
                "aipuzzlesource001s4v3",
            ],
        )
        self.assertEqual(
            [output.candidate_id for output in result.outputs],
            [generated.candidate.candidate_id for generated in item.candidates],
        )
        self.assertEqual(
            [output.resolved_plan_id for output in result.outputs],
            ["normal-s4", "normal-s4", "normal-s4"],
        )
        for output in result.outputs:
            output_path = self.output / output.output_relative_path
            self.assertTrue(output_path.is_file())
            parsed = parse_level_bytes(
                output_path.read_bytes(), source_name=output_path.name
            )
            self.assertEqual(parsed.template_id, output.output_template_id)

    def test_idempotent_retry_returns_original_result(self) -> None:
        key = new_idempotency_key()
        first = self._export(key=key)
        second = self._export(key=key)

        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(len(list(self.output.glob("aipuzzle*_m.bytes"))), 1)
        self.assertEqual(len(list((self.output / REPORTS_DIRECTORY).glob("20260909*"))), 1)

    def test_same_key_with_different_payload_is_rejected(self) -> None:
        key = new_idempotency_key()
        self._export(key=key)
        with self.assertRaises(ExportConflictError):
            self._export(key=key, duplicate=True)

    def test_same_key_rejects_changed_report_and_source_metadata(self) -> None:
        key = new_idempotency_key()
        self._export(key=key)
        changed_source = replace(
            self.selection.source,
            source_locator="renamed_source001_m.bytes",
        )
        cases = (
            (self.profile, replace(self.selection, online_level_number=301)),
            (self.profile, replace(self.selection, level_id="level-301")),
            (replace(self.profile, name="另一环境"), self.selection),
            (self.profile, replace(self.selection, source=changed_source)),
        )
        for profile, selection in cases:
            with self.subTest(profile=profile.name, level_id=selection.level_id):
                with self.assertRaises(ExportConflictError):
                    export_selected(
                        ExportRequest(
                            profile=profile,
                            selections=(selection,),
                            idempotency_key=key,
                        ),
                        now=self.NOW,
                    )

    def test_orphan_formal_bytes_requires_reconciliation(self) -> None:
        orphan = self.output / "aipuzzleorphaneds4_m.bytes"
        orphan.write_bytes(self.source_path.read_bytes())
        with self.assertRaises(ReconciliationRequiredError):
            self._export()

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_control_directory_symlink_escape_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.output / CONTROL_DIRECTORY).symlink_to(
            outside, target_is_directory=True
        )

        with self.assertRaises(ReconciliationRequiredError):
            self._export()

        self.assertFalse((outside / "workspace.json").exists())
        self.assertFalse((outside / "transactions").exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_control_subdirectory_symlink_escape_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        control = self.output / CONTROL_DIRECTORY
        control.mkdir()
        (control / "transactions").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ReconciliationRequiredError):
            self._export()

        self.assertEqual(list(outside.iterdir()), [])

    def test_internal_id_wire_and_immutable_records_are_consistent(self) -> None:
        result = self._export()
        exported = result.outputs[0]
        output_path = self.output / exported.output_relative_path
        parsed = parse_level_bytes(output_path.read_bytes(), source_name=output_path.name)

        self.assertEqual(parsed.template_id, exported.output_template_id)
        registry = json.loads(
            (self.output / exported.registry_relative_path).read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (self.output / exported.manifest_relative_path).read_text(encoding="utf-8")
        )
        self.assertEqual(registry["output_sha256"], exported.output_sha256)
        self.assertEqual(manifest["output_sha256"], exported.output_sha256)
        self.assertEqual(manifest["source_locator"], self.source_path.name)
        self.assertEqual(manifest["source_tile_counts"], {"A": 5, "B": 4})
        self.assertNotIn(str(self.root), json.dumps(manifest, ensure_ascii=False))
        self.assertNotIn("completed_at", manifest)
        self.assertNotIn("recovery_count", manifest)
        self.assertNotIn("last_recovery_result", manifest)
        self.assertFalse(manifest["execution_state_embedded"])
        self.assertEqual(
            manifest["execution_state_authority"]["kind"],
            "mutable_transaction_ledger",
        )
        self.assertIn("candidate_seed_ordinal", manifest)
        self.assertIn("candidate_seed_sha256", manifest)
        self.assertTrue(manifest["manual_evidence_override"]["confirmed"])
        validation = manifest["validation"]
        self.assertEqual(
            validation["coverage_audit"]["adjacent_single_full_relations"], []
        )
        self.assertIn("static_relations", validation["coverage_audit"])
        self.assertIn("inventory", validation)
        self.assertIn("effective_constraints", validation)
        self.assertIn("unit_access_reports", validation)
        self.assertIn("combination_conclusion", validation)
        detail_path = (
            self.output
            / REPORTS_DIRECTORY
            / result.batch_id
            / f"{result.batch_id}_验证明细.json"
        )
        detail = json.loads(detail_path.read_text(encoding="utf-8"))
        self.assertEqual(detail["batch"]["status"], "validation_passed")
        self.assertFalse(detail["batch"]["execution_state_embedded"])

    def test_manual_inconclusive_export_records_exact_evidence_and_refuses_stale_acknowledgement(self) -> None:
        # Isolate the transaction boundary with an explicit recomputed evidence
        # fixture; the validator's own three-state checks are tested separately.
        difficulty = dict(self.selection.generated.candidate.difficulty_simulation)
        difficulty["evidence_coverage"] = {
            **dict(difficulty.get("evidence_coverage") or {}),
            "materialization_fidelity": "synthetic",
            "overall_status": "inconclusive",
        }
        experience = dict(self.selection.generated.candidate.experience_analysis)
        experience["hard_validation"] = {
            "status": "inconclusive", "passed": False,
            "reasons": ["整盘悬置持续上限尚未证明"],
        }
        evidence_record = inconclusive_evidence_record(difficulty, experience)
        self.assertTrue(evidence_record["acknowledgement_required"])
        unconfirmed = replace(self.selection, manual_evidence_override=None)
        with mock.patch.object(
            export_service_module, "validate_candidate",
            return_value=self.selection.generated.validation,
        ), mock.patch.object(
            export_service_module, "_recompute_validated_evidence",
            return_value=(difficulty, experience),
        ), self.assertRaisesRegex(ExportConflictError, "需要在应用内人工确认"):
            export_selected(
                ExportRequest(
                    profile=self.profile,
                    selections=(unconfirmed,),
                    idempotency_key=new_idempotency_key(),
                ),
                now=self.NOW,
            )
        selection = replace(self.selection, manual_evidence_override=evidence_record)
        request = ExportRequest(
            profile=self.profile, selections=(selection,),
            idempotency_key=new_idempotency_key(),
        )
        with mock.patch.object(
            export_service_module, "validate_candidate",
            return_value=self.selection.generated.validation,
        ), mock.patch.object(
            export_service_module, "_recompute_validated_evidence",
            return_value=(difficulty, experience),
        ):
            result = export_selected(request, now=self.NOW)
        manifest = json.loads(
            (self.output / result.outputs[0].manifest_relative_path).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["manual_evidence_override"]["axes"], evidence_record["axes"])
        self.assertTrue(manifest["manual_evidence_override"]["confirmed"])
        detail_path = self.output / REPORTS_DIRECTORY / result.batch_id / f"{result.batch_id}_验证明细.json"
        detail = json.loads(detail_path.read_text(encoding="utf-8"))
        self.assertEqual(
            detail["outputs"][0]["manual_evidence_override"]["inconclusive_axes"],
            ["whole_board_completion", "difficulty_evidence"],
        )
        markdown_path = self.output / REPORTS_DIRECTORY / result.batch_id / f"{result.batch_id}_验证报告.md"
        self.assertIn("人工确认的非推荐结论", markdown_path.read_text(encoding="utf-8"))
        stale = replace(selection, manual_evidence_override={"status": "inconclusive"})
        with mock.patch.object(
            export_service_module, "validate_candidate",
            return_value=self.selection.generated.validation,
        ), mock.patch.object(
            export_service_module, "_recompute_validated_evidence",
            return_value=(difficulty, experience),
        ), self.assertRaisesRegex(ExportConflictError, "证据状态已变化"):
            export_selected(
                ExportRequest(
                    profile=self.profile, selections=(stale,),
                    idempotency_key=new_idempotency_key(),
                ),
                now=self.NOW,
            )
        public_transaction = (
            self.output
            / REPORTS_DIRECTORY
            / "导出事务"
            / f"{result.idempotency_key}.json"
        )
        self.assertTrue(public_transaction.is_file())
        public_record = json.loads(public_transaction.read_text(encoding="utf-8"))
        self.assertEqual(public_record["request_payload_hash"], result.request_payload_hash)
        ledger = json.loads(
            (
                self.output
                / CONTROL_DIRECTORY
                / "transactions"
                / f"{result.idempotency_key}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(ledger["state"], "completed")
        self.assertEqual(
            ["prepared", "files_written", "verified", "completed"],
            [item["state"] for item in ledger["state_history"]],
        )

    def test_disproved_rule_conclusion_can_export_after_exact_confirmation(self) -> None:
        difficulty = dict(self.selection.generated.candidate.difficulty_simulation)
        experience = dict(self.selection.generated.candidate.experience_analysis)
        experience["hard_validation"] = {
            "status": "disproved",
            "passed": False,
            "reasons": ["模拟路径发现循环死锁"],
        }
        failed_report = replace(self.selection.generated.validation, passed=False)
        evidence_record = inconclusive_evidence_record(
            difficulty,
            experience,
            candidate_validation_passed=False,
        )
        self.assertEqual(evidence_record["status"], "disproved")
        self.assertIn(
            "candidate_rule_validation", evidence_record["disproved_axes"]
        )
        self.assertIn(
            "whole_board_completion", evidence_record["disproved_axes"]
        )
        selection = replace(
            self.selection,
            generated=replace(
                self.selection.generated,
                validation=failed_report,
            ),
            manual_evidence_override=evidence_record,
        )
        with mock.patch.object(
            export_service_module,
            "validate_candidate",
            return_value=failed_report,
        ), mock.patch.object(
            export_service_module,
            "_recompute_validated_evidence",
            return_value=(difficulty, experience),
        ):
            result = export_selected(
                ExportRequest(
                    profile=self.profile,
                    selections=(selection,),
                    idempotency_key=new_idempotency_key(),
                ),
                now=self.NOW,
            )
        manifest = json.loads(
            (self.output / result.outputs[0].manifest_relative_path).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(manifest["validation"]["passed"])
        self.assertEqual(
            manifest["manual_evidence_override"]["status"], "disproved"
        )

    def test_technical_candidate_corruption_cannot_be_acknowledged_away(self) -> None:
        technical_error = CandidateValidationError(
            "候选身份与当前源文件不一致",
            evidence={"check": "candidate_identity"},
        )
        with mock.patch.object(
            export_service_module,
            "validate_candidate",
            side_effect=technical_error,
        ), self.assertRaises(CandidateValidationError) as caught:
            self._export()
        self.assertEqual(
            caught.exception.detail.evidence["check"], "candidate_identity"
        )

    def test_export_retains_independently_recomputed_dynamic_evidence(self) -> None:
        with mock.patch.object(
            export_service_module,
            "simulate_candidate_difficulty",
            wraps=export_service_module.simulate_candidate_difficulty,
        ) as recompute_difficulty, mock.patch.object(
            export_service_module,
            "evaluate_candidate_experience",
            wraps=export_service_module.evaluate_candidate_experience,
        ) as recompute_experience:
            result = self._export()

        self.assertEqual(recompute_difficulty.call_count, 1)
        self.assertEqual(recompute_experience.call_count, 1)
        exported = result.outputs[0]
        manifest = json.loads(
            (self.output / exported.manifest_relative_path).read_text(
                encoding="utf-8"
            )
        )
        descriptor = manifest["revalidation_evidence"]
        evidence_path = self.output / descriptor["relative_path"]
        evidence_bytes = evidence_path.read_bytes()
        evidence = json.loads(evidence_bytes.decode("utf-8"))
        revalidation = evidence["revalidation"]

        self.assertEqual(
            descriptor["evidence_sha256"],
            hashlib.sha256(evidence_bytes).hexdigest(),
        )
        self.assertEqual(
            descriptor["revalidation_sha256"],
            config_hash(revalidation),
        )
        self.assertEqual(
            evidence["revalidation_sha256"],
            descriptor["revalidation_sha256"],
        )
        self.assertEqual(
            descriptor["validation_report_sha256"],
            config_hash(manifest["validation"]),
        )
        self.assertEqual(
            config_hash(revalidation["difficulty_simulation"]),
            config_hash(
                self.selection.generated.candidate.difficulty_simulation
            ),
        )
        self.assertEqual(
            config_hash(revalidation["experience_analysis"]),
            config_hash(self.selection.generated.candidate.experience_analysis),
        )
        self.assertEqual(
            revalidation["identity"]["content_semantic_hash"],
            exported.content_semantic_hash,
        )
        self.assertEqual(
            revalidation["identity"]["output_sha256"],
            exported.output_sha256,
        )
        versions = revalidation["versions"]
        coverage = revalidation["difficulty_simulation"]["evidence_coverage"]
        for name in (
            "common_seed_version",
            "materialization_version",
            "simulation_version",
        ):
            self.assertEqual(versions[name], coverage[name])
        self.assertEqual(
            versions["experience_analysis_version"],
            revalidation["experience_analysis"]["version"],
        )

        strategies = {
            "random",
            "greedy",
            "local_optimal",
            "unlock",
            "adversarial",
        }
        simulation = revalidation["difficulty_simulation"]
        for duplicate in (
            list(simulation["duplicates"])
            + list(simulation["source_duplicates"])
        ):
            self.assertEqual(set(duplicate["strategies"]), strategies)
            for strategy in strategies:
                self.assertIn(
                    "reveal_curve",
                    duplicate["strategies"][strategy],
                )

    def test_retry_recovers_same_prepared_transaction_without_new_version(self) -> None:
        key = new_idempotency_key()
        first = self._export(key=key)
        ledger_path = (
            self.output / CONTROL_DIRECTORY / "transactions" / f"{key}.json"
        )
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["state"] = "prepared"
        ledger["state_history"] = ledger["state_history"][:1]
        ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        recovered = self._export(key=key)

        self.assertTrue(recovered.recovered)
        self.assertEqual(recovered.outputs, first.outputs)
        self.assertEqual(recovered.batch_id, first.batch_id)
        self.assertEqual(len(list(self.output.glob("aipuzzle*_m.bytes"))), 1)

    def test_orphan_staging_before_prepared_publish_is_fail_closed(self) -> None:
        key = new_idempotency_key()
        original_exclusive_json = export_service_module._exclusive_json
        saw_complete_staging = False

        def interrupt_before_ledger(path, payload, **kwargs):
            nonlocal saw_complete_staging
            if path.parent.name == "transactions":
                staging = self.output / CONTROL_DIRECTORY / "staging" / key
                saw_complete_staging = staging.is_dir() and all(
                    (staging / item["relative_path"]).is_file()
                    for item in payload["files"]
                )
                raise RuntimeError("simulated interruption before prepared ledger")
            return original_exclusive_json(path, payload, **kwargs)

        with mock.patch.object(
            export_service_module,
            "_exclusive_json",
            side_effect=interrupt_before_ledger,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                self._export(key=key)

        self.assertTrue(saw_complete_staging)
        ledger = self.output / CONTROL_DIRECTORY / "transactions" / f"{key}.json"
        self.assertFalse(ledger.exists())
        self.assertEqual(list(self.output.glob("aipuzzle*_m.bytes")), [])
        with self.assertRaisesRegex(ReconciliationRequiredError, "staging"):
            self._export(key=key)

    def test_archived_completed_artifacts_do_not_block_new_export_or_get_restored(self) -> None:
        first = self._export()
        output = first.outputs[0]
        ledger_path = self.output / CONTROL_DIRECTORY / "transactions" / f"{first.idempotency_key}.json"
        ledger = json.loads(ledger_path.read_text())
        missing = [entry for entry in ledger["files"] if entry["relative_path"] in {
            output.output_relative_path, output.manifest_relative_path,
        } or entry["relative_path"].endswith("_revalidation.json")]
        for entry in missing:
            (self.output / entry["relative_path"]).unlink()
        second = self._export(duplicate=True)
        self.assertNotEqual(first.outputs[0].output_template_id, second.outputs[0].output_template_id)
        self.assertTrue(all(not (self.output / entry["relative_path"]).exists() for entry in missing))
        self.assertEqual(json.loads(ledger_path.read_text()), ledger)
        self.assertEqual(second.outputs[0].version, 2)

    def test_same_completed_retry_still_requires_its_own_files(self) -> None:
        key = new_idempotency_key()
        first = self._export(key=key)
        (self.output / first.outputs[0].output_relative_path).unlink()
        with self.assertRaises(ReconciliationRequiredError):
            self._export(key=key)

    def test_archiving_entire_export_keeps_ledger_name_and_version_reserved(self) -> None:
        first = self._export()
        ledger = json.loads((self.output / CONTROL_DIRECTORY / "transactions" / f"{first.idempotency_key}.json").read_text())
        for entry in ledger["files"]:
            (self.output / entry["relative_path"]).unlink()
        result = self._export(duplicate=True)
        self.assertEqual(result.outputs[0].version, 2)
        self.assertEqual(result.outputs[0].output_template_id, "aipuzzlesource001s4v2")
        with self.assertRaises(ExportConflictError):
            self._export()

    def test_moving_only_registry_reserves_identity_without_restoring_shard(self) -> None:
        first = self._export()
        registry = self.output / first.outputs[0].registry_relative_path
        registry.unlink()
        result = self._export(duplicate=True)
        self.assertEqual(result.outputs[0].version, 2)
        self.assertFalse(registry.exists())

    def test_unrelated_archived_bytes_do_not_require_a_staging_backup(self) -> None:
        first = self._export()
        ledger = json.loads((self.output / CONTROL_DIRECTORY / "transactions" / f"{first.idempotency_key}.json").read_text())
        missing = [entry for entry in ledger["files"] if entry["relative_path"] == first.outputs[0].output_relative_path]
        for entry in missing:
            (self.output / entry["relative_path"]).unlink()
        staging = self.output / CONTROL_DIRECTORY / "staging" / first.idempotency_key
        (staging / missing[0]["relative_path"]).unlink()
        result = self._export(duplicate=True)
        self.assertEqual(result.outputs[0].version, 2)
        self.assertTrue(all(not (self.output / entry["relative_path"]).exists() for entry in missing))

    def test_recovery_never_overwrites_existing_conflicting_content(self) -> None:
        first = self._export()
        target = self.output / first.outputs[0].output_relative_path
        target.unlink()
        target.write_bytes(b"foreign content")
        with self.assertRaises(ReconciliationRequiredError):
            self._export(duplicate=True)
        self.assertEqual(target.read_bytes(), b"foreign content")

    def test_registry_and_ledger_paths_cannot_escape_output_root(self) -> None:
        first_key = new_idempotency_key()
        first = self._export(key=first_key)
        registry_path = self.output / first.outputs[0].registry_relative_path
        original_registry_bytes = registry_path.read_bytes()
        registry = json.loads(original_registry_bytes.decode("utf-8"))
        registry["output_relative_path"] = "../outside_m.bytes"
        registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(ReconciliationRequiredError):
            self._export(duplicate=True)

        # Restore the registry so the completed ledger path can be audited alone.
        registry_path.write_bytes(original_registry_bytes)
        ledger_path = (
            self.output
            / CONTROL_DIRECTORY
            / "transactions"
            / f"{first_key}.json"
        )
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["files"][0]["relative_path"] = "../outside.json"
        ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(ReconciliationRequiredError):
            self._export(key=first_key)

    def test_same_candidate_cannot_repeat_in_one_batch(self) -> None:
        with self.assertRaisesRegex(InputValidationError, "同一候选"):
            export_selected(
                ExportRequest(
                    profile=self.profile,
                    selections=(self.selection, self.selection),
                    idempotency_key=new_idempotency_key(),
                ),
                now=self.NOW,
            )

    def test_candidate_ordinal_cannot_be_forged(self) -> None:
        forged = replace(self.selection, candidate_ordinal=2)
        with self.assertRaisesRegex(InputValidationError, "候选序号"):
            export_selected(
                ExportRequest(
                    profile=self.profile,
                    selections=(self.selection, forged),
                    idempotency_key=new_idempotency_key(),
                ),
                now=self.NOW,
            )

    def test_target_name_preoccupied_without_registry_is_never_claimed(self) -> None:
        target = self.output / "aipuzzlesource001s4_m.bytes"
        target.write_bytes(self.source_path.read_bytes())
        with self.assertRaises(ReconciliationRequiredError):
            self._export()

    def test_export_rechecks_that_output_is_not_inside_readonly_source(self) -> None:
        unsafe_profile = Profile(
            self.profile.profile_id,
            self.profile.name,
            str(self.root),
            str(self.root),
            str(self.output),
            self.profile.ulinkconfig_root,
        )
        with self.assertRaises(InputValidationError):
            export_selected(
                ExportRequest(
                    profile=unsafe_profile,
                    selections=(self.selection,),
                    idempotency_key=new_idempotency_key(),
                ),
                now=self.NOW,
            )

    def test_delimiter_free_family_collision_is_rejected_before_write(self) -> None:
        def prepared(source_id: str, signature: str):
            return SimpleNamespace(
                family=f"aipuzzle{source_id}{signature}",
                signature=signature,
                selection=SimpleNamespace(
                    source=SimpleNamespace(
                        level=SimpleNamespace(template_id=source_id)
                    )
                ),
            )

        first = prepared("base", "s4s5")
        second = prepared("bases4", "s5")
        self.assertEqual(first.family, second.family)
        with self.assertRaisesRegex(ExportConflictError, "同一批次"):
            export_service_module._validate_naming_namespace((), (first, second))

    def test_family_collision_with_history_is_rejected(self) -> None:
        pending = SimpleNamespace(
            family="aipuzzlebases4s5",
            signature="s5",
            selection=SimpleNamespace(
                source=SimpleNamespace(
                    level=SimpleNamespace(template_id="bases4")
                )
            ),
        )
        records = (
            {
                "source_template_id": "base",
                "signature": "s4s5",
                "family": "aipuzzlebases4s5",
            },
        )
        with self.assertRaisesRegex(ExportConflictError, "历史"):
            export_service_module._validate_naming_namespace(records, (pending,))

    def test_display_id_and_staging_casefold_collisions_are_rejected(self) -> None:
        prepared = export_service_module._prepare_selections((self.selection,))
        with self.assertRaisesRegex(ExportConflictError, "模板 ID"):
            export_service_module._validate_output_id_namespace(
                ({"output_template_id": "AIPUZZLESOURCE001S4"},),
                prepared,
                (1,),
            )
        staged = {"Report/File.json": b"first"}
        with self.assertRaisesRegex(ExportConflictError, "staging"):
            export_service_module._stage_artifact(
                staged, "report/file.json", b"second"
            )

    def test_prepare_selections_reports_real_candidate_progress(self) -> None:
        events: list[tuple[int, int, str]] = []
        export_service_module._prepare_selections(
            (self.selection,),
            progress_callback=lambda completed, total, current: events.append(
                (completed, total, current)
            ),
        )
        self.assertEqual(
            events,
            [
                (0, 1, self.selection.generated.candidate.candidate_id),
                (1, 1, self.selection.generated.candidate.candidate_id),
            ],
        )


if __name__ == "__main__":
    unittest.main()
