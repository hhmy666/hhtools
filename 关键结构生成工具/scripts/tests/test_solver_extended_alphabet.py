from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from puzzle_core import solver
from puzzle_core.errors import CandidateValidationError, OperationCancelledError, ProofLimitExceeded


class ExtendedAlphabetProofTests(unittest.TestCase):
    def test_six_type_capacity_fallback_retains_exact_safe_path(self):
        types = tuple('CDDCC' + 'CCEEFFCDEF' + 'GGG' * 4 + 'HHH')
        masks = tuple([0] + [1 << (i - 1) for i in range(1, 5)] + [16] * 10
                      + [32767] + [1 << (i - 1) for i in range(16, 30)])
        self.assertIsNone(solver._analyze_small_alphabet(
            masks, types, 'C', (7, 8), solver.SolverLimits(), 2))
        proof = solver.analyze_puzzle(masks, types, threshold_type='C')
        self.assertTrue(solver.validate_target(proof, 5)[0])
        path = solver.simulate_order(proof.recommended_order, types,
                                     threshold_type='C', capacity=7)
        self.assertTrue(path.complete)
        self.assertEqual(path.gate_structure_step, 5)
        self.assertEqual(path.literal_first_match_click, 5)
        self.assertTrue(any(item.failure_reachable for item in proof.capacity_proofs))

    def test_convolution_checks_cancellation_before_alphabet_expansion(self):
        checks = []
        def cancel(phase):
            checks.append(phase)
            raise OperationCancelledError('cancel convolution')
        types = tuple('CCCDDDEEEFFFGGGHHH')
        with self.assertRaises(OperationCancelledError), solver.proof_checkpoints(cancel):
            solver._analyze_small_alphabet((0,) * len(types), types, 'C', (7, 8),
                                           solver.SolverLimits(), 2, 5)
        self.assertEqual(checks, ['assignment_attempt'])
        self.assertIsNone(solver._proof_checkpoint.get())

    def test_six_type_waves_prove_s5_despite_raw_geometry_budget(self):
        types = tuple('CDCDCD' + 'E' * 21 + 'F' * 21 + 'G' * 21 + 'H' * 21)
        masks = tuple(
            0 if i == 0 else 1 << (i - 1) if i < 6 else
            (1 << 6) - 1 if i < 27 else
            ((1 << 21) - 1) << (6 + ((i - 6) // 21 - 1) * 21)
            for i in range(len(types))
        )
        limits = solver.SolverLimits(max_states=50_000, max_path_count=1000)
        self.assertTrue(solver.proof_state_budget_exceeded(masks, limits.max_states))
        proof = solver.analyze_puzzle(masks, types, threshold_type='C', limits=limits)
        self.assertTrue(solver.validate_target(proof, 5)[0])
        self.assertEqual(proof.threshold_steps, (5,))
        self.assertEqual(proof.literal_first_match_clicks, (5,))
        self.assertTrue(proof.singleton_rule_ok)
        self.assertEqual(proof.recommended_pre_gate_recycles, 0)
        self.assertTrue(all(p.classification == 'all_safe' for p in proof.capacity_proofs))

    def test_residue_convolution_is_charged_to_proof_budget(self):
        # 37 ideal subproblems hide hundreds of residue combinations. The
        # latter must also be bounded before expanding beyond five types.
        types = tuple('CCCDDDEEEFFFGGGHHH')
        with self.assertRaises(ProofLimitExceeded):
            solver._analyze_small_alphabet(
                (0,) * len(types), types, 'C', (7, 8),
                solver.SolverLimits(max_states=100), 2, 5,
            )

    def test_extended_alphabet_matches_independent_generic_proofs(self):
        rng = random.Random(62026)
        compressed_count = 0
        fallback_count = 0
        for type_count in (6, 7, 9, 12):
            for case in range(3):
                types = list(solver.STRUCTURE_TYPES[:type_count] * 3)
                rng.shuffle(types)
                types = tuple(types)
                original = tuple(rng.randrange(1 << i) for i in range(len(types)))
                permutation = list(range(len(types)))
                rng.shuffle(permutation)
                masks_list = [0] * len(types)
                for index, mask in enumerate(original):
                    masks_list[permutation[index]] = sum(
                        1 << permutation[j] for j in range(len(types)) if mask & (1 << j))
                masks = tuple(masks_list)
                capacities = (2 * type_count + 1,)
                limits = solver.SolverLimits(max_path_count=17 if case == 0 else 10**15)
                expected = solver._theoretical_proof(masks, types, 'C', limits, 0, 2)
                actual = solver._analyze_small_alphabet(masks, types, 'C', capacities, limits, 2)
                if actual is None:
                    fallback_count += 1
                    actual = solver.analyze_puzzle(masks, types, threshold_type='C',
                                                   capacities=capacities, limits=limits)
                else:
                    compressed_count += 1
                self.assertEqual(actual.theoretical_complete_path_count, expected[0])
                self.assertEqual(actual.path_count_capped, expected[1])
                self.assertEqual(actual.threshold_steps, expected[2])
                self.assertEqual(actual.literal_first_match_clicks, expected[3])
                self.assertEqual(actual.singleton_rule_ok, expected[5] is None)
                self.assertEqual(actual.recommended_path_constraints_ok, expected[8])
                capacity = solver._capacity_proof(masks, types, 'C', capacities[0], limits)
                for field in ('complete_path_count', 'path_count_capped', 'complete_exists',
                              'failure_reachable', 'instantaneous_peak', 'settled_peak'):
                    self.assertEqual(getattr(actual.capacity_proofs[0], field),
                                     getattr(capacity, field), (type_count, case, field))
                self.assertTrue(solver.simulate_order(actual.recommended_order, types,
                                threshold_type='C', capacity=capacities[0]).complete)
        self.assertGreater(compressed_count, 0)
        self.assertGreater(fallback_count, 0)

    def test_six_types_still_reject_wrong_optimum_not_singletons(self):
        types = tuple('CCCDDDEEEFFFGGGHHH' * 2)
        with self.assertRaises(CandidateValidationError) as raised:
            solver.analyze_puzzle((0,) * len(types), types, threshold_type='C',
                                  reject_invalid_target=5)
        self.assertEqual(raised.exception.detail.evidence['reason'], 'gate_step_mismatch')

    def test_six_type_safe_geometry_does_not_accept_wrong_gate_step(self):
        types = tuple('CCCDDDEEEFFFGGGHHH' * 2)
        masks = tuple(0 if i == 0 else 1 << (i - 1) for i in range(len(types)))
        with self.assertRaises(CandidateValidationError) as raised:
            solver.analyze_puzzle(masks, types, threshold_type='C',
                                  reject_invalid_target=5)
        self.assertEqual(raised.exception.detail.evidence['reason'], 'gate_step_mismatch')


if __name__ == '__main__':
    unittest.main()
