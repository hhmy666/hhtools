-- Trino / Redash. Run with the same date range, levels, and player shard as
-- the ReplayInfo extract. A shard of -1 means all players. Each output row
-- labels one exact parameter setting over the complete reporting window.
--
-- Resource weights and player-position denominator follow the "关卡副本数据"
-- query (第一份SQL-兼容版.md, cost_data / user_level_data / level_data). This is
-- the query's weighted consumption metric, not a wallet debit.
-- Date bounds are inclusive in Shanghai business dates.

WITH level_rows AS (
    SELECT
        'tile01a' AS product,
        log_date, uid, user_level_index, level_id, level_template_id,
        level_dup_para_id, level_dup_para, daily_level_enter, daily_level_pass,
        daily_level_use_life, daily_level_use_life_when_unlimited,
        daily_level_use_undo, daily_level_use_propeller, daily_level_use_magneto,
        daily_level_use_ingame_extraspace_when_unlimited,
        daily_level_use_ingame_extraspace, daily_level_use_bag_spring,
        daily_level_use_bag_spring_iap, daily_level_use_bag_spring_ad
    FROM dataplatform_caspain_045_chopin.dws_tile01a_os_level_mid_log_daily
    WHERE log_date BETWEEN DATE '{{ date.start }}' AND DATE '{{ date.end }}'
      AND user_level_index BETWEEN {{ level_start }} AND {{ level_end }}

    UNION ALL

    SELECT
        'tile01g' AS product,
        log_date, uid, user_level_index, level_id, level_template_id,
        level_dup_para_id, level_dup_para, daily_level_enter, daily_level_pass,
        daily_level_use_life, daily_level_use_life_when_unlimited,
        daily_level_use_undo, daily_level_use_propeller, daily_level_use_magneto,
        daily_level_use_ingame_extraspace_when_unlimited,
        daily_level_use_ingame_extraspace, daily_level_use_bag_spring,
        daily_level_use_bag_spring_iap, daily_level_use_bag_spring_ad
    FROM dataplatform_caspain_045_chopin.dws_tile01g_os_level_mid_log_daily
    WHERE log_date BETWEEN DATE '{{ date.start }}' AND DATE '{{ date.end }}'
      AND user_level_index BETWEEN {{ level_start }} AND {{ level_end }}
),

daily_player_parameters AS (
    SELECT
        product,
        CAST(user_level_index AS INTEGER) AS level_index,
        trim(CAST(level_id AS VARCHAR)) AS level_id,
        trim(CAST(level_template_id AS VARCHAR)) AS level_template_id,
        CAST(level_dup_para_id AS INTEGER) AS level_dup_para_id,
        trim(CAST(level_dup_para AS VARCHAR)) AS level_dup_para,
        trim(CAST(uid AS VARCHAR)) AS uid,
        log_date,
        sum(daily_level_enter) AS entries,
        sum(daily_level_pass) AS wins,
        sum(daily_level_use_life) * 200
            + sum(daily_level_use_life_when_unlimited) * 100
            + sum(daily_level_use_undo) * 600
            + sum(daily_level_use_propeller) * 900
            + sum(daily_level_use_magneto) * 900
            + sum(daily_level_use_ingame_extraspace_when_unlimited) * 1200
            + sum(daily_level_use_ingame_extraspace) * 600
            + (sum(daily_level_use_bag_spring)
                + sum(daily_level_use_bag_spring_iap)
                + sum(daily_level_use_bag_spring_ad)) * 900 AS weighted_cost
    FROM level_rows
    WHERE nullif(trim(CAST(uid AS VARCHAR)), '') IS NOT NULL
      AND nullif(trim(CAST(level_id AS VARCHAR)), '') IS NOT NULL
      AND lower(trim(CAST(level_id AS VARCHAR))) <> 'unknown'
      AND nullif(trim(CAST(level_template_id AS VARCHAR)), '') IS NOT NULL
      AND try_cast(level_dup_para_id AS INTEGER) BETWEEN 1 AND 10
      AND regexp_like(trim(CAST(level_dup_para AS VARCHAR)), '^[0-9]+[|]-?[0-9]+[|][0-9]+[|][0-9]+$')
      AND (
          {{ cohort_shard }} = -1
          OR bitwise_and(from_big_endian_64(xxhash64(
              to_utf8(concat(product, ':', trim(CAST(uid AS VARCHAR))))
          )), 255) = {{ cohort_shard }}
      )
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
),

player_parameters AS (
    SELECT
        product, level_index, level_id, level_template_id,
        level_dup_para_id, level_dup_para, uid,
        sum(entries) AS entries,
        sum(wins) AS wins,
        IF(count_if(weighted_cost IS NULL) = 0, sum(weighted_cost)) AS weighted_cost
    FROM daily_player_parameters
    GROUP BY 1, 2, 3, 4, 5, 6, 7
),

cohort_parameters AS (
    SELECT
        product,
        level_index,
        level_id,
        level_template_id,
        level_dup_para_id,
        level_dup_para,
        count_if(entries > 0) AS entrants,
        count(*) AS player_levels,
        count(weighted_cost) AS observed_cost_player_levels,
        sum(entries) AS attempts,
        sum(wins) AS wins,
        sum(weighted_cost) AS weighted_cost_total
    FROM player_parameters
    GROUP BY 1, 2, 3, 4, 5, 6
)

SELECT
    DATE '{{ date.start }}' AS cohort_start,
    DATE '{{ date.end }}' AS cohort_end,
    {{ cohort_shard }} AS cohort_shard,
    'level_report_weighted_resources_v1' AS cost_definition,
    product,
    level_index,
    level_id,
    level_template_id,
    level_dup_para_id,
    level_dup_para,
    entrants,
    player_levels,
    observed_cost_player_levels,
    attempts,
    wins,
    CASE WHEN player_levels = observed_cost_player_levels THEN weighted_cost_total END
        AS weighted_cost_total,
    CASE WHEN player_levels = observed_cost_player_levels
        THEN CAST(weighted_cost_total AS DOUBLE) / NULLIF(player_levels, 0)
    END AS mean_consumption
FROM cohort_parameters
WHERE entrants > 0
ORDER BY product, level_index, level_id, level_template_id, level_dup_para_id, level_dup_para
