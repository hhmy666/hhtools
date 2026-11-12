from __future__ import annotations

import cProfile
import hashlib
import json
import sys
import unittest
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.difficulty import _action_priority, _simulate_path, _stable_seed  # noqa: E402


STRATEGIES = ("random", "greedy", "local_optimal", "unlock", "adversarial")


class CountingBlockerMasks(Sequence[int]):
    """Real sequence that records full-board reads, without timing noise."""

    def __init__(self, values: tuple[int, ...]) -> None:
        self.values = values
        self.full_scans = 0

    def __len__(self) -> int:
        return len(self.values)

    def __getitem__(self, index: int) -> int:
        return self.values[index]

    def __iter__(self) -> Iterator[int]:
        self.full_scans += 1
        return iter(self.values)


class DifficultyPerformanceTests(unittest.TestCase):
    def test_priority_cache_preserves_original_identifier_value_types(self) -> None:
        # The original hash stringifies identifiers. Python considers True
        # equal to 1 and False equal to 0, but their original hashes differ.
        cases = (
            (1, 0, 9542067559630410478),
            (True, 0, 5380558721525747586),
            (1, False, 6768059017288866712),
            (1, 0.0, 13317015018802816209),
        )
        for duplicate_index, trial_id, expected in cases:
            with self.subTest(duplicate_index=repr(duplicate_index), trial_id=repr(trial_id)):
                self.assertEqual(
                    _action_priority(
                        template_sha="priority-value-type-fixture",
                        duplicate_index=duplicate_index,
                        trial_id=trial_id,
                        decision_step=0,
                        tile_index=7,
                    ),
                    expected,
                )

    def test_paired_paths_reuse_common_action_priority_hashes(self) -> None:
        profiler = cProfile.Profile()
        profiler.enable()
        source = _simulate_path(
            (0,) * 24,
            ("A:0",) * 24,
            strategy="random",
            template_sha="paired-priority-cache-fixture",
        )
        candidate = _simulate_path(
            (0,) * 24,
            ("S:C",) * 24,
            strategy="random",
            template_sha="paired-priority-cache-fixture",
        )
        profiler.disable()

        self.assertTrue(source.complete)
        self.assertTrue(candidate.complete)
        self.assertEqual(source.clicked_source_indices, candidate.clicked_source_indices)
        hashes = sum(
            entry.callcount
            for entry in profiler.getstats()
            if entry.code is _stable_seed.__code__
        )
        # Each of the 300 (step, source index) pairs needs one stable hash;
        # the candidate reuses the exact common priority instead of rehashing.
        self.assertLessEqual(hashes, 300)

    def test_each_path_reads_the_whole_blocker_board_only_once(self) -> None:
        # Rescanning all 24 tiles for every hypothetical action is the
        # calibration hotspot. All strategies still click the complete board.
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                blockers = CountingBlockerMasks((0,) * 24)
                outcome = _simulate_path(
                    blockers,
                    ("A:0",) * 24,
                    strategy=strategy,
                    template_sha="full-board-read-fixture",
                )

                self.assertTrue(outcome.complete)
                self.assertEqual(outcome.click_count, 24)
                self.assertEqual(outcome.expanded_states, 300)
                self.assertLessEqual(blockers.full_scans, 1)

    def test_complete_path_outputs_match_pre_optimization_goldens(self) -> None:
        # Digests cover every outcome field, including the full reveal trace,
        # source click order, structure releases, matches and failure timing.
        # Captured from the original whole-board scan on these literal boards.
        fixtures = (
            (
                "branched",
                (0, 0, 0, 1, 2, 4, 9, 18, 36, 64, 128, 256, 1, 2, 4, 192, 384, 320),
                (
                    "S:C", "S:C", "S:C", "A:0", "B:0", "A:1",
                    "S:D", "S:D", "S:D", "A:0", "B:0", "A:1",
                    "A:0", "B:0", "A:1", "B:1", "B:1", "B:1",
                ),
                (
                    "2e5f2ebd273092fcf6faed47c150ccb9bf15324a1eca4cf0bda931a2f0f80bc1",
                    "7f951d026ab1060cac79168f7260ce9f6a75f65b1c081dcf6b115dfecfe716c2",
                    "927616c1e0638dc78d8562bdb183700a72241b3f86b63ad2927dc7a80dd10320",
                    "dddba29f163a0783eb49c86b76af5302ae8aebbc542340140ae753a546b8014c",
                    "be99c551f57de55deabfc03b5e10f3badd8b08bcbdfe8d88c043437715de9990",
                ),
            ),
            (
                "cycle_and_missing_blocker",
                (0, 0, 0, 16, 8, 256),
                ("S:C", "S:C", "S:C", "A:0", "A:0", "A:0"),
                ("d50e397a6d6331d9a82538914504c53cabf6c244c7520ffce9377d52b231b9b2",) * 5,
            ),
            (
                "full_bag",
                (0,) * 9,
                ("S:C", "S:D", "S:E", "A:0", "A:1", "B:0", "B:1", "T:9", "T:10"),
                ("7afc998647fc8fdc4a9b0a0a39f03e45f7f5816a7689469016486e184d20ec96",) * 5,
            ),
            (
                "empty",
                (),
                (),
                ("286b45975bd448a43a9fb689684adc90fa5fa247c6d215903579189c3bd84590",) * 5,
            ),
        )
        for name, blockers, labels, digests in fixtures:
            for strategy, expected in zip(STRATEGIES, digests):
                with self.subTest(board=name, strategy=strategy):
                    outcome = _simulate_path(
                        blockers,
                        labels,
                        strategy=strategy,
                        source_indices=tuple(range(101, 101 + len(labels))),
                        template_sha="performance-regression-fixture",
                        duplicate_index=3,
                        trial_id=7,
                    )
                    payload = json.dumps(
                        asdict(outcome),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.assertEqual(hashlib.sha256(payload).hexdigest(), expected)


if __name__ == "__main__":
    unittest.main()
