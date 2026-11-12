import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from puzzle_core.solver import (analyze_puzzle, validate_target,
                                has_direct_gate_triple, minimum_gate_structure_step)


class ZeroRecycleOptimumTests(unittest.TestCase):
    def test_prefilters_respect_zero_recycles(self):
        masks = (0, 1, 2, 4, 8, 8)
        types = tuple('CDCDCD')
        self.assertEqual(minimum_gate_structure_step(masks, types, threshold_type='C'), 3)
        self.assertEqual(minimum_gate_structure_step(
            masks, types, threshold_type='C', required_pre_gate_recycles=0), 5)
        # This graph forces D's triple before C's; it is not evidence of a
        # shorter zero-recycle solution and must not trigger that prefilter.
        forced_masks = (0, 1, 2, 4, 8, 16)
        forced_types = tuple('CDDDCC')
        self.assertTrue(has_direct_gate_triple(forced_masks, forced_types, 'C', below_step=5))
        self.assertFalse(has_direct_gate_triple(forced_masks, forced_types, 'C', below_step=5,
                                               required_pre_gate_recycles=0))

    def test_six_click_recycled_path_does_not_override_five_click_optimum(self):
        for tail in ('', 'E' * 9 + 'F' * 9 + 'G' * 3):
            with self.subTest(tail=bool(tail)):
                # CDCDC reaches C on click 5. CDCDDC first recycles D,
                # then reaches C on click 6 with only 3 tiles in the bag.
                types = tuple('CDCDCD' + tail)
                masks = (0, 1, 2, 4, 8, 8) + tuple(
                    1 << (i - 1) for i in range(6, len(types)))
                proof = analyze_puzzle(masks, types, threshold_type='C',
                                       required_pre_gate_recycles=0,
                                       reject_invalid_target=5)
                self.assertEqual(proof.minimum_structure_step, 5)
                self.assertEqual(proof.recommended_gate_click, 5)
                self.assertEqual(proof.recommended_pre_gate_recycles, 0)
                self.assertTrue(validate_target(proof, 5)[0])


if __name__ == '__main__':
    unittest.main()
