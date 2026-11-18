"""Player-policy rollouts with legacy A/B dealing and pre-click tower exchanges."""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from math import exp, isfinite, log
from pathlib import Path
from random import Random
from statistics import fmean

if __package__:
    from .sim_core.geometry import A_TILE, B_TILE, build_geometry, footprint
    from .sim_core.legacy_materialization import (
        LegacyRandom, materialize_legacy_ab,
        parse_client_tile_config, parse_level_tile_config, select_legacy_pattern_pool,
    )
    from .sim_core.online_player_simulation import FEATURE_COUNT, FEATURE_VERSION, PlayerBoard, _State
    from .config_paths import discover_config, require_config
else:
    from sim_core.geometry import A_TILE, B_TILE, build_geometry, footprint
    from sim_core.legacy_materialization import (
        LegacyRandom, materialize_legacy_ab,
        parse_client_tile_config, parse_level_tile_config, select_legacy_pattern_pool,
    )
    from sim_core.online_player_simulation import FEATURE_COUNT, FEATURE_VERSION, PlayerBoard, _State
    from config_paths import discover_config, require_config

CONFIG = discover_config()
CONFIG_FILES = ("config_client_game", "config_client_level_fuuu_dup", "config_client_tile", "config_client_level_tile")
QUANTILES = (0, .1, .25, .5, .75, .9, .95, .99, 1)
ASSETS = (502, 503, 506, 508, 515)
STRUCTURE_LABELS = range(-23, -11)


def _pipe_numbers(value):
    return tuple(int(part) for part in value.split("|"))


def _local_config(config=None):
    config = require_config(config)
    signature = tuple((config / f"{name}.csv").stat().st_mtime_ns for name in CONFIG_FILES)
    return _read_config(config, signature)


@lru_cache(maxsize=1)
def _read_config(config, signature):
    def rows(name):
        with (config / f"{name}.csv").open(encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))

    game = {row["Key"]: row["Value"] for row in rows("config_client_game")}
    fuuu = {}
    for row in rows("config_client_level_fuuu_dup"):
        try:
            fuuu[int(row["LevelFuuuDup"])] = (
                _pipe_numbers(row["TowerHelpGameProgressParam"]),
                _pipe_numbers(row["TowerHelpBagTileParam"]),
            )
        except (ValueError, KeyError, AttributeError):
            continue

    catalog = parse_client_tile_config((config / "config_client_tile.csv").read_text(encoding="utf-8-sig"))
    ranges = parse_level_tile_config((config / "config_client_level_tile.csv").read_text(encoding="utf-8-sig"))
    return (
        (_pipe_numbers(game["tower_help_game_progress_param"]), _pipe_numbers(game["tower_help_bag_tile_param"])),
        fuuu, ranges, catalog, game.get("tower_help_random_switch", "T") == "T",
        int(game.get("hide_tile_help_type", 1)), max(1, min(7, int(game.get("coreplay_stage_num", 7)))),
    )


def _deal(board, params, level_index, catalog, ranges):
    slots = [tile.tile_id for tile in board.tiles]
    a = [i for i, label in enumerate(slots) if label == A_TILE]
    b = [i for i, label in enumerate(slots) if label == B_TILE]
    if len(a) % 3 or len(b) % 3:
        raise ValueError(f"{board.template_id} 的 A/B 占位数量无法分成完整三消组。")
    if a or b:
        pool = select_legacy_pattern_pool(
            level_index=level_index, level_tile_rows=ranges,
            available_tile_ids=catalog.available_tile_ids, normal_tile_ids=catalog.normal_tile_ids,
            dup_a_types=params.a_kinds if a else 0, dup_b_types=params.b_kinds if b else 0,
            dup_seed=params.seed,
        )
        deal = materialize_legacy_ab(
            pattern_pool=pool.pattern_pool, a_positions=a, b_positions=b,
            dup_a_types=params.a_kinds if a else 0, dup_b_types=params.b_kinds if b else 0,
            dup_seed=params.seed,
        )
        for index, label in deal.a_assignments + deal.b_assignments:
            slots[index] = label
    return _finish_labels(board, slots, catalog)


def _finish_labels(board, slots, catalog):
    if len(slots) != len(board.tiles):
        raise ValueError(f"{board.template_id} 的模拟牌面张数与模板不一致。")
    if any(label not in STRUCTURE_LABELS and not 1000 <= label < 9000 for label in slots):
        raise ValueError(f"{board.template_id} 包含当前模拟未覆盖的牌型。")
    structure_types = list(dict.fromkeys(label for label in slots if label in STRUCTURE_LABELS))
    if structure_types:
        used = set(slots)
        available = [label for label in catalog.normal_tile_ids if label not in used]
        if not available:
            available = list(catalog.normal_tile_ids)
        if not available:
            raise ValueError(f"{board.template_id} 的结构块在客户端基本图案池中没有可用图案。")
        # With no configured icon-specific player coefficients, the ordering
        # of the selected icons is immaterial to play. Keep the same
        # group identity and cycle the pool only when it is exhausted.
        remap = {label: available[index % len(available)] for index, label in enumerate(structure_types)}
        slots = [remap.get(label, label) for label in slots]
    if not slots or any(count % 3 for count in Counter(slots).values()):
        raise ValueError(f"{board.template_id} 初始化后有图案无法组成完整三消组。")
    return list(slots)


@dataclass
class OldLuck:
    rng: LegacyRandom
    progress_factors: tuple[int, ...]
    bag_factors: tuple[int, ...]
    duplicate_luck: int
    dda: int
    switch: bool
    hidden_type: int

    def helps(self, progress, bag_size):
        progress_factor = self.progress_factors[min(len(self.progress_factors) - 1,
                                                    progress // (100 // len(self.progress_factors)))]
        bag_factor = self.bag_factors[bag_size] if bag_size < len(self.bag_factors) else 100
        tower = min(100, max(0, progress_factor * bag_factor * self.duplicate_luck // 10000))
        return self.rng.get_random(100) <= tower + self.dda


class State(_State):
    def __init__(self, board, towers, tower_for, stage_count):
        super().__init__(board)
        self.towers = towers
        self.tower_for = tower_for
        self.slot_for_identity = list(range(len(board.labels)))
        self.identity_at_slot = list(range(len(board.labels)))
        self.bag_order = []
        self.undo_candidate = None
        self.staging = [[] for _ in range(stage_count)]
        self.capacity = 7
        self.pressure_count = 0
        self.first_dead_end = None

    def stage_tops(self):
        return [stack[-1] for stack in self.staging if stack]

    def released(self, index):
        remaining = ~self.removed
        upper = 1 << index
        return tuple(lower for lower in self.board.dependents[index]
                     if remaining & (1 << lower) and self.board.blockers[lower] & remaining == upper)

    def progress(self):
        eliminated = self.removed.bit_count() - self.size - sum(map(len, self.staging))
        return max(0, 100 * eliminated // len(self.board.labels))

    @property
    def won(self):
        return super().won and not any(self.staging)

    def record_pressure(self):
        if self.size != self.capacity - 1:
            return
        self.pressure_count += 1
        if self.first_dead_end is None and not any(
            self.bag[self.board.labels[index]] == 2 for index in (*self.available, *self.stage_tops())
        ):
            self.first_dead_end = self.progress()

    def swap(self, first, second):
        if first == second:
            return
        labels = self.board.labels
        for pos, other in ((first, second), (second, first)):
            if self.exposed_cells[pos] >= 2:
                self._change_recognizable(labels[pos], -1)
                self._change_recognizable(labels[other], 1)
        labels[first], labels[second] = labels[second], labels[first]
        before, after = self.identity_at_slot[first], self.identity_at_slot[second]
        self.identity_at_slot[first], self.identity_at_slot[second] = after, before
        self.slot_for_identity[before], self.slot_for_identity[after] = second, first

    def old_luck_swap(self, index, luck):
        tower = self.tower_for[index]
        if tower is None or not luck.switch:
            return
        remaining = ~self.removed
        top = next((pos for pos in self.towers[tower]
                    if pos != index and remaining & (1 << pos) and pos not in self.available), None)
        if top is None:
            return
        helps = luck.helps(self.progress(), self.size)
        labels = self.board.labels
        visible = Counter(labels[pos] for pos in (self.available - {index}) if self.tower_for[pos] != tower)
        visible.update(labels[pos] for pos in self.stage_tops())
        hidden_tower = []
        hidden_board = []
        for pos in reversed(self.slot_for_identity):
            if not remaining & (1 << pos) or pos in self.available:
                continue
            if self.tower_for[pos] is not None:
                hidden_tower.append(pos)
            elif luck.hidden_type == 2 and self.exposed_cells[pos] == 0:
                hidden_board.append(pos)
        hidden = hidden_tower + hidden_board

        if helps:
            # The client increments the clicked label only if already in the bag.
            bag = self.bag.copy()
            if bag[labels[index]]:
                bag[labels[index]] += 1
            target = next((pos for pos in hidden if bag[labels[pos]] == 2), None)
            if target is None:
                target = next((pos for pos in hidden if bag[labels[pos]] + visible[labels[pos]] >= 2), None)
        else:
            target = next((pos for pos in hidden if not self.bag[labels[pos]] and not visible[labels[pos]]), None)
            if target is None:
                target = next((pos for pos in hidden if not self.bag[labels[pos]]), None)
        if target is not None:
            self.swap(top, target)

    def _record_insertion(self, index, old_count):
        label = self.board.labels[index]
        if old_count == 2:
            self.bag_order = [pos for pos in self.bag_order if self.board.labels[pos] != label]
        else:
            stop = next((i + 1 for i in range(len(self.bag_order) - 1, -1, -1)
                         if self.board.labels[self.bag_order[i]] == label), len(self.bag_order))
            self.bag_order.insert(stop, index)

    def _insert_into_bag(self, index):
        label = self.board.labels[index]
        if self.bag[label] == 2:
            del self.bag[label]
            self.size -= 2
        else:
            self.bag[label] += 1
            self.size += 1

    def play(self, index, luck):
        label = self.board.labels[index]
        old_count = self.bag[label]
        if luck is not None:
            self.old_luck_swap(index, luck)
        if index in self.available:
            self._remove_from_board(index)
        else:
            stack = next(stack for stack in self.staging if stack and stack[-1] == index)
            stack.pop()
        self._insert_into_bag(index)
        self._record_insertion(index, old_count)
        self.last_click = index
        self.undo_candidate = index if old_count != 2 else None
        self.record_pressure()

    def _move_to_staging(self, position):
        min(self.staging, key=len).append(position)

    def _stage_bag_tile(self, pos):
        self.bag_order.remove(pos)
        label = self.board.labels[pos]
        self.bag[label] -= 1
        if not self.bag[label]:
            del self.bag[label]
        self.size -= 1
        self._move_to_staging(pos)

    def revive(self):
        # Ordinary bag spring intervention. The actual offer may vary by player/config.
        for _ in range(min(3, self.size)):
            self._stage_bag_tile(self.bag_order[-1])
        self.undo_candidate = None

    def _remove_from_board(self, index):
        self.available.discard(index)
        self.available.update(self.released(index))
        self.removed |= 1 << index
        self.visible_area -= self.exposed_cells[index]
        if self.exposed_cells[index] >= 2:
            self._change_recognizable(self.board.labels[index], -1)
        self.exposed_cells[index] = 0
        for lower in self.board.dependents[index]:
            if self.removed & (1 << lower):
                continue
            before = self.exposed_cells[lower]
            after = sum(not (cover & ~self.removed) for cover in self.board.face_covers[lower])
            self.visible_area += after - before
            self.exposed_cells[lower] = after
            if before < 2 <= after:
                self._change_recognizable(self.board.labels[lower], 1)

    def apply_prop(self, asset, rng):
        previous = self.undo_candidate
        self.undo_candidate = None
        if asset == 502:
            if not self.bag_order:
                return False
            self._stage_bag_tile(previous if previous in self.bag_order else self.bag_order[-1])
            return True
        if asset == 508:
            if self.capacity >= 10:
                return False
            self.capacity += 1
            return True
        if asset == 515:
            # Replay evidence does not identify the configured shuffle subtype.
            live = [index for index in range(len(self.board.labels)) if not self.removed & (1 << index)]
            if len(live) < 2:
                return False
            for i in range(len(live) - 1, 0, -1):
                self.swap(live[i], live[rng.randrange(i + 1)])
            return True
        if asset == 506:
            if not self.available:
                return False
            initial = sorted(self.available)
            for _ in range(3):
                if not initial:
                    initial = sorted(self.available)
                    if not initial:
                        break
                chosen = max(initial, key=lambda index: len(self.released(index)))
                initial.remove(chosen)
                self._remove_from_board(chosen)
                self._move_to_staging(chosen)
            return True
        if asset == 503:
            remaining = ~self.removed
            board_order = [i for i in self.slot_for_identity if remaining & (1 << i)]
            ordered = list(reversed(self.bag_order)) + sorted(self.available) + self.stage_tops() + board_order
            if not ordered:
                return False
            labels = self.board.labels
            label = labels[ordered[0]]
            for _ in range(3 - self.bag[label]):
                visible = max((i for i in self.available if labels[i] == label), default=None)
                core = visible if visible is not None else next((i for i in board_order if labels[i] == label), None)
                if core is not None:
                    board_order.remove(core)
                    self._remove_from_board(core)
                else:
                    for stack in reversed(self.staging):
                        found = next((pos for pos in reversed(stack) if labels[pos] == label), None)
                        if found is not None:
                            stack.remove(found)
                            break
            self.bag_order = [i for i in self.bag_order if self.board.labels[i] != label]
            self.size -= self.bag.pop(label, 0)
            return True
        return False

    def choices(self):
        stage = self.stage_tops()
        if not stage and self.capacity == 7:
            return super().options()
        labels = self.board.labels
        candidates = sorted(self.available) + stage
        clickable = Counter(labels[pos] for pos in candidates)
        free = self.capacity - self.size
        remaining = ~self.removed
        positions = self.board.positions
        last = positions[self.last_click] if self.last_click is not None else None
        diverse = len(set(self.recognizable_labels) | {labels[index] for index in stage}) >= 8
        crowded = self.visible_area + 4 * len(stage) >= 64
        options = {}
        for pos in candidates:
            label = labels[pos]
            count = self.bag[label]
            affordable = clickable[label] >= 3 - count and free >= 3 - count
            dependents = self.board.dependents[pos] if pos in self.available else ()
            released = self.released(pos) if dependents else ()
            releases_target = approaches_target = False
            if free >= 2:
                for lower in dependents:
                    if self.exposed_cells[lower] < 2 or not self.bag[labels[lower]]:
                        continue
                    covers = (self.board.blockers[lower] & remaining).bit_count()
                    releases_target |= covers == 1
                    approaches_target |= covers == 2
            near = last is not None and abs(positions[pos][0] - last[0]) + abs(positions[pos][1] - last[1]) <= 4
            options[pos] = (
                int(count == 2)
                | int(count == 1) << 1
                | int(count == 0 and clickable[label] >= 3) << 2
                | int(bool(released)) << 3
                | int(free == 1 and count != 2) << 4
                | int(free <= 3 and affordable) << 5
                | int(free == 2 and count != 2 and not affordable) << 6
                | int(count < 2 and any(self.exposed_cells[lower] >= 2 and labels[lower] == label
                                         for lower in released)) << 7
                | int(near) << 8
                | int(diverse and count == 2) << 9
                | int(crowded and count == 0 and affordable) << 10
                | int(releases_target) << 11
                | int(approaches_target) << 12
            )
        return options


class Behavior:
    """Replay-based context distributions for timing and intervention decisions."""

    def __init__(self, payload):
        self.click_timing = {(x["bag"], x["surface"]): x["quantiles"]
                             for x in payload.get("click_intervals_ms", ())}
        self.props = {(x["bag"], x["surface"], x["retry"]): x for x in payload.get("prop_trigger", ())}
        self.revives = {x["retry"]: x for x in payload.get("revive_accept", ())}
        time_ratio = payload.get("foreground_time_ratio")
        self.foreground_time_ratio = (time_ratio if isinstance(time_ratio, (int, float))
                                      and isfinite(time_ratio) and time_ratio > 0 else None)

    @staticmethod
    def _rate(row):
        return row["uses"] / row["opportunities"] if row and row["opportunities"] > 0 else 0.0

    @staticmethod
    def _surface(visible_count):
        return 0 if visible_count <= 8 else 1 if visible_count <= 15 else 2

    def seconds(self, bag, visible_count, percentile):
        surface = self._surface(visible_count)
        row = self.click_timing.get((bag, surface)) or self.click_timing.get((bag, -1))
        if row is None or len(row) != len(QUANTILES) - 2:
            return 0.0
        values = (max(0, row[0] / 2), *row, row[-1])
        for upper in range(1, len(QUANTILES)):
            if percentile <= QUANTILES[upper]:
                fraction = (percentile - QUANTILES[upper - 1]) / (QUANTILES[upper] - QUANTILES[upper - 1])
                return (values[upper - 1] + fraction * (values[upper] - values[upper - 1])) / 1000
        return row[-1] / 1000

    def prop(self, state, retry, rng):
        count = len(state.available) + len(state.stage_tops())
        bag = state.size
        row = self.props.get((bag, self._surface(count), retry)) or self.props.get((bag, -1, retry))
        if not row or row.get("opportunities", 0) <= 0:
            return None
        weights = [row.get(f"asset_{asset}", 0) for asset in ASSETS]
        if rng.random() * row["opportunities"] >= sum(weights):
            return None
        return rng.choices(ASSETS, weights=weights)[0]

    def revive(self, retry, rng):
        return rng.random() < self._rate(self.revives.get(retry))


def _geometry(template):
    graph = build_geometry(template)
    faces = [footprint(tile) for tile in template.tiles]
    blockers = tuple(sum(1 << i for i in group) for group in graph.direct_blockers)
    face_masks = tuple(
        tuple(sum(1 << upper for upper in group if cell in faces[upper]) for cell in sorted(faces[index]))
        for index, group in enumerate(graph.direct_blockers)
    )
    dependents = tuple(tuple(sorted(group)) for group in graph.direct_blocked_tiles)
    positions = tuple((tile.x, tile.y) for tile in template.tiles)
    tower_for = tuple(tile.tower_index for tile in template.tiles)
    # Z ties follow the reverse serialization scan of PrepareCurTowerTopTile.
    towers = tuple(tuple(sorted(tower.member_indices, key=lambda i: (template.tiles[i].z, i), reverse=True))
                   for tower in template.towers)
    return blockers, dependents, face_masks, positions, tower_for, towers


def _priority_tables(coefficients):
    if len(coefficients) != FEATURE_COUNT or any(not isfinite(x) or abs(x) > 20 for x in coefficients):
        raise ValueError("玩家策略权重格式无效。")

    def table(values):
        weights = [1.0]
        for alpha in values:
            factor = exp(alpha)
            weights.extend(weight * factor for weight in weights[:])
        return tuple(weights)

    return table(coefficients[:7]), table(coefficients[7:])


def _sample_policy_indexes(weights, trials, rng):
    population = range(len(weights))
    rank = sorted(population, key=lambda i: log(-log(max(rng.random(), 1e-300))) - log(weights[i]))
    indexes = rank[:trials]
    if trials > len(rank):
        indexes.extend(rng.choices(population, weights=weights, k=trials - len(rank)))
    rng.shuffle(indexes)
    return indexes


def _play_index(state, priorities, rng, tower_bias=None):
    candidates = state.choices()
    low, high = priorities
    if tower_bias is None:
        weights = [low[code & 127] * high[code >> 7] for code in candidates.values()]
    else:
        mask, bag_high_min, tower_low_max, factors = tower_bias
        remaining = mask & ~state.removed
        factor = factors.get((state.size >= bag_high_min, remaining.bit_count() <= tower_low_max), 1.0)
        weights = [low[code & 127] * high[code >> 7] * (factor if remaining & (1 << index) else 1.0)
                   for index, code in candidates.items()]
    return rng.choices(tuple(candidates), weights=weights)[0]


@dataclass(frozen=True)
class Trial:
    won: bool
    pressure: int
    dead_end: int | None
    seconds: float
    props: int
    revives: int


def _rollout(state, priorities, rng, behavior, retry, luck, tower_bias=None):
    duration = 0.0
    props = revives = 0
    aid_rng = Random(rng.getrandbits(64))
    timing = Random(rng.getrandbits(64))
    tempo = timing.random() if behavior.click_timing else None
    for _ in range(len(state.board.labels) * 3 + 12):
        if state.won:
            break
        if state.size >= state.capacity:
            if revives < 3 and behavior.revive(retry, aid_rng):
                state.revive()
                revives += 1
            else:
                break
        elif not state.available and not state.stage_tops():
            break
        if props < 10 and behavior.props:
            asset = behavior.prop(state, retry, aid_rng)
            if asset is not None and state.apply_prop(asset, aid_rng):
                props += 1
                if state.won:
                    break
        if tempo is not None:
            visible = len(state.available) + len(state.stage_tops())
            percentile = tempo if timing.random() < .65 else timing.random()
            duration += behavior.seconds(state.size, visible, percentile)
        selected = _play_index(state, priorities[state.size >= state.capacity - 2], rng, tower_bias)
        state.play(selected, luck)
    return Trial(state.won, state.pressure_count, state.first_dead_end, duration, props, revives)


def build_engine(model_payload, *, config_directory=None):
    if model_payload.get("feature_version") != FEATURE_VERSION:
        raise ValueError("玩家点击模型与当前模拟特征版本不一致。")
    profiles = model_payload.get("player_policies", ())
    weights = [float(profile.get("weight", 1)) for profile in profiles]
    if not weights or any(not isfinite(weight) or weight <= 0 for weight in weights):
        raise ValueError("玩家点击模型缺少有效的玩家风格。")

    @lru_cache(maxsize=None)
    def policy(i):
        profile = profiles[i]
        regular = _priority_tables(profile["coefficients"])
        pressure = profile.get("pressure_coefficients")
        return regular, _priority_tables(pressure) if pressure is not None else regular

    behavior = Behavior(model_payload.get("behavior", {}))
    tower_model = model_payload.get("tower_choices", {})
    tower_factors = {(row["bag_high"], row["tower_low"]): exp(row["log_multiplier"])
                     for row in tower_model.get("bias_by_context", ())}
    default, fuuu, ranges, catalog, tower_on, hidden_type, stage_count = _local_config(config_directory)

    @lru_cache(maxsize=8)
    def geometry(template):
        return _geometry(template)

    def estimate(board, entry, params, trials, entropy, baseline=None, progress=None, *, materialized_labels=None):
        if trials <= 0:
            raise ValueError("模拟局数必须大于 0。")
        if entry.seed_group is not None and entry.seed_group > 0:
            raise ValueError(f"{board.template_id} 使用分组 Seed，当前局面复现仅支持普通 Seed。")
        if any(tile.has_debuff_index for tile in board.tiles):
            raise ValueError(f"{board.template_id} 包含特殊牌效果，当前局面复现不支持。")
        level_index = getattr(entry, "level_index", None)
        strategy = getattr(entry, "fuuu", None)
        factors = fuuu.get(strategy, default)
        if not 0 < len(factors[0]) < 100 or len(factors[1]) < 7:
            factors = default
        dda = 0 if baseline is None else baseline
        try:
            labels = (
                _deal(board, params, level_index or 300, catalog, ranges)
                if materialized_labels is None
                else _finish_labels(board, materialized_labels, catalog)
            )
        except ValueError as exc:
            raise ValueError(f"{board.template_id} 初始牌面无法复现：{exc}") from exc
        blockers, dependents, face_masks, positions, tower_for, towers = geometry(board)
        enabled = tower_on and bool(towers)
        tower_mask = sum(1 << index for index, tower in enumerate(tower_for) if tower is not None)
        tower_bias = (tower_mask, tower_model.get("bag_high_min", 5), tower_model.get("tower_low_max", 12),
                      tower_factors) if tower_mask and tower_factors else None

        def initial():
            player_board = PlayerBoard(labels.copy(), blockers, dependents, face_masks, positions)
            state = State(player_board, towers, tower_for, stage_count)
            luck = OldLuck(LegacyRandom(1611232022 + params.seed), *factors, params.luck,
                           dda, enabled, hidden_type) if enabled else None
            return state, luck

        rng = Random(entropy)
        indexes = _sample_policy_indexes(weights, trials, rng)
        duplicate = entry.duplicate or 1
        retry = 0 if duplicate == 1 else 1 if duplicate < 4 else 2
        simulations = []
        naked_wins = 0
        for completed, i in enumerate(indexes, 1):
            choices = policy(i)
            entropy_i = rng.getrandbits(64)
            state, luck = initial()
            played = _rollout(state, choices, Random(entropy_i), behavior, retry, luck, tower_bias)
            simulations.append(played)
            naked_wins += played.won and not played.props and not played.revives
            if progress is not None and (completed % 50 == 0 or completed == trials):
                progress(completed, trials)

        failed_dead_ends = [t.dead_end for t in simulations if not t.won and t.dead_end is not None]
        press_winners = [t.pressure for t in simulations if t.won and t.pressure]
        mean_props = fmean(t.props for t in simulations)
        mean_revives = fmean(t.revives for t in simulations)
        notes = []
        if enabled:
            notes.append("塔换牌使用本地旧 L 配置。个人历史胜率和 IAP 保护没有接入。")
            if baseline is None:
                notes.append("未提供个人历史 DDA，按本地默认增量 0 模拟。")
        if tower_bias:
            notes.append("点塔倾向由真实有塔回放校准，会随背包压力和剩余塔牌变化。")
        if behavior.props or behavior.revives:
            notes.append("按回放分布模拟撤回、磁铁、抓钩、扩容、洗牌和复活。未识别道具、洗牌子类型、复活报价和玩家库存未接入。")
        if behavior.foreground_time_ratio is not None:
            notes.append("时长按真实回放点击间隔抽样，并用线上前台时长与点击累计时长的中位比例调整。")
        if getattr(entry, "strategy_id", None):
            notes.append("此副本按配置和模板开局，未复现依赖个人局内历史的结构降难干预。")
        features = {
            "naked_win_rate": naked_wins / trials,
            "assisted_win_rate": sum(t.won for t in simulations) / trials,
            "mean_prop_uses": mean_props,
            "mean_revives": mean_revives,
            "mean_duration_seconds": (fmean(t.seconds for t in simulations) * (behavior.foreground_time_ratio or 1)
                                      if behavior.click_timing else None),
            "failure_fuuu": fmean(value > 50 for value in failed_dead_ends) if failed_dead_ends else None,
            "failure_dead_end_progress": fmean(failed_dead_ends) / 100 if failed_dead_ends else None,
            "win_fuuu": fmean(value > 3 for value in press_winners) if press_winners else None,
            "high_pressure_count": fmean(press_winners) if press_winners else None,
            "failure_fuuu_samples": len(failed_dead_ends),
            "win_fuuu_samples": len(press_winners),
            "player_styles": len(set(indexes)),
            "naked_wins": naked_wins,
            "fuuu_strategy_id": strategy,
            "mean_cost": None,
            "notes": notes,
        }
        return features

    return estimate
