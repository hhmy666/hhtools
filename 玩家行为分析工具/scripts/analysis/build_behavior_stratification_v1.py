from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd


REPLICATED = {"已复刻", "高置信复刻"}
INDEPENDENT = {"独立强复刻", "独立方向复刻"}
CONFLICT = {"方向相反", "新增样本效应归零"}

# 第一版主分层只使用玩家实时解谜过程；结果、道具和模板静态特征不决定主层。
PRIMARY_AXIS = {
    "duration_per_click_median": "节奏卡顿",
    "pressure_certain_share": "背包压力",
    "visible_narrow_share": "路径压缩",
    "visible_forced_short_window_share": "路径压缩",
    "visible_no_solution_share": "路径压缩",
    "visible_no_solution_dwell_median_sec": "认知停滞",
    "selected_nonviable_share": "选择陷阱",
}

DESCRIPTIVE_METRICS = {
    "partial_visible_share_median": ("半露牌负荷偏高", "半露牌负荷偏低"),
    "selected_new_visible_median": ("单步新露出偏多", "单步新露出偏少"),
    "selected_immediate_triple_share": ("更偏即时三消", "较少选即时三消"),
}

SUPPORT_METRICS = {
    "direct_late_minus_early_gap_sec",
    "pressure_recovery_rate",
    "manual_prop_per_100_clicks",
    "fail_rate",
    "attempt_count",
    "hidden_information_dependency_share",
    "pressure_visible_no_solution_share",
}

LAYER_ORDER = {
    "L4_多维高负担": 4,
    "L3_复合卡点": 3,
    "L2_单点卡顿": 2,
    "L1_稳定低负担": 1,
    "L0_未见稳定过程差异": 0,
    "P_待补样": -1,
}


def burden_direction(effect: float, orientation: int) -> str:
    if orientation == 0 or abs(effect) <= 1e-12:
        return "描述性"
    return "高负担" if effect * orientation > 0 else "低负担"


def assign_layer(player_count: int, high_axes: set[str], low_axes: set[str]) -> str:
    if player_count < 10:
        return "P_待补样"
    if len(high_axes) >= 3:
        return "L4_多维高负担"
    if len(high_axes) == 2:
        return "L3_复合卡点"
    if len(high_axes) == 1:
        return "L2_单点卡顿"
    if low_axes:
        return "L1_稳定低负担"
    return "L0_未见稳定过程差异"


def evidence_grade(
    player_count: int,
    validation_statuses: list[str],
    conflict_count: int,
    combined_label: str = "150人",
) -> str:
    if player_count < 10:
        base = "D_待补样"
    elif any(value in INDEPENDENT for value in validation_statuses):
        base = "A_新玩家独立复刻"
    elif player_count >= 20:
        base = f"B_{combined_label}高覆盖"
    else:
        base = f"C_{combined_label}稳定"
    return f"{base}+含{conflict_count}条验证冲突" if conflict_count else base


def joined(values: list[str]) -> str:
    return "|".join(value for value in values if value)


def build_instance_layers(
    profiles: pd.DataFrame,
    evidence: pd.DataFrame,
    validation: pd.DataFrame,
    combined_label: str = "150人",
) -> pd.DataFrame:
    validation_map = {
        (row.business_instance_id, row.attempt_phase, row.metric): str(
            row.validation_status
        )
        for row in validation.itertuples(index=False)
    }
    formal = evidence[
        evidence["scope"].eq("业务实例")
        & evidence["replication_status"].isin(REPLICATED)
    ].copy()
    grouped = {
        key: frame.copy()
        for key, frame in formal.groupby(
            ["business_instance_id", "attempt_phase"], sort=False
        )
    }

    rows: list[dict[str, Any]] = []
    for profile in profiles.itertuples(index=False):
        key = (profile.business_instance_id, profile.attempt_phase)
        frame = grouped.get(key, pd.DataFrame())
        high_axes: set[str] = set()
        low_axes: set[str] = set()
        independent_high_axes: set[str] = set()
        high_evidence: list[tuple[float, str]] = []
        low_evidence: list[tuple[float, str]] = []
        support_evidence: list[tuple[float, str]] = []
        descriptive: list[tuple[float, str]] = []
        statuses: list[str] = []
        conflicts: list[str] = []

        for signal in frame.itertuples(index=False):
            status = validation_map.get(key + (signal.metric,), "")
            if status:
                statuses.append(status)
            effect = float(signal.player_relative_effect_median)
            direction = burden_direction(effect, int(signal.burden_orientation))
            label = f"{signal.metric_label}({effect:+.2f})"
            if status in CONFLICT:
                conflicts.append(f"{signal.metric_label}:{status}")
                continue

            axis = PRIMARY_AXIS.get(signal.metric)
            if axis and direction == "高负担":
                high_axes.add(axis)
                high_evidence.append((abs(effect), label))
                if status in INDEPENDENT:
                    independent_high_axes.add(axis)
            elif axis and direction == "低负担":
                low_axes.add(axis)
                low_evidence.append((abs(effect), label))
            elif signal.metric in DESCRIPTIVE_METRICS and effect != 0:
                labels = DESCRIPTIVE_METRICS[signal.metric]
                descriptive.append((abs(effect), labels[0 if effect > 0 else 1]))
            elif signal.metric in SUPPORT_METRICS:
                support_evidence.append((abs(effect), label))

        high_evidence.sort(reverse=True)
        low_evidence.sort(reverse=True)
        support_evidence.sort(reverse=True)
        descriptive.sort(reverse=True)
        layer = assign_layer(int(profile.player_count), high_axes, low_axes)
        rows.append(
            {
                "level_index": int(profile.level_index),
                "attempt_phase": profile.attempt_phase,
                "behavior_layer": layer,
                "layer_order": LAYER_ORDER[layer],
                "evidence_grade": evidence_grade(
                    int(profile.player_count), statuses, len(conflicts), combined_label
                ),
                "player_count": int(profile.player_count),
                "sample_reliability": profile.sample_reliability,
                "business_instance_key": profile.business_instance_key,
                "business_instance_id": profile.business_instance_id,
                "level_template_id": profile.level_template_id,
                "level_dup_para_id": profile.level_dup_para_id,
                "level_dup_para": profile.level_dup_para,
                "primary_high_axis_count": len(high_axes),
                "primary_high_axes": joined(sorted(high_axes)),
                "independent_high_axes": joined(sorted(independent_high_axes)),
                "primary_low_axes": joined(sorted(low_axes)),
                "primary_high_evidence": joined([x[1] for x in high_evidence]),
                "primary_low_evidence": joined([x[1] for x in low_evidence]),
                "descriptive_behavior": joined([x[1] for x in descriptive]),
                "supporting_evidence": joined([x[1] for x in support_evidence]),
                "validation_conflicts": joined(conflicts),
                "fail_rate_median": profile.fail_rate_median,
                "duration_per_click_median": profile.duration_per_click_median,
                "pressure_certain_share_median": profile.pressure_certain_share_median,
                "visible_no_solution_share_median": profile.visible_no_solution_share_median,
                "manual_prop_per_100_clicks_median": profile.manual_prop_per_100_clicks_median,
                "geometry_click_coverage_median": profile.geometry_click_coverage_median,
                "pattern_point_coverage_median": profile.pattern_point_coverage_median,
                "tower_attempt_rate": profile.tower_attempt_rate,
                "cn_attempt_rate": profile.cn_attempt_rate,
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["attempt_phase", "layer_order", "player_count", "level_index"],
        ascending=[False, False, False, True],
    )


def dominant_weighted_layer(frame: pd.DataFrame) -> str:
    weights = frame.groupby("behavior_layer")["player_count"].sum()
    if weights.empty:
        return ""
    return str(weights.sort_values(ascending=False).index[0])


def level_label(high_share: float, friction_share: float, coverage: float) -> str:
    if coverage < 0.70:
        return "样本覆盖不足"
    if high_share >= 0.50:
        return "多数玩家遇到复合卡点"
    if high_share >= 0.25:
        return "部分玩家遇到复合卡点"
    if friction_share >= 0.50:
        return "多数玩家遇到局部卡顿"
    if friction_share >= 0.25:
        return "部分玩家遇到局部卡顿"
    return "整体未见稳定高负担"


def build_level_summary(instances: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (level_index, phase), frame in instances.groupby(
        ["level_index", "attempt_phase"], sort=True
    ):
        total_weight = int(frame["player_count"].sum())
        classified = frame[frame["layer_order"].ge(0)]
        classified_weight = int(classified["player_count"].sum())
        layer_weight = frame.groupby("behavior_layer")["player_count"].sum()
        high_weight = int(
            layer_weight.reindex(
                ["L4_多维高负担", "L3_复合卡点"], fill_value=0
            ).sum()
        )
        friction_weight = high_weight + int(
            layer_weight.get("L2_单点卡顿", 0)
        )
        coverage = classified_weight / total_weight if total_weight else 0.0
        high_share = high_weight / total_weight if total_weight else 0.0
        friction_share = friction_weight / total_weight if total_weight else 0.0
        axes = Counter()
        for row in classified.itertuples(index=False):
            for axis in str(row.primary_high_axes).split("|"):
                if axis:
                    axes[axis] += int(row.player_count)
        top_instances = frame.sort_values(
            ["layer_order", "player_count"], ascending=False
        ).head(3)
        rows.append(
            {
                "level_index": int(level_index),
                "attempt_phase": phase,
                "level_behavior_label": level_label(
                    high_share, friction_share, coverage
                ),
                "dominant_layer": dominant_weighted_layer(frame),
                "instance_count": len(frame),
                "instance_player_weight": total_weight,
                "classified_player_weight_share": coverage,
                "compound_high_burden_share": high_share,
                "any_friction_share": friction_share,
                "L4_player_weight": int(layer_weight.get("L4_多维高负担", 0)),
                "L3_player_weight": int(layer_weight.get("L3_复合卡点", 0)),
                "L2_player_weight": int(layer_weight.get("L2_单点卡顿", 0)),
                "L1_player_weight": int(layer_weight.get("L1_稳定低负担", 0)),
                "L0_player_weight": int(
                    layer_weight.get("L0_未见稳定过程差异", 0)
                ),
                "pending_player_weight": int(layer_weight.get("P_待补样", 0)),
                "main_high_burden_axes": joined(
                    [name for name, _ in axes.most_common()]
                ),
                "top_instances": "；".join(
                    top_instances["business_instance_key"].astype(str).tolist()
                ),
            }
        )
    return pd.DataFrame(rows)


def build_summary(instances: pd.DataFrame, levels: pd.DataFrame) -> dict[str, Any]:
    phase_layers = (
        instances.groupby(["attempt_phase", "behavior_layer"])
        .agg(instance_count=("business_instance_id", "size"), player_weight=("player_count", "sum"))
        .reset_index()
        .to_dict(orient="records")
    )
    first = levels[levels["attempt_phase"].eq("首局")]
    return {
        "instance_rows": len(instances),
        "level_rows": len(levels),
        "phase_layer_counts": phase_layers,
        "first_phase_levels_with_compound_high_burden_share_ge_25pct": int(
            first["compound_high_burden_share"].ge(0.25).sum()
        ),
        "first_phase_levels_with_any_friction_share_ge_25pct": int(
            first["any_friction_share"].ge(0.25).sum()
        ),
        "classification_rule": {
            "L4": "至少3个实时过程高负担轴",
            "L3": "2个实时过程高负担轴",
            "L2": "1个实时过程高负担轴",
            "L1": "无高负担轴，且至少1个稳定低负担轴",
            "L0": "样本>=10且未见稳定实时过程差异",
            "P": "实例阶段玩家数<10",
        },
    }


def build_layer_migration(
    previous: pd.DataFrame, current: pd.DataFrame, previous_label: str, current_label: str
) -> tuple[pd.DataFrame, dict[str, Any]]:
    keys = ["business_instance_id", "attempt_phase"]
    left = previous[
        keys + ["level_index", "business_instance_key", "behavior_layer", "player_count"]
    ].rename(
        columns={
            "behavior_layer": "previous_layer",
            "player_count": "previous_player_count",
            "level_index": "previous_level_index",
            "business_instance_key": "previous_instance_key",
        }
    )
    right = current[
        keys + ["level_index", "business_instance_key", "behavior_layer", "player_count"]
    ].rename(
        columns={
            "behavior_layer": "current_layer",
            "player_count": "current_player_count",
        }
    )
    merged = left.merge(right, on=keys, how="outer", indicator=True)
    merged["level_index"] = merged["level_index"].fillna(merged["previous_level_index"])
    merged["business_instance_key"] = merged["business_instance_key"].fillna(
        merged["previous_instance_key"]
    )
    merged["migration_type"] = ""
    shared = merged["_merge"].eq("both")
    same = shared & merged["previous_layer"].eq(merged["current_layer"])
    resolved = (
        shared
        & merged["previous_layer"].eq("P_待补样")
        & merged["current_layer"].ne("P_待补样")
    )
    merged.loc[same, "migration_type"] = "等级不变"
    merged.loc[resolved, "migration_type"] = "待补样已定级"
    merged.loc[shared & ~same & ~resolved, "migration_type"] = "等级调整"
    merged.loc[merged["_merge"].eq("left_only"), "migration_type"] = "仅旧样本存在"
    merged.loc[merged["_merge"].eq("right_only"), "migration_type"] = "新增实例阶段"
    merged["previous_cohort"] = previous_label
    merged["current_cohort"] = current_label
    output = merged[
        [
            "level_index",
            "attempt_phase",
            "business_instance_key",
            "business_instance_id",
            "previous_cohort",
            "current_cohort",
            "previous_layer",
            "current_layer",
            "previous_player_count",
            "current_player_count",
            "migration_type",
        ]
    ].sort_values(["migration_type", "attempt_phase", "level_index", "business_instance_key"])
    previously_classified = shared & merged["previous_layer"].ne("P_待补样")
    stable_classified = previously_classified & merged["previous_layer"].eq(
        merged["current_layer"]
    )
    summary = {
        "shared_instance_phases": int(shared.sum()),
        "previously_classified_shared": int(previously_classified.sum()),
        "previously_classified_same_layer": int(stable_classified.sum()),
        "previously_classified_same_layer_rate": (
            float(stable_classified.sum() / previously_classified.sum())
            if previously_classified.sum()
            else None
        ),
        "previous_pending_resolved": int(resolved.sum()),
        "new_instance_phases": int(merged["_merge"].eq("right_only").sum()),
    }
    return output, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path)
    parser.add_argument("--validation-file", default="08_旧100与新增50复刻验证.csv")
    parser.add_argument("--combined-label", default="150人")
    parser.add_argument("--previous-instance-file", type=Path)
    parser.add_argument("--previous-label", default="旧样本")
    parser.add_argument("--validation-label", default="新增验证集")
    parser.add_argument("--title", default="100–199关玩家实时行为分层 v1")
    parser.add_argument(
        "--cohort-description",
        default="150名完整覆盖玩家；旧100人发现集+新50人独立验证集",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    profiles = pd.read_csv(args.input_dir / "05_实例行为画像.csv")
    evidence = pd.read_csv(args.input_dir / "04_实例维度复刻证据.csv")
    validation = pd.read_csv(args.input_dir / args.validation_file)
    instances = build_instance_layers(
        profiles, evidence, validation, combined_label=args.combined_label
    )
    levels = build_level_summary(instances)
    summary = build_summary(instances, levels)
    validation_summary_path = args.input_dir / "独立样本验证摘要.json"
    validation_summary = (
        json.loads(validation_summary_path.read_text(encoding="utf-8"))
        if validation_summary_path.exists()
        else None
    )
    migration = None
    if args.previous_instance_file:
        previous = pd.read_csv(args.previous_instance_file)
        migration, migration_summary = build_layer_migration(
            previous, instances, args.previous_label, args.combined_label
        )
        summary["layer_migration"] = migration_summary
    instances.to_csv(args.output_dir / "12_第一版实例行为分层.csv", index=False)
    levels.to_csv(args.output_dir / "13_第一版关卡号汇总.csv", index=False)
    if migration is not None:
        migration.to_csv(
            args.output_dir / f"14_{args.previous_label}到{args.combined_label}分层迁移.csv",
            index=False,
        )
    (args.output_dir / "第一版行为分层摘要.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.staging_dir:
        args.staging_dir.mkdir(parents=True, exist_ok=True)
        instance_path = args.staging_dir / "instances.ndjson"
        level_path = args.staging_dir / "levels.ndjson"
        instances.to_json(instance_path, orient="records", lines=True, force_ascii=False)
        levels.to_json(level_path, orient="records", lines=True, force_ascii=False)
        migration_manifest = None
        if migration is not None:
            migration_path = args.staging_dir / "migration.ndjson"
            migration.to_json(
                migration_path, orient="records", lines=True, force_ascii=False
            )
            migration_manifest = {
                "path": str(migration_path.resolve()),
                "columns": migration.columns.tolist(),
                "rows": len(migration),
            }
        manifest = {
            "title": args.title,
            "source": args.cohort_description,
            "combined_label": args.combined_label,
            "previous_label": args.previous_label,
            "discovery_label": args.previous_label,
            "validation_label": args.validation_label,
            "summary": summary,
            "validation_summary": validation_summary,
            "instances": {
                "path": str(instance_path.resolve()),
                "columns": instances.columns.tolist(),
                "rows": len(instances),
            },
            "levels": {
                "path": str(level_path.resolve()),
                "columns": levels.columns.tolist(),
                "rows": len(levels),
            },
            "migration": migration_manifest,
        }
        (args.staging_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
