from __future__ import annotations

import cProfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from puzzle_core import difficulty, search


class SearchCachePerformanceTests(unittest.TestCase):
    def test_bootstrap_cache_is_bounded_and_large_trials_remain_exact(self):
        difficulty._paired_bootstrap_draws.cache_clear()
        for size in range(1, 19):
            difficulty._paired_bootstrap_interval([True] * size, [False] * size)
        retained = difficulty._paired_bootstrap_draws.cache_info().currsize
        self.assertLessEqual(retained, 16)
        result = difficulty._paired_bootstrap_interval([True] * 65, [False] * 65)
        self.assertEqual(result['paired_trials'], 65)
        self.assertEqual(result['interval95'], [1.0, 1.0])
        self.assertEqual(result['mean_pass_difference'], 1.0)
        self.assertEqual(difficulty._paired_bootstrap_draws.cache_info().currsize,
                         retained)

    def test_bootstrap_reuses_draws_without_changing_interval(self):
        profiler = cProfile.Profile()
        profiler.enable()
        for _ in range(3):
            result = difficulty._paired_bootstrap_interval(
                [True, False, True, True, False],
                [False, True, True, False, False],
            )
        profiler.disable()
        self.assertEqual(result['interval95'], [-0.4, 0.8])
        self.assertEqual(result['mean_pass_difference'], 0.2)
        hashes = sum(entry.callcount for entry in profiler.getstats()
                     if entry.code is difficulty._stable_seed.__code__)
        self.assertLessEqual(hashes, 256 * 5)

    def test_selection_evaluates_each_candidate_once_per_call(self):
        candidates = [SimpleNamespace(
            structure_signature=str(i), experience_signature=str(i),
            attempt_index=i, attempt_seed=i,
            experience_analysis={'metrics': {
                'structure_count': 10, 'initial_clickable_count': i,
            }},
        ) for i in range(10)]
        for select in (search._select_experience_diverse,
                       search._select_difficulty_evaluation_pool):
            with self.subTest(select=select.__name__):
                profiler = cProfile.Profile()
                profiler.enable()
                selected = select(candidates, 4)
                profiler.disable()
                self.assertEqual([c.structure_signature for c in selected],
                                 ['0', '8', '4', '2'])
                for function in (search._experience_vector,
                                 search._experience_mechanism_fingerprint):
                    calls = sum(entry.callcount for entry in profiler.getstats()
                                if entry.code is function.__code__)
                    self.assertLessEqual(calls, len(candidates))
                # A later search may reuse objects with refreshed simulation
                # evidence. No selection cache may survive this invocation.
                candidates[9].experience_analysis['metrics']['initial_clickable_count'] = -10
                refreshed = select(candidates, 4)
                self.assertEqual([c.structure_signature for c in refreshed],
                                 ['0', '8', '9', '4'])
                candidates[9].experience_analysis['metrics']['initial_clickable_count'] = 9


if __name__ == '__main__':
    unittest.main()
