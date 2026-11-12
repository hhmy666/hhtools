import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from puzzle_core.search import _find_type_assignment
from puzzle_core.solver import SolverLimits, validate_target


class FixedOrderRandom(random.Random):
    def shuffle(self, values):
        pass

    def randrange(self, *args):
        return 0


class ExtendedAssignmentTests(unittest.TestCase):
    def test_six_type_assignment_reaches_compressed_proof(self):
        n = 90
        masks = tuple(0 if i == 0 else 1 << (i - 1) if i < 6 else
                      (1 << 6) - 1 if i < 27 else
                      ((1 << 21) - 1) << (6 + ((i - 6) // 21 - 1) * 21)
                      for i in range(n))
        result = _find_type_assignment(
            masks, tuple('CDEFGH'), (1, 1, 7, 7, 7, 7), 5, 'C',
            FixedOrderRandom(1), 1, SolverLimits(max_states=50_000, max_path_count=1000),
        )
        self.assertIsNotNone(result)
        self.assertTrue(validate_target(result[2], 5)[0])
