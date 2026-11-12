from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from build_behavior_stratification_v1 import (
    PRIMARY_AXIS,
    REPLICATED,
    build_instance_layers,
    build_level_summary,
)


METRIC_INFO: dict[str, dict[str, str]] = {
    "duration_per_click_median": {
        "role": "主分层",
        "axis": "节奏卡顿",
        "definition": "每次点击动作自己的间隔时长中位数，与该玩家其它关卡相比是否变慢。",
        "interpretation": "升高表示玩家在这类局面平均需要更久决策。",
    },
    "pressure_certain_share": {
        "role": "主分层",
        "axis": "背包压力",
        "definition": "玩家实际经过的确定高背包占用状态，在全部可分析点击中的占比。",
        "interpretation": "升高表示本局更频繁把玩家推入高压背包状态。",
    },
    "visible_narrow_share": {
        "role": "主分层",
        "axis": "路径压缩",
        "definition": "当前可见牌与背包中，可继续形成三消的花色不超过1种的点击占比。",
        "interpretation": "升高表示玩家面对的可行选择更窄。",
    },
    "visible_forced_short_window_share": {
        "role": "主分层",
        "axis": "路径压缩",
        "definition": "高压局面下，短窗搜索找不到在背包装满前完成下一次三消路径的占比。",
        "interpretation": "升高表示短期内更容易无路可走。",
    },
    "visible_no_solution_share": {
        "role": "主分层",
        "axis": "路径压缩",
        "definition": "背包与当前至少露出四分之一的牌中，没有任何花色能凑满3张的点击占比。",
        "interpretation": "这是玩家当前信息集的可见无解，不等同全局无解。",
    },
    "visible_no_solution_dwell_median_sec": {
        "role": "主分层",
        "axis": "认知停滞",
        "definition": "处于可见无解局面时，玩家下一步操作前的思考时长中位数。",
        "interpretation": "升高表示玩家在无明显组合的局面停顿更久。",
    },
    "selected_nonviable_share": {
        "role": "主分层",
        "axis": "选择陷阱",
        "definition": "存在更安全替代选择时，玩家仍选择短窗不可行路径的占比。",
        "interpretation": "升高表示局面更容易诱导玩家关闭可行路径。",
    },
    "partial_visible_share_median": {
        "role": "玩法描述",
        "axis": "信息暴露",
        "definition": "至少露出四分之一但尚不可点击的牌，占当前剩余牌的比例中位数。",
        "interpretation": "描述半露信息负荷，没有默认好坏方向。",
    },
    "selected_new_visible_median": {
        "role": "玩法描述",
        "axis": "释放偏好",
        "definition": "玩家一次点击后新增可见牌数量的中位数。",
        "interpretation": "描述玩家是否在持续释放下层牌，没有默认好坏方向。",
    },
    "selected_immediate_triple_share": {
        "role": "玩法描述",
        "axis": "消除偏好",
        "definition": "可立即完成三消时，玩家选择该类牌的占比。",
        "interpretation": "描述即时消除倾向，没有默认好坏方向。",
    },
    "direct_late_minus_early_gap_sec": {
        "role": "辅助证据",
        "axis": "后段减速",
        "definition": "对局后段与前段的每点击思考时长差。",
        "interpretation": "用于判断残局是否明显变慢，不单独决定主层。",
    },
    "pressure_recovery_rate": {
        "role": "辅助证据",
        "axis": "压力恢复",
        "definition": "进入高压背包状态后，玩家重新回落到安全状态的比例。",
        "interpretation": "恢复率低说明高压更难自行解除。",
    },
    "manual_prop_per_100_clicks": {
        "role": "辅助证据",
        "axis": "道具依赖",
        "definition": "每100次点击中玩家主动使用局内道具的次数。",
        "interpretation": "只作结果辅助，不把活动自动背包+1算作主动选择。",
    },
    "fail_rate": {
        "role": "结果校验",
        "axis": "失败结果",
        "definition": "玩家在该实例阶段的失败比例。",
        "interpretation": "用于验证过程负担是否带来失败，不直接决定主层。",
    },
    "attempt_count": {
        "role": "结果校验",
        "axis": "尝试负担",
        "definition": "同一玩家在该实例阶段贡献的有效尝试次数。",
        "interpretation": "用于观察重试负担，不直接决定主层。",
    },
    "hidden_information_dependency_share": {
        "role": "辅助证据",
        "axis": "隐藏信息依赖",
        "definition": "可见短窗无路，但全知短窗存在路径的高压局面占比。",
        "interpretation": "升高表示正确选择更依赖玩家尚不可见的信息。",
    },
    "pressure_visible_no_solution_share": {
        "role": "辅助证据",
        "axis": "高压无解重合",
        "definition": "高背包压力与可见无解同时出现的点击占比。",
        "interpretation": "用于定位压力与无明显组合重叠的局面。",
    },
}


LAYER_DEFINITIONS = [
    ["L4_多维高负担", "至少3个主分层高负担轴", "多种实时困难同时出现，优先复盘实际卡点。"],
    ["L3_复合卡点", "2个主分层高负担轴", "存在稳定复合卡点。"],
    ["L2_单点卡顿", "1个主分层高负担轴", "存在稳定单一过程问题。"],
    ["L1_稳定低负担", "无高负担轴，至少1个稳定低负担轴", "相对玩家自己的正常水平更顺畅。"],
    ["L0_未见稳定过程差异", "玩家数不少于10，未检出稳定过程差异", "不等于绝对简单，只表示没有跨玩家一致的异常。"],
    ["P_待补样", "实例阶段玩家数少于10", "保留实例，不强行分层。"],
]


def burden_direction(effect: float, orientation: int) -> str:
    if orientation == 0 or abs(effect) <= 1e-12:
        return "描述性"
    return "高负担" if effect * orientation > 0 else "低负担"


def load_raw_sources(paths: list[Path]) -> tuple[pd.DataFrame, dict[str, Any]]:
    columns = [
        "product",
        "user_id",
        "cohort_max_level",
        "cohort_max_lt",
        "cohort_covered_level_count",
        "event_date",
        "client_version",
        "platform",
        "level_index",
        "level_status",
        "replay_info",
    ]
    frames = [pd.read_csv(path, usecols=columns, low_memory=False) for path in paths]
    raw = pd.concat(frames, ignore_index=True)
    players = raw.drop_duplicates("user_id")
    valid = raw[raw["level_status"].isin(["pass", "fail"])]
    replay = valid["replay_info"].fillna("")
    actions = int((replay.str.count(r"\|") + replay.ne("").astype(int)).sum())
    summary = {
        "players": int(raw["user_id"].nunique()),
        "level_indices": int(raw["level_index"].nunique()),
        "raw_attempt_rows": int(len(raw)),
        "valid_outcome_attempts": int(len(valid)),
        "excluded_exit_attempts": int((raw["level_status"] == "exit").sum()),
        "decision_actions": actions,
        "date_min": str(raw["event_date"].min()),
        "date_max": str(raw["event_date"].max()),
        "product": "、".join(sorted(raw["product"].dropna().astype(str).unique())),
        "platform": "、".join(sorted(raw["platform"].dropna().astype(str).unique())),
        "client_version_min": str(raw["client_version"].dropna().min()),
        "client_version_max": str(raw["client_version"].dropna().max()),
        "player_max_level_min": int(players["cohort_max_level"].min()),
        "player_max_level_median": float(players["cohort_max_level"].median()),
        "player_max_lt_min": int(players["cohort_max_lt"].min()),
        "players_covering_all_100_levels": int(
            players["cohort_covered_level_count"].eq(100).sum()
        ),
    }
    return raw, summary


def build_all_cohort_instances(
    profiles: pd.DataFrame, evidence: pd.DataFrame
) -> pd.DataFrame:
    empty_validation = pd.DataFrame(
        columns=["business_instance_id", "attempt_phase", "metric", "validation_status"]
    )
    instances = build_instance_layers(
        profiles, evidence, empty_validation, combined_label="550人"
    )
    formal = evidence[
        evidence["scope"].eq("业务实例")
        & evidence["replication_status"].isin(REPLICATED)
        & evidence["metric"].isin(PRIMARY_AXIS)
    ].copy()
    formal["is_high_confidence"] = formal["replication_status"].eq("高置信复刻")
    grouped = (
        formal.groupby(["business_instance_id", "attempt_phase"], as_index=False)
        .agg(
            primary_formal_signal_count=("metric", "size"),
            primary_high_confidence_signal_count=("is_high_confidence", "sum"),
            primary_direction_agreement_median=("direction_agreement", "median"),
            primary_wilson90_low_median=("direction_agreement_wilson90_low", "median"),
            primary_split_half_agreement_median=(
                "split_half_direction_agreement",
                "median",
            ),
            primary_leave_one_out_flip_max=(
                "leave_one_player_out_sign_flip_rate",
                "max",
            ),
        )
    )
    instances = instances.merge(
        grouped, on=["business_instance_id", "attempt_phase"], how="left"
    )
    count_cols = [
        "primary_formal_signal_count",
        "primary_high_confidence_signal_count",
    ]
    instances[count_cols] = instances[count_cols].fillna(0).astype(int)
    instances["sample_coverage"] = pd.cut(
        instances["player_count"],
        bins=[-1, 4, 9, 19, float("inf")],
        labels=["D_少于5人", "C_5至9人", "B_10至19人", "A_至少20人"],
    ).astype(str)

    def stability(row: pd.Series) -> str:
        if row["player_count"] < 10:
            return "待补样"
        if row["primary_formal_signal_count"] == 0:
            return "当前未检出稳定主信号"
        if (
            row["player_count"] >= 20
            and row["primary_high_confidence_signal_count"] > 0
            and row["primary_split_half_agreement_median"] >= 0.95
        ):
            return "高稳定"
        return "稳定"

    instances["layer_stability"] = instances.apply(stability, axis=1)
    columns = [
        "level_index",
        "attempt_phase",
        "behavior_layer",
        "layer_order",
        "layer_stability",
        "player_count",
        "sample_coverage",
        "business_instance_key",
        "business_instance_id",
        "level_template_id",
        "level_dup_para_id",
        "level_dup_para",
        "primary_high_axis_count",
        "primary_high_axes",
        "primary_low_axes",
        "primary_high_evidence",
        "primary_low_evidence",
        "descriptive_behavior",
        "supporting_evidence",
        "primary_formal_signal_count",
        "primary_high_confidence_signal_count",
        "primary_direction_agreement_median",
        "primary_wilson90_low_median",
        "primary_split_half_agreement_median",
        "primary_leave_one_out_flip_max",
        "fail_rate_median",
        "duration_per_click_median",
        "pressure_certain_share_median",
        "visible_no_solution_share_median",
        "manual_prop_per_100_clicks_median",
        "geometry_click_coverage_median",
        "pattern_point_coverage_median",
        "tower_attempt_rate",
        "cn_attempt_rate",
    ]
    return instances[columns]


def review_priority(label: str) -> str:
    return {
        "多数玩家遇到复合卡点": "优先验收：复合卡点",
        "部分玩家遇到复合卡点": "重点抽查：部分实例存在复合卡点",
        "多数玩家遇到局部卡顿": "重点抽查：局部卡顿",
        "部分玩家遇到局部卡顿": "常规抽查：部分玩家卡顿",
        "整体未见稳定高负担": "常规监控",
        "样本覆盖不足": "继续补样",
    }.get(label, "")


def build_level_table(levels: pd.DataFrame) -> pd.DataFrame:
    enriched = levels.copy()
    total = enriched["instance_player_weight"].replace(0, pd.NA)
    for layer in ["L4", "L3", "L2", "L1", "L0"]:
        enriched[f"{layer}_share"] = enriched[f"{layer}_player_weight"] / total
    enriched["pending_share"] = enriched["pending_player_weight"] / total
    first = enriched[enriched["attempt_phase"].eq("首局")].copy()
    retry = enriched[enriched["attempt_phase"].eq("非首局")].copy()
    first["review_priority"] = first["level_behavior_label"].map(review_priority)
    first = first.add_prefix("first_").rename(columns={"first_level_index": "level_index"})
    retry = retry.add_prefix("retry_").rename(columns={"retry_level_index": "level_index"})
    merged = first.merge(retry, on="level_index", how="outer").sort_values("level_index")
    columns = [
        "level_index",
        "first_level_behavior_label",
        "first_review_priority",
        "first_instance_count",
        "first_instance_player_weight",
        "first_classified_player_weight_share",
        "first_compound_high_burden_share",
        "first_any_friction_share",
        "first_L4_share",
        "first_L3_share",
        "first_L2_share",
        "first_L1_share",
        "first_L0_share",
        "first_pending_share",
        "first_main_high_burden_axes",
        "first_top_instances",
        "retry_level_behavior_label",
        "retry_instance_player_weight",
        "retry_classified_player_weight_share",
        "retry_compound_high_burden_share",
        "retry_any_friction_share",
        "retry_pending_share",
        "retry_main_high_burden_axes",
    ]
    return merged[columns]


def build_metric_table(evidence: pd.DataFrame) -> pd.DataFrame:
    scoped = evidence[evidence["scope"].eq("业务实例")].copy()
    scoped["is_formal"] = scoped["replication_status"].isin(REPLICATED)
    scoped["burden_direction"] = scoped.apply(
        lambda row: burden_direction(
            float(row["player_relative_effect_median"]), int(row["burden_orientation"])
        ),
        axis=1,
    )
    rows: list[dict[str, Any]] = []
    for metric, frame in scoped.groupby("metric", sort=False):
        formal = frame[frame["is_formal"]]
        info = METRIC_INFO.get(metric, {})
        eligible = frame[frame["player_count"].ge(10)]
        bootstrap_stable = (
            formal["bootstrap90_low"].gt(0) | formal["bootstrap90_high"].lt(0)
        )
        split_exact = formal["split_half_direction_agreement"].eq(1)
        loo_stable = formal["leave_one_player_out_sign_flip_rate"].eq(0)
        formal_count = len(formal)
        if formal_count >= 100:
            stability = "正式信号充足，对半抽样高度稳定"
        elif formal_count > 0:
            stability = "存在正式信号，但覆盖实例较少"
        else:
            stability = "当前未形成正式实例信号"
        rows.append(
            {
                "role": info.get("role", "辅助证据"),
                "axis": info.get("axis", str(frame["dimension"].iloc[0])),
                "metric": metric,
                "metric_label": str(frame["metric_label"].iloc[0]),
                "definition": info.get("definition", ""),
                "interpretation": info.get("interpretation", ""),
                "candidate_instance_phase_count": len(frame),
                "eligible_n_ge_10_count": len(eligible),
                "formal_signal_count": formal_count,
                "formal_rate_among_eligible": (
                    formal_count / len(eligible) if len(eligible) else None
                ),
                "high_burden_signal_count": int(
                    formal["burden_direction"].eq("高负担").sum()
                ),
                "low_burden_signal_count": int(
                    formal["burden_direction"].eq("低负担").sum()
                ),
                "descriptive_signal_count": int(
                    formal["burden_direction"].eq("描述性").sum()
                ),
                "high_confidence_signal_count": int(
                    formal["replication_status"].eq("高置信复刻").sum()
                ),
                "formal_player_count_median": formal["player_count"].median(),
                "direction_agreement_median": formal["direction_agreement"].median(),
                "wilson90_low_median": formal[
                    "direction_agreement_wilson90_low"
                ].median(),
                "split_half_agreement_median": formal[
                    "split_half_direction_agreement"
                ].median(),
                "split_half_exact_share": split_exact.mean() if formal_count else None,
                "bootstrap_excludes_zero_share": (
                    bootstrap_stable.mean() if formal_count else None
                ),
                "leave_one_out_no_flip_share": (
                    loo_stable.mean() if formal_count else None
                ),
                "stability_summary": stability,
            }
        )
    order = {"主分层": 0, "玩法描述": 1, "辅助证据": 2, "结果校验": 3}
    result = pd.DataFrame(rows)
    result["role_order"] = result["role"].map(order).fillna(9)
    return result.sort_values(
        ["role_order", "formal_signal_count", "metric_label"],
        ascending=[True, False, True],
    ).drop(columns="role_order")


def build_summary(
    sample: dict[str, Any],
    instances: pd.DataFrame,
    levels: pd.DataFrame,
    metrics: pd.DataFrame,
    evidence: pd.DataFrame,
) -> dict[str, Any]:
    phase_layers = (
        instances.groupby(["attempt_phase", "behavior_layer"])
        .agg(
            instance_count=("business_instance_id", "size"),
            player_weight=("player_count", "sum"),
        )
        .reset_index()
    )
    phase_total = phase_layers.groupby("attempt_phase")["player_weight"].transform("sum")
    phase_layers["player_weight_share"] = phase_layers["player_weight"] / phase_total
    formal = metrics["formal_signal_count"].sum()
    formal_evidence = evidence[
        evidence["scope"].eq("业务实例")
        & evidence["replication_status"].isin(REPLICATED)
    ]
    bootstrap_stable = formal_evidence["bootstrap90_low"].gt(0) | formal_evidence[
        "bootstrap90_high"
    ].lt(0)
    first_levels = levels[levels["attempt_phase"].eq("首局")]
    first_l4 = instances[
        instances["attempt_phase"].eq("首局")
        & instances["behavior_layer"].eq("L4_多维高负担")
    ]
    return {
        "sample": sample,
        "instance_phase_rows": int(len(instances)),
        "business_instances": int(instances["business_instance_id"].nunique()),
        "formal_instance_metric_signals": int(formal),
        "formal_signal_stability": {
            "direction_agreement_median": float(
                formal_evidence["direction_agreement"].median()
            ),
            "wilson90_low_median": float(
                formal_evidence["direction_agreement_wilson90_low"].median()
            ),
            "split_half_agreement_mean": float(
                formal_evidence["split_half_direction_agreement"].mean()
            ),
            "split_half_agreement_median": float(
                formal_evidence["split_half_direction_agreement"].median()
            ),
            "split_half_exact_share": float(
                formal_evidence["split_half_direction_agreement"].eq(1).mean()
            ),
            "bootstrap_excludes_zero_share": float(bootstrap_stable.mean()),
            "leave_one_out_no_flip_share": float(
                formal_evidence["leave_one_player_out_sign_flip_rate"].eq(0).mean()
            ),
            "n_ge_20_share": float(formal_evidence["player_count"].ge(20).mean()),
        },
        "phase_layer_counts": phase_layers.to_dict(orient="records"),
        "first_level_label_counts": first_levels["level_behavior_label"].value_counts().to_dict(),
        "first_classified_player_weight_share": float(
            1
            - phase_layers.loc[
                phase_layers["attempt_phase"].eq("首局")
                & phase_layers["behavior_layer"].eq("P_待补样"),
                "player_weight_share",
            ].sum()
        ),
        "retry_classified_player_weight_share": float(
            1
            - phase_layers.loc[
                phase_layers["attempt_phase"].eq("非首局")
                & phase_layers["behavior_layer"].eq("P_待补样"),
                "player_weight_share",
            ].sum()
        ),
        "first_l4_instances": first_l4[
            [
                "level_index",
                "business_instance_key",
                "player_count",
                "primary_high_axes",
                "layer_stability",
            ]
        ].to_dict(orient="records"),
        "top_first_levels": first_levels.nlargest(10, "compound_high_burden_share")[
            [
                "level_index",
                "level_behavior_label",
                "compound_high_burden_share",
                "any_friction_share",
                "main_high_burden_axes",
            ]
        ].to_dict(orient="records"),
        "deployment_assessment": {
            "current_range": "可用于100至199关的正式行为分层和策划验收排序。",
            "same_population_online_levels": "可以批量用于同产品、同平台且拥有足够有效玩家样本的线上关卡。",
            "other_populations": "新手、iOS、付费玩家或不同版本结构需要按相同流程单独复刻。",
            "prelaunch_levels": "没有真实玩家数据的新关不能直接使用本分层，需要状态引擎与模拟玩家提供预测。",
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--raw-source", type=Path, action="append", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.staging_dir.mkdir(parents=True, exist_ok=True)
    _, sample = load_raw_sources(args.raw_source)
    profiles = pd.read_csv(args.input_dir / "05_实例行为画像.csv")
    evidence = pd.read_csv(args.input_dir / "04_实例维度复刻证据.csv")
    instances = build_all_cohort_instances(profiles, evidence)
    levels_long = build_level_summary(instances)
    levels = build_level_table(levels_long)
    metrics = build_metric_table(evidence)
    summary = build_summary(sample, instances, levels_long, metrics, evidence)

    level_path = args.output_dir / "15_550人关卡号完整分层表.csv"
    instance_path = args.output_dir / "16_550人实例完整分层表.csv"
    metric_path = args.output_dir / "17_550人行为维度稳定性表.csv"
    summary_path = args.output_dir / "18_550人全量分析摘要.json"
    levels.to_csv(level_path, index=False)
    instances.to_csv(instance_path, index=False)
    metrics.to_csv(metric_path, index=False)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    staging_paths = {}
    for name, frame in [
        ("levels", levels),
        ("instances", instances),
        ("metrics", metrics),
    ]:
        path = args.staging_dir / f"{name}.ndjson"
        frame.to_json(path, orient="records", lines=True, force_ascii=False)
        staging_paths[name] = {
            "path": str(path.resolve()),
            "rows": len(frame),
            "columns": frame.columns.tolist(),
        }
    manifest = {
        "title": "100至199关玩家行为分层结论（550人）",
        "subtitle": "tile01g Android高进度玩家，2026-07-01至2026-08-31",
        "summary": summary,
        "layers": LAYER_DEFINITIONS,
        **staging_paths,
    }
    (args.staging_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
