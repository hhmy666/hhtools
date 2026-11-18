"""Stream verified board choices from scoped online ReplayInfo exports."""

from __future__ import annotations

import csv
import gzip
import hashlib
import re
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Mapping

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[2] / "ai模拟玩家行为"))

from puzzle_core.online_player_simulation import PlayerBoard, ReplayEvidence, observe_replay  # noqa: E402
from restore_player_replay import ConfigCatalog, ReplayRestorer, parse_int, parse_replay  # noqa: E402


@dataclass(frozen=True)
class ReplaySource:
    """Identities are comparable only within the same product and namespace.

    Leave the namespace empty for raw stable IDs. Exports using locally
    generated rank IDs need a namespace specific to that cohort/export.
    """

    path: Path
    default_product: str | None = None
    player_namespace: str = ""
    player_id_format: str = "as_is"

    def __post_init__(self) -> None:
        if self.player_id_format not in ("as_is", "raw", "product_sha256"):
            raise ValueError(f"未知玩家 ID 格式：{self.player_id_format}")

    def normalized_player(self, product: str, player: str) -> str:
        if self.player_id_format == "raw":
            player = hashlib.sha256(f"{product}:{player}".encode()).hexdigest()
        if self.player_id_format == "product_sha256":
            if not re.fullmatch(r"[0-9a-fA-F]{64}", player):
                raise ValueError(f"{Path(self.path).name} 含非 SHA256 玩家 ID")
            player = player.lower()
        return f"{self.player_namespace}:{player}" if self.player_namespace else player


@dataclass(frozen=True)
class TrainingAttempt:
    product: str
    player: str
    template: str
    family: str
    board_id: tuple[str, ...]
    ratio: float
    board: PlayerBoard
    replay: ReplayEvidence
    observed_naked_win: bool | None

    @property
    def player_key(self) -> tuple[str, str]:
        return self.product, self.player


def _fold(value: str, domain: str, folds: int) -> int:
    digest = hashlib.sha256(f"{domain}:{value}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % folds


def player_fold(product: str, player: str, folds: int = 4) -> int:
    return _fold(f"{product}:{player}", "player", folds)


def template_fold(family: str, folds: int = 4) -> int:
    return _fold(family, "board", folds)


def family_of(template: str) -> str:
    name = re.sub(r"^(?:aipuzzle|newv[0-9]+|lesst|not|nt)+", "", template.lower())
    return re.sub(r"(?:v[0-9]+|s[0-9]+)+$", "", name)


def _field(row: Mapping[str, str | None], key: str) -> str:
    return (row.get(key) or "").strip()


def _bag_curve(value: str) -> tuple[int, ...]:
    # The client appends `(char)(bag count + 48)` after each manual click.
    if re.fullmatch(r"[0-9]+", value):
        return tuple(map(int, value))
    if re.fullmatch(r"[0-9](?:[|,;\s]+[0-9])+", value):
        return tuple(map(int, re.findall(r"[0-9]", value)))
    return ()


def _surface_curve(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]+(?:-[0-9]+)*", value):
        return ()
    return tuple(map(int, value.split("-")))


_BOARD_FIELDS = (
    "platform", "client_version", "level_index", "level_id", "level_template_id",
    "level_dup_para", "level_dup_para_id",
)
_REQUIRED_FIELDS = {
    "user_id", "level_index", "level_id", "level_template_id", "level_dup_para",
    "level_dup_para_id", "replay_info", "bag_tile_change_curve",
}
_WIN_STATUS = {"pass", "success", "win"}
_LOSS_STATUS = {"fail", "lose", "failed"}
_SWAP_COUNTERS = (
    "tower_easy_success_count", "tower_hard_success_count",
    "nontower_easy_success_count", "nontower_hard_success_count",
)


class PlayerReplayLoader:
    def __init__(self, config_dir: Path, template_dir: Path, *, cache_size: int = 2048) -> None:
        self.catalog = ConfigCatalog(config_dir)
        self.restorer = ReplayRestorer(self.catalog, template_dir, SCRIPTS, 0)
        self.template_dir = template_dir
        self.stats: Counter[str] = Counter()
        strategy_path = config_dir / "config_level_strategy_group.csv"
        self.strategy_groups = {}
        if strategy_path.is_file():
            with strategy_path.open(encoding="utf-8-sig", newline="") as stream:
                self.strategy_groups = {
                    group_id: group for group in csv.DictReader(stream)
                    if (group_id := parse_int(group.get("StrategyGroupId")))
                }
        # Both geometry and label generation consume the same decoded templates.
        self.restorer.parse_level_file = lru_cache(maxsize=cache_size)(self.restorer.parse_level_file)
        self._resolve_board = lru_cache(maxsize=cache_size)(self._resolve_board)

    def _resolve_board(
        self, board_id: tuple[str, ...],
    ) -> tuple[PlayerBoard | None, float, str]:
        row = dict(zip(_BOARD_FIELDS, board_id[1:]))
        name = row["level_template_id"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            return None, 0, "invalid_template_id"
        try:
            level = self.restorer.parse_level_file(self.template_dir / f"{name}_m.bytes")
        except (OSError, ValueError):
            return None, 0, "template_unavailable"
        if level.tower_member_indices:
            return None, 0, "unsupported_tower"
        if any(tile.has_debuff_index for tile in level.tiles):
            return None, 0, "unsupported_debuff"
        parts = row["level_dup_para"].split("|")
        if len(parts) != 4 or any(parse_int(part) is None for part in parts):
            return None, 0, "invalid_observed_dup_para"
        level_index = parse_int(row["level_index"]) or 0
        duplicate = parse_int(row["level_dup_para_id"]) or 0
        if not row["level_id"] or level_index < 1 or not 1 <= duplicate <= 10:
            return None, 0, "missing_observed_config"
        resolution = self.catalog.resolve_level(row, 1)
        if resolution.error or resolution.source != "input_level_id":
            return None, 0, "unmatched_observed_config"
        restored = self.restorer.reconstruct(resolution)
        # The bounded board cache already stores the useful result of replay
        # preparation. Drop the restorer's private copies of the same board.
        self.restorer.reconstruction_cache.clear()
        self.restorer.template_cache.clear()
        if not restored.status.startswith("已还原_使用输入实际配置"):
            return None, 0, "board_not_reconstructed"
        labels = tuple(tile.tile_id for tile in restored.tiles)
        if any(not (1000 <= label <= 9999 or -23 <= label <= -12) for label in labels):
            return None, 0, "unsupported_tile_mechanic"
        try:
            board = PlayerBoard.from_level(level, labels)
        except ValueError:
            return None, 0, "invalid_static_board"
        ratio = sum(-23 <= tile.tile_id <= -12 for tile in level.tiles) / len(level.tiles)
        return board, ratio, "resolved"

    def _has_unverified_strategy(self, row: Mapping[str, str | None]) -> bool:
        if _field(row, "strategy_effective_list") not in ("", "[]", "0"):
            return True
        strategy_instance = _field(row, "strategy_instance_config")
        if strategy_instance:
            return strategy_instance.split("|", 1)[0] in ("1", "2")
        template = _field(row, "level_template_id")
        if not any(marker in template for marker in ("model", "hhmy", "zwd")):
            return False
        level_config = self.catalog.level_by_id.get(_field(row, "level_id"), {})
        group_id = parse_int(level_config.get("StrategyGroupId"), 0)
        if not group_id:
            return False
        group = self.strategy_groups.get(group_id)
        if group is None:
            return True
        duplicate = max(1, min(10, parse_int(_field(row, "level_dup_para_id"), 1) or 1))
        parts = _field(group, f"InstanceId{duplicate}").split("|")
        return len(parts) >= 3 and parts[0] in ("1", "2") and bool(parts[2])

    def _observe(
        self, row: Mapping[str, str | None], product: str, player: str,
    ) -> TrainingAttempt | None:
        pair_status = _field(row, "pair_status")
        replay_variants = _field(row, "replay_variants")
        if pair_status not in ("", "paired") or replay_variants not in ("", "1"):
            self.stats["ambiguous_replay_pair"] += 1
            return None
        raw_replay = _field(row, "replay_info")
        bag = _bag_curve(_field(row, "bag_tile_change_curve"))
        if not raw_replay or not bag:
            self.stats["missing_replay_or_bag"] += 1
            return None
        if self._has_unverified_strategy(row):
            self.stats["unsupported_observed_strategy"] += 1
            return None
        if any((parse_int(_field(row, field), 0) or 0) > 0 for field in _SWAP_COUNTERS):
            self.stats["observed_runtime_swaps"] += 1
            return None
        if max(bag) > 7 or any(_field(row, field) not in ("", "0") for field in (
            "extra_space_booster_num", "unlimited_ingame_extra_space_time",
        )):
            self.stats["wider_or_boosted_capacity"] += 1
            return None
        board_id = (product, *(_field(row, field) for field in _BOARD_FIELDS))
        board, ratio, reason = self._resolve_board(board_id)
        if board is None:
            self.stats[reason] += 1
            return None
        events = parse_replay(raw_replay)
        steps = tuple((event["action_id"], event["tile_nums"]) for event in events)
        surface_text = _field(row, "surface_normal_tile_change_curve")
        surface = _surface_curve(surface_text)
        if surface_text and len(surface) != len(bag):
            self.stats["unaligned_surface_curve"] += 1
            return None
        replay = observe_replay(board, steps, bag, observed_surface_sizes=surface or None)
        self.stats[f"replay_{replay.status}"] += 1
        if not replay.evidence:
            return None
        if replay.status in (
            "bag_mismatch", "surface_mismatch", "click_not_reproducible", "missing_bag_observation",
        ):
            # Wrong labels can produce the same early bag cardinalities. Leave
            # every decision from a conflicting reconstruction out of fitting.
            self.stats["conflicting_board_omitted"] += 1
            return None
        naked_win = self._naked_outcome(row, replay, steps, bag)
        if replay.status == "full" and naked_win is None:
            remaining = steps[len(replay.evidence):]
            if remaining and remaining[0][0] == 1:
                self.stats["continued_after_model_seven_full"] += 1
                return None
        template = _field(row, "level_template_id")
        self.stats["verified_attempts"] += 1
        self.stats["verified_choices"] += len(replay.evidence)
        self.stats["ordinary_verified_attempts" if ratio == 0 else "structured_verified_attempts"] += 1
        return TrainingAttempt(product, player, template, family_of(template), board_id, ratio, board, replay, naked_win)

    @staticmethod
    def _naked_outcome(
        row: Mapping[str, str | None], replay: ReplayEvidence,
        steps: tuple[tuple[int | None, list[int]], ...], bag: tuple[int, ...],
    ) -> bool | None:
        status = _field(row, "level_status").lower()
        # extra_space_num is the player's remaining +1 inventory. Actual
        # replay actions and timed/booster flags are used for capacity checks.
        clean = all(_field(row, key) == "0" for key in ("prop_use_count", "level_revive_count"))
        ends_at_replay_stop = len(replay.evidence) == len(steps) == len(bag)
        if not clean or not ends_at_replay_stop:
            return None
        if replay.status == "won" and status in _WIN_STATUS:
            return True
        if replay.status == "full" and status in _LOSS_STATUS and bag[-1] == 7:
            # Older event exports pair a specific full-bag event. Use it when
            # present; modern settlement may omit it, so keep the end censored.
            if (parse_int(_field(row, "bag_full_event_count"), 0) or 0) > 0:
                return False
        return None

    def iter_attempts(self, sources: Iterable[ReplaySource]) -> Iterator[TrainingAttempt]:
        self.stats.clear()
        seen_sources = set()
        input_players = set()
        verified_players = set()
        for source in sources:
            path = Path(source.path)
            stat = path.stat()
            file_key = stat.st_dev, stat.st_ino
            if file_key in seen_sources:
                raise ValueError(f"回放文件重复输入：{path.name}")
            seen_sources.add(file_key)
            opener = gzip.open if path.suffix.lower() == ".gz" else open
            with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                missing = _REQUIRED_FIELDS - set(reader.fieldnames or ())
                if missing:
                    raise ValueError(f"{path.name} 缺少回放字段：{', '.join(sorted(missing))}")
                for row in reader:
                    self.stats["input_attempts"] += 1
                    product = _field(row, "product").lower() or source.default_product
                    player = _field(row, "user_id")
                    if product not in ("tile01a", "tile01g") or not player:
                        self.stats["missing_product_or_player"] += 1
                        continue
                    normalized = source.normalized_player(product, player)
                    input_players.add((product, normalized))
                    attempt = self._observe(row, product, normalized)
                    if attempt is not None:
                        verified_players.add(attempt.player_key)
                        yield attempt
        self.stats["input_players"] = len(input_players)
        self.stats["verified_players"] = len(verified_players)
