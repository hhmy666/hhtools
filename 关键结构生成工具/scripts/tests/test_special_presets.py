from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.errors import CandidateValidationError  # noqa: E402
from puzzle_core.geometry import GeometryGraph  # noqa: E402
from puzzle_core.models import (  # noqa: E402
    Layout,
    RawOutputPlan,
    RawPuzzleUnit,
    StepSpec,
    TowerMode,
)
from puzzle_core.planning import normalize_output_plan  # noqa: E402
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from puzzle_core.search import (  # noqa: E402
    GenerationOptions,
    QualityOptions,
    UnitGenerationOptions,
    evaluate_candidate_experience,
    search_candidates,
)
from puzzle_core.special_presets import (  # noqa: E402
    SPECIAL_GRAMMAR_VERSION,
    all_types_wave_contract,
    analyze_long_range_assignment,
    find_long_range_assignment,
    serial_mechanism_contract,
)
from puzzle_core.solver import SolverLimits, analyze_puzzle  # noqa: E402
from puzzle_core.template_reader import parse_level_bytes  # noqa: E402
from puzzle_core.validator import validate_candidate  # noqa: E402


def _chain_masks(count: int) -> tuple[int, ...]:
    return tuple(0 if index == 0 else 1 << (index - 1) for index in range(count))


def _chain_graph(count: int) -> GeometryGraph:
    direct = tuple(
        frozenset() if index == 0 else frozenset({index - 1})
        for index in range(count)
    )
    transitive = tuple(frozenset(range(index)) for index in range(count))
    blocked = []
    for index in range(count):
        blocked.append(
            frozenset({index + 1}) if index + 1 < count else frozenset()
        )
    return GeometryGraph(
        direct_blockers=direct,
        transitive_blockers=transitive,
        direct_blocked_tiles=tuple(blocked),
        relations=(),
        included_indices=frozenset(range(count)),
    )


def _varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def _overlapping_chain_level(count: int, template_id: str):
    """Build a strict top-to-bottom chain without adjacent full covers."""

    payload = [
        _bytes_field(2, template_id.encode("utf-8")),
        _varint_field(4, 4),
        _varint_field(5, 4),
    ]
    for index in range(count):
        tile = b"".join(
            (
                _varint_field(1, index % 2),
                _varint_field(2, 0),
                _varint_field(3, count - 1 - index),
                _varint_field(4, -10 if index % 2 == 0 else -11),
            )
        )
        payload.append(_bytes_field(6, tile))
    return parse_level_bytes(b"".join(payload), source_name=f"{template_id}_m.bytes")


class AllTypesLongRangeGoldenTests(unittest.TestCase):
    def test_positive_three_and_four_wave_goldens_have_auditable_contracts(self) -> None:
        limits = SolverLimits(max_states=100_000)
        for wave_count in (3, 4):
            with self.subTest(wave_count=wave_count):
                generated = find_long_range_assignment(
                    _chain_masks(36),
                    tuple("CDEFGHIJKLMN"),
                    target_step=5,
                    rng=random.Random(7 + wave_count),
                    assignment_attempts=600,
                    limits=limits,
                    preferred_wave_count=wave_count,
                )

                self.assertIsNotNone(generated)
                assert generated is not None
                labels, gate, generated_proof, generated_contract = generated
                proof, contract = analyze_long_range_assignment(
                    _chain_masks(36),
                    labels,
                    threshold_type=gate,
                    target_step=5,
                    limits=limits,
                    preferred_wave_count=wave_count,
                )

                self.assertEqual(proof, generated_proof)
                self.assertEqual(contract, generated_contract)
                self.assertEqual(
                    contract["grammar_version"], SPECIAL_GRAMMAR_VERSION
                )
                self.assertEqual(contract["solver_status"], "proved")
                self.assertEqual(contract["wave_count"], wave_count)
                self.assertEqual(
                    contract["wave_schema_id"],
                    f"all-types-long-range-{wave_count}-wave-v1",
                )
                self.assertEqual(len(contract["wave_assignment"]), wave_count)
                self.assertEqual(
                    len(contract["wave_dependency_dag"]), wave_count - 1
                )
                self.assertEqual(
                    contract["entry_event"]["id"],
                    "wave_first_triplet_completed",
                )
                self.assertEqual(
                    contract["completion_event"]["id"],
                    "wave_all_structures_completed",
                )
                self.assertTrue(contract["dominance_chain"])
                self.assertEqual(contract["shortcut_count"], 0)
                self.assertTrue(contract["capacity_safe_exits"])
                self.assertEqual(
                    contract["suspended_exposure_bound"]["status"],
                    "not_proved",
                )

    def test_unproved_suspended_exposure_keeps_composite_hard_validation_inconclusive(self) -> None:
        level = _overlapping_chain_level(36, "longrange-hard-gate")
        generated = find_long_range_assignment(
            _chain_masks(36),
            tuple("CDEFGHIJKLMN"),
            target_step=5,
            rng=random.Random(17),
            assignment_attempts=600,
            limits=SolverLimits(max_states=100_000),
            preferred_wave_count=3,
        )
        self.assertIsNotNone(generated)
        assert generated is not None
        labels, gate, proof, contract = generated
        unit = SimpleNamespace(
            unit_id="long-range",
            role="long-range",
            target_step=5,
            intrinsic_target_step=5,
            threshold_type=gate,
            selected_indices=tuple(range(36)),
            assigned_types=labels,
            blocker_masks=_chain_masks(36),
            proof=proof,
        )
        candidate = SimpleNamespace(
            units=(unit,),
            replacements=tuple(
                SimpleNamespace(tile_index=index, structure_type=label)
                for index, label in enumerate(labels)
            ),
        )
        report = evaluate_candidate_experience(
            level,
            candidate,
            QualityOptions(
                special_preset="all_types_long_range",
                long_range_wave_count="3",
                online_style_reference="off",
            ),
            {
                "duplicates": [],
                "evidence_coverage": {
                    "completion_witness_status": "proved",
                    "missing_completion_witness_duplicate_indices": [],
                },
            },
        )

        self.assertEqual(contract["solver_status"], "proved")
        self.assertEqual(
            contract["suspended_exposure_bound"]["status"], "not_proved"
        )
        self.assertEqual(report["special_preset"]["status"], "inconclusive")
        self.assertEqual(report["hard_validation"]["status"], "inconclusive")
        self.assertFalse(report["hard_validation"]["passed"])
        self.assertIn(
            "suspended_exposure_bound_not_proved",
            report["hard_validation"]["reasons"],
        )

    def test_negative_three_and_four_wave_goldens_reject_shortcuts(self) -> None:
        for wave_count in (3, 4):
            with self.subTest(wave_count=wave_count):
                generated = find_long_range_assignment(
                    _chain_masks(36),
                    tuple("CDEFGHIJKLMN"),
                    target_step=5,
                    rng=random.Random(9 + wave_count),
                    assignment_attempts=600,
                    limits=SolverLimits(max_states=100_000),
                    preferred_wave_count=wave_count,
                )
                assert generated is not None
                labels, _gate, proof, _contract = generated

                negative = all_types_wave_contract(
                    (0,) * 36,
                    labels,
                    proof,
                    preferred_wave_count=wave_count,
                )

                self.assertEqual(negative["solver_status"], "disproved")
                self.assertFalse(negative["dominance_chain"])
                self.assertGreater(negative["shortcut_count"], 0)
                self.assertIn(
                    "wave_completion_does_not_dominate_later_triplet",
                    negative["reasons"],
                )


class SerialMechanismGoldenTests(unittest.TestCase):
    @staticmethod
    def _unit(
        unit_id: str,
        role: str,
        selected: tuple[int, ...],
        labels: tuple[str, ...],
    ) -> SimpleNamespace:
        proof = analyze_puzzle(
            _chain_masks(len(selected)),
            labels,
            threshold_type=labels[0],
            limits=SolverLimits(max_states=100_000),
        )
        return SimpleNamespace(
            unit_id=unit_id,
            role=role,
            selected_indices=selected,
            assigned_types=labels,
            proof=proof,
        )

    def test_positive_serial_golden_proves_no_shortcut_or_reverse_edge(self) -> None:
        high = self._unit(
            "high", "high", tuple(range(9)), ("C",) * 3 + ("D",) * 3 + ("E",) * 3
        )
        low = self._unit(
            "low", "low", tuple(range(9, 18)), ("F",) * 3 + ("G",) * 3 + ("H",) * 3
        )

        contract = serial_mechanism_contract(_chain_graph(18), (high, low))

        self.assertEqual(contract["solver_status"], "proved")
        self.assertTrue(contract["dominance_chain"])
        self.assertEqual(contract["shortcut_count"], 0)
        self.assertFalse(contract["reverse_dependency"])
        self.assertTrue(contract["capacity_safe_segments"])
        self.assertEqual(contract["cluster_order"], ["high", "low"])

    def test_negative_serial_golden_exposes_shortcut(self) -> None:
        high = self._unit(
            "high", "high", tuple(range(9)), ("C",) * 3 + ("D",) * 3 + ("E",) * 3
        )
        low = self._unit(
            "low", "low", tuple(range(9, 18)), ("F",) * 3 + ("G",) * 3 + ("H",) * 3
        )
        graph = _chain_graph(18)
        transitive = list(graph.transitive_blockers)
        # Low tiles only depend on one high member; every low type can complete
        # while other high members remain, which is a concrete shortcut.
        for index in range(9, 18):
            transitive[index] = frozenset({8})
        graph = GeometryGraph(
            direct_blockers=graph.direct_blockers,
            transitive_blockers=tuple(transitive),
            direct_blocked_tiles=graph.direct_blocked_tiles,
            relations=(),
            included_indices=graph.included_indices,
        )

        contract = serial_mechanism_contract(graph, (high, low))

        self.assertEqual(contract["solver_status"], "disproved")
        self.assertFalse(contract["dominance_chain"])
        self.assertEqual(set(contract["shortcut_types"]), {"F", "G", "H"})


class SpecialPresetSearchValidatorIntegrationTests(unittest.TestCase):
    def test_rejected_special_preset_is_kept_for_manual_review(self) -> None:
        level = _overlapping_chain_level(18, "serialrejected")
        plan = normalize_output_plan(
            RawOutputPlan(
                plan_id="serial-s5-s5-rejected",
                units=(
                    RawPuzzleUnit(StepSpec.exact(5), "high"),
                    RawPuzzleUnit(StepSpec.exact(5), "low"),
                ),
                layout=Layout.Z,
                tower_mode=TowerMode.DISALLOW,
            ),
            batch_seed=9,
            template_key=level.template_id,
        )[0]
        options = GenerationOptions(
            unit_options=(
                UnitGenerationOptions(
                    type_count=3,
                    preferred_coords=tuple(
                        (tile.x, tile.y, tile.z) for tile in level.tiles[:9]
                    ),
                ),
                UnitGenerationOptions(
                    type_count=3,
                    preferred_coords=tuple(
                        (tile.x, tile.y, tile.z) for tile in level.tiles[9:]
                    ),
                ),
            ),
            replacement_sources=("A", "B"),
            quality=QualityOptions(
                special_preset="serial_mechanism",
                online_style_reference="off",
            ),
            candidate_count=1,
            attempt_budget=2,
            assignment_attempts=600,
            solver_limits=SolverLimits(max_states=100_000),
        )

        def rejected_experience(*args, **kwargs):
            report = evaluate_candidate_experience(*args, **kwargs)
            report = dict(report)
            special = dict(report["special_preset"])
            special.update(
                {
                    "status": "rejected",
                    "reasons": ["fixture_forces_special_preset_rejection"],
                }
            )
            report["special_preset"] = special
            hard = dict(report["hard_validation"])
            hard.update(
                {
                    "status": "disproved",
                    "passed": False,
                    "reasons": ["fixture_forces_special_preset_rejection"],
                    "static_rule_status": "disproved",
                }
            )
            report["hard_validation"] = hard
            coverage = dict(report["evidence_coverage"])
            coverage["overall"] = "reject"
            report["evidence_coverage"] = coverage
            return report

        with patch(
            "puzzle_core.search.evaluate_candidate_experience",
            side_effect=rejected_experience,
        ):
            searched = search_candidates(
                level, plan, options, candidate_seed=903
            )
        candidate = searched.candidates[0]

        self.assertEqual(
            candidate.experience_analysis["hard_validation"]["status"],
            "disproved",
        )
        self.assertNotIn("special_preset_proof", searched.rejection_counts)
        self.assertGreaterEqual(
            searched.rejection_counts["rule_warning_special_preset_proof"],
            1,
        )
        with patch(
            "puzzle_core.validator.simulate_candidate_difficulty",
            return_value=candidate.difficulty_simulation,
        ), patch(
            "puzzle_core.validator.evaluate_candidate_experience",
            side_effect=rejected_experience,
        ), self.assertRaises(CandidateValidationError):
            validate_candidate(level, plan, options, candidate)
        with patch(
            "puzzle_core.validator.simulate_candidate_difficulty",
            return_value=candidate.difficulty_simulation,
        ), patch(
            "puzzle_core.validator.evaluate_candidate_experience",
            side_effect=rejected_experience,
        ):
            report = validate_candidate(
                level,
                plan,
                options,
                candidate,
                require_exportable_experience=False,
            )

        self.assertFalse(report.passed)
        self.assertTrue(report.technical_integrity_passed)
        self.assertTrue(report.manual_export_eligible)
        self.assertFalse(report.auto_recommendation_eligible)
        self.assertIn(
            "experience_analysis",
            {check.code for check in report.rule_failures},
        )

    def test_all_types_long_range_three_and_four_wave_fail_closed_at_strict_validation_without_suspended_bound_proof(self) -> None:
        level = _overlapping_chain_level(36, "longrangee2e")
        plan = normalize_output_plan(
            RawOutputPlan(
                plan_id="long-range-s5",
                units=(RawPuzzleUnit(StepSpec.exact(5), "long-range"),),
                layout=Layout.SINGLE,
                tower_mode=TowerMode.DISALLOW,
            ),
            batch_seed=7,
            template_key=level.template_id,
        )[0]
        for wave_count in (3, 4):
            with self.subTest(wave_count=wave_count):
                options = GenerationOptions(
                    unit_options=(
                        UnitGenerationOptions(
                            type_count=12,
                            groups_per_type=(1,) * 12,
                            allowed_types=tuple("CDEFGHIJKLMN"),
                            preferred_coords=tuple(
                                (tile.x, tile.y, tile.z)
                                for tile in level.tiles
                            ),
                        ),
                    ),
                    replacement_sources=("A", "B"),
                    allowed_types=tuple("CDEFGHIJKLMN"),
                    quality=QualityOptions(
                        special_preset="all_types_long_range",
                        long_range_wave_count=str(wave_count),
                        online_style_reference="off",
                    ),
                    candidate_count=1,
                    attempt_budget=2,
                    assignment_attempts=600,
                    solver_limits=SolverLimits(max_states=100_000),
                )

                candidate = search_candidates(
                    level, plan, options, candidate_seed=701 + wave_count
                ).candidates[0]
                evidence = candidate.experience_analysis["special_preset"]

                self.assertEqual(evidence["status"], "inconclusive")
                self.assertEqual(evidence["wave_count"], wave_count)
                self.assertEqual(
                    evidence["wave_schema_id"],
                    f"all-types-long-range-{wave_count}-wave-v1",
                )
                self.assertEqual(evidence["shortcut_count"], 0)
                self.assertTrue(evidence["capacity_safe_exits"])
                self.assertEqual(
                    evidence["suspended_exposure_bound"]["status"],
                    "not_proved",
                )
                self.assertEqual(
                    candidate.experience_analysis["hard_validation"]["status"],
                    "inconclusive",
                )
                with patch(
                    "puzzle_core.validator.simulate_candidate_difficulty",
                    return_value=candidate.difficulty_simulation,
                ), self.assertRaises(CandidateValidationError) as caught:
                    validate_candidate(level, plan, options, candidate)
                self.assertIn(
                    "整板体验证据不足", str(caught.exception)
                )
                with patch(
                    "puzzle_core.validator.simulate_candidate_difficulty",
                    return_value=candidate.difficulty_simulation,
                ):
                    review_report = validate_candidate(
                        level,
                        plan,
                        options,
                        candidate,
                        require_exportable_experience=False,
                    )
                self.assertTrue(review_report.passed)
                experience_check = next(
                    check
                    for check in review_report.checks
                    if check.code == "experience_analysis"
                )
                self.assertFalse(
                    experience_check.evidence["auto_recommendation_eligible"]
                )
                self.assertIn("用户确认提示后仍可导出", experience_check.message)

    def test_serial_mechanism_search_to_validator(self) -> None:
        level = _overlapping_chain_level(18, "seriale2e")
        plan = normalize_output_plan(
            RawOutputPlan(
                plan_id="serial-s5-s5",
                units=(
                    RawPuzzleUnit(StepSpec.exact(5), "high"),
                    RawPuzzleUnit(StepSpec.exact(5), "low"),
                ),
                layout=Layout.Z,
                tower_mode=TowerMode.DISALLOW,
            ),
            batch_seed=9,
            template_key=level.template_id,
        )[0]
        options = GenerationOptions(
            unit_options=(
                UnitGenerationOptions(
                    type_count=3,
                    preferred_coords=tuple(
                        (tile.x, tile.y, tile.z) for tile in level.tiles[:9]
                    ),
                ),
                UnitGenerationOptions(
                    type_count=3,
                    preferred_coords=tuple(
                        (tile.x, tile.y, tile.z) for tile in level.tiles[9:]
                    ),
                ),
            ),
            replacement_sources=("A", "B"),
            quality=QualityOptions(
                special_preset="serial_mechanism",
                online_style_reference="off",
            ),
            candidate_count=1,
            attempt_budget=2,
            assignment_attempts=600,
            solver_limits=SolverLimits(max_states=100_000),
        )

        candidate = search_candidates(
            level, plan, options, candidate_seed=902
        ).candidates[0]
        # The fixture has no exact DupPara1--10 profile, so this assertion is
        # scoped to the serial-mechanism recognizer/generator/validator path.
        # Formal export remains covered by the strict selection/export tests.
        report = validate_candidate(
            level,
            plan,
            options,
            candidate,
            require_exportable_experience=False,
        )
        evidence = candidate.experience_analysis["special_preset"]

        self.assertTrue(report.passed)
        self.assertEqual(evidence["status"], "matched")
        self.assertEqual(evidence["cluster_order"], ["high", "low"])
        self.assertEqual(evidence["shortcut_count"], 0)
        self.assertFalse(evidence["reverse_dependency"])
        self.assertTrue(evidence["capacity_safe_segments"])


if __name__ == "__main__":
    unittest.main()
