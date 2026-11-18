"""Replay and sample choices on an already materialized, static seven-slot board.

Unlike difficulty.py's heuristic probes, this engine consumes observed clicks
and an external, fitted player policy. It does not generate or guess A/B labels.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import exp, isfinite
from random import Random
from typing import Iterable, Sequence

from .geometry import build_static_geometry, footprint
from .template_reader import LevelTemplate


# Move features: closes a triple, extends a pair, starts a visible triple,
# unlocks a tile, fills the last slot, continues an affordable visible match,
# commits the next-to-last slot without a visible same-type finish, reveals
# a recognizable next match, is spatially near the previous click, closes a
# match amid many visible patterns, starts a match on a crowded board, or
# removes the last or next-to-last cover of a visible target already in the bag.
FEATURE_COUNT = 13
FEATURE_VERSION = "visible-player-choices-v3"
_CAPACITY = 7
_PRESSURE_SLOTS = 5


@dataclass(frozen=True)
class PlayerBoard:
    labels: tuple[int, ...]
    blockers: tuple[int, ...]
    dependents: tuple[tuple[int, ...], ...]
    face_covers: tuple[tuple[int, ...], ...] = ()
    positions: tuple[tuple[int, int], ...] = ()

    @classmethod
    def from_level(cls, level: LevelTemplate, labels: Sequence[int]) -> PlayerBoard:
        if level.tower_member_indices or any(tile.has_debuff_index for tile in level.tiles):
            raise ValueError("动态塔或特殊牌需要客户端状态复现")
        if len(labels) != len(level.tiles) or not labels or any(
            label <= 0 and not -23 <= label <= -12 for label in labels
        ):
            raise ValueError("缺少实际牌面图案")
        if any(count % 3 for count in Counter(labels).values()):
            raise ValueError("实际牌面存在不成组图案")
        graph = build_static_geometry(level)
        footprints = [footprint(tile) for tile in level.tiles]
        face_covers = tuple(
            tuple(
                sum(1 << upper for upper in blockers if cell in footprints[upper])
                for cell in sorted(footprints[index])
            )
            for index, blockers in enumerate(graph.direct_blockers)
        )
        return cls(
            tuple(labels),
            tuple(sum(1 << i for i in blockers) for blockers in graph.direct_blockers),
            tuple(tuple(sorted(values)) for values in graph.direct_blocked_tiles),
            face_covers,
            tuple((tile.x, tile.y) for tile in level.tiles),
        )


class _State:
    def __init__(self, board: PlayerBoard) -> None:
        self.board = board
        self.available = {index for index, mask in enumerate(board.blockers) if not mask}
        self.removed = 0
        self.bag: Counter[int] = Counter()
        self.size = 0
        self.last_click: int | None = None
        self.exposed_cells = [sum(cover == 0 for cover in faces) for faces in board.face_covers]
        self.visible_area = sum(self.exposed_cells)
        self.recognizable_labels = Counter(
            board.labels[index] for index, cells in enumerate(self.exposed_cells) if cells >= 2
        )

    def released(self, index: int) -> tuple[int, ...]:
        return tuple(
            lower for lower in self.board.dependents[index]
            if self.board.blockers[lower] & ~self.removed == 1 << index
        )

    def options(self) -> dict[int, int]:
        """Describe each legal choice using the currently visible board."""
        labels = self.board.labels
        clickable_counts = Counter(labels[index] for index in self.available)
        free = _CAPACITY - self.size
        remaining = ~self.removed
        positions = self.board.positions
        faces = self.board.face_covers
        last_position = positions[self.last_click] if positions and self.last_click is not None else None
        crowded = (self.visible_area if faces else 4 * len(self.available)) >= 64
        diverse = len(self.recognizable_labels or clickable_counts) >= 8
        result = {}
        for index in sorted(self.available):
            label = labels[index]
            count = self.bag[label]
            affordable = clickable_counts[label] >= 3 - count and free >= 3 - count
            released = self.released(index)
            recognizable_match = (
                bool(faces) and count < 2 and any(
                    self.exposed_cells[lower] >= 2
                    and labels[lower] == label
                    for lower in released
                )
            )
            releases_target = False
            approaches_target = False
            if faces and free >= 2:
                for lower in self.board.dependents[index]:
                    if self.exposed_cells[lower] < 2 or self.bag[labels[lower]] == 0:
                        continue
                    cover_count = (self.board.blockers[lower] & remaining).bit_count()
                    releases_target |= cover_count == 1
                    approaches_target |= cover_count == 2
            nearby = False
            if last_position is not None:
                x, y = positions[index]
                nearby = abs(x - last_position[0]) + abs(y - last_position[1]) <= 4
            result[index] = (
                int(count == 2)
                | int(count == 1) << 1
                | int(count == 0 and clickable_counts[label] >= 3) << 2
                | int(bool(released)) << 3
                | int(free == 1 and count != 2) << 4
                | int(free <= 3 and affordable) << 5
                | int(free == 2 and count != 2 and not affordable) << 6
                | int(recognizable_match) << 7
                | int(nearby) << 8
                | int(diverse and count == 2) << 9
                | int(crowded and count == 0 and affordable) << 10
                | int(releases_target) << 11
                | int(approaches_target) << 12
            )
        return result

    def click(self, index: int) -> None:
        label = self.board.labels[index]
        self.available.remove(index)
        self.available.update(self.released(index))
        self.removed |= 1 << index
        self.last_click = index
        if self.exposed_cells:
            self.visible_area -= self.exposed_cells[index]
            self.exposed_cells[index] = 0
            self._change_recognizable(label, -1)
            for lower in self.board.dependents[index]:
                before = self.exposed_cells[lower]
                after = sum(not (cover & ~self.removed) for cover in self.board.face_covers[lower])
                self.visible_area += after - before
                self.exposed_cells[lower] = after
                if before < 2 <= after:
                    self._change_recognizable(self.board.labels[lower], 1)
        if self.bag[label] == 2:
            del self.bag[label]
            self.size -= 2
        else:
            self.bag[label] += 1
            self.size += 1

    def _change_recognizable(self, label: int, delta: int) -> None:
        self.recognizable_labels[label] += delta
        if self.recognizable_labels[label] == 0:
            del self.recognizable_labels[label]

    @property
    def won(self) -> bool:
        return self.removed.bit_count() == len(self.board.labels) and self.size == 0


@dataclass(frozen=True)
class ChoiceObservation:
    # Counts of available tiles with each feature code; one choice per state.
    available: tuple[tuple[int, int], ...]
    selected: int
    high_pressure: bool = False
    bag_size: int = 0
    visible_area: int = 0
    visible_pattern_count: int = 0
    remaining_tiles: int = 0


@dataclass(frozen=True)
class ReplayEvidence:
    evidence: tuple[ChoiceObservation, ...]
    status: str


def observe_replay(
    board: PlayerBoard, steps: Iterable[tuple[int | None, Sequence[int]]],
    observed_bag_sizes: Sequence[int],
    *,
    observed_surface_sizes: Sequence[int] | None = None,
) -> ReplayEvidence:
    """Keep only the verifiable prefix before any unaudited state change.

    Replays use a 1-based index. Require observed bag size at each click to
    corroborate the reconstructed state; a mismatch removes the affected click
    from the training set. Do not treat the rest of an ambiguous replay as loss.
    """
    state = _State(board)
    evidence: list[ChoiceObservation] = []
    for action, targets in steps:
        if action != 1:
            return ReplayEvidence(tuple(evidence), "non_click_action")
        if len(targets) != 1 or targets[0] - 1 not in state.available:
            return ReplayEvidence(tuple(evidence), "click_not_reproducible")
        if len(evidence) >= len(observed_bag_sizes):
            return ReplayEvidence(tuple(evidence), "missing_bag_observation")
        index = targets[0] - 1
        options = state.options()
        distribution = Counter(options.values())
        high_pressure = state.size >= _PRESSURE_SLOTS
        choice = ChoiceObservation(
            tuple(sorted(distribution.items())), options[index], high_pressure,
            bag_size=state.size,
            visible_area=state.visible_area if state.exposed_cells else 4 * len(options),
            visible_pattern_count=len(state.recognizable_labels) if state.exposed_cells
            else len({board.labels[tile] for tile in options}),
            remaining_tiles=len(board.labels) - state.removed.bit_count(),
        )
        state.click(index)
        if observed_bag_sizes[len(evidence)] != state.size:
            return ReplayEvidence(tuple(evidence), "bag_mismatch")
        if observed_surface_sizes is not None and (
            len(evidence) >= len(observed_surface_sizes)
            or observed_surface_sizes[len(evidence)] != len(state.available)
        ):
            return ReplayEvidence(tuple(evidence), "surface_mismatch")
        evidence.append(choice)
        if state.won:
            return ReplayEvidence(tuple(evidence), "won")
        if state.size >= _CAPACITY:
            return ReplayEvidence(tuple(evidence), "full")
    return ReplayEvidence(tuple(evidence), "incomplete")


@dataclass(frozen=True)
class PlayerPolicy:
    weight: float
    coefficients: tuple[float, ...]
    pressure_coefficients: tuple[float, ...] | None = None

    def priorities(self, *, high_pressure: bool = False) -> tuple[float, ...]:
        coefficients = (
            self.pressure_coefficients if high_pressure and self.pressure_coefficients is not None
            else self.coefficients
        )
        if len(coefficients) != FEATURE_COUNT or any(
            not isfinite(value) or abs(value) > 20 for value in coefficients
        ):
            raise ValueError("玩家策略权重不合法")
        priorities = [1.0]
        for value in coefficients:
            multiplier = exp(value)
            priorities += [priority * multiplier for priority in priorities]
        return tuple(priorities)


def fit_policy(evidence: Iterable[ChoiceObservation], *, weight: float) -> PlayerPolicy:
    """Fit a regularized multinomial choice model against legal alternatives."""
    observed = [0] * (1 << FEATURE_COUNT)
    opportunities: Counter[tuple[tuple[int, int], ...]] = Counter()
    for choice in evidence:
        if len(choice.available) > 1:
            observed[choice.selected] += 1
            opportunities[choice.available] += 1
    total = sum(observed)
    if not total:
        raise ValueError("没有可区分不同选择的点击证据")
    codes_by_feature = tuple(
        tuple(code for code in range(len(observed)) if code & (1 << i))
        for i in range(FEATURE_COUNT)
    )
    coefficients = [0.0] * FEATURE_COUNT
    # Binary features keep the log likelihood gradient bounded. Regularization
    # prevents a rare, nearly deterministic choice from overpowering all others.
    for _ in range(160):
        priorities = PlayerPolicy(weight, tuple(coefficients)).priorities()
        expected = [0.0] * len(observed)
        for counts, frequency in opportunities.items():
            denominator = sum(count * priorities[code] for code, count in counts)
            for code, count in counts:
                expected[code] += frequency * count * priorities[code] / denominator
        step = [
            0.8 * (sum(observed[code] - expected[code] for code in codes) / total
                   - 0.005 * coefficients[i])
            for i, codes in enumerate(codes_by_feature)
        ]
        for i, delta in enumerate(step):
            coefficients[i] += delta
        if max(map(abs, step)) < 1e-4:
            break
    return PlayerPolicy(weight, tuple(coefficients))


def simulate_players(
    board: PlayerBoard, policies: Sequence[PlayerPolicy], *, trials: int = 1000,
    seed: int = 0,
) -> int:
    """Count seven-slot naked wins; keep a player's policy stable for a run."""
    if trials <= 0 or not policies or any(
        not isfinite(policy.weight) or policy.weight <= 0 for policy in policies
    ):
        raise ValueError("需要有效的玩家样本分布和模拟次数")
    rng = Random(seed)
    priorities = [(policy.priorities(), policy.priorities(high_pressure=True)) for policy in policies]
    mix = [policy.weight for policy in policies]
    wins = 0
    for regular_priority, pressure_priority in rng.choices(priorities, weights=mix, k=trials):
        state = _State(board)
        while state.available and state.size < _CAPACITY:
            priority = pressure_priority if state.size >= _PRESSURE_SLOTS else regular_priority
            options = state.options()
            index = rng.choices(tuple(options), weights=[priority[code] for code in options.values()])[0]
            state.click(index)
        wins += state.won
    return wins
