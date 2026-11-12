from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.errors import InputValidationError
from puzzle_core import solver
from puzzle_core.solver import (
    SolverLimits,
    analyze_puzzle,
    simulate_order,
    validate_target,
)


def chain_masks(size: int) -> tuple[int, ...]:
    return tuple(0 if index == 0 else 1 << (index - 1) for index in range(size))


class SolverTest(unittest.TestCase):
    def test_five_type_capacity_fallback_preserves_safe_recommended_path(self):
        types = tuple('CDDCC' + 'CCEEFFCDEF' + 'GGG' * 4)
        masks = tuple([0] + [1 << (i - 1) for i in range(1, 5)] + [16] * 10
                      + [32767] + [1 << (i - 1) for i in range(16, 27)])
        proof = analyze_puzzle(masks, types, threshold_type='C')
        self.assertTrue(validate_target(proof, 5)[0])
        path = simulate_order(proof.recommended_order, types, threshold_type='C', capacity=7)
        self.assertTrue(path.complete)
        self.assertEqual(path.gate_structure_step, 5)
        self.assertEqual(path.literal_first_match_click, 5)

    def test_proof_cancellation_is_checked_and_scope_is_restored(self):
        from puzzle_core.errors import OperationCancelledError
        checks = []
        def cancel(phase):
            self.assertEqual(phase, 'assignment_attempt')
            checks.append(phase)
            if len(checks) == 2:
                raise OperationCancelledError('test cancel')
        with self.assertRaises(OperationCancelledError), solver.proof_checkpoints(cancel):
            solver._capacity_proof((0,) * 15, tuple('CCCDDDEEEFFFGGG'), 'C', 7, SolverLimits())
        self.assertIsNone(solver._proof_checkpoint.get())
        self.assertEqual(len(checks), 2)

    def test_five_type_compression_matches_independent_generic_solver(self):
        import random
        rng = random.Random(426)
        for _ in range(12):
            types = list('CCCDDDEEEFFFGGG')
            rng.shuffle(types)
            masks = tuple(rng.randrange(1 << i) for i in range(15))
            expected = analyze_puzzle(masks, types, threshold_type='C', capacities=(12,))
            actual = solver._analyze_small_alphabet(masks, tuple(types), 'C', (12,), SolverLimits(), 2)
            for key in ('threshold_steps', 'literal_first_match_clicks', 'singleton_rule_ok',
                        'theoretical_complete_path_count', 'recommended_path_constraints_ok'):
                self.assertEqual(getattr(expected, key), getattr(actual, key), key)
            self.assertEqual(expected.capacity_proofs[0].complete_path_count,
                             actual.capacity_proofs[0].complete_path_count)

    def test_search_rejection_stops_before_capacity_enumeration(self):
        from unittest.mock import patch
        from puzzle_core.errors import CandidateValidationError
        types = tuple('CCCDDDEEEFFFGGG' * 2)
        with patch.object(solver, '_capacity_proof', side_effect=AssertionError('unnecessary proof')):
            with self.assertRaises(CandidateValidationError):
                analyze_puzzle((0,) * len(types), types, threshold_type='C', reject_invalid_target=5)

    def test_singleton_witness_filter_only_rejects_reachable_violations(self):
        types = tuple('CCCDDDEEEFFFGGG')
        self.assertTrue(solver.has_five_singleton_witness((0,) * len(types), types))
        self.assertFalse(solver.has_five_singleton_witness(chain_masks(len(types)), types))
        self.assertFalse(solver.has_five_singleton_witness((0,) * 9, tuple('CCCDDDEEE')))

    def test_compressed_safe_five_type_proof_and_unsafe_fallback(self):
        from unittest.mock import patch
        types = tuple('CDCDCD' + 'EEEFFFGGG' * 3)
        with patch.object(solver, '_analyze_small_alphabet', return_value=None):
            expected = analyze_puzzle(chain_masks(len(types)), types, threshold_type='C')
        actual = solver._analyze_small_alphabet(chain_masks(len(types)), types, 'C', (7, 8), SolverLimits(), 2)
        self.assertEqual(actual.threshold_steps, expected.threshold_steps)
        self.assertEqual(actual.singleton_rule_ok, expected.singleton_rule_ok)
        types = tuple('CCCDDDEEEFFFGGG')
        actual = solver._analyze_small_alphabet((0,) * 15, types, 'C', (7, 8), SolverLimits(), 2)
        self.assertIsNone(actual)

    def test_three_type_capacity_proofs_reuse_identical_state_traversal(self):
        from unittest.mock import patch
        types = tuple("CCDEECDDE")
        masks = chain_masks(len(types))
        expected = tuple(solver._capacity_proof(masks, types, "C", cap, SolverLimits()) for cap in (7, 8))
        with patch.object(solver, "_capacity_proof", wraps=solver._capacity_proof) as measured:
            proof = analyze_puzzle(masks, types, threshold_type="C")
        self.assertEqual(proof.capacity_proofs, expected)
        self.assertEqual(measured.call_count, 1)

    def test_geometry_budget_precheck_matches_exhaustive_small_graphs(self):
        import random
        rng = random.Random(412)
        for _ in range(30):
            masks = tuple(rng.randrange(1 << i) for i in range(8))
            count = sum(all(not (removed >> i & 1) or not (mask & ~removed)
                            for i, mask in enumerate(masks)) for removed in range(256))
            self.assertFalse(solver.proof_state_budget_exceeded(masks, count))
            self.assertTrue(solver.proof_state_budget_exceeded(masks, count - 1))

    def test_large_three_type_geometry_has_exact_compressed_proof(self):
        types = tuple("CDE" * 9)
        proof = analyze_puzzle((0,) * 27, types, threshold_type="C")
        self.assertEqual(proof.threshold_steps, (3, 4, 5, 6, 7))
        self.assertEqual(proof.literal_first_match_clicks, (3, 4, 5, 6, 7))
        self.assertTrue(proof.path_count_capped)
        self.assertTrue(proof.recommended_path_constraints_ok)
        self.assertTrue(all(p.classification == 'all_safe' for p in proof.capacity_proofs))

    def test_compressed_proof_matches_exhaustive_small_dags(self):
        import random
        rng = random.Random(971)
        for case in range(30):
            types = list('CCCDDDEEE' if case < 20 else ('CCCCCCDDDEEE' if case % 2 else 'CCCCCCDDDDDD'))
            size = len(types)
            original = tuple(rng.randrange(1 << i) for i in range(size))
            permutation = list(range(size)); rng.shuffle(permutation)
            masks_list = [0] * size
            for i, mask in enumerate(original):
                masks_list[permutation[i]] = sum(1 << permutation[j] for j in range(size) if mask & (1 << j))
            masks = tuple(masks_list)
            rng.shuffle(types); types = tuple(types)
            limits = SolverLimits(max_path_count=17 if case % 3 == 0 else 10**15)
            expected = analyze_puzzle(masks, types, threshold_type='C', limits=limits)
            if solver.has_direct_gate_triple(masks, types, 'C'):
                self.assertEqual(expected.minimum_structure_step, 3)
            actual = solver._analyze_small_alphabet(masks, types, 'C', (7, 8), limits, 2)
            for key in ('threshold_steps', 'literal_first_match_clicks', 'theoretical_complete_path_count',
                        'path_count_capped', 'singleton_rule_ok', 'recommended_path_constraints_ok'):
                self.assertEqual(getattr(actual, key), getattr(expected, key), key)
            for a, e in zip(actual.capacity_proofs, expected.capacity_proofs):
                for key in ('complete_path_count', 'path_count_capped', 'complete_exists', 'failure_reachable',
                            'gate_reachable', 'classification', 'instantaneous_peak', 'settled_peak'):
                    self.assertEqual(getattr(a, key), getattr(e, key), key)
            sim = simulate_order(actual.recommended_order, types, threshold_type='C', capacity=7)
            self.assertTrue(sim.complete)
            self.assertEqual(sim.gate_structure_step, actual.minimum_structure_step)

    def test_large_chain_preserves_s5_and_no_pre_gate_recycle(self):
        types = tuple('CDCDCD' + 'E' * 24)
        proof = analyze_puzzle(chain_masks(30), types, threshold_type='C')
        self.assertEqual(proof.threshold_steps, (5,))
        self.assertEqual(proof.literal_first_match_clicks, (5,))
        self.assertEqual(proof.theoretical_complete_path_count, 1)
        self.assertEqual(proof.recommended_pre_gate_recycles, 0)
        self.assertTrue(validate_target(proof, 5)[0])

    def test_confirmed_s4_recycling_example(self) -> None:
        types = ("C", "C", "E", "E", "E", "D", "C", "D", "D")
        proof = analyze_puzzle(
            chain_masks(len(types)),
            types,
            threshold_type="C",
            required_pre_gate_recycles=1,
            first_recycle_lead_tiles=2,
        )
        self.assertEqual(proof.minimum_structure_step, 4)
        self.assertEqual(proof.literal_first_match_clicks, (5,))
        self.assertEqual(proof.recommended_pre_gate_recycles, 1)
        self.assertEqual(proof.recommended_first_match_click, 5)
        self.assertTrue(proof.singleton_rule_ok)
        self.assertTrue(
            validate_target(
                proof,
                4,
                required_pre_gate_recycles=1,
                first_recycle_lead_tiles=2,
            )[0]
        )

    def test_confirmed_s8_match_before_capacity_check(self) -> None:
        # The first eleven clicks are the user's confirmed prefix.  The final
        # F/G/D tiles complete the three required groups after C is removed.
        types = (
            "C", "C", "E", "E", "E", "F", "F", "G", "G", "D", "C",
            "F", "G", "D", "D",
        )
        masks = chain_masks(len(types))
        proof = analyze_puzzle(
            masks,
            types,
            threshold_type="C",
            required_pre_gate_recycles=1,
            first_recycle_lead_tiles=2,
        )
        self.assertEqual(proof.minimum_structure_step, 8)
        capacity = {item.capacity: item for item in proof.capacity_proofs}
        self.assertEqual(capacity[7].classification, "all_fail")
        self.assertFalse(capacity[7].complete_exists)
        self.assertTrue(capacity[8].complete_exists)
        self.assertTrue(
            validate_target(
                proof,
                8,
                required_pre_gate_recycles=1,
                first_recycle_lead_tiles=2,
            )[0]
        )

        simulation = simulate_order(
            tuple(range(len(types))), types, threshold_type="C", capacity=8
        )
        self.assertTrue(simulation.complete)
        self.assertEqual(simulation.gate_structure_step, 8)
        self.assertEqual(simulation.steps[10].instantaneous_size, 8)
        self.assertEqual(simulation.steps[10].settled_size, 5)
        self.assertFalse(simulation.steps[10].failed)

    def test_s5_s6_s7_and_s9_gate_definitions(self) -> None:
        sequences = {
            5: ("C", "C", "D", "E", "C", "D", "D", "E", "E"),
            6: (
                "C", "C", "D", "E", "F", "C", "D", "D", "E", "E", "F", "F"
            ),
            7: (
                "C", "C", "D", "E", "F", "G", "C",
                "D", "D", "E", "E", "F", "F", "G", "G",
            ),
            9: (
                "C", "C", "D", "D", "E", "E", "F", "F", "C",
                "D", "E", "F",
            ),
        }
        for target, types in sequences.items():
            with self.subTest(target=target):
                proof = analyze_puzzle(
                    chain_masks(len(types)), types, threshold_type="C"
                )
                self.assertEqual(proof.minimum_structure_step, target)
                self.assertTrue(proof.singleton_rule_ok)
                self.assertTrue(validate_target(proof, target)[0])
        nine_capacity = {
            item.capacity: item
            for item in analyze_puzzle(
                chain_masks(len(sequences[9])),
                sequences[9],
                threshold_type="C",
            ).capacity_proofs
        }
        self.assertEqual(nine_capacity[7].classification, "all_fail")
        self.assertEqual(nine_capacity[8].classification, "all_fail")

    def test_five_singletons_are_reported_as_diagnostics(self) -> None:
        types = ("C", "D", "E", "F", "G") * 3
        proof = analyze_puzzle(chain_masks(len(types)), types, threshold_type="C")
        self.assertFalse(proof.singleton_rule_ok)
        self.assertEqual(proof.singleton_violation_types, ("C", "D", "E", "F", "G"))

    def test_counts_must_be_multiples_of_three(self) -> None:
        with self.assertRaises(InputValidationError):
            analyze_puzzle((0, 0, 0, 0), ("C", "C", "C", "D"), threshold_type="C")

    def test_zero_recycles_makes_s5_gate_the_first_match_on_click_five(self) -> None:
        types = ("C", "C", "D", "E", "C", "D", "D", "E", "E")
        proof = analyze_puzzle(
            chain_masks(len(types)),
            types,
            threshold_type="C",
        )

        self.assertTrue(proof.recommended_path_constraints_ok)
        self.assertEqual(proof.recommended_pre_gate_recycles, 0)
        self.assertEqual(proof.recommended_first_match_click, 5)
        self.assertEqual(proof.recommended_gate_click, 5)
        self.assertTrue(validate_target(proof, 5)[0])

    def test_recycle_count_is_exact_and_lead_tiles_delay_first_match(self) -> None:
        # CC EEE DD C D: E is recycled on click 5, then C reaches s5 on
        # click 8; the last D completes the full path on click 9.
        types = ("C", "C", "E", "E", "E", "D", "D", "C", "D")
        proof = analyze_puzzle(
            chain_masks(len(types)),
            types,
            threshold_type="C",
            required_pre_gate_recycles=1,
            first_recycle_lead_tiles=2,
        )
        self.assertEqual(proof.recommended_pre_gate_recycles, 1)
        self.assertEqual(proof.recommended_first_match_click, 5)
        self.assertEqual(proof.recommended_gate_click, 8)
        self.assertTrue(validate_target(proof, 5)[0])

        too_much_lead = analyze_puzzle(
            chain_masks(len(types)),
            types,
            threshold_type="C",
            required_pre_gate_recycles=1,
            first_recycle_lead_tiles=3,
        )
        self.assertFalse(too_much_lead.recommended_path_constraints_ok)
        self.assertIn(
            "no_recommended_path_with_recycle_constraints",
            validate_target(too_much_lead, 5)[1],
        )

    def test_recycle_constrained_recommendation_is_itself_capacity_safe(self) -> None:
        # A capacity-safe completion exists, but the first unconstrained order
        # found by the solver overflows at click 13.  The displayed recommended
        # order must satisfy capacity, gate pressure, recycle count, and lead-in
        # together rather than borrowing those facts from different paths.
        types = (
            "D", "D", "E", "E", "E", "C", "C", "C", "F", "G", "H",
            "I", "F", "G", "H", "D", "I", "F", "G", "H", "I",
        )
        masks = (
            tuple(0 if index == 0 else 1 << (index - 1) for index in range(8))
            + tuple(1 << 7 for _ in range(8, len(types)))
        )
        proof = analyze_puzzle(
            masks,
            types,
            threshold_type="C",
            required_pre_gate_recycles=1,
            first_recycle_lead_tiles=2,
            limits=SolverLimits(
                max_states=500_000,
                max_path_count=1_000_000,
            ),
        )

        trace = simulate_order(
            proof.recommended_order,
            types,
            threshold_type="C",
            capacity=7,
        )
        self.assertTrue(trace.complete)
        self.assertIsNone(trace.failed_at)
        self.assertEqual(proof.recommended_gate_click, 8)
        self.assertEqual(proof.recommended_pre_gate_recycles, 1)
        self.assertGreaterEqual(proof.recommended_first_match_click or 0, 5)
        self.assertTrue(
            validate_target(
                proof,
                5,
                required_pre_gate_recycles=1,
                first_recycle_lead_tiles=2,
            )[0]
        )


if __name__ == "__main__":
    unittest.main()
