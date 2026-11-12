"""A bad legal choice must not invalidate a proven optimal solution."""
import sys
import random
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from puzzle_core.solver import analyze_puzzle, validate_target
from puzzle_core.search import _find_type_assignment, _intrinsic_targets
from puzzle_core.solver import SolverLimits


class OptimalPathAcceptanceTests(unittest.TestCase):
    def test_auto_and_reveal_modes_do_not_relax_requested_optimum(self):
        for mode in ('auto', 'board_reveal', 'intrinsic_gate', 'hybrid'):
            for attempt in (0, 1):
                self.assertEqual(_intrinsic_targets(5, mode, attempt), (5,))

    def test_search_keeps_s5_assignment_despite_bad_singleton_path(self):
        class ChosenOrder(random.Random):
            def randrange(self, *args):
                return 0

            def shuffle(self, values):
                if len(values) == 18:
                    values[:] = list('CDCDCD' + 'EEEFFFGGGHHH')

        masks = tuple(0 if i in {0, 6, 9, 12, 15} else 1 << (i - 1)
                      for i in range(18))
        result = _find_type_assignment(masks, tuple('CDEFGH'), (1,) * 6,
                                      5, 'C', ChosenOrder(), 1, SolverLimits())
        self.assertIsNotNone(result)
        self.assertFalse(result[2].singleton_rule_ok)
        self.assertTrue(validate_target(result[2], 5)[0])

    def test_s5_allows_bad_paths_with_five_singletons(self):
        for tail_count in (3, 15):
            with self.subTest(tail_count=tail_count):
                types = tuple('CDCDCD' + 'EEEFFFGGG' + 'H' * tail_count)
                # C's third tile requires CDCDC, exactly five clicks.
                # Other chains can be ignored or played first: the latter
                # allows five singleton types but does not erase the optimum.
                starts = {0, 6, 9, 12, 15}
                masks = tuple(0 if i in starts else 1 << (i - 1)
                              for i in range(len(types)))
                proof = analyze_puzzle(masks, types, threshold_type='C',
                                       reject_invalid_target=5)
                self.assertEqual(proof.minimum_structure_step, 5)
                self.assertFalse(proof.singleton_rule_ok)
                self.assertEqual(proof.recommended_first_match_click, 5)
                self.assertTrue(validate_target(proof, 5)[0])
                self.assertFalse(validate_target(proof, 4)[0])
                self.assertFalse(validate_target(proof, 6)[0])


if __name__ == '__main__':
    unittest.main()
