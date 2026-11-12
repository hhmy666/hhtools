from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.config_io import loads_batch_request, resolve_batch_config  # noqa: E402
from puzzle_core.errors import InputValidationError  # noqa: E402
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from services.job_service import generate_batch  # noqa: E402
from services.profile_service import Profile  # noqa: E402
from services.selection_service import (  # noqa: E402
    CandidateChoice,
    loads_selection_payload,
    resolve_export_selections,
    summarize_candidate_choices,
)


def _varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def _chain_bytes(template_id: str) -> bytes:
    payload = [_bytes_field(2, template_id.encode()), _varint_field(4, 12), _varint_field(5, 4)]
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


def _config(template_id: str):
    return resolve_batch_config(
        loads_batch_request(
            json.dumps(
                {
                    "schema_version": 1,
                    "template_keys": [template_id],
                    "batch_seed": 123,
                    "generation_defaults": {
                        "replacement_sources": ["A", "B"],
                        "candidate_count": 2,
                        "attempt_budget": 40,
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


class SelectionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source001_m.bytes"
        self.source.write_bytes(_chain_bytes("source001"))
        self.output = self.root / "output"
        self.output.mkdir()
        self.profile = Profile("profile-1", "test", None, None, str(self.output))
        self.config = _config("source001")
        self.generated = generate_batch(
            self.config,
            self.profile,
            file_inputs={"source001": self.source},
        )
        # Selection-boundary tests isolate identifier/proof handling from the
        # simulator.  Mark the fixture candidates as having met their selected
        # difficulty target; dedicated tests below cover the rejection path.
        for generated in self.generated.items[0].candidates:
            difficulty = generated.candidate.difficulty_simulation
            difficulty["qualified"] = True
            difficulty.setdefault("trend", {})["safety_qualified"] = True
            difficulty["trend"]["becomes_easier"] = True
            difficulty.setdefault("evidence_coverage", {}).update(
                {
                    "materialization_fidelity": "reconstructed",
                    "overall_status": "acceptable",
                }
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_strict_selection_file_and_resolution(self) -> None:
        candidate = self.generated.items[0].candidates[1].candidate
        payload = json.dumps(
            {
                "schema_version": 1,
                "selections": [
                    {
                        "template_key": "source001",
                        "resolved_plan_id": "normal-s4",
                        "candidate_id": candidate.candidate_id,
                        "online_level_number": 300,
                        "level_id": "level-300",
                    }
                ],
            }
        )
        choices = loads_selection_payload(payload)
        resolved = resolve_export_selections(self.config, self.generated, choices)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].generated.candidate.candidate_id, candidate.candidate_id)
        self.assertEqual(resolved[0].candidate_ordinal, 2)
        self.assertEqual(resolved[0].online_level_number, 300)

    def test_multiple_candidates_from_same_work_item_resolve_in_review_order(self) -> None:
        candidates = self.generated.items[0].candidates
        records = [
            {
                "template_key": "source001",
                "resolved_plan_id": "normal-s4",
                "candidate_id": generated.candidate.candidate_id,
            }
            for generated in reversed(candidates)
        ]

        choices = loads_selection_payload(
            json.dumps({"schema_version": 1, "selections": records})
        )
        resolved = resolve_export_selections(self.config, self.generated, choices)

        self.assertEqual(
            [item.generated.candidate.candidate_id for item in resolved],
            [record["candidate_id"] for record in records],
        )
        self.assertEqual([item.candidate_ordinal for item in resolved], [2, 1])
        self.assertEqual(
            summarize_candidate_choices(choices),
            {
                "candidate_count": 2,
                "work_item_count": 1,
                "groups": [
                    {
                        "template_key": "source001",
                        "resolved_plan_id": "normal-s4",
                        "candidate_ids": [
                            record["candidate_id"] for record in records
                        ],
                        "candidate_count": 2,
                    }
                ],
            },
        )

    def test_retained_candidate_exports_with_its_own_resolved_plan_snapshot(self) -> None:
        work = self.config.work_items[0]
        original = self.generated.items[0].candidates[0]
        retained_plan = replace(
            work.plan,
            units=(replace(work.plan.units[0], step=5),),
            signature="s5",
            config_hash="retained-plan-hash",
        )
        retained = replace(original, resolved_plan=retained_plan)
        generated = replace(
            self.generated,
            items=(
                replace(
                    self.generated.items[0],
                    candidates=(retained,),
                ),
            ),
        )
        choice = CandidateChoice(
            template_key="source001",
            resolved_plan_id=work.plan.plan_id,
            candidate_id=retained.candidate.candidate_id,
            manual_evidence_override=True,
        )

        (selection,) = resolve_export_selections(self.config, generated, (choice,))

        self.assertEqual(selection.plan.signature, "s5")
        self.assertEqual(selection.plan.config_hash, "retained-plan-hash")

    def test_candidate_payload_discloses_template_specific_parameter_source(self) -> None:
        row = self.generated.items[0]
        candidate = replace(
            row.candidates[0], parameter_source="template:replace",
            uses_independent_parameters=True,
        )
        payload = replace(row, candidates=(candidate,)).as_dict()["candidates"][0]

        self.assertTrue(payload["parameter_provenance"]["non_uniform"])
        self.assertEqual(
            payload["parameter_provenance"]["reason"],
            "template_specific",
        )
        self.assertIsNotNone(payload["resolved_plan"])

    def test_empty_review_state_is_summarizable_but_empty_export_is_rejected(self) -> None:
        self.assertEqual(
            summarize_candidate_choices(()),
            {"candidate_count": 0, "work_item_count": 0, "groups": []},
        )
        with self.assertRaisesRegex(InputValidationError, "至少需要"):
            loads_selection_payload(
                json.dumps({"schema_version": 1, "selections": []})
            )

    def test_unknown_or_stale_candidate_is_rejected(self) -> None:
        choice = CandidateChoice("source001", "normal-s4", "candidate_deadbeef")
        with self.assertRaisesRegex(InputValidationError, "未能唯一复现"):
            resolve_export_selections(self.config, self.generated, (choice,))

    def test_failed_or_unknown_plan_is_rejected(self) -> None:
        candidate = self.generated.items[0].candidates[0].candidate
        choice = CandidateChoice("source001", "missing-plan", candidate.candidate_id)
        with self.assertRaisesRegex(InputValidationError, "不属于"):
            resolve_export_selections(self.config, self.generated, (choice,))

    def test_unqualified_difficulty_requires_confirmation_but_remains_exportable(self) -> None:
        original = self.generated.items[0].candidates[0]
        difficulty = dict(original.candidate.difficulty_simulation)
        difficulty["qualified"] = False
        difficulty["trend"] = {
            **dict(difficulty.get("trend", {})),
            "safety_qualified": False,
        }
        changed = replace(
            original,
            candidate=replace(
                original.candidate,
                difficulty_simulation=difficulty,
            ),
        )
        items = list(self.generated.items)
        items[0] = replace(
            items[0],
            candidates=(changed, *items[0].candidates[1:]),
        )
        generated = replace(self.generated, items=tuple(items))
        choice = CandidateChoice(
            "source001", "normal-s4", changed.candidate.candidate_id
        )
        with self.assertRaisesRegex(InputValidationError, "人工确认"):
            resolve_export_selections(self.config, generated, (choice,))
        resolved = resolve_export_selections(
            self.config,
            generated,
            (replace(choice, manual_evidence_override=True),),
        )
        self.assertEqual(len(resolved), 1)
        self.assertIn(
            "difficulty_target",
            resolved[0].manual_evidence_override["disproved_axes"],
        )
        self.assertIn(
            "duplicate_gradient_safety",
            resolved[0].manual_evidence_override["disproved_axes"],
        )

    def test_inconclusive_whole_board_evidence_requires_explicit_manual_confirmation(self) -> None:
        original = self.generated.items[0].candidates[0]
        experience = dict(original.candidate.experience_analysis)
        experience["hard_validation"] = {
            "status": "inconclusive",
            "passed": False,
            "reasons": ["尚未补齐整盘完成见证"],
        }
        changed = replace(
            original,
            candidate=replace(original.candidate, experience_analysis=experience),
        )
        item = replace(self.generated.items[0], candidates=(changed,))
        regenerated = replace(self.generated, items=(item,))
        choice = CandidateChoice(
            "source001", "normal-s4", changed.candidate.candidate_id
        )

        with self.assertRaisesRegex(InputValidationError, "人工确认") as caught:
            resolve_export_selections(self.config, regenerated, (choice,))

        self.assertEqual(
            caught.exception.detail.evidence["hard_validation_status"],
            "inconclusive",
        )
        confirmed = replace(choice, manual_evidence_override=True)
        resolved = resolve_export_selections(self.config, regenerated, (confirmed,))
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].manual_evidence_override["status"], "inconclusive")
        self.assertIn(
            "whole_board_completion",
            resolved[0].manual_evidence_override["inconclusive_axes"],
        )

    def test_synthetic_difficulty_evidence_requires_explicit_manual_confirmation(self) -> None:
        original = self.generated.items[0].candidates[0]
        difficulty = dict(original.candidate.difficulty_simulation)
        difficulty["evidence_coverage"] = {
            **dict(difficulty.get("evidence_coverage") or {}),
            "materialization_fidelity": "synthetic",
            "overall_status": "inconclusive",
        }
        changed = replace(
            original,
            candidate=replace(original.candidate, difficulty_simulation=difficulty),
        )
        regenerated = replace(
            self.generated,
            items=(replace(self.generated.items[0], candidates=(changed,)),),
        )
        choice = CandidateChoice(
            "source001", "normal-s4", changed.candidate.candidate_id
        )

        with self.assertRaisesRegex(InputValidationError, "人工确认"):
            resolve_export_selections(self.config, regenerated, (choice,))
        resolved = resolve_export_selections(
            self.config,
            regenerated,
            (replace(choice, manual_evidence_override=True),),
        )
        self.assertIn(
            "difficulty_evidence",
            resolved[0].manual_evidence_override["inconclusive_axes"],
        )

    def test_disproved_whole_board_evidence_requires_confirmation_but_remains_exportable(self) -> None:
        original = self.generated.items[0].candidates[0]
        experience = dict(original.candidate.experience_analysis)
        experience["hard_validation"] = {
            "status": "disproved",
            "passed": False,
            "reasons": ["存在结构与 A/B 循环死锁"],
        }
        changed = replace(
            original,
            candidate=replace(original.candidate, experience_analysis=experience),
            validation=replace(original.validation, passed=False),
        )
        item = replace(self.generated.items[0], candidates=(changed,))
        regenerated = replace(self.generated, items=(item,))
        choice = CandidateChoice(
            "source001", "normal-s4", changed.candidate.candidate_id
        )

        with self.assertRaisesRegex(InputValidationError, "人工确认") as caught:
            resolve_export_selections(self.config, regenerated, (choice,))

        self.assertEqual(
            caught.exception.detail.evidence["hard_validation_status"],
            "disproved",
        )
        resolved = resolve_export_selections(
            self.config,
            regenerated,
            (replace(choice, manual_evidence_override=True),),
        )
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].manual_evidence_override["status"], "disproved")
        self.assertIn(
            "whole_board_completion",
            resolved[0].manual_evidence_override["disproved_axes"],
        )
        self.assertIn(
            "candidate_rule_validation",
            resolved[0].manual_evidence_override["disproved_axes"],
        )

    def test_duplicate_json_fields_and_duplicate_choices_are_rejected(self) -> None:
        with self.assertRaisesRegex(InputValidationError, "重复字段"):
            loads_selection_payload(
                '{"schema_version":1,"schema_version":1,"selections":[]}'
            )
        candidate = self.generated.items[0].candidates[0].candidate.candidate_id
        record = {
            "template_key": "source001",
            "resolved_plan_id": "normal-s4",
            "candidate_id": candidate,
        }
        with self.assertRaisesRegex(InputValidationError, "重复导出"):
            loads_selection_payload(
                json.dumps({"schema_version": 1, "selections": [record, record]})
            )

    def test_review_metadata_types_are_strict(self) -> None:
        candidate = self.generated.items[0].candidates[0].candidate.candidate_id
        with self.assertRaisesRegex(InputValidationError, "线上关卡号"):
            loads_selection_payload(
                json.dumps(
                    {
                        "schema_version": 1,
                        "selections": [
                            {
                                "template_key": "source001",
                                "resolved_plan_id": "normal-s4",
                                "candidate_id": candidate,
                                "online_level_number": 1.5,
                            }
                        ],
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
