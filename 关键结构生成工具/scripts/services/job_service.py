"""Synchronous, UI-independent batch generation orchestration."""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from puzzle_core.config_io import (
    GenerationWorkItem,
    ResolvedBatchConfig,
    resolve_candidate_generation_options,
)
from puzzle_core.difficulty import difficulty_profile_from_simulation
from puzzle_core.errors import NoCandidateError, OperationCancelledError, PuzzleToolError
from puzzle_core.planning import (
    DerivedSeed,
    canonical_json,
    derive_candidate_seed_material,
)
from puzzle_core.models import ResolvedOutputPlan
from puzzle_core.search import (
    ALGORITHM_VERSION,
    Candidate,
    canonical_structure_identity,
    GenerationOptions,
    feasible_structure_count_limit,
    effective_structure_count_minimum,
    search_candidates,
)
from puzzle_core.validator import CandidateValidationReport, validate_candidate

from .import_service import (
    LoadedTemplate,
    TemplateInspection,
    inspect_loaded_template,
    load_template_file,
    load_template_id,
)
from .difficulty_profile_service import (
    load_difficulty_profile,
    load_difficulty_profiles,
)
from .profile_service import Profile
from .task_control import CooperativeTaskControl


JOB_FORMAT_VERSION = 1


def _candidate_experience_signature(candidate: Candidate) -> str:
    """Return a stable non-empty fingerprint, including legacy candidates."""

    signature = str(getattr(candidate, "experience_signature", "") or "").strip()
    return signature or candidate.structure_signature


@dataclass(frozen=True)
class GeneratedCandidate:
    candidate: Candidate
    validation: CandidateValidationReport
    generation_options: GenerationOptions | None = None
    is_recommended: bool = False
    recommendation_rank: int | None = None
    recommendation_reasons: tuple[str, ...] = ()
    resolved_plan: ResolvedOutputPlan | None = None
    parameter_source: str = ""
    parameter_config_hash: str = ""
    uses_independent_parameters: bool | None = None
    configured_parameters: Mapping[str, Any] | None = None
    uniform_parameters: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        plan = self.resolved_plan
        return {
            "candidate": self.candidate.as_dict(),
            "validation": self.validation.as_dict(),
            "resolved_generation_options": None
            if self.generation_options is None
            else asdict(self.generation_options),
            "is_recommended": self.is_recommended,
            "recommendation_rank": self.recommendation_rank,
            "recommendation_reasons": list(self.recommendation_reasons),
            "resolved_plan": None
            if plan is None
            else {
                "plan_id": plan.plan_id,
                "source_plan_id": plan.source_plan_id,
                "tower_mode": plan.tower_mode.value,
                "layout": None if plan.layout is None else plan.layout.value,
                "serial_difficulty": plan.serial_difficulty.value,
                "units": [
                    {
                        "unit_id": unit.unit_id,
                        "role": unit.role.value,
                        "step": unit.step,
                    }
                    for unit in plan.units
                ],
                "signature": plan.signature,
                "config_hash": plan.config_hash,
            },
            "parameter_source": self.parameter_source,
            "parameter_config_hash": self.parameter_config_hash,
            "uses_independent_parameters": self.uses_independent_parameters,
            "configured_parameters": self.configured_parameters,
            "uniform_parameters": self.uniform_parameters,
        }


@dataclass(frozen=True)
class GenerationItemResult:
    template_key: str
    plan_id: str
    plan_signature: str
    parameter_source: str
    generation_seed: int
    source: LoadedTemplate | None
    inspection: TemplateInspection | None
    candidates: tuple[GeneratedCandidate, ...]
    attempts_completed: int
    rejection_counts: Mapping[str, int]
    target_candidate_count: int
    status: str
    error: Mapping[str, Any] | None = None
    generation_seed_sha256: str = ""
    effective_attempt_budget_per_slot: int = 0
    slot_attempts_completed: Mapping[int, int] = field(default_factory=dict)
    source_plan_id: str = ""
    search_progress: Mapping[str, Any] = field(default_factory=dict)
    shared_attempts_completed: int | None = None

    def as_dict(self) -> dict[str, Any]:
        parameter_identities = {
            candidate.parameter_config_hash or candidate.candidate.plan_signature
            for candidate in self.candidates
        }
        mixed_parameters = len(parameter_identities) > 1
        candidates = []
        for candidate in self.candidates:
            payload = candidate.as_dict()
            source = candidate.parameter_source or self.parameter_source
            independent = (
                source != "batch"
                if candidate.uses_independent_parameters is None
                else candidate.uses_independent_parameters
            )
            non_uniform = mixed_parameters or independent
            payload["parameter_provenance"] = {
                "non_uniform": non_uniform,
                "reason": (
                    "mixed_batches"
                    if mixed_parameters and not independent
                    else "template_specific_and_mixed_batches"
                    if mixed_parameters
                    else "template_specific"
                    if independent
                    else "uniform_batch"
                ),
                "parameter_source": source,
                "plan_signature": candidate.candidate.plan_signature,
                "generation_config_hash": candidate.candidate.generation_config_hash,
                "parameter_config_hash": candidate.parameter_config_hash,
            }
            candidates.append(payload)
        return {
            "template_key": self.template_key,
            "plan_id": self.plan_id,
            "plan_signature": self.plan_signature,
            "parameter_source": self.parameter_source,
            "source_plan_id": self.source_plan_id or self.plan_id,
            "generation_seed": self.generation_seed,
            "generation_seed_sha256": self.generation_seed_sha256,
            "source": None
            if self.source is None
            else {
                "input_mode": self.source.input_mode,
                "source_locator": self.source.source_locator,
                "source_filename": self.source.source_filename,
                "template_id": self.source.level.template_id,
                "source_sha256": self.source.level.sha256,
            },
            "inspection": None if self.inspection is None else self.inspection.as_dict(),
            "candidates": candidates,
            "candidate_progress": {
                "found": len(self.candidates),
                "target": self.target_candidate_count,
                "independent_experience_signature_count": len(
                    {
                        _candidate_experience_signature(item.candidate)
                        for item in self.candidates
                    }
                ),
                "percent": (
                    100
                    if self.target_candidate_count == 0
                    else round(
                        len(self.candidates) * 100 / self.target_candidate_count
                    )
                ),
            },
            "attempts_completed": self.attempts_completed,
            "search_progress": dict(self.search_progress),
            "search_budget": {
                "scope": "per_work_item",
                "attempts_completed": self.shared_attempts_completed if self.shared_attempts_completed is not None else self.attempts_completed,
                "current_attempt_budget": self.effective_attempt_budget_per_slot,
                "cumulative_attempts_completed": self.attempts_completed,
                "slot_attempts_completed": {
                    str(key): int(value)
                    for key, value in self.slot_attempts_completed.items()
                },
                "unfinished_candidate_slots": max(
                    0, self.target_candidate_count - len(self.candidates)
                ),
            },
            "rejection_counts": dict(self.rejection_counts),
            "status": self.status,
            "error": None if self.error is None else dict(self.error),
        }


@dataclass(frozen=True)
class GenerationBatchResult:
    format_version: int
    batch_seed: int
    resolved_batch_hash: str
    source_config_hash: str
    algorithm_version: str
    items: tuple[GenerationItemResult, ...]
    run_status: str = "completed"
    batch_seed_source: str = "provided"
    locked_config_snapshot: Mapping[str, Any] | None = None
    source_snapshot_id: str | None = None
    total_work_item_count: int | None = None
    total_candidate_target_count: int | None = None

    @property
    def succeeded(self) -> int:
        return sum(item.status == "success" for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.items)

    @property
    def partially_succeeded(self) -> int:
        return sum(item.status == "partial" for item in self.items)

    @property
    def usable(self) -> int:
        return sum(bool(item.candidates) for item in self.items)

    @property
    def cancelled(self) -> int:
        return sum(item.status == "cancelled" for item in self.items)

    @property
    def pending(self) -> int:
        return sum(item.status == "pending" for item in self.items)

    def as_dict(self) -> dict[str, Any]:
        work_item_count = max(
            len(self.items),
            int(self.total_work_item_count or 0),
        )
        unobserved_pending_count = max(0, work_item_count - len(self.items))
        running_count = sum(item.status == "running" for item in self.items)
        pending_count = self.pending + unobserved_pending_count
        candidate_target_count = max(
            sum(item.target_candidate_count for item in self.items),
            int(self.total_candidate_target_count or 0),
        )
        independent_experience_signatures = {
            _candidate_experience_signature(candidate.candidate)
            for item in self.items
            for candidate in item.candidates
        }
        return {
            "format_version": self.format_version,
            "batch_seed": self.batch_seed,
            "batch_seed_source": self.batch_seed_source,
            "resolved_batch_hash": self.resolved_batch_hash,
            "source_config_hash": self.source_config_hash,
            "algorithm_version": self.algorithm_version,
            "locked_config_snapshot": None
            if self.locked_config_snapshot is None
            else dict(self.locked_config_snapshot),
            "source_snapshot_id": self.source_snapshot_id,
            "summary": {
                "run_status": self.run_status,
                "work_item_count": work_item_count,
                "success_count": self.succeeded,
                "partial_success_count": self.partially_succeeded,
                "usable_count": self.usable,
                "failure_count": self.failed,
                "cancelled_count": self.cancelled,
                "pending_count": pending_count,
                "running_count": running_count,
                "classified_work_item_count": (
                    self.succeeded
                    + self.partially_succeeded
                    + self.failed
                    + self.cancelled
                    + pending_count
                    + running_count
                ),
                "retryable_count": (
                    self.failed
                    + self.partially_succeeded
                    + self.cancelled
                    + pending_count
                ),
                "candidate_found_count": sum(
                    len(item.candidates) for item in self.items
                ),
                "candidate_target_count": candidate_target_count,
                "independent_experience_signature_count": len(
                    independent_experience_signatures
                ),
            },
            "items": [item.as_dict() for item in self.items],
        }


def _error_payload(error: Exception) -> dict[str, Any]:
    if isinstance(error, PuzzleToolError):
        return error.detail.as_dict()
    detail = getattr(error, "as_dict", None)
    if callable(detail):
        value = detail()
        if isinstance(value, Mapping):
            return dict(value)
    return {
        "code": type(error).__name__,
        "message": str(error),
    }


def _generation_error_payload(error: Exception) -> dict[str, Any]:
    """Add product-level failure semantics without changing core error codes."""

    payload = _error_payload(error)
    if payload.get("code") != "NO_VALID_CANDIDATE":
        return payload
    evidence = payload.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    rejection_counts = evidence.get("rejection_counts")
    if evidence.get('scope') != 'per_work_item' and isinstance(rejection_counts, Mapping) and int(
        rejection_counts.get("precision_profile_coverage") or 0
    ) > 0:
        payload.update(
            {
                "failure_kind": "missing_required_evidence",
                "recommended_action": {
                    "id": "complete_evidence",
                    "label": "补齐精准模式证据",
                    "message": (
                        "补齐或去歧义 config_level 中的原关卡 DupPara1–10，"
                        "或关闭精准模式后重新生成。"
                    ),
                },
            }
        )
    elif "attempt_budget" in evidence:
        current_budget = int(evidence.get("attempt_budget") or 1)
        payload.update(
            {
                "failure_kind": "budget_exhausted",
                "recommended_action": {
                    "id": "continue_search",
                    "label": "继续运行",
                    "message": "当前预算内未命中，不代表无解；可扩大预算继续搜索。",
                    "next_attempt_budget": max(2, current_budget * 2),
                },
            }
        )
    else:
        payload.update(
            {
                "failure_kind": "parameter_conflict",
                "recommended_action": {
                    "id": "adjust_parameters",
                    "label": "调整当前模板参数",
                    "message": "当前组合存在可证明的约束冲突，需要先调整参数。",
                },
            }
        )
    return payload


def _candidate_count_error_payload(
    error: Exception,
    *,
    found_count: int,
    target_count: int,
    candidate_index: int | None = None,
    exhausted_attempt_budget: int | None = None,
) -> dict[str, Any]:
    """Describe an incomplete candidate target while retaining useful results."""

    payload = _generation_error_payload(error)
    payload["candidate_progress"] = {
        "found": found_count,
        "target": target_count,
    }
    if (
        payload.get("failure_kind") == "budget_exhausted"
        and candidate_index is not None
        and exhausted_attempt_budget is not None
    ):
        # This marker is deliberately attached only after a concrete candidate
        # slot has consumed its complete attempt window.  The app timeline can
        # then emit one stable event per slot/budget tier instead of repeating
        # the same warning for both the provisional and terminal snapshots.
        evidence = payload.get("evidence")
        payload["evidence"] = {
            **(dict(evidence) if isinstance(evidence, Mapping) else {}),
            "candidate_index": int(candidate_index),
            "attempt_budget": int(exhausted_attempt_budget),
            "slot_budget_exhausted": True,
        }
    action = payload.get("recommended_action")
    if payload.get("failure_kind") == "budget_exhausted" and isinstance(
        action, Mapping
    ):
        payload["recommended_action"] = {
            **dict(action),
            "message": (
                f"已找到 {found_count}/{target_count} 个不同候选；"
                "当前预算未补齐，可扩大预算继续搜索。"
            ),
        }
    return payload


def _recommend_candidates(
    candidates: Iterable[GeneratedCandidate],
) -> tuple[GeneratedCandidate, ...]:
    """Apply the auditable recommendation pipeline in a fixed order.

    Hard validation and evidence sufficiency are eligibility gates.  Online
    style is a later soft preference and is never treated as observed player
    risk.  Historical candidates without the v6 experience report retain the
    legacy eligibility path so task-record recovery remains usable.
    """

    values = tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.candidate.candidate_index,
                item.candidate.candidate_id,
            ),
        )
    )
    if not values:
        return ()
    def analysis(item: GeneratedCandidate) -> Mapping[str, object]:
        value = getattr(item.candidate, "experience_analysis", {})
        return value if isinstance(value, Mapping) else {}

    def nested(value: Mapping[str, object], key: str) -> Mapping[str, object]:
        child = value.get(key)
        return child if isinstance(child, Mapping) else {}

    def number(value: object, fallback: float = float("inf")) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def is_eligible(item: GeneratedCandidate) -> bool:
        # A returned validation report means technical integrity passed, but
        # failed product-rule checks still exclude the candidate from automatic
        # recommendation.  The candidate remains in ``values`` for explicit
        # review and manual-confirmation export.
        if item.validation is not None and not item.validation.passed:
            return False
        report = analysis(item)
        simulation = item.candidate.difficulty_simulation
        if not report:
            return bool(simulation.get("qualified")) and bool(
                simulation.get("trend", {}).get("becomes_easier")
            )
        hard_status = str(nested(report, "hard_validation").get("status", "inconclusive"))
        pressure = str(nested(report, "simulation_pressure").get("level", "inconclusive"))
        target = str(simulation.get("target", "fidelity"))
        evidence = nested(report, "evidence_coverage")
        evidence_status = str(evidence.get("overall", "acceptable"))
        trend = simulation.get("trend", {})
        trend_safe = bool(
            trend.get(
                "safety_qualified",
                trend.get("becomes_easier", False),
            )
        ) if isinstance(trend, Mapping) else False
        return (
            hard_status == "proved"
            and pressure != "inconclusive"
            and (pressure != "high" or target == "strong")
            and evidence_status not in {"inconclusive", "reject"}
            and bool(simulation.get("qualified"))
            and trend_safe
        )

    eligible = [item for item in values if is_eligible(item)]

    def rank_key(item: GeneratedCandidate) -> tuple[object, ...]:
        report = analysis(item)
        archetype = nested(report, "archetype")
        requested = str(archetype.get("requested", "auto"))
        primary = str(archetype.get("primary", "unknown"))
        archetype_mismatch = 0 if requested in {"auto", primary} else 1
        archetype_score = number(archetype.get("match_score"), 0.0)
        pressure = str(nested(report, "simulation_pressure").get("level", "medium"))
        pressure_order = {"low": 0, "medium": 1, "high": 2, "inconclusive": 3}.get(pressure, 3)
        simulation = item.candidate.difficulty_simulation
        distance = number(
            nested(simulation, "difficulty_distance").get(
                "total", simulation.get("source_difficulty_distance")
            )
        )
        style_distance = number(
            nested(report, "online_style_distance").get("total"), 0.0
        )
        layout_score = number(
            getattr(item.candidate, "quality_metrics", {}).get(
                "layout_score", item.candidate.quality_score
            ),
            0.0,
        )
        return (
            archetype_mismatch,
            -archetype_score,
            pressure_order,
            distance,
            style_distance,
            -layout_score,
            getattr(item.candidate, "experience_signature", ""),
            item.candidate.candidate_id,
        )

    ranked = sorted(
        eligible,
        key=rank_key,
    )
    recommended_count = 1 if len(ranked) == 1 else 2
    selected = {item.candidate.candidate_id: rank for rank, item in enumerate(ranked[:recommended_count], 1)}
    result: list[GeneratedCandidate] = []
    for item in values:
        rank = selected.get(item.candidate.candidate_id)
        reasons: list[str] = []
        if rank is not None:
            report = analysis(item)
            primary = nested(report, "archetype").get("primary")
            distance = nested(
                item.candidate.difficulty_simulation,
                "difficulty_distance",
            ).get(
                "total",
                item.candidate.difficulty_simulation.get(
                    "source_difficulty_distance"
                ),
            )
            pressure = nested(report, "simulation_pressure").get("level")
            reasons.append(
                ("整板体验原型匹配；" if primary else "")
                + ("机制压力未越线；" if pressure else "")
                + ("难度最接近原模板" if rank == 1 else "难度距离次优")
                + (
                    "" if distance is None else f"（距离 {float(distance):.2f}）"
                )
            )
            style = nested(report, "online_style_distance").get("total")
            if style is not None:
                reasons.append(f"线上风格距离 {float(style):.2f}；布局仅作打平")
        recommendation_reasons = tuple(reasons)
        # Continuations keep completed slot objects intact whenever their
        # recommendation metadata did not change.  Besides avoiding needless
        # churn, this preserves the existing resume contract for callers that
        # cache candidate objects by identity.
        if (
            item.is_recommended == (rank is not None)
            and item.recommendation_rank == rank
            and item.recommendation_reasons == recommendation_reasons
        ):
            result.append(item)
        else:
            result.append(
                replace(
                    item,
                    is_recommended=rank is not None,
                    recommendation_rank=rank,
                    recommendation_reasons=recommendation_reasons,
                )
            )
    return tuple(result)


def _load_for_key(
    profile: Profile,
    template_key: str,
    file_inputs: Mapping[str, Path],
    loaded_inputs: Mapping[str, LoadedTemplate],
) -> LoadedTemplate:
    snapshotted = loaded_inputs.get(template_key)
    if snapshotted is not None:
        if snapshotted.level.template_id != template_key:
            raise ValueError("loaded input TemplateId does not match its template key")
        return snapshotted
    selected_file = file_inputs.get(template_key)
    if selected_file is not None:
        return load_template_file(selected_file, expected_template_id=template_key)
    return load_template_id(profile, template_key)


def generation_seed_for_item(
    config: ResolvedBatchConfig,
    item: GenerationWorkItem,
    source: LoadedTemplate,
    candidate_index: int = 1,
) -> int:
    """Return one candidate-slot seed, distinct from the A/B pattern seed."""

    return generation_seed_material_for_item(
        config,
        item,
        source,
        candidate_index=candidate_index,
    ).value


def generation_seed_material_for_item(
    config: ResolvedBatchConfig,
    item: GenerationWorkItem,
    source: LoadedTemplate,
    *,
    candidate_index: int = 1,
) -> DerivedSeed:
    """Return the 64-bit seed and complete SHA-256 derivation digest."""

    return derive_candidate_seed_material(
        config.batch.batch_seed,
        source.level.sha256,
        item.plan,
        candidate_index,
        ALGORITHM_VERSION,
    )


def generate_batch(
    config: ResolvedBatchConfig,
    profile: Profile,
    *,
    file_inputs: Mapping[str, Path] | None = None,
    loaded_inputs: Mapping[str, LoadedTemplate] | None = None,
    source_snapshot_id: str | None = None,
    control: CooperativeTaskControl | None = None,
    previous_result: GenerationBatchResult | None = None,
    retry_statuses: Iterable[str] = ("failed", "partial", "cancelled", "pending"),
    retry_work_item_keys: Iterable[tuple[str, str, str]] = (),
    excluded_retry_failure_kinds: Iterable[str] = ("parameter_conflict",),
    progress_callback: Callable[[GenerationBatchResult], None] | None = None,
    mark_control_completed: bool = True,
    attempt_budget_multiplier: int = 1,
) -> GenerationBatchResult:
    """Generate and independently validate every normalized work item.

    A failed template/plan is retained as a structured row and does not abort
    other work items.  This function performs no writes to the source repo or
    formal output directory.
    """

    if (
        isinstance(attempt_budget_multiplier, bool)
        or not isinstance(attempt_budget_multiplier, int)
        or attempt_budget_multiplier < 1
    ):
        raise ValueError("attempt_budget_multiplier must be a positive integer")

    direct_files = dict(file_inputs or {})
    loaded_sources = dict(loaded_inputs or {})
    overlap = sorted(set(direct_files) & set(loaded_sources))
    if overlap:
        raise ValueError(
            "template keys cannot use direct paths and snapshots together: "
            + ", ".join(overlap)
        )
    known_keys = {item.template_key for item in config.work_items}
    unknown = sorted((set(direct_files) | set(loaded_sources)) - known_keys)
    if unknown:
        raise ValueError("file_inputs contains unknown template keys: " + ", ".join(unknown))

    if previous_result is not None and (
        previous_result.batch_seed != config.batch.batch_seed
        or previous_result.resolved_batch_hash != config.batch.config_hash
        or previous_result.source_config_hash != config.source_config_hash
    ):
        raise ValueError("上次结果与当前锁定配置不一致，不能续跑")

    retryable = frozenset(str(value) for value in retry_statuses)
    forced_retry_keys = frozenset(tuple(value) for value in retry_work_item_keys)
    excluded_failure_kinds = frozenset(
        str(value) for value in excluded_retry_failure_kinds
    )
    previous_by_key = {
        (item.template_key, item.plan_id, item.plan_signature): item
        for item in (() if previous_result is None else previous_result.items)
    }

    def build_result(
        rows: list[GenerationItemResult], run_status: str
    ) -> GenerationBatchResult:
        return GenerationBatchResult(
            format_version=JOB_FORMAT_VERSION,
            batch_seed=config.batch.batch_seed,
            resolved_batch_hash=config.batch.config_hash,
            source_config_hash=config.source_config_hash,
            algorithm_version=ALGORITHM_VERSION,
            items=tuple(rows),
            run_status=run_status,
            batch_seed_source=config.batch_seed_source,
            locked_config_snapshot=json.loads(config.normalized_config_json)
            if config.normalized_config_json
            else None,
            source_snapshot_id=source_snapshot_id,
            total_work_item_count=len(config.work_items),
            total_candidate_target_count=sum(
                work.generation_options.candidate_count
                for work in config.work_items
            ),
        )

    def publish(rows: list[GenerationItemResult], run_status: str) -> None:
        if progress_callback is not None:
            # Progress is a complete task snapshot, not a prefix/delta. During
            # continuation later completed items have not been visited yet,
            # but their candidates must remain visible and count as progress.
            by_key = dict(previous_by_key)
            by_key.update({(row.template_key, row.plan_id, row.plan_signature): row
                           for row in rows})
            visible = [by_key[key] for work in config.work_items
                       for key in [(work.template_key, work.plan.plan_id, work.plan.signature)]
                       if key in by_key]
            progress_callback(build_result(visible, run_status))

    source_cache: dict[str, LoadedTemplate] = {}
    inspection_cache: dict[str, TemplateInspection] = {}
    difficulty_profile_cache = load_difficulty_profiles(
        profile,
        tuple(item.template_key for item in config.work_items),
    )
    results = []
    for work_index, item in enumerate(config.work_items):
        item_key = (item.template_key, item.plan.plan_id, item.plan.signature)
        previous_item = previous_by_key.get(item_key)
        if previous_item is not None and item_key not in forced_retry_keys and (
            previous_item.status not in retryable
            or (
                previous_item.error
                and previous_item.error.get("failure_kind")
                in excluded_failure_kinds
            )
        ):
            results.append(previous_item)
            publish(results, "running")
            continue
        source = None
        inspection = None
        seed = 0 if previous_item is None else previous_item.generation_seed
        seed_sha256 = (
            "" if previous_item is None else previous_item.generation_seed_sha256
        )
        previous_candidates = (
            () if previous_item is None else previous_item.candidates
        )
        # Older snapshots may contain the same layout or the same experience
        # fingerprint from independent candidate slots.  Keep the first
        # occurrence only so recovered progress obeys the current uniqueness
        # contract before any new slot is searched.
        generated_items: list[GeneratedCandidate] = []
        structure_signatures: set[str] = set()
        experience_signatures: set[str] = set()
        duplicate_recovered_slots: set[int] = set()
        rejection_counts: Counter[str] = Counter(
            {} if previous_item is None else previous_item.rejection_counts
        )
        for generated_item in previous_candidates:
            signature = canonical_structure_identity(generated_item.candidate.structure_signature)
            if signature in structure_signatures:
                rejection_counts["duplicate_candidate_recovered"] += 1
                if generated_item.candidate.candidate_index > 0:
                    duplicate_recovered_slots.add(
                        generated_item.candidate.candidate_index
                    )
                continue
            experience_signature = _candidate_experience_signature(
                generated_item.candidate
            )
            if experience_signature in experience_signatures:
                rejection_counts["duplicate_experience_signature_recovered"] += 1
                if generated_item.candidate.candidate_index > 0:
                    duplicate_recovered_slots.add(
                        generated_item.candidate.candidate_index
                    )
                continue
            structure_signatures.add(signature)
            experience_signatures.add(experience_signature)
            generated_items.append(generated_item)
        attempts_completed = (
            0 if previous_item is None else previous_item.attempts_completed
        )
        shared_attempts = (0 if previous_item is None else
                           previous_item.shared_attempts_completed
                           if previous_item.shared_attempts_completed is not None
                           else previous_item.attempts_completed)
        slot_attempts_completed = {
            int(key): int(value)
            for key, value in (
                {} if previous_item is None else previous_item.slot_attempts_completed
            ).items()
        }
        for duplicate_slot in duplicate_recovered_slots:
            # This slot was previously searched without the stronger
            # experience uniqueness constraint. Re-evaluate its deterministic
            # window with the retained fingerprints excluded.
            slot_attempts_completed.pop(duplicate_slot, None)
        try:
            if control is not None:
                control.checkpoint("generation_work_item")
            if item.template_key not in source_cache:
                source_cache[item.template_key] = _load_for_key(
                    profile, item.template_key, direct_files, loaded_sources
                )
                inspection_cache[item.template_key] = inspect_loaded_template(
                    source_cache[item.template_key]
                )
            source = source_cache[item.template_key]
            inspection = inspection_cache[item.template_key]
            authoritative_template_id = source.level.template_id
            if authoritative_template_id not in difficulty_profile_cache:
                difficulty_profile_cache[authoritative_template_id] = load_difficulty_profile(
                    profile, authoritative_template_id
                )
            difficulty_profile = difficulty_profile_cache[authoritative_template_id]
            for retained in generated_items:
                retained_profile = difficulty_profile_from_simulation(
                    retained.candidate.difficulty_simulation
                )
                if retained_profile is not None:
                    difficulty_profile = retained_profile
                    break
            target_candidate_count = item.generation_options.candidate_count
            effective_attempt_budget = (
                item.generation_options.attempt_budget * attempt_budget_multiplier
            )
            completed_candidate_slots = {
                generated.candidate.candidate_index
                for generated in generated_items
                if generated.candidate.candidate_index > 0
            }
            last_budget_error: NoCandidateError | None = None
            last_exhausted_candidate_index: int | None = None
            last_exhausted_attempt_budget: int | None = None
            last_search_publish = 0.0
            maximum_structure_count = feasible_structure_count_limit(
                source.level, item.generation_options, item.plan.tower_mode)
            minimum_structure_count = effective_structure_count_minimum(
                source.level, item.generation_options, item.plan.tower_mode)
            while len(generated_items) < target_candidate_count and shared_attempts < effective_attempt_budget:
                candidate_index = next(index for index in range(1, target_candidate_count + 1)
                                       if index not in completed_candidate_slots)
                if control is not None:
                    control.checkpoint("generation_candidate")
                seed_material = generation_seed_material_for_item(
                    config,
                    item,
                    source,
                    candidate_index=shared_attempts + 1,
                )
                if not seed_sha256:
                    seed = seed_material.value
                    seed_sha256 = seed_material.sha256
                candidate_options = resolve_candidate_generation_options(
                    item,
                    batch_seed=config.batch.batch_seed,
                    candidate_seed=seed_material.value,
                    candidate_index=candidate_index,
                    maximum_structure_count=maximum_structure_count,
                    minimum_structure_count=minimum_structure_count,
                )
                if attempt_budget_multiplier > 1:
                    candidate_options = replace(
                        candidate_options,
                        attempt_budget=(
                            candidate_options.attempt_budget
                            * attempt_budget_multiplier
                        ),
                    )
                # One completed draw is the commit boundary. A distinct seed
                # also re-resolves automatic counts; misses do not lock all
                # remaining work into one unlucky type/group allocation.
                attempt_start_index = shared_attempts
                attempt_count = 1
                precision_validation_cache: dict[str, CandidateValidationReport] = {}

                def publish_search_progress(completed, diagnostics, exhausted=False):
                    nonlocal last_search_publish
                    now = time.monotonic()
                    if now - last_search_publish < 5 and not exhausted:
                        return
                    last_search_publish = now
                    # These observations are not a committed search window:
                    # cancelling must not skip its still-unranked candidates.
                    publish(results + [GenerationItemResult(
                        template_key=item.template_key, plan_id=item.plan.plan_id,
                        source_plan_id=item.plan.source_plan_id,
                        plan_signature=item.plan.signature, parameter_source=item.parameter_source,
                        generation_seed=seed, generation_seed_sha256=seed_sha256,
                        source=source, inspection=inspection,
                        candidates=_recommend_candidates(generated_items),
                        attempts_completed=attempts_completed,
                        shared_attempts_completed=shared_attempts,
                        rejection_counts=dict(rejection_counts),
                        target_candidate_count=target_candidate_count, status='running',
                        effective_attempt_budget_per_slot=effective_attempt_budget,
                        slot_attempts_completed=dict(slot_attempts_completed),
                        search_progress={'candidate_index': candidate_index,
                                         'attempts_searched': shared_attempts,
                                         'attempt_budget': candidate_options.attempt_budget,
                                         'exhausted': exhausted,
                                         'rejection_counts': dict(diagnostics)},
                    )], 'running')

                def accept_precision_candidate(candidate: Candidate) -> bool:
                    report = validate_candidate(
                        source.level,
                        item.plan,
                        candidate_options,
                        candidate,
                        difficulty_profile=difficulty_profile,
                        require_exportable_experience=False,
                    )
                    precision_validation_cache[candidate.candidate_id] = report
                    return report.auto_recommendation_eligible

                try:
                    searched = search_candidates(
                        source.level,
                        item.plan,
                        candidate_options,
                        candidate_seed=seed_material.value,
                        candidate_seed_sha256=seed_material.sha256,
                        candidate_index=candidate_index,
                        candidate_seed_ordinal=shared_attempts + 1,
                        exclude_structure_signatures=structure_signatures,
                        exclude_experience_signatures=experience_signatures,
                        attempt_start_index=attempt_start_index,
                        attempt_count=attempt_count,
                        difficulty_profile=difficulty_profile,
                        search_progress=publish_search_progress,
                        cooperative_checkpoint=(
                            None if control is None else control.checkpoint
                        ),
                        **(
                            {"final_candidate_acceptance": accept_precision_candidate}
                            if candidate_options.precision_mode
                            else {}
                        ),
                    )
                except NoCandidateError as exc:
                    failure = _generation_error_payload(exc)
                    evidence = failure.get("evidence")
                    evidence = (
                        dict(evidence) if isinstance(evidence, Mapping) else {}
                    )
                    missed_rejections = evidence.get("rejection_counts")
                    if isinstance(missed_rejections, Mapping):
                        rejection_counts.update(
                            {
                                str(key): int(value)
                                for key, value in missed_rejections.items()
                            }
                        )
                    if failure.get("failure_kind") != "budget_exhausted":
                        # Static contradictions are invariant across slot
                        # seeds.  Preserve their rejection telemetry, but do
                        # not count unexecuted attempts or search later slots.
                        raise
                    attempts_completed += 1
                    shared_attempts += 1
                    slot_attempts_completed[candidate_index] = slot_attempts_completed.get(candidate_index, 0) + 1
                    last_budget_error = exc
                    last_exhausted_candidate_index = candidate_index
                    last_exhausted_attempt_budget = candidate_options.attempt_budget
                    if shared_attempts >= effective_attempt_budget:
                        publish_search_progress(1, rejection_counts, exhausted=True)
                    continue
                # Commit only after independent validation. Cancellation in
                # proof/validation will replay this one draw, not lose a hit.
                rejection_counts.update(searched.rejection_counts)
                if len(searched.candidates) != 1:
                    raise RuntimeError("candidate-slot search did not return exactly one candidate")
                candidate = searched.candidates[0]
                experience_signature = _candidate_experience_signature(candidate)
                duplicate_reason = (
                    'duplicate_candidate_across_slots'
                    if canonical_structure_identity(candidate.structure_signature) in structure_signatures
                    else 'duplicate_experience_signature_across_slots'
                    if experience_signature in experience_signatures else None)
                if duplicate_reason:
                    attempts_completed += 1
                    shared_attempts += 1
                    slot_attempts_completed[candidate_index] = slot_attempts_completed.get(candidate_index, 0) + 1
                    rejection_counts[duplicate_reason] += 1
                    continue
                validation_report = precision_validation_cache.get(candidate.candidate_id)
                if validation_report is None:
                    validation_report = validate_candidate(
                        source.level, item.plan, candidate_options, candidate,
                        difficulty_profile=difficulty_profile,
                        require_exportable_experience=False)
                attempts_completed += 1
                shared_attempts += 1
                slot_attempts_completed[candidate_index] = slot_attempts_completed.get(candidate_index, 0) + 1
                if not validation_report.passed:
                    rejection_counts['independent_validation'] += 1
                    continue
                if (
                    candidate_options.precision_mode
                    and not validation_report.auto_recommendation_eligible
                ):
                    rejection_counts["precision_independent_validation"] += 1
                    last_budget_error = NoCandidateError(
                        "精准模式预算内无符合全部参数、难度目标与中低机制压力的候选",
                        evidence={
                            "attempt_budget": candidate_options.attempt_budget,
                            "rejection_counts": {
                                "precision_independent_validation": 1
                            },
                        },
                    )
                    continue
                structure_signatures.add(canonical_structure_identity(candidate.structure_signature))
                experience_signatures.add(experience_signature)
                completed_candidate_slots.add(candidate_index)
                generated_items.append(
                    GeneratedCandidate(
                        candidate=candidate,
                        validation=validation_report,
                        generation_options=candidate_options,
                        resolved_plan=item.plan,
                        parameter_source=item.parameter_source,
                        parameter_config_hash=item.parameter_config_hash,
                        uses_independent_parameters=item.uses_independent_parameters,
                        configured_parameters=item.configured_parameters,
                        uniform_parameters=item.uniform_parameters,
                    )
                )
                # Candidate slots are independent.  Publish each completed slot
                # immediately so the UI can show useful work before the full
                # requested count has been reached.
                publish(
                    results
                    + [
                        GenerationItemResult(
                            template_key=item.template_key,
                            plan_id=item.plan.plan_id,
                            source_plan_id=item.plan.source_plan_id,
                            plan_signature=item.plan.signature,
                            parameter_source=item.parameter_source,
                            generation_seed=seed,
                            source=source,
                            inspection=inspection,
                            candidates=_recommend_candidates(generated_items),
                            attempts_completed=attempts_completed,
                            shared_attempts_completed=shared_attempts,
                            rejection_counts=dict(rejection_counts),
                            target_candidate_count=target_candidate_count,
                            status="running",
                            generation_seed_sha256=seed_sha256,
                            effective_attempt_budget_per_slot=effective_attempt_budget,
                            slot_attempts_completed=dict(slot_attempts_completed),
                        )
                    ],
                    "running",
                )
            generated = _recommend_candidates(generated_items)
            if len(generated) < target_candidate_count:
                last_budget_error = NoCandidateError(
                    "当前成品项搜索上限内未找到足够的不同候选",
                    evidence={"attempt_budget": effective_attempt_budget,
                              "attempt_count": shared_attempts,
                              "scope": "per_work_item",
                              "rejection_counts": dict(rejection_counts)})
                last_exhausted_candidate_index = len(generated) + 1
                last_exhausted_attempt_budget = effective_attempt_budget
                results.append(
                    GenerationItemResult(
                        template_key=item.template_key,
                        plan_id=item.plan.plan_id,
                        source_plan_id=item.plan.source_plan_id,
                        plan_signature=item.plan.signature,
                        parameter_source=item.parameter_source,
                        generation_seed=seed,
                        source=source,
                        inspection=inspection,
                        candidates=generated,
                        attempts_completed=attempts_completed,
                        shared_attempts_completed=shared_attempts,
                        rejection_counts=dict(rejection_counts),
                        target_candidate_count=target_candidate_count,
                        status="partial" if generated else "failed",
                        error=_candidate_count_error_payload(
                            last_budget_error,
                            found_count=len(generated),
                            target_count=target_candidate_count,
                            candidate_index=last_exhausted_candidate_index,
                            exhausted_attempt_budget=last_exhausted_attempt_budget,
                        ),
                        generation_seed_sha256=seed_sha256,
                        effective_attempt_budget_per_slot=effective_attempt_budget,
                        slot_attempts_completed=dict(slot_attempts_completed),
                    )
                )
                publish(results, "running")
                continue
            results.append(
                GenerationItemResult(
                    template_key=item.template_key,
                    plan_id=item.plan.plan_id,
                    source_plan_id=item.plan.source_plan_id,
                    plan_signature=item.plan.signature,
                    parameter_source=item.parameter_source,
                    generation_seed=seed,
                    source=source,
                    inspection=inspection,
                    candidates=generated,
                    attempts_completed=attempts_completed,
                    shared_attempts_completed=shared_attempts,
                    rejection_counts=dict(rejection_counts),
                    target_candidate_count=target_candidate_count,
                    status="success",
                    generation_seed_sha256=seed_sha256,
                    effective_attempt_budget_per_slot=effective_attempt_budget,
                    slot_attempts_completed=dict(slot_attempts_completed),
                )
            )
            publish(results, "running")
        except OperationCancelledError as exc:
            retained_candidates = _recommend_candidates(generated_items)
            item_status = "partial" if retained_candidates else "cancelled"
            results.append(
                GenerationItemResult(
                    template_key=item.template_key,
                    plan_id=item.plan.plan_id,
                    source_plan_id=item.plan.source_plan_id,
                    plan_signature=item.plan.signature,
                    parameter_source=item.parameter_source,
                    generation_seed=seed,
                    source=source,
                    inspection=inspection,
                    candidates=retained_candidates,
                    attempts_completed=attempts_completed,
                    shared_attempts_completed=shared_attempts,
                    rejection_counts=dict(rejection_counts),
                    target_candidate_count=item.generation_options.candidate_count,
                    status=item_status,
                    error={
                        **_generation_error_payload(exc),
                        "failure_kind": "user_stopped",
                        "recommended_action": {
                            "id": "use_partial_results"
                            if retained_candidates
                            else "retry",
                            "label": "使用已找到候选"
                            if retained_candidates
                            else "重新运行",
                        },
                    },
                    generation_seed_sha256=seed_sha256,
                    effective_attempt_budget_per_slot=(
                        item.generation_options.attempt_budget
                        * attempt_budget_multiplier
                    ),
                    slot_attempts_completed=dict(slot_attempts_completed),
                )
            )
            for remaining in config.work_items[work_index + 1 :]:
                remaining_key = (
                    remaining.template_key,
                    remaining.plan.plan_id,
                    remaining.plan.signature,
                )
                retained = previous_by_key.get(remaining_key)
                if retained is not None and retained.status not in retryable:
                    results.append(retained)
                else:
                    results.append(
                        GenerationItemResult(
                            template_key=remaining.template_key,
                            plan_id=remaining.plan.plan_id,
                            source_plan_id=remaining.plan.source_plan_id,
                            plan_signature=remaining.plan.signature,
                            parameter_source=remaining.parameter_source,
                            generation_seed=0,
                            source=None,
                            inspection=None,
                            candidates=(),
                            attempts_completed=0,
                            rejection_counts={},
                            target_candidate_count=(
                                remaining.generation_options.candidate_count
                            ),
                            status="pending",
                            effective_attempt_budget_per_slot=(
                                remaining.generation_options.attempt_budget
                                * attempt_budget_multiplier
                            ),
                        )
                    )
            if all(row.status == "success" for row in results):
                terminal_status = "completed"
            elif any(row.candidates for row in results):
                terminal_status = "partial"
            else:
                terminal_status = "cancelled"
            cancelled_result = build_result(results, terminal_status)
            if progress_callback is not None:
                progress_callback(cancelled_result)
            return cancelled_result
        except Exception as exc:  # per-item isolation is a product requirement
            retained_candidates = _recommend_candidates(generated_items)
            results.append(
                GenerationItemResult(
                    template_key=item.template_key,
                    plan_id=item.plan.plan_id,
                    source_plan_id=item.plan.source_plan_id,
                    plan_signature=item.plan.signature,
                    parameter_source=item.parameter_source,
                    generation_seed=seed,
                    source=source,
                    inspection=inspection,
                    candidates=retained_candidates,
                    attempts_completed=attempts_completed,
                    shared_attempts_completed=shared_attempts,
                    rejection_counts=dict(rejection_counts),
                    target_candidate_count=item.generation_options.candidate_count,
                    status="partial" if retained_candidates else "failed",
                    error=_generation_error_payload(exc),
                    generation_seed_sha256=seed_sha256,
                    effective_attempt_budget_per_slot=(
                        item.generation_options.attempt_budget
                        * attempt_budget_multiplier
                    ),
                    slot_attempts_completed=dict(slot_attempts_completed),
                )
            )
            publish(results, "running")
    if control is not None:
        try:
            control.checkpoint("generation_complete")
        except OperationCancelledError:
            if all(item.status == "success" for item in results):
                terminal_status = "completed"
            elif any(item.candidates for item in results):
                terminal_status = "partial"
            else:
                terminal_status = "cancelled"
            final = build_result(results, terminal_status)
            publish(results, terminal_status)
            return final
        if mark_control_completed:
            control.mark_completed()
    if any(item.status in {"failed", "partial", "cancelled", "pending"} for item in results):
        run_status = "partial" if any(item.candidates for item in results) else "failed"
    else:
        run_status = "completed"
    return build_result(results, run_status)


def render_generation_result(result: GenerationBatchResult) -> bytes:
    """Canonical JSON suitable for a candidate-review snapshot."""

    return canonical_json(result.as_dict()).encode("utf-8")
