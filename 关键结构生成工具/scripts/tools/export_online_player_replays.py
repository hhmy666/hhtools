"""Print a read-only Trino query for Redash ReplayInfo exports or coverage.

Run the coverage query first. Split large exports by product, Shanghai date
and player shard. Keep the shard count fixed within a run so shards are
disjoint and player IDs stay linkable across dates. The optional limit is a
deterministic attempt sample. An uncapped shard retains every eligible replay.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta


_PRODUCTS = ("tile01a", "tile01g")
_TEXT_SETTLEMENT_FIELDS = (
    "level_start_time",
    "level_status",
    "client_version",
    "platform",
    "level_value_test",
    "bag_tile_change_curve",
    "surface_normal_tile_change_curve",
)
_INT_SETTLEMENT_FIELDS = (
    "level_index",
    "level_dup_para_id",
    "level_play_count",
    "level_part_index",
    "prop_use_count",
    "level_revive_count",
    "level_time_revive_count",
    "level_space_revive_count",
    "extra_space_num",
    "extra_space_booster_num",
    "unlimited_ingame_extra_space_time",
    "bag_space_remain",
)
_SETTLEMENT_FIELDS = _TEXT_SETTLEMENT_FIELDS + _INT_SETTLEMENT_FIELDS


def _capture_window(
    product: str,
    start: date,
    end: date,
    level_from: int,
    level_to: int,
    shard_count: int,
    shard: int,
) -> str:
    next_day = end + timedelta(days=1)
    prefix = "dataplatform_caspain_045_chopin.dwd_"
    table = f"{prefix}{product}_os_client_events_fact_log_realtime"
    date_start = f"WITH_TIMEZONE(TIMESTAMP '{start} 00:00:00', 'Asia/Shanghai')"
    date_end = f"WITH_TIMEZONE(TIMESTAMP '{next_day} 00:00:00', 'Asia/Shanghai')"
    player = f"CONCAT('{product}:', TRIM(uid))"
    shard_filter = ""
    if shard_count > 1:
        shard_filter = f"""
      AND MOD(
          BITWISE_AND(FROM_BIG_ENDIAN_64(XXHASH64(TO_UTF8({player}))), 9223372036854775807),
          {shard_count}
      ) = {shard}"""
    raw_numbers = []
    for field in _INT_SETTLEMENT_FIELDS:
        if field != "level_index":
            raw_numbers.append(f"        TRY_CAST({field} AS INTEGER) AS {field}")
    normalized_ints = ",\n".join(raw_numbers)
    normalized_settlement = ",\n".join(
        f"        MAX({field}) FILTER (WHERE action = 'level_end') AS {field}"
        for field in _SETTLEMENT_FIELDS
    )
    settlement_signature = ",\n            ".join((*_SETTLEMENT_FIELDS, "level_dup_para"))
    return f"""WITH replay_events AS (
    SELECT
        TRIM(uid) AS raw_player_id,
        log_time AS event_time,
        action,
        NULLIF(TRIM(CAST(level_id AS VARCHAR)), '') AS level_id,
        NULLIF(TRIM(CAST(level_template_id AS VARCHAR)), '') AS level_template_id,
        NULLIF(TRIM(CAST(level_dup_para AS VARCHAR)), '') AS level_dup_para,
        NULLIF(TRIM(CAST(level_start_time AS VARCHAR)), '') AS level_start_time,
        NULLIF(LOWER(TRIM(CAST(level_status AS VARCHAR))), '') AS level_status,
        NULLIF(TRIM(CAST(app_version AS VARCHAR)), '') AS client_version,
        NULLIF(LOWER(TRIM(CAST(platform AS VARCHAR))), '') AS platform,
        NULLIF(LOWER(TRIM(CAST(level_value_test AS VARCHAR))), '') AS level_value_test,
        NULLIF(TRIM(CAST(bag_tile_change_curve AS VARCHAR)), '') AS bag_tile_change_curve,
        NULLIF(TRIM(CAST(surface_normal_tile_change_curve AS VARCHAR)), '') AS surface_normal_tile_change_curve,
        NULLIF(TRIM(CAST(replay_info AS VARCHAR)), '') AS replay_info,
        COALESCE(TRY_CAST(user_level_index AS INTEGER), TRY_CAST(level_index AS INTEGER)) AS level_index,
{normalized_ints}
    FROM {table}
    WHERE bp_timestamp >= {date_start}
      AND bp_timestamp < {date_end}
      AND log_time >= {date_start}
      AND log_time < {date_end}
      AND action IN ('level_end', 'user_level_replay')
      AND NULLIF(TRIM(uid), '') IS NOT NULL{shard_filter}
),
paired AS (
    SELECT
        TO_HEX(SHA256(TO_UTF8(CONCAT('{product}:', raw_player_id)))) AS user_id,
        event_time,
        level_id,
        level_template_id,
{normalized_settlement},
        COALESCE(
            MAX(level_dup_para) FILTER (WHERE action = 'level_end'),
            MAX(level_dup_para) FILTER (WHERE action = 'user_level_replay')
        ) AS level_dup_para,
        MAX(replay_info) FILTER (WHERE action = 'user_level_replay') AS replay_info,
        COUNT(DISTINCT replay_info) FILTER (WHERE action = 'user_level_replay') AS replay_variants,
        COUNT(DISTINCT level_dup_para) FILTER (WHERE action = 'user_level_replay') AS replay_dup_para_variants,
        MAX(level_dup_para) FILTER (WHERE action = 'level_end') AS settled_dup_para,
        MAX(level_dup_para) FILTER (WHERE action = 'user_level_replay') AS replay_dup_para,
        COUNT_IF(action = 'level_end') AS level_end_events,
        COUNT_IF(action = 'user_level_replay') AS level_replay_events,
        COUNT(DISTINCT ROW(
            {settlement_signature}
        )) FILTER (WHERE action = 'level_end') AS settlement_variants
    FROM replay_events
    WHERE level_index BETWEEN {level_from} AND {level_to}
       OR (action = 'user_level_replay' AND level_index IS NULL)
    GROUP BY raw_player_id, event_time, level_id, level_template_id
),
attempts AS (
    SELECT *,
        CASE
            WHEN settlement_variants <> 1 THEN 'settlement_conflict'
            WHEN replay_variants = 0 THEN 'missing_replay'
            WHEN replay_variants <> 1 THEN 'replay_conflict'
            WHEN replay_dup_para_variants > 1 THEN 'parameter_conflict'
            WHEN settled_dup_para IS NOT NULL AND replay_dup_para IS NOT NULL
                 AND settled_dup_para <> replay_dup_para THEN 'parameter_conflict'
            WHEN level_id IS NULL OR level_template_id IS NULL THEN 'missing_board_identity'
            ELSE 'paired'
        END AS pair_status
    FROM paired
    WHERE level_end_events > 0
)"""


def build_query(
    *,
    product: str,
    start: date,
    end: date,
    level_from: int = 200,
    level_to: int = 600,
    shard_count: int = 1,
    shard: int = 0,
    limit: int | None = None,
    quality: bool = False,
) -> str:
    if product not in _PRODUCTS:
        raise ValueError("产品只能是 tile01a 或 tile01g")
    if start > end or end == date.max:
        raise ValueError("请给出有效的起止日期")
    if not 1 <= level_from <= level_to:
        raise ValueError("关卡范围应为有序的正整数")
    if not 0 <= shard < shard_count:
        raise ValueError("分片编号从 0 开始，且小于分片总数")
    if limit is not None and (limit < 1 or quality):
        raise ValueError("--limit 只能用于回放导出，且大于 0")

    ctes = _capture_window(product, start, end, level_from, level_to, shard_count, shard)
    if quality:
        return f"""{ctes}
SELECT
    '{product}' AS product,
    CASE WHEN GROUPING(client_version, platform) = 3 THEN 'total' ELSE 'version_platform' END AS scope,
    client_version,
    platform,
    COUNT(*) AS attempts,
    COUNT(DISTINCT user_id) AS players,
    COUNT_IF(pair_status = 'paired') AS paired_attempts,
    COUNT(DISTINCT IF(pair_status = 'paired', user_id, NULL)) AS paired_players,
    COUNT_IF(pair_status = 'missing_replay') AS missing_replay_attempts,
    COUNT_IF(pair_status = 'settlement_conflict') AS settlement_conflicts,
    COUNT_IF(pair_status = 'replay_conflict') AS replay_conflicts,
    COUNT_IF(pair_status = 'parameter_conflict') AS parameter_conflicts,
    COUNT_IF(pair_status = 'missing_board_identity') AS missing_board_ids,
    COUNT_IF(pair_status = 'paired' AND level_dup_para IS NULL) AS paired_missing_dup_para,
    COUNT_IF(pair_status = 'paired' AND bag_tile_change_curve IS NULL) AS paired_missing_bag_curve,
    COUNT_IF(pair_status = 'paired' AND surface_normal_tile_change_curve IS NULL) AS paired_missing_surface_curve,
    COUNT_IF(level_status IN ('pass', 'win', 'success')) AS settled_wins,
    COUNT_IF(level_status IN ('exit', 'quit')) AS settled_exits
FROM attempts
GROUP BY GROUPING SETS ((), (client_version, platform))
ORDER BY scope, attempts DESC
"""
    sampling = ""
    if limit is not None:
        sampling = f"""
ORDER BY XXHASH64(TO_UTF8(CONCAT_WS('|', user_id, CAST(event_time AS VARCHAR), level_id, level_template_id))),
         user_id, event_time, level_id, level_template_id
LIMIT {limit}"""
    settlement_columns = ",\n    ".join(_SETTLEMENT_FIELDS)
    return f"""{ctes}
SELECT
    '{product}' AS product,
    user_id,
    DATE(AT_TIMEZONE(event_time, 'Asia/Shanghai')) AS event_date,
    event_time,
    level_id,
    level_template_id,
    level_dup_para,
    {settlement_columns},
    replay_variants,
    replay_info
FROM attempts
WHERE pair_status = 'paired'{sampling}
"""


def parse_date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("日期格式为 YYYY-MM-DD") from None
    if result.isoformat() != value:
        raise argparse.ArgumentTypeError("日期格式为 YYYY-MM-DD")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", choices=_PRODUCTS, required=True)
    parser.add_argument("--start", type=parse_date, required=True, help="上海日期，含首日")
    parser.add_argument("--end", type=parse_date, required=True, help="上海日期，含末日")
    parser.add_argument("--level-from", type=int, default=200)
    parser.add_argument("--level-to", type=int, default=600)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard", type=int, default=0, help="从 0 开始的玩家分片编号")
    parser.add_argument("--limit", type=int, help="可选的单批确定性样本数上限")
    parser.add_argument("--quality", action="store_true", help="只输出分片覆盖和质量统计")
    args = parser.parse_args()
    try:
        print(build_query(**vars(args)), end="")
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
