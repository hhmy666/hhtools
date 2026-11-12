import random
import sys
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from puzzle_core import search
from puzzle_core.solver import SolverLimits, validate_target


class GuidedAssignmentTests(unittest.TestCase):
    def test_layer_windows_cover_legal_selections_without_cross_span_sampling(self):
        level = SimpleNamespace(tiles=tuple(SimpleNamespace(layer=z) for z in range(1, 8) for _ in range(3)))
        pool = tuple(range(21))
        windows = search._layer_window_pools(level, pool, 5, 18)
        self.assertEqual(len(windows), 2)
        for window in windows:
            layers = [level.tiles[i].layer for i in window]
            self.assertLessEqual(max(layers) - min(layers), 5)
            self.assertGreaterEqual(len(window), 18)
        self.assertTrue(set(range(18)).issubset(windows[0]))
        self.assertTrue(set(range(3, 21)).issubset(windows[1]))
        self.assertEqual(search._layer_window_pools(level, pool, None, 18), (pool,))
        self.assertEqual(search._layer_window_pools(level, pool, 5, 19), ())

    def test_real_five_type_geometry_has_exact_s5_with_two_layer_spans(self):
        masks = (1611661952, 2686453632, 26847744512, 109538470912, 876240547840,
                 1752346877952, 2199024041984, 540016640, 2097152, 15728640,
                 2174746624, 4311744512, 17293115392, 34452013056, 68736253952,
                 275112787968, 550225575936, 1099780063232, 2199425908736, 0,
                 1610612736, 2684354560, 26843545600, 53687091200, 109521666048,
                 438086664192, 876173328384, 3023656976384, 3848290697216) + (0,) * 13
        layers = (1,) * 7 + (2,) * 13 + (3,) * 9 + (4,) * 13
        result = search._guided_zero_recycle_assignment(
            masks, tuple('GMKLI'), (2, 4, 2, 3, 3), 5, None,
            random.Random(96), SolverLimits(), layers, 2, None,
        )
        self.assertIsNotNone(result)
        assigned, gate, proof = result
        self.assertEqual(Counter(assigned), {'G': 6, 'M': 12, 'K': 6, 'L': 9, 'I': 9})
        for label in set(assigned):
            occupied = [z for z, value in zip(layers, assigned) if value == label]
            self.assertGreaterEqual(max(occupied) - min(occupied), 2)
        self.assertEqual(proof.minimum_structure_step, 5)
        self.assertEqual(proof.recommended_pre_gate_recycles, 0)
        self.assertTrue(validate_target(proof, 5)[0])


if __name__ == '__main__':
    unittest.main()
