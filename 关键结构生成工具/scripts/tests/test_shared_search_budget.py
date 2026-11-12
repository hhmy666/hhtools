"""Work-item budget contracts; expensive proof search is controlled at its boundary."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_job_service import _chain_bytes, _config
from puzzle_core.errors import NoCandidateError, OperationCancelledError
from puzzle_core.search import SearchResult, search_candidates
from services.job_service import generate_batch, generation_seed_material_for_item
from services.profile_service import Profile


class SharedSearchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = root / 'chain001_m.bytes'
        self.source.write_bytes(_chain_bytes('chain001', 15))
        self.profile = Profile('p', 'test', None, None, str(root))

    def run_batch(self, config, **kwargs):
        return generate_batch(config, self.profile,
                              file_inputs={'chain001': self.source}, **kwargs)

    def test_real_search_stops_before_ceiling_when_target_is_met(self):
        result = self.run_batch(_config(['chain001']))
        self.assertEqual(len(result.items[0].candidates), 1)
        self.assertLess(result.items[0].attempts_completed, 30)

    def test_empty_search_spends_one_shared_budget_not_one_per_candidate(self):
        def miss(*args, **kwargs):
            raise NoCandidateError('未命中', evidence={
                'attempt_budget': args[2].attempt_budget,
                'attempt_count': kwargs['attempt_count'], 'rejection_counts': {}})
        with patch('services.job_service.search_candidates', side_effect=miss):
            result = self.run_batch(_config(['chain001'], candidate_count=5))
        self.assertEqual(result.items[0].attempts_completed, 30)
        self.assertEqual(result.items[0].status, 'failed')
        self.assertEqual(result.items[0].as_dict()['search_budget']['scope'], 'per_work_item')

    def test_draw_cursor_and_seed_provenance_survive_misses(self):
        windows = []
        def first_misses(*args, **kwargs):
            windows.append(kwargs['attempt_start_index'])
            if len(windows) == 1:
                raise NoCandidateError('未命中', evidence={'attempt_budget': 30})
            return search_candidates(*args, **kwargs)
        config = _config(['chain001'])
        with patch('services.job_service.search_candidates', side_effect=first_misses):
            result = self.run_batch(config)
        item = result.items[0]
        self.assertEqual(windows, list(range(len(windows))))
        self.assertEqual(item.status, 'success', item.error)
        candidate = item.candidates[0].candidate
        self.assertEqual(candidate.candidate_index, 1)
        self.assertEqual(candidate.candidate_seed_ordinal, candidate.attempt_index + 1)
        seed = generation_seed_material_for_item(config, config.work_items[0], item.source,
                    candidate_index=candidate.candidate_seed_ordinal)
        self.assertEqual(seed.sha256, candidate.candidate_seed_sha256)

    def test_cancellation_during_validation_replays_only_uncommitted_draw(self):
        fixture = self.run_batch(_config(['chain001'])).items[0].candidates[0]
        draws = []
        def hit(*args, **kwargs):
            ordinal = kwargs['attempt_start_index']
            draws.append(ordinal)
            candidate = replace(fixture.candidate, candidate_id=f'hit-{ordinal}',
                candidate_index=kwargs['candidate_index'], structure_signature=f'layout-{ordinal}',
                experience_signature=f'experience-{ordinal}')
            return SearchResult((candidate,), 1, {}, kwargs['candidate_seed'])
        config = _config(['chain001'], candidate_count=2)
        with patch('services.job_service.search_candidates', side_effect=hit), patch(
                'services.job_service.validate_candidate', side_effect=[
                    fixture.validation, OperationCancelledError('取消'), fixture.validation]):
            first = self.run_batch(config)
            resumed = self.run_batch(config, previous_result=first)
        self.assertEqual(first.items[0].shared_attempts_completed, 1)
        self.assertEqual(len(first.items[0].candidates), 1)
        self.assertEqual(draws, [0, 1, 1])
        self.assertEqual(resumed.items[0].status, 'success')
        self.assertEqual(resumed.items[0].candidates[0].candidate.candidate_id, 'hit-0')

    def test_partial_results_survive_and_resume_only_new_attempts(self):
        fixture = self.run_batch(_config(['chain001'])).items[0].candidates[0]
        seeds = []
        def controlled(*args, **kwargs):
            seeds.append(kwargs['candidate_seed'])
            n = len(seeds)
            if n not in (1, 3, 32):
                raise NoCandidateError('未命中', evidence={
                    'attempt_budget': args[2].attempt_budget,
                    'attempt_count': kwargs['attempt_count'], 'rejection_counts': {}})
            candidate = replace(fixture.candidate, candidate_id=f'hit-{n}',
                                candidate_index=kwargs['candidate_index'],
                                structure_signature=f'layout-{n}', experience_signature=f'exp-{n}')
            return SearchResult((candidate,), 1, {}, kwargs['candidate_seed'])
        config = _config(['chain001'], candidate_count=3)
        snapshots = []
        with patch('services.job_service.search_candidates', side_effect=controlled), patch(
            'services.job_service.validate_candidate', return_value=fixture.validation):
            first = self.run_batch(config, progress_callback=snapshots.append)
            same = self.run_batch(config, previous_result=first)
            self.assertEqual(len(seeds), 30)
            resumed = self.run_batch(config, previous_result=same, attempt_budget_multiplier=2)
        self.assertEqual(first.items[0].status, 'partial')
        self.assertEqual(len(first.items[0].candidates), 2)
        self.assertTrue(any(len(s.items[0].candidates) == 1 for s in snapshots))
        self.assertEqual(resumed.items[0].status, 'success')
        self.assertEqual(resumed.items[0].attempts_completed, 32)
        self.assertEqual(len(seeds), len(set(seeds)))
        self.assertEqual([c.candidate.candidate_id for c in resumed.items[0].candidates],
                         ['hit-1', 'hit-3', 'hit-32'])


if __name__ == '__main__':
    unittest.main()
