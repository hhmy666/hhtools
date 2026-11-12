from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.errors import CandidateValidationError, NoCandidateError  # noqa: E402
from puzzle_core.difficulty import DifficultyProfile, DuplicateParameter  # noqa: E402
from puzzle_core.models import (  # noqa: E402
    RawOutputPlan,
    RawPuzzleUnit,
    StepSpec,
    TowerMode,
)
from puzzle_core.planning import normalize_output_plan  # noqa: E402
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from puzzle_core.search import (  # noqa: E402
    canonical_structure_identity,
    ARCHETYPE_MATCH_POLICY_VERSION,
    GenerationOptions,
    MAJORITY_A_ON_TIE,
    PREFER_A_ELSE_B,
    QualityOptions,
    UnitGenerationOptions,
    _base_eligible,
    _archetype_match_evidence,
    _effective_count_limit,
    _effective_max_layer,
    _experience_dynamic_inputs,
    _experience_vector,
    generation_options_hash,
    effective_structure_count_minimum,
    _quality_score,
    _robust_standardize,
    _restricted_masks,
    _select_experience_diverse,
    _validate_options,
    search_candidates,
    structure_layer_span,
)
from puzzle_core.geometry import build_static_geometry  # noqa: E402
from puzzle_core.template_reader import parse_level_bytes  # noqa: E402
from puzzle_core.topology import analyze_topology as core_analyze_topology  # noqa: E402
from puzzle_core.validator import (  # noqa: E402
    _effective_count_minimum as _validator_effective_count_minimum,
    _eligible_source_pool as _validator_eligible_source_pool,
    validate_candidate,
)


def _varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def _chain_level(*, all_a: bool = False):
    payload = [_bytes_field(2, b"chain001"), _varint_field(4, 15), _varint_field(5, 4)]
    for index in range(12):
        tile = b"".join(
            (
                _varint_field(1, index),
                _varint_field(2, 1),
                _varint_field(3, index),
                _varint_field(
                    4, -10 if all_a or index % 2 == 0 else -11
                ),
            )
        )
        payload.append(_bytes_field(6, tile))
    return parse_level_bytes(b"".join(payload), source_name="chain001_m.bytes")


def _tower_level():
    payload = [_bytes_field(2, b"towerfixture"), _varint_field(4, 8), _varint_field(5, 4)]
    for x in (0, 3):
        for z in range(6):
            tile = b"".join(
                (
                    _varint_field(1, x),
                    _varint_field(2, 0),
                    _varint_field(3, z),
                    _varint_field(4, -10),
                )
            )
            payload.append(_bytes_field(6, tile))
    for x in (0, 3):
        tower = b"".join(
            (_varint_field(1, x), _varint_field(2, 0), _varint_field(3, 0))
        )
        payload.append(_bytes_field(7, tower))
    return parse_level_bytes(b"".join(payload), source_name="towerfixture_m.bytes")


def _full_cover_rule_level():
    payload = [_bytes_field(2, b"coverfixture"), _varint_field(4, 12), _varint_field(5, 4)]
    coords = (
        (0, 0, 0),
        (0, 0, 1),  # adjacent full-cover pair: both endpoints forbidden
        (3, 0, 1),
        (3, 0, 2),  # another adjacent pair, viewed from either direction
        (6, 0, 0),
        (6, 0, 2),  # same projection with a two-layer gap: both remain eligible
        (9, 0, 0),
    )
    for x, y, z in coords:
        tile = b"".join(
            (
                _varint_field(1, x),
                _varint_field(2, y),
                _varint_field(3, z),
                _varint_field(4, -10),
            )
        )
        payload.append(_bytes_field(6, tile))
    return parse_level_bytes(b"".join(payload), source_name="coverfixture_m.bytes")


def _mixed_tower_level():
    payload = [
        _bytes_field(2, b"mixedtower"),
        _varint_field(4, 128),
        _varint_field(5, 4),
    ]
    # Six selectable non-tower Tiles span effective layers 1..6.
    for index in range(6):
        tile = b"".join(
            (
                _varint_field(1, index * 3),
                _varint_field(2, 0),
                _varint_field(3, index),
                _varint_field(4, -10 if index % 2 == 0 else -11),
            )
        )
        payload.append(_bytes_field(6, tile))
    # Four high Tiles belong to one explicit tower and extend max layer to 10.
    for z in range(6, 10):
        tile = b"".join(
            (
                _varint_field(1, 100),
                _varint_field(2, 0),
                _varint_field(3, z),
                _varint_field(4, -11),
            )
        )
        payload.append(_bytes_field(6, tile))
    payload.append(
        _bytes_field(
            7,
            b"".join(
                (
                    _varint_field(1, 100),
                    _varint_field(2, 0),
                    _varint_field(3, 0),
                )
            ),
        )
    )
    return parse_level_bytes(b"".join(payload), source_name="mixedtower_m.bytes")


def _source_priority_level(tile_ids=(-10, -11, -11), z_values=None):
    payload = [
        _bytes_field(2, b"sourcepriority"),
        _varint_field(4, 12),
        _varint_field(5, 4),
    ]
    z_values = tuple(0 for _ in tile_ids) if z_values is None else tuple(z_values)
    for index, (tile_id, z) in enumerate(zip(tile_ids, z_values)):
        payload.append(
            _bytes_field(
                6,
                b"".join(
                    (
                        _varint_field(1, index * 3),
                        _varint_field(2, 0),
                        _varint_field(3, z),
                        _varint_field(4, tile_id),
                    )
                ),
            )
        )
    return parse_level_bytes(
        b"".join(payload), source_name="sourcepriority_m.bytes"
    )


def _source_structure_level():
    payload = [
        _bytes_field(2, b"sourcecn"),
        _varint_field(4, 18),
        _varint_field(5, 6),
    ]
    for column, tile_id in enumerate((-12, -13, -14)):
        for z in (0, 2, 4):
            payload.append(
                _bytes_field(
                    6,
                    b"".join(
                        (
                            _varint_field(1, column * 3),
                            _varint_field(2, 0),
                            _varint_field(3, z),
                            _varint_field(4, tile_id),
                        )
                    ),
                )
            )
    return parse_level_bytes(
        b"".join(payload), source_name="sourcecn_m.bytes"
    )


def _quality_risk_level():
    payload = [
        _bytes_field(2, b"qualityrisk"),
        _varint_field(4, 40),
        _varint_field(5, 8),
    ]
    # Six columns, with two selected structure positions at Z=1 and Z=3.
    # One untouched A below each column supplies deterministic A/B nesting.
    for column in range(6):
        x = column * 3
        for z, tile_id in ((1, -10), (3, -11), (0, -10)):
            payload.append(
                _bytes_field(
                    6,
                    b"".join(
                        (
                            _varint_field(1, x),
                            _varint_field(2, 0),
                            _varint_field(3, z),
                            _varint_field(4, tile_id),
                        )
                    ),
                )
            )
    return parse_level_bytes(b"".join(payload), source_name="qualityrisk_m.bytes")


def _plan_and_options():
    plan = normalize_output_plan(
        RawOutputPlan(
            plan_id="s4",
            units=(RawPuzzleUnit(StepSpec.exact(4), "U1"),),
            tower_mode=TowerMode.DISALLOW,
        ),
        batch_seed=7,
        template_key="chain001",
    )[0]
    options = GenerationOptions(
        unit_options=(UnitGenerationOptions(type_count=3),),
        # These broad search/validator fixtures intentionally exercise both
        # source types.  Keep that scope explicit now that the product default
        # is the original-template majority rule.
        replacement_sources=("A", "B"),
        attempt_budget=30,
        assignment_attempts=500,
    )
    return plan, options


class SearchAndValidationTests(unittest.TestCase):
    def test_same_type_span_rejects_impossible_geometry_before_proof(self):
        import random
        from collections import Counter
        from puzzle_core.search import _find_type_assignment
        from puzzle_core.solver import SolverLimits
        diagnostics = Counter()
        with patch("puzzle_core.search.analyze_puzzle", side_effect=AssertionError("proof must not run")):
            result = _find_type_assignment(
                (0, 1, 2), ("C",), (1,), 3, None, random.Random(1), 10,
                SolverLimits(), tile_layers=(1, 1, 2), min_same_type_layer_span=2,
                assignment_diagnostics=diagnostics,
            )
        self.assertIsNone(result)
        self.assertGreater(diagnostics["same_type_layer_span"], 0)

    def test_same_type_span_is_enforced_independently_for_each_new_type(self):
        level = _chain_level()
        plan, options = _plan_and_options()
        candidate = search_candidates(level, plan, options, candidate_seed=2026).candidates[0]
        # Seed fixture spans E=9, D=6, J=6, while all new tiles span 10.
        bounded = replace(options, min_same_type_layer_span=7)
        with self.assertRaises(CandidateValidationError) as caught:
            validate_candidate(level, plan, bounded, replace(candidate, generation_config_hash=generation_options_hash(bounded)), require_exportable_experience=False)
        self.assertEqual(caught.exception.detail.evidence["check"], "same_type_layer_span")
        with self.assertRaises(NoCandidateError):
            search_candidates(level, plan, replace(options, min_same_type_layer_span=20), candidate_seed=2026)

    def test_same_type_span_prunes_narrow_type_even_when_global_span_is_wide(self):
        import random
        from puzzle_core.search import _find_type_assignment
        from puzzle_core.solver import SolverLimits

        class FixedOrderRandom(random.Random):
            def shuffle(self, values):
                pass

            def randrange(self, *args):
                return 0

        with patch("puzzle_core.search.simulate_order", side_effect=AssertionError("narrow type reached full sequence simulation")), patch("puzzle_core.search.analyze_puzzle", side_effect=AssertionError("narrow type reached proof")):
            found = _find_type_assignment(
                (0, 1, 2, 4, 8, 16), ("C", "D"), (1, 1), 3, "C",
                FixedOrderRandom(1), 1, SolverLimits(),
                tile_layers=(1, 1, 1, 3, 3, 3), min_same_type_layer_span=2,
            )
        self.assertIsNone(found)

    def test_same_type_span_accepts_exact_boundary_and_checks_conflicts(self):
        import random
        from puzzle_core.search import _find_type_assignment
        from puzzle_core.solver import SolverLimits
        from puzzle_core.errors import InputValidationError
        found = _find_type_assignment((0, 1, 2), ("C",), (1,), 3, None, random.Random(1), 10, SolverLimits(), tile_layers=(1, 2, 3), min_same_type_layer_span=2)
        self.assertIsNotNone(found)
        plan, options = _plan_and_options()
        with self.assertRaises(InputValidationError):
            _validate_options(plan, replace(options, min_same_type_layer_span=2, max_structure_layer_span=1))

    def test_five_type_compressed_assignment_is_not_rejected_by_raw_state_count(self):
        import random
        from puzzle_core.search import _find_type_assignment
        from puzzle_core.solver import SolverLimits, proof_state_budget_exceeded
        # Five fully separated waves: only one type is exposed at a time.
        # Raw tile subsets exceed the budget, but residue proof stays small.
        masks = tuple(0 if i < 18 else ((1 << 18) - 1) << ((i // 18 - 1) * 18)
                      for i in range(90))
        limits = SolverLimits()
        self.assertTrue(proof_state_budget_exceeded(masks, limits.max_states))
        found = _find_type_assignment(masks, tuple('CDEFG'), (6,) * 5, 3,
                                     'C', random.Random(77), 1, limits)
        self.assertIsNotNone(found)
        self.assertTrue(found[2].singleton_rule_ok)
        self.assertTrue(all(p.complete_exists for p in found[2].capacity_proofs))

    def test_large_five_type_assignment_constructs_gate_before_random_search(self):
        import random
        from puzzle_core.search import _find_type_assignment
        from puzzle_core.solver import SolverLimits, validate_target
        masks = tuple(0 if i == 0 else 1 << (i - 1) for i in range(30))
        found = _find_type_assignment(masks, tuple('CDEFG'), (2, 2, 2, 2, 2), 5,
                                      'C', random.Random(77), 1, SolverLimits())
        self.assertIsNotNone(found)
        self.assertTrue(validate_target(found[2], 5)[0])

    def test_group_budget_uses_legal_source_positions(self):
        from puzzle_core.search import feasible_structure_count_limit
        level = _chain_level()
        plan, options = _plan_and_options()
        options = replace(options, replacement_sources=MAJORITY_A_ON_TIE,
                          total_percent_limit=100, total_count_limit=None)
        self.assertEqual(feasible_structure_count_limit(level, options, plan.tower_mode), 6)
        with self.assertRaises(NoCandidateError) as raised:
            feasible_structure_count_limit(level, replace(options, total_count_minimum=7), plan.tower_mode)
        self.assertEqual(raised.exception.detail.evidence['available'], 6)
        self.assertNotIn('attempt_budget', raised.exception.detail.evidence)

    def test_assignment_prefilter_does_not_resolve_label_renamings_twice(self):
        import random
        from puzzle_core.search import _find_type_assignment
        from puzzle_core.solver import SolverLimits
        calls = []
        def reject(masks, assigned, *, threshold_type, **kwargs):
            names = {}
            normalized = tuple(names.setdefault(label, len(names)) for label in assigned)
            calls.append((normalized, names[threshold_type]))
            return 3
        with patch("puzzle_core.search.minimum_gate_structure_step", side_effect=reject):
            result = _find_type_assignment((0,) * 9, ("C", "D", "E"), (1,1,1), 5, None, random.Random(12), 2000, SolverLimits())
        self.assertIsNone(result)
        self.assertGreater(len(calls), 0)
        self.assertEqual(len(calls), len(set(calls)))

    def test_structure_identity_ignores_global_renaming_but_preserves_partition(self):
        import json
        def signature(labels):
            return json.dumps([{"group":"U1","tiles":[[i,0,0,label] for i,label in enumerate(labels)]}])
        original = signature("CCCDDDEEE")
        renamed = signature("FFFHHHGGG")
        regrouped = signature("CDCCDD EEE".replace(" ", ""))
        self.assertEqual(canonical_structure_identity(original), canonical_structure_identity(renamed))
        self.assertNotEqual(canonical_structure_identity(original), canonical_structure_identity(regrouped))
        self.assertEqual(canonical_structure_identity("legacy-opaque-id"), "legacy-opaque-id")

    def test_percent_minimum_uses_cap_population_and_rounds_up(self):
        level = _mixed_tower_level()
        options = GenerationOptions(total_percent_minimum=35)
        self.assertEqual(effective_structure_count_minimum(level, options, TowerMode.DISALLOW), 3)
        self.assertEqual(effective_structure_count_minimum(level, options, TowerMode.ALLOW), 4)
        self.assertEqual(effective_structure_count_minimum(level, options, TowerMode.ONLY), 4)
        self.assertEqual(_validator_effective_count_minimum(level, options, TowerMode.DISALLOW), 3)
        self.assertEqual(_validator_effective_count_minimum(level, options, TowerMode.ALLOW), 4)
        _, legacy = _plan_and_options()
        self.assertEqual(generation_options_hash(replace(legacy, total_percent_minimum=0)), generation_options_hash(legacy))

    def test_percent_minimum_is_independently_enforced_and_reported(self):
        level = _chain_level()
        plan, original = _plan_and_options()
        accepted = replace(original, total_percent_minimum=75)
        candidate = search_candidates(level, plan, accepted, candidate_seed=2026).candidates[0]
        report = validate_candidate(level, plan, accepted, candidate, require_exportable_experience=False)
        self.assertEqual(report.effective_constraints.configured_percent_minimum, 75)
        self.assertEqual(report.effective_constraints.effective_structure_count_minimum, 9)
        rejected = replace(original, total_percent_minimum=76)
        with self.assertRaisesRegex(NoCandidateError, "下限"):
            search_candidates(level, plan, rejected, candidate_seed=2026)
        with self.assertRaises(CandidateValidationError) as caught:
            validate_candidate(level, plan, rejected, replace(candidate, generation_config_hash=generation_options_hash(rejected)), require_exportable_experience=False)
        self.assertEqual(caught.exception.detail.evidence["check"], "count_minimum")
        rounded_conflict = replace(original, total_percent_minimum=25, total_percent_limit=25)
        with self.assertRaisesRegex(NoCandidateError, "下限超过"):
            search_candidates(_mixed_tower_level(), plan, rounded_conflict, candidate_seed=2026)

    def test_structure_minimum_is_enforced_by_search_and_independent_validator(self):
        level = _chain_level()
        plan, options = _plan_and_options()
        candidate = search_candidates(level, plan, options, candidate_seed=2026).candidates[0]
        bounded = replace(options, total_count_minimum=10)
        with self.assertRaisesRegex(NoCandidateError, "下限"):
            search_candidates(level, plan, bounded, candidate_seed=2026)
        rewritten_identity = replace(candidate, generation_config_hash=generation_options_hash(bounded))
        with self.assertRaises(CandidateValidationError) as caught:
            validate_candidate(level, plan, bounded, rewritten_identity, require_exportable_experience=False)
        self.assertEqual(caught.exception.detail.evidence["check"], "count_minimum")
        accepted = replace(options, total_count_minimum=9)
        report = validate_candidate(
            level, plan, accepted,
            search_candidates(level, plan, accepted, candidate_seed=2026).candidates[0],
            require_exportable_experience=False,
        )
        self.assertTrue(report.effective_constraints.count_minimum_passed)
        self.assertEqual(report.effective_constraints.configured_count_minimum, 9)

    @staticmethod
    def _complete_difficulty_profile(
        duppara: str = "7|0|100|4",
    ) -> DifficultyProfile:
        return DifficultyProfile(
            duplicate_params=tuple(
                DuplicateParameter.parse(duppara) for _ in range(10)
            ),
            coverage="complete",
        )

    def test_precision_mode_incomplete_profile_skips_all_candidate_simulations(self) -> None:
        from puzzle_core import search as search_module

        level = _tower_level()
        plan = normalize_output_plan(
            RawOutputPlan(plan_id="tower", units=(), tower_mode=TowerMode.ONLY),
            batch_seed=3,
            template_key="towerfixture",
        )[0]
        options = GenerationOptions(
            unit_options=(),
            tower_options=UnitGenerationOptions(type_count=3),
            precision_mode=True,
            candidate_count=1,
            attempt_budget=100,
            assignment_attempts=1,
        )
        original_simulate = search_module.simulate_candidate_difficulty
        candidate_simulations = 0
        phases: list[str] = []

        def count_candidate_simulations(*args, **kwargs):
            nonlocal candidate_simulations
            if args[1]:
                candidate_simulations += 1
            return original_simulate(*args, **kwargs)

        with patch(
            "puzzle_core.search.simulate_candidate_difficulty",
            side_effect=count_candidate_simulations,
        ):
            with self.assertRaises(NoCandidateError) as caught:
                search_candidates(
                    level, plan, options, candidate_seed=77,
                    cooperative_checkpoint=phases.append,
                )

        self.assertEqual(candidate_simulations, 0)
        self.assertNotIn("search_attempt", phases)
        self.assertNotIn("attempt_budget", caught.exception.detail.evidence)
        self.assertEqual(
            caught.exception.detail.evidence["rejection_counts"]["precision_profile_coverage"],
            1,
        )

    def test_precision_mode_parameter_underflow_skips_candidate_simulations(self) -> None:
        from puzzle_core import search as search_module

        level = _chain_level()
        plan, options = _plan_and_options()
        precise = replace(options, precision_mode=True, attempt_budget=3)
        original_simulate = search_module.simulate_candidate_difficulty
        candidate_simulations = 0

        def count_candidate_simulations(*args, **kwargs):
            nonlocal candidate_simulations
            if args[1]:
                candidate_simulations += 1
            return original_simulate(*args, **kwargs)

        with patch(
            "puzzle_core.search.simulate_candidate_difficulty",
            side_effect=count_candidate_simulations,
        ):
            with self.assertRaises(NoCandidateError) as caught:
                search_candidates(
                    level, plan, precise, candidate_seed=2026,
                    difficulty_profile=self._complete_difficulty_profile("0|0|100|0"),
                )

        self.assertEqual(candidate_simulations, 0)
        self.assertGreaterEqual(
            caught.exception.detail.evidence["rejection_counts"]["precision_parameter_underflow"],
            1,
        )

    def test_precision_mode_changes_options_hash_without_changing_legacy_default(self) -> None:
        _, options = _plan_and_options()
        legacy_hash = (
            "6d3ca15206874ad3dcd400e65decc83e7d4a6354dc30745b25c4fb6087745178"
        )

        self.assertEqual(generation_options_hash(options), legacy_hash)
        self.assertEqual(
            generation_options_hash(replace(options, precision_mode=False)),
            legacy_hash,
        )
        self.assertNotEqual(
            generation_options_hash(replace(options, precision_mode=True)),
            legacy_hash,
        )

    def test_precision_mode_does_not_return_reviewable_rule_failures(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        precise = replace(
            options,
            precision_mode=True,
            max_structure_layer_span=0,
            attempt_budget=3,
        )

        with self.assertRaises(NoCandidateError) as caught:
            search_candidates(
                level, plan, precise, candidate_seed=2026,
                difficulty_profile=self._complete_difficulty_profile(),
            )

        evidence = caught.exception.detail.evidence
        self.assertIn("精准模式", caught.exception.detail.message)
        self.assertGreaterEqual(
            evidence["rejection_counts"]["precision_rule_failure"], 1
        )

    def test_precision_mode_skips_simulation_for_known_rule_failures(self) -> None:
        from puzzle_core import search as search_module

        level = _chain_level()
        plan, options = _plan_and_options()
        precise = replace(
            options,
            precision_mode=True,
            max_structure_layer_span=0,
            attempt_budget=3,
        )
        original_simulate = search_module.simulate_candidate_difficulty
        candidate_simulations = 0

        def count_candidate_simulations(*args, **kwargs):
            nonlocal candidate_simulations
            if args[1]:
                candidate_simulations += 1
            return original_simulate(*args, **kwargs)

        with patch(
            "puzzle_core.search.simulate_candidate_difficulty",
            side_effect=count_candidate_simulations,
        ):
            with self.assertRaises(NoCandidateError):
                search_candidates(
                    level, plan, precise, candidate_seed=2026,
                    difficulty_profile=self._complete_difficulty_profile(),
                )

        self.assertEqual(candidate_simulations, 0)

    def test_precision_mode_excludes_high_pressure_after_simulation(self) -> None:
        from puzzle_core import search as search_module

        level = _chain_level()
        plan, options = _plan_and_options()
        precise = replace(options, precision_mode=True, attempt_budget=3)
        original_evaluate = search_module.evaluate_candidate_experience

        def high_pressure(*args, **kwargs):
            report = original_evaluate(*args, **kwargs)
            if len(args) >= 4 or "simulation" in kwargs:
                report = {
                    **report,
                    "simulation_pressure": {"level": "high"},
                }
            return report

        with patch(
            "puzzle_core.search.evaluate_candidate_experience",
            side_effect=high_pressure,
        ):
            with self.assertRaises(NoCandidateError) as caught:
                search_candidates(
                    level, plan, precise, candidate_seed=2026,
                    difficulty_profile=self._complete_difficulty_profile(),
                )

        evidence = caught.exception.detail.evidence
        self.assertGreaterEqual(
            evidence["rejection_counts"]["precision_pressure"], 1
        )

    def test_precision_mode_searches_beyond_initial_simulation_shortlist(self) -> None:
        from puzzle_core import search as search_module

        # Tower placement only changed labels; use genuine static partitions
        # so this regression still tests more than twelve distinct structures.
        level = _chain_level()
        plan, base_options = _plan_and_options()
        options = replace(base_options, precision_mode=True, candidate_count=1, attempt_budget=100)
        original_simulate = search_module.simulate_candidate_difficulty
        original_evaluate = search_module.evaluate_candidate_experience
        simulated_candidates = 0
        phases: list[str] = []

        def only_thirteenth_qualifies(*args, **kwargs):
            nonlocal simulated_candidates
            report = original_simulate(*args, **kwargs)
            if args[1]:
                simulated_candidates += 1
                report = {
                    **report,
                    "qualified": simulated_candidates == 13,
                    "trend": {
                        **report["trend"],
                        "safety_qualified": True,
                    },
                }
            return report

        def admissible_pressure(*args, **kwargs):
            report = original_evaluate(*args, **kwargs)
            if len(args) >= 4 or "simulation" in kwargs:
                report = {
                    **report,
                    "hard_validation": {"status": "proved"},
                    "simulation_pressure": {"level": "low"},
                }
            return report

        with patch(
            "puzzle_core.search.simulate_candidate_difficulty",
            side_effect=only_thirteenth_qualifies,
        ), patch(
            "puzzle_core.search.evaluate_candidate_experience",
            side_effect=admissible_pressure,
        ):
            searched = search_candidates(
                level,
                plan,
                options,
                candidate_seed=77,
                difficulty_profile=self._complete_difficulty_profile(),
                final_candidate_acceptance=lambda _: True,
                cooperative_checkpoint=phases.append,
            )

        self.assertEqual(len(searched.candidates), 1)
        self.assertTrue(searched.candidates[0].difficulty_simulation["qualified"])
        self.assertGreaterEqual(
            searched.rejection_counts["precision_difficulty_target"], 12
        )
        self.assertEqual(phases.count("precision_candidate_validation"), 13)

    def test_precision_mode_continues_after_independent_validation_failure(self) -> None:
        from puzzle_core import search as search_module

        level = _chain_level()
        plan, base_options = _plan_and_options()
        options = replace(base_options, precision_mode=True, candidate_count=1, attempt_budget=100)
        original_simulate = search_module.simulate_candidate_difficulty
        original_evaluate = search_module.evaluate_candidate_experience
        validations = 0

        def qualifying_simulation(*args, **kwargs):
            report = original_simulate(*args, **kwargs)
            if args[1]:
                report = {
                    **report,
                    "qualified": True,
                    "trend": {**report["trend"], "safety_qualified": True},
                }
            return report

        def qualifying_experience(*args, **kwargs):
            report = original_evaluate(*args, **kwargs)
            if len(args) >= 4 or "simulation" in kwargs:
                report = {
                    **report,
                    "hard_validation": {"status": "proved"},
                    "simulation_pressure": {"level": "low"},
                }
            return report

        def independent_validation(candidate):
            nonlocal validations
            validations += 1
            return validations > 1

        with patch(
            "puzzle_core.search.simulate_candidate_difficulty",
            side_effect=qualifying_simulation,
        ), patch(
            "puzzle_core.search.evaluate_candidate_experience",
            side_effect=qualifying_experience,
        ):
            searched = search_candidates(
                level,
                plan,
                options,
                candidate_seed=77,
                difficulty_profile=self._complete_difficulty_profile(),
                final_candidate_acceptance=independent_validation,
            )

        self.assertEqual(len(searched.candidates), 1)
        self.assertEqual(validations, 2)
        self.assertEqual(
            searched.rejection_counts["precision_independent_validation"], 1
        )

    @staticmethod
    def _strategy_summary(
        pass_probability: float,
        *,
        trials: int = 1,
        progressive_reveal: bool = False,
    ) -> dict[str, object]:
        start = 0.0 if progressive_reveal else 9.0
        end = 9.0
        distribution = lambda value: {"median": value}  # noqa: E731
        reveal_curve = {
            "curve": [
                {
                    "cumulative_exposed_structure_count": distribution(start),
                },
                {
                    "cumulative_exposed_structure_count": distribution(end),
                },
            ],
            "first_three_structure_matches": {
                "1": {
                    "progress": distribution(
                        0.8 if progressive_reveal else 0.05
                    )
                }
            },
            "released_ab_total_per_path": distribution(
                9.0 if progressive_reveal else 0.0
            ),
            "dangling_one_area": distribution(
                3.0 if progressive_reveal else 0.0
            ),
            "dangling_two_area": distribution(
                2.0 if progressive_reveal else 0.0
            ),
        }
        return {
            "pass_probability": pass_probability,
            "trials": trials,
            "reveal_curve": reveal_curve,
        }

    def test_mechanism_pressure_uses_strategy_vector_not_legacy_aggregate(self) -> None:
        candidate = SimpleNamespace(units=())
        simulation = {
            "duplicates": [
                {
                    "capacity7_failure_path_ratio": 1.0,
                    "strategy_failure_vector": {
                        "random": 0.1,
                        "greedy": 0.0,
                        "local_optimal": 0.0,
                        "unlock": 0.0,
                        "adversarial": 0.0,
                    },
                }
            ]
        }

        _reveals, difficulty = _experience_dynamic_inputs(candidate, simulation)

        self.assertEqual(difficulty["worst_failure_rate"], 0.1)

    def test_path_spread_is_computed_within_each_duplicate(self) -> None:
        candidate = SimpleNamespace(units=(), replacements=())
        first = (1.0, 0.9, 0.8, 0.7, 0.6)
        second = (0.6, 0.5, 0.4, 0.3, 0.2)
        simulation = {
            "duplicates": [
                {
                    "duplicate_index": index,
                    "strategies": {
                        name: self._strategy_summary(probability)
                        for name, probability in zip(
                            (
                                "random",
                                "greedy",
                                "local_optimal",
                                "unlock",
                                "adversarial",
                            ),
                            probabilities,
                        )
                    },
                }
                for index, probabilities in enumerate((first, second), 1)
            ]
        }

        reveals, difficulty = _experience_dynamic_inputs(candidate, simulation)

        self.assertAlmostEqual(difficulty["path_spread"], 0.4)
        self.assertAlmostEqual(difficulty["median_path_spread"], 0.4)
        self.assertEqual(
            [row["spread"] for row in difficulty["per_duplicate_path_spread"]],
            [0.4, 0.4],
        )
        # A cross-DupPara flatten would incorrectly report 0.8.
        self.assertNotEqual(difficulty["path_spread"], 0.8)
        self.assertEqual(
            reveals["per_duplicate_path_spread"],
            difficulty["per_duplicate_path_spread"],
        )

    def test_diversity_standardization_is_invariant_to_unit_scaling(self) -> None:
        self.assertAlmostEqual(
            _robust_standardize(0.8, 0.5, 0.2),
            _robust_standardize(80.0, 50.0, 20.0),
        )

    @staticmethod
    def _diversity_candidate(
        name: str,
        *,
        final_stage_bucket: int = 0,
        dangling_bucket: int = 0,
        match_bucket: int = 0,
        release_bucket: int = 0,
        relation: str = "parallel",
        static_shift: float = 0.0,
    ) -> SimpleNamespace:
        curve = []
        for stage in range(5):
            bucket = final_stage_bucket if stage == 4 else 0
            curve.append(
                {
                    "current_clickable_structure_count": bucket,
                    "cumulative_exposed_structure_count": bucket,
                    "complete_structure_group_type_count": bucket,
                    "dangling_one_type_count": (
                        dangling_bucket if stage == 2 else 0
                    ),
                    "dangling_two_type_count": (
                        dangling_bucket if stage == 2 else 0
                    ),
                }
            )
        compact = {
            "curve": curve,
            "match_progress": [match_bucket, match_bucket, match_bucket],
            "release_per_click": release_bucket,
            "release_on_completion": release_bucket,
            "release_total": release_bucket,
        }
        reveal_buckets = {
            "initial": 0,
            "after_clearing_ab": 0,
            "dynamic": {
                "first_match_progress": match_bucket,
                "dangling_area": dangling_bucket,
            },
            "per_duplicate_path_spread": [0],
            "full_curve_and_release": [
                {
                    "strategy_reveal_strength": {
                        strategy: final_stage_bucket
                        for strategy in (
                            "random",
                            "greedy",
                            "local_optimal",
                            "unlock",
                            "adversarial",
                        )
                    },
                    "strategy_curve_and_release": {
                        strategy: compact
                        for strategy in (
                            "random",
                            "greedy",
                            "local_optimal",
                            "unlock",
                            "adversarial",
                        )
                    },
                    "median_reveal_strength": final_stage_bucket,
                    "worst_reveal_strength": final_stage_bucket,
                    "reveal_strength_spread": 0,
                }
            ],
        }
        dependency_dag = (
            [{"from_cluster": 1, "to_cluster": 2}]
            if relation == "serial"
            else []
        )
        pair_counts = {
            "parallel": int(relation == "parallel"),
            "overlap": int(relation == "overlap"),
            "serial": int(relation == "serial"),
        }
        public_graph = {
            "clusters": [
                {"cluster_id": 1, "size": 3},
                {"cluster_id": 2, "size": 3},
            ],
            "dependency_dag": dependency_dag,
            "pair_relation_counts": pair_counts,
        }
        canonical_graph = {
            "canonicalization_version": "fixture-v1",
            "clusters": [{"stable_label": "a"}, {"stable_label": "b"}],
            "dependency_edges": (
                [{"from_label": "a", "to_label": "b"}]
                if relation == "serial"
                else []
            ),
            "pair_relation_counts": pair_counts,
        }
        report = {
            "metrics": {
                "structure_count": 6,
                "initial_clickable_count": static_shift,
                "direct_ab_cover_ratio": min(1.0, static_shift),
                "released_ab_per_structure": static_shift,
                "dependency_component_count": 2 + static_shift,
                "layer_span": 2 + static_shift,
            },
            "online_style_distance": {"total": static_shift},
            "composite_cluster_graph": public_graph,
            "experience_signature_evidence": {
                "composite_cluster_graph": canonical_graph,
                "reveal_buckets": reveal_buckets,
            },
        }
        return SimpleNamespace(
            experience_analysis=report,
            experience_signature=f"experience:{name}",
            structure_signature=f"structure:{name}",
        )

    def test_experience_vector_uses_dynamic_five_stage_and_cluster_topology(self) -> None:
        baseline = self._diversity_candidate("baseline")
        fifth_stage = self._diversity_candidate(
            "fifth-stage", final_stage_bucket=4
        )
        suspended_match_release = self._diversity_candidate(
            "suspended-match-release",
            dangling_bucket=4,
            match_bucket=3,
            release_bucket=2,
        )
        serial = self._diversity_candidate("serial", relation="serial")

        baseline_vector = _experience_vector(baseline)

        self.assertGreater(len(baseline_vector), 40)
        self.assertNotEqual(baseline_vector, _experience_vector(fifth_stage))
        self.assertNotEqual(
            baseline_vector,
            _experience_vector(suspended_match_release),
        )
        self.assertNotEqual(baseline_vector, _experience_vector(serial))

    def test_diverse_selection_prefers_new_mechanism_over_static_coordinate_tweak(self) -> None:
        baseline = self._diversity_candidate("baseline")
        coordinate_tweak = self._diversity_candidate(
            "coordinate-tweak", static_shift=4.0
        )
        different_mechanism = self._diversity_candidate(
            "different-mechanism",
            final_stage_bucket=4,
            dangling_bucket=3,
            match_bucket=2,
            release_bucket=4,
            relation="serial",
        )

        selected = _select_experience_diverse(
            [baseline, coordinate_tweak, different_mechanism], 2
        )

        self.assertEqual(
            [item.experience_signature for item in selected],
            ["experience:baseline", "experience:different-mechanism"],
        )

    @staticmethod
    def _classified_archetype(primary: str) -> dict[str, object]:
        scores = {
            "intrinsic_gate": 0.2,
            "board_reveal": 0.2,
            "hybrid": 0.2,
        }
        scores[primary] = 0.9
        return {
            "classification_status": "classified",
            "primary": primary,
            "automatic_primary": primary,
            "classification_confidence": 0.8,
            "scores": scores,
        }

    def test_auto_archetype_uses_frozen_target_not_candidate_itself(self) -> None:
        level = _quality_risk_level()
        intrinsic = _archetype_match_evidence(
            level=level,
            candidate_archetype=self._classified_archetype(
                "intrinsic_gate"
            ),
            requested_archetype="auto",
            structure_count=9,
            special_preset="none",
            simulation=None,
        )
        hybrid = _archetype_match_evidence(
            level=level,
            candidate_archetype=self._classified_archetype("hybrid"),
            requested_archetype="auto",
            structure_count=9,
            special_preset="none",
            simulation=None,
        )

        self.assertEqual(intrinsic["version"], ARCHETYPE_MATCH_POLICY_VERSION)
        self.assertEqual(intrinsic["baseline"], "reference_cohort")
        self.assertEqual(intrinsic["target_archetype"], "hybrid")
        self.assertEqual(
            intrinsic["target_archetype"], hybrid["target_archetype"]
        )
        self.assertFalse(intrinsic["matches_target"])
        self.assertTrue(hybrid["matches_target"])
        self.assertFalse(intrinsic["candidate_features_used_to_choose_target"])

    def test_auto_archetype_uses_independent_source_cn_classification(self) -> None:
        level = _source_structure_level()
        first = _archetype_match_evidence(
            level=level,
            candidate_archetype=self._classified_archetype(
                "intrinsic_gate"
            ),
            requested_archetype="auto",
            structure_count=9,
            special_preset="none",
            simulation=None,
        )
        second = _archetype_match_evidence(
            level=level,
            candidate_archetype=self._classified_archetype(
                "board_reveal"
            ),
            requested_archetype="auto",
            structure_count=9,
            special_preset="none",
            simulation=None,
        )

        self.assertEqual(first["baseline"], "source_template")
        self.assertEqual(
            first["target_source"],
            "independent_source_template_classification",
        )
        self.assertEqual(first["target_archetype"], second["target_archetype"])
        self.assertEqual(first["source_structure_count"], 9)
        self.assertIsInstance(first["source_template_classification"], dict)
        self.assertFalse(first["candidate_features_used_to_choose_target"])

    def test_explicit_archetype_remains_the_user_selected_target(self) -> None:
        result = _archetype_match_evidence(
            level=_chain_level(),
            candidate_archetype=self._classified_archetype("hybrid"),
            requested_archetype="board_reveal",
            structure_count=9,
            special_preset="none",
            simulation=None,
        )

        self.assertEqual(result["baseline"], "user_selection")
        self.assertEqual(result["target_archetype"], "board_reveal")
        self.assertFalse(result["matches_target"])

    def test_reveal_strength_gives_each_strategy_one_vote(self) -> None:
        candidate = SimpleNamespace(
            units=(), replacements=tuple(range(9))
        )

        def simulation(random_trials: int) -> dict[str, object]:
            return {
                "archetype_baseline": "source_template",
                "duplicates": [
                    {
                        "duplicate_index": 1,
                        "strategies": {
                            name: self._strategy_summary(
                                1.0,
                                trials=(random_trials if name == "random" else 1),
                                progressive_reveal=name
                                in {"random", "adversarial"},
                            )
                            for name in (
                                "random",
                                "greedy",
                                "local_optimal",
                                "unlock",
                                "adversarial",
                            )
                        },
                    }
                ],
            }

        first_reveals, first_difficulty = _experience_dynamic_inputs(
            candidate, simulation(12)
        )
        second_reveals, second_difficulty = _experience_dynamic_inputs(
            candidate, simulation(1200)
        )

        self.assertEqual(first_reveals, second_reveals)
        self.assertEqual(first_difficulty, second_difficulty)
        self.assertEqual(
            first_reveals["aggregation_policy"],
            "each_strategy_one_vote_per_duplicate_v1",
        )
        self.assertEqual(
            set(
                first_reveals["per_duplicate_strategy_reveal"][0][
                    "strategies"
                ]
            ),
            {
                "random",
                "greedy",
                "local_optimal",
                "unlock",
                "adversarial",
            },
        )
        self.assertGreater(first_reveals["worst_reveal_strength"], 0)
        self.assertEqual(
            first_difficulty["archetype_baseline"], "source_template"
        )

    def test_quality_score_combines_layout_nesting_and_vertical_risk(self) -> None:
        level = _quality_risk_level()
        selected = tuple(index for index in range(len(level.tiles)) if index % 3 != 2)

        score, metrics, summary, warnings = _quality_score(
            level,
            build_static_geometry(level),
            (selected,),
            QualityOptions(position="unrestricted", symmetry="off"),
        )

        self.assertAlmostEqual(metrics["unique_nesting_load"], 0.5)
        self.assertAlmostEqual(metrics["ab_cover_relation_density"], 1.0)
        self.assertAlmostEqual(metrics["repeated_ab_cover_load"], 0.5)
        self.assertAlmostEqual(metrics["structure_participation_rate"], 1.0)
        self.assertAlmostEqual(metrics["vertical_overlap_pair_density"], 0.5)
        self.assertAlmostEqual(metrics["vertical_mean_layer_gap"], 2.0)
        self.assertAlmostEqual(metrics["vertical_cross_layer_share"], 1.0)
        self.assertAlmostEqual(metrics["same_column_gap_per_block"], 0.5)
        self.assertEqual(metrics["structure_layer_span"], 2.0)
        self.assertAlmostEqual(
            score,
            metrics["layout_score"] - metrics["experience_risk_penalty"],
        )
        self.assertAlmostEqual(
            metrics["layout_score"],
            metrics["center_layout_contribution"]
            + metrics["symmetry_layout_contribution"]
            + metrics["density_layout_contribution"],
        )
        self.assertAlmostEqual(
            metrics["nesting_risk_penalty"],
            metrics["repeated_ab_cover_penalty"]
            + metrics["unique_nesting_penalty"]
            + metrics["structure_participation_penalty"],
        )
        self.assertAlmostEqual(
            metrics["vertical_risk_penalty"],
            metrics["vertical_overlap_penalty"]
            + metrics["vertical_layer_gap_penalty"]
            + metrics["vertical_cross_layer_penalty"]
            + metrics["same_column_gap_penalty"],
        )
        self.assertTrue(summary)
        self.assertIsInstance(warnings, tuple)

    def test_majority_source_uses_original_counts_and_a_wins_ties(self) -> None:
        options = GenerationOptions(
            replacement_sources=(MAJORITY_A_ON_TIE,)
        )
        b_majority = _source_priority_level()
        b_pool = _base_eligible(
            b_majority,
            build_static_geometry(b_majority),
            options,
            tower_membership=False,
        )
        self.assertEqual(b_pool, (1, 2))

        a_majority = _source_priority_level((-10, -10, -11))
        a_pool = _base_eligible(
            a_majority,
            build_static_geometry(a_majority),
            options,
            tower_membership=False,
        )
        self.assertEqual(a_pool, (0, 1))

        tie = _source_priority_level((-10, -11))
        tie_pool = _base_eligible(
            tie,
            build_static_geometry(tie),
            options,
            tower_membership=False,
        )
        self.assertEqual(tie_pool, (0,))

        # Majority selection uses original totals, not the post-filter pool:
        # both majority-A Tiles are above max_layer, so B must not become a
        # silent fallback merely because it remains eligible.
        filtered_majority = _source_priority_level(
            (-10, -10, -11),
            z_values=(2, 2, 0),
        )
        filtered_options = replace(options, max_layer=1)
        filtered_graph = build_static_geometry(filtered_majority)
        self.assertEqual(
            _base_eligible(
                filtered_majority,
                filtered_graph,
                filtered_options,
                tower_membership=False,
            ),
            (),
        )
        self.assertEqual(
            _validator_eligible_source_pool(
                filtered_majority,
                filtered_graph,
                filtered_options,
                tower_membership=False,
                tower_mode=TowerMode.DISALLOW,
            ),
            (),
        )

    def test_legacy_priority_keeps_a_first_then_b_fallback_and_manual_overrides(self) -> None:
        level = _source_priority_level((-10, -10, -11))
        graph = build_static_geometry(level)
        legacy = GenerationOptions(replacement_sources=(PREFER_A_ELSE_B,))
        self.assertEqual(
            _base_eligible(level, graph, legacy, tower_membership=False),
            (0, 1),
        )
        # Old explicit plans preferred even a minority A.  The new majority
        # default picks B here, so the two rules must not be conflated.
        minority_a = _source_priority_level((-10, -11, -11))
        minority_graph = build_static_geometry(minority_a)
        self.assertEqual(
            _base_eligible(minority_a, minority_graph, legacy, tower_membership=False),
            (0,),
        )
        self.assertEqual(
            _validator_eligible_source_pool(
                minority_a,
                minority_graph,
                legacy,
                tower_membership=False,
                tower_mode=TowerMode.DISALLOW,
            ),
            (0,),
        )
        filtered_a = _source_priority_level(
            (-10, -11, -11), z_values=(2, 0, 0)
        )
        filtered_graph = build_static_geometry(filtered_a)
        filtered_legacy = replace(legacy, max_layer=1)
        self.assertEqual(
            _base_eligible(
                filtered_a, filtered_graph, filtered_legacy, tower_membership=False
            ),
            (1, 2),
        )
        self.assertEqual(
            _validator_eligible_source_pool(
                filtered_a,
                filtered_graph,
                filtered_legacy,
                tower_membership=False,
                tower_mode=TowerMode.DISALLOW,
            ),
            (1, 2),
        )
        self.assertEqual(
            _base_eligible(
                level,
                graph,
                GenerationOptions(replacement_sources=("B",)),
                tower_membership=False,
            ),
            (2,),
        )
        self.assertEqual(
            _base_eligible(
                level,
                graph,
                GenerationOptions(replacement_sources=("A", "B")),
                tower_membership=False,
            ),
            (0, 1, 2),
        )

    def test_disallow_percentages_use_non_tower_population_and_height(self) -> None:
        level = _mixed_tower_level()
        count_options = GenerationOptions(total_percent_limit=60)
        layer_options = GenerationOptions(max_layer_percent=50)

        self.assertEqual(
            _effective_count_limit(level, count_options, TowerMode.DISALLOW),
            3,
        )
        self.assertEqual(
            _effective_count_limit(level, count_options, TowerMode.ALLOW),
            6,
        )
        self.assertEqual(
            _effective_max_layer(level, layer_options, TowerMode.DISALLOW),
            3,
        )
        self.assertEqual(
            _effective_max_layer(level, layer_options, TowerMode.ALLOW),
            5,
        )

    def test_structure_layer_span_uses_highest_minus_lowest_layer(self) -> None:
        level = _mixed_tower_level()
        span = structure_layer_span(level, (0, 5))

        # Layers 1 and 6 have span 5: a configured limit of 5 accepts it,
        # while the same selection exceeds a limit of 4 by exactly one.
        self.assertEqual(span, 5)
        self.assertLessEqual(span, 5)
        self.assertGreater(span, 4)
        self.assertIsNone(structure_layer_span(level, ()))

    def test_structure_layer_span_failure_remains_reviewable(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        limited = replace(
            options,
            max_structure_layer_span=0,
            attempt_budget=3,
        )

        searched = search_candidates(
            level,
            plan,
            limited,
            candidate_seed=2026,
        )
        candidate = searched.candidates[0]
        report = validate_candidate(
            level,
            plan,
            limited,
            candidate,
            require_exportable_experience=False,
        )

        self.assertGreaterEqual(
            searched.rejection_counts["rule_warning_structure_layer_span"],
            1,
        )
        self.assertFalse(report.passed)
        self.assertTrue(report.technical_integrity_passed)
        self.assertTrue(report.manual_export_eligible)
        self.assertIn(
            "structure_layer_span",
            {check.code for check in report.rule_failures},
        )
        with self.assertRaises(CandidateValidationError):
            validate_candidate(level, plan, limited, candidate)

    def test_topology_failure_remains_reviewable_but_not_recommended(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        limited = replace(options, attempt_budget=3)

        def forced_failure(*args, **kwargs):
            report = core_analyze_topology(*args, **kwargs)
            return replace(
                report,
                passed=False,
                reasons=("fixture_forces_topology_failure",),
            )

        with patch(
            "puzzle_core.search.analyze_topology",
            side_effect=forced_failure,
        ):
            searched = search_candidates(
                level,
                plan,
                limited,
                candidate_seed=2026,
            )
        candidate = searched.candidates[0]

        with patch(
            "puzzle_core.validator.analyze_topology",
            side_effect=forced_failure,
        ):
            report = validate_candidate(
                level,
                plan,
                limited,
                candidate,
                require_exportable_experience=False,
            )

        self.assertFalse(report.passed)
        self.assertTrue(report.technical_integrity_passed)
        self.assertTrue(report.manual_export_eligible)
        self.assertFalse(report.auto_recommendation_eligible)
        self.assertIn(
            "unit_topology",
            {check.code for check in report.rule_failures},
        )
        self.assertGreaterEqual(
            searched.rejection_counts[
                "rule_warning_topology_fixture_forces_topology_failure"
            ],
            1,
        )

    def test_disallow_accepts_structure_percent_up_to_one_hundred(self) -> None:
        plan, options = _plan_and_options()
        for percent in (50, 70, 100):
            _validate_options(plan, replace(options, total_percent_limit=percent))
        for percent in (0, 101):
            with self.assertRaises(Exception):
                _validate_options(plan, replace(options, total_percent_limit=percent))

    def test_candidate_pool_rejects_both_ends_of_adjacent_full_cover_only(self) -> None:
        level = _full_cover_rule_level()
        pool = _base_eligible(
            level,
            build_static_geometry(level),
            GenerationOptions(),
            tower_membership=False,
        )
        self.assertTrue({0, 1, 2, 3}.isdisjoint(pool))
        self.assertTrue({4, 5, 6}.issubset(pool))

    def test_intrinsic_masks_do_not_keep_paths_through_removed_external_ab(self) -> None:
        payload = [
            _bytes_field(2, b"bridgefixture"),
            _varint_field(4, 8),
            _varint_field(5, 4),
        ]
        for x, z in ((0, 0), (1, 1), (2, 2)):
            tile = b"".join(
                (
                    _varint_field(1, x),
                    _varint_field(2, 0),
                    _varint_field(3, z),
                    _varint_field(4, -10),
                )
            )
            payload.append(_bytes_field(6, tile))
        level = parse_level_bytes(b"".join(payload), source_name="bridgefixture_m.bytes")
        graph = build_static_geometry(level)
        self.assertIn(2, graph.transitive_blockers[0])
        self.assertNotIn(2, graph.direct_blockers[0])
        # Selected structures 0 and 2 do not overlap. Index 1 is external A/B
        # and is treated as cleared in the structure-intrinsic snapshot.
        self.assertEqual(_restricted_masks((0, 2), graph), (0, 0))

    def test_independent_validation_proves_each_unit_once_before_combination(self):
        from puzzle_core import validator
        level = _chain_level()
        plan, options = _plan_and_options()
        candidate = search_candidates(level, plan, options, candidate_seed=2026).candidates[0]
        with patch.object(validator, 'analyze_puzzle', wraps=validator.analyze_puzzle) as recompute:
            with patch('puzzle_core.combination.analyze_puzzle', side_effect=AssertionError('duplicate proof')):
                report = validate_candidate(level, plan, options, candidate, require_exportable_experience=False)
        self.assertTrue(report.passed)
        self.assertEqual(recompute.call_count, 1)

    def test_deterministic_search_and_independent_validation(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        first = search_candidates(level, plan, options, candidate_seed=2026)
        second = search_candidates(level, plan, options, candidate_seed=2026)
        self.assertEqual(
            first.candidates[0].structure_signature,
            second.candidates[0].structure_signature,
        )
        # This case exercises deterministic search and independent structural
        # validation.  Formal export eligibility (difficulty fidelity and
        # DupPara trend evidence) is covered by the dedicated export tests.
        report = validate_candidate(
            level,
            plan,
            options,
            first.candidates[0],
            require_exportable_experience=False,
        )
        self.assertTrue(report.passed)
        self.assertTrue(report.technical_integrity_passed)
        self.assertTrue(report.manual_export_eligible)
        self.assertFalse(report.auto_recommendation_eligible)
        self.assertIn(
            "difficulty_simulation",
            {check.code for check in report.rule_failures},
        )
        self.assertEqual(report.replacement_count, 9)
        self.assertEqual(sum(dict(report.structure_counts).values()), 9)
        self.assertEqual(report.coverage_audit.adjacent_single_full_count, 0)
        self.assertEqual(report.inventory.source_total_tile_count, 12)
        self.assertEqual(report.inventory.output_total_tile_count, 12)
        self.assertEqual(report.inventory.source_existing_structure_count, 0)
        self.assertEqual(report.inventory.new_structure_count, 9)
        self.assertEqual(report.inventory.output_structure_count, 9)
        self.assertEqual(
            report.effective_constraints.effective_structure_count_limit,
            12,
        )
        self.assertTrue(report.effective_constraints.count_limit_passed)
        self.assertTrue(report.effective_constraints.layer_limit_passed)
        self.assertEqual(len(report.unit_access_reports), 1)
        self.assertEqual(
            report.initial_entry_count,
            sum(
                len(item.initial_entry_indices)
                for item in report.unit_access_reports
            ),
        )
        self.assertIsNotNone(report.combination_conclusion)
        self.assertTrue(report.combination_conclusion.all_units_qualified)
        payload = report.as_dict()
        self.assertEqual(payload["coverage_audit"]["adjacent_single_full_relations"], ())
        self.assertIn("output_structure_counts", payload["inventory"])
        self.assertIn("external_direct_blockers_by_tile", payload["unit_access_reports"][0])
        self.assertIn("interleaving_result_at_capacity_7", payload["combination_proof"])

    def test_search_can_deterministically_exclude_a_collected_layout(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        first = search_candidates(
            level,
            plan,
            options,
            candidate_seed=2026,
            candidate_index=1,
        ).candidates[0]

        second = search_candidates(
            level,
            plan,
            options,
            candidate_seed=2026,
            candidate_index=1,
            exclude_structure_signatures={first.structure_signature},
        ).candidates[0]
        replay = search_candidates(
            level,
            plan,
            options,
            candidate_seed=2026,
            candidate_index=1,
            exclude_structure_signatures={first.structure_signature},
        ).candidates[0]

        self.assertNotEqual(second.structure_signature, first.structure_signature)
        self.assertEqual(replay.structure_signature, second.structure_signature)

    def test_search_can_deterministically_exclude_collected_experience(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        first = search_candidates(
            level,
            plan,
            options,
            candidate_seed=2026,
            candidate_index=1,
        ).candidates[0]

        second = search_candidates(
            level,
            plan,
            options,
            candidate_seed=2026,
            candidate_index=1,
            exclude_experience_signatures={first.experience_signature},
        ).candidates[0]
        replay = search_candidates(
            level,
            plan,
            options,
            candidate_seed=2026,
            candidate_index=1,
            exclude_experience_signatures={first.experience_signature},
        ).candidates[0]

        self.assertNotEqual(second.experience_signature, first.experience_signature)
        self.assertEqual(replay.experience_signature, second.experience_signature)

    def test_repeated_ab_cover_is_primary_nesting_penalty_signal(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        candidate = search_candidates(
            level, plan, options, candidate_seed=2026
        ).candidates[0]
        metrics = candidate.quality_metrics
        calibration = metrics["experience_risk_size_calibration"]
        expected_nesting_penalty = calibration * (
            metrics["repeated_ab_cover_load"] * 1.25
            + metrics["unique_nesting_load"] * 0.40
            + metrics["structure_participation_rate"] * 0.35
        )

        self.assertAlmostEqual(
            metrics["nesting_risk_penalty"], expected_nesting_penalty
        )
        self.assertAlmostEqual(
            metrics["repeated_ab_cover_penalty"],
            calibration * metrics["repeated_ab_cover_load"] * 1.25,
        )
        self.assertAlmostEqual(
            candidate.quality_score,
            metrics["layout_score"] - metrics["experience_risk_penalty"],
        )

    def test_search_and_validator_preserve_exact_pre_gate_recycle_rule(self) -> None:
        level = _chain_level(all_a=True)
        plan = normalize_output_plan(
            RawOutputPlan(
                plan_id="s5-with-one-recycle",
                units=(RawPuzzleUnit(StepSpec.exact(5), "U1"),),
                tower_mode=TowerMode.DISALLOW,
            ),
            batch_seed=7,
            template_key="chain001",
        )[0]
        options = GenerationOptions(
            unit_options=(
                UnitGenerationOptions(
                    type_count=3,
                    required_pre_gate_recycles=1,
                    first_recycle_lead_tiles=2,
                ),
            ),
            attempt_budget=10,
            assignment_attempts=500,
        )

        candidate = search_candidates(
            level, plan, options, candidate_seed=2026
        ).candidates[0]
        proof = candidate.units[0].proof
        self.assertEqual(proof.recommended_pre_gate_recycles, 1)
        self.assertGreaterEqual(proof.recommended_first_match_click, 5)
        self.assertTrue(
            validate_candidate(
                level,
                plan,
                options,
                candidate,
                require_exportable_experience=False,
            ).passed
        )

    def test_validator_rejects_relaxed_intrinsic_target(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        candidate = search_candidates(level, plan, options, candidate_seed=2026).candidates[0]
        tampered = replace(candidate, units=(replace(candidate.units[0], intrinsic_target_step=3),))
        with self.assertRaises(CandidateValidationError) as caught:
            validate_candidate(level, plan, options, tampered)
        self.assertEqual(caught.exception.detail.evidence['check'], 'unit_target_step')

    def test_validator_rejects_tampered_replacement_metadata(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        candidate = search_candidates(level, plan, options, candidate_seed=2026).candidates[0]
        bad_first = replace(candidate.replacements[0], source_tile_id=-99)
        tampered = replace(
            candidate,
            replacements=(bad_first,) + candidate.replacements[1:],
        )
        with self.assertRaises(CandidateValidationError):
            validate_candidate(level, plan, options, tampered)

    def test_validator_binds_candidate_to_plan_and_generation_options(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        candidate = search_candidates(level, plan, options, candidate_seed=2026).candidates[0]

        with self.assertRaises(CandidateValidationError) as caught:
            validate_candidate(
                level,
                plan,
                options,
                replace(candidate, plan_id="another-plan"),
                require_exportable_experience=False,
            )
        self.assertEqual(
            caught.exception.detail.evidence["check"],
            "candidate_identity",
        )

        only_a = replace(options, replacement_sources=("A",))
        self.assertTrue(any(item.source_tile_id == -11 for item in candidate.replacements))
        with self.assertRaises(CandidateValidationError):
            validate_candidate(level, plan, only_a, candidate)

    def test_validator_requires_exact_unit_replacement_bijection(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        candidate = search_candidates(level, plan, options, candidate_seed=2026).candidates[0]
        used = {item.tile_index for item in candidate.replacements}
        unused = [tile for tile in level.tiles if tile.index not in used]
        self.assertEqual(len(unused), 3)
        label = candidate.units[0].assigned_types[0]
        additions = tuple(
            replace(
                candidate.replacements[0],
                tile_index=tile.index,
                source_tile_id=tile.tile_id,
                structure_type=label,
                structure_tile_id=-12 - (ord(label) - ord("C")),
                x=tile.x,
                y=tile.y,
                z=tile.z,
            )
            for tile in unused
        )
        tampered = replace(
            candidate,
            replacements=tuple(sorted(candidate.replacements + additions, key=lambda item: item.tile_index)),
        )

        with self.assertRaises(CandidateValidationError):
            validate_candidate(level, plan, options, tampered)

    def test_validator_rejects_tampered_unit_role(self) -> None:
        level = _chain_level()
        plan, options = _plan_and_options()
        candidate = search_candidates(level, plan, options, candidate_seed=2026).candidates[0]
        changed_unit = replace(candidate.units[0], role="left")

        with self.assertRaises(CandidateValidationError):
            validate_candidate(
                level,
                plan,
                options,
                replace(candidate, units=(changed_unit,)),
            )

    def test_tower_only_is_balanced_and_has_no_fake_step_proof(self) -> None:
        level = _tower_level()
        plan = normalize_output_plan(
            RawOutputPlan(
                plan_id="tower",
                units=(),
                tower_mode=TowerMode.ONLY,
            ),
            batch_seed=3,
            template_key="towerfixture",
        )[0]
        options = GenerationOptions(
            unit_options=(),
            tower_options=UnitGenerationOptions(type_count=3),
            attempt_budget=5,
            assignment_attempts=1,
        )
        candidate = search_candidates(
            level, plan, options, candidate_seed=77
        ).candidates[0]
        report = validate_candidate(
            level,
            plan,
            options,
            candidate,
            require_exportable_experience=False,
        )
        self.assertEqual(candidate.units, ())
        self.assertIsNone(report.combination_proof)
        self.assertIsNone(report.combination_conclusion)
        self.assertEqual(report.tower_structure_count, 9)
        self.assertEqual(report.non_tower_structure_count, 0)
        self.assertEqual(report.coverage_audit.adjacent_single_full_count, 0)
        self.assertGreater(len(report.coverage_audit.tower_initial_full_relations), 0)
        self.assertLessEqual(candidate.tower_group.max_min_difference, 1)

    def test_tower_replacements_must_equal_declared_tower_group(self) -> None:
        level = _tower_level()
        plan = normalize_output_plan(
            RawOutputPlan(plan_id="tower", units=(), tower_mode=TowerMode.ONLY),
            batch_seed=3,
            template_key="towerfixture",
        )[0]
        options = GenerationOptions(
            unit_options=(),
            tower_options=UnitGenerationOptions(type_count=3),
            attempt_budget=5,
            assignment_attempts=1,
        )
        candidate = search_candidates(level, plan, options, candidate_seed=77).candidates[0]
        used = {item.tile_index for item in candidate.replacements}
        unused = [tile for tile in level.tiles if tile.index not in used]
        self.assertEqual(len(unused), 3)
        label = candidate.tower_group.assigned_types[0]
        additions = tuple(
            replace(
                candidate.replacements[0],
                tile_index=tile.index,
                source_tile_id=tile.tile_id,
                structure_type=label,
                structure_tile_id=-12 - (ord(label) - ord("C")),
                x=tile.x,
                y=tile.y,
                z=tile.z,
                tower_index=tile.tower_index,
            )
            for tile in unused
        )
        tampered = replace(
            candidate,
            replacements=tuple(sorted(candidate.replacements + additions, key=lambda item: item.tile_index)),
        )

        with self.assertRaises(CandidateValidationError):
            validate_candidate(level, plan, options, tampered)

    def test_tower_balance_snapshot_is_independently_recomputed(self) -> None:
        level = _tower_level()
        plan = normalize_output_plan(
            RawOutputPlan(plan_id="tower", units=(), tower_mode=TowerMode.ONLY),
            batch_seed=3,
            template_key="towerfixture",
        )[0]
        options = GenerationOptions(
            unit_options=(),
            tower_options=UnitGenerationOptions(type_count=3),
            attempt_budget=5,
            assignment_attempts=1,
        )
        candidate = search_candidates(level, plan, options, candidate_seed=77).candidates[0]
        tampered_group = replace(
            candidate.tower_group,
            max_min_difference=candidate.tower_group.max_min_difference + 1,
        )

        with self.assertRaises(CandidateValidationError):
            validate_candidate(
                level,
                plan,
                options,
                replace(candidate, tower_group=tampered_group),
            )


if __name__ == "__main__":
    unittest.main()
