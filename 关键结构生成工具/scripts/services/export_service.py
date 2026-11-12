"""Crash-safe, no-overwrite formal export service.

The generation/search pipeline intentionally never writes formal output.  This
module is the single boundary that turns explicitly selected, independently
validated candidates into immutable ``_m.bytes`` files and audit records.

The implementation uses a workspace lock, a mutable recovery ledger, staging
on the target filesystem, and ``os.link`` as the no-replace placement primitive.
It never writes to the source repository and never invokes Git.
"""

from __future__ import annotations

import contextlib
import datetime as _datetime
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, Tuple

try:  # The first supported distribution target is macOS.
    import fcntl
except ImportError:  # pragma: no cover - fail closed on unsupported platforms
    fcntl = None  # type: ignore[assignment]

from puzzle_core.errors import (
    ExportConflictError,
    InputValidationError,
    ReconciliationRequiredError,
)
from puzzle_core.difficulty import (
    difficulty_profile_from_simulation,
    simulate_candidate_difficulty,
)
from puzzle_core.models import ResolvedOutputPlan
from puzzle_core.naming import naming_family, versioned_template_id
from puzzle_core.planning import canonical_json, config_hash
from puzzle_core.reporting import (
    render_authoritative_json,
    render_batch_markdown,
    render_comparison_csv,
)
from puzzle_core.rewrite import rewrite_template, verify_rewrite
from puzzle_core.search import (
    ALGORITHM_VERSION,
    GenerationOptions,
    evaluate_candidate_experience,
)
from puzzle_core.template_reader import parse_level_bytes
from puzzle_core.validator import validate_candidate

from .import_service import LoadedTemplate
from .job_service import GeneratedCandidate
from .path_service import validate_output_root
from .profile_service import Profile
from .task_control import CooperativeTaskControl


WORKSPACE_FORMAT_VERSION = 1
TRANSACTION_FORMAT_VERSION = 1
MANIFEST_FORMAT_VERSION = 2
REVALIDATION_EVIDENCE_FORMAT_VERSION = 1
RULES_VERSION = "puzzle-rules-v1.6"
EXPORT_FORMAT_VERSION = "formal-export-v2"

REPORTS_DIRECTORY = "验证报告与生成记录"
REGISTRY_DIRECTORY = "名称注册表"
PUBLIC_TRANSACTIONS_DIRECTORY = "导出事务"
CONTROL_DIRECTORY = ".关键结构生成工具"
SEMANTIC_ID_PLACEHOLDER = "AI_PUZZLE_TEMPLATE_ID"

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_BATCH_ID = re.compile(r"^\d{8}(?:_\d{2,})?$")
_STATE_ORDER = {"prepared": 0, "files_written": 1, "verified": 2, "completed": 3}


@dataclass(frozen=True)
class ExportSelection:
    """One reviewed candidate selected for one formal output file."""

    source: LoadedTemplate
    plan: ResolvedOutputPlan
    generation_options: GenerationOptions
    generated: GeneratedCandidate
    generation_seed: int
    candidate_ordinal: int = 1
    original_plan_id: str | None = None
    online_level_number: str | int | None = None
    level_id: str | None = None
    manual_evidence_override: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExportRequest:
    profile: Profile
    selections: Tuple[ExportSelection, ...]
    idempotency_key: str
    allow_duplicate_as_new_version: bool = False


@dataclass(frozen=True)
class ExportedTemplate:
    source_template_id: str
    output_template_id: str
    signature: str
    version: int
    output_relative_path: str
    manifest_relative_path: str
    registry_relative_path: str
    output_sha256: str
    content_semantic_hash: str
    puzzle_semantic_hash: str
    config_hash: str
    candidate_id: str | None = None
    resolved_plan_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExportResult:
    output_workspace_id: str
    idempotency_key: str
    request_payload_hash: str
    batch_id: str
    state: str
    outputs: Tuple[ExportedTemplate, ...]
    recovered: bool = False
    recovery_count: int = 0
    recovery_limit: str = (
        "Only an intact same-workspace ledger and staging set can be resumed; "
        "missing or contradictory recovery material requires reconciliation."
    )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["outputs"] = [item.as_dict() for item in self.outputs]
        return payload


@dataclass(frozen=True)
class _PreparedSelection:
    selection: ExportSelection
    validation: Any
    family: str
    signature: str
    content_semantic_hash: str
    puzzle_semantic_hash: str
    config_hash: str
    difficulty_simulation: Mapping[str, Any]
    experience_analysis: Mapping[str, Any]


def inconclusive_evidence_record(
    difficulty: Mapping[str, Any] | None,
    experience: Mapping[str, Any] | None,
    *,
    candidate_validation_passed: bool | None = True,
) -> dict[str, Any]:
    """Return the backend-owned record for an explicit user risk decision.

    Puzzle-rule failures and missing evidence remain visible and are excluded
    from automatic recommendation, but the user may explicitly export them.
    File identity, deterministic regeneration, rewrite integrity and evidence
    payload consistency remain non-overridable technical boundaries.
    """

    difficulty_value = difficulty if isinstance(difficulty, Mapping) else {}
    experience_value = experience if isinstance(experience, Mapping) else {}
    hard_validation = experience_value.get("hard_validation")
    hard_validation = (
        hard_validation if isinstance(hard_validation, Mapping) else {}
    )
    raw_hard = str(hard_validation.get("status", "inconclusive")).strip().lower()
    if raw_hard in {"proved", "passed", "pass", "valid"}:
        hard_status = "proved"
    elif raw_hard in {"disproved", "failed", "fail", "invalid", "rejected"}:
        hard_status = "disproved"
    else:
        hard_status = "inconclusive"

    qualified = difficulty_value.get("qualified")
    qualified_status = (
        "proved"
        if qualified is True
        else "disproved"
        if qualified is False
        else "inconclusive"
    )
    trend = difficulty_value.get("trend")
    trend = trend if isinstance(trend, Mapping) else {}
    if "safety_qualified" in trend:
        trend_safe = trend.get("safety_qualified")
    else:
        trend_safe = trend.get("becomes_easier")
    trend_status = (
        "proved"
        if trend_safe is True
        else "disproved"
        if trend_safe is False
        else "inconclusive"
    )
    coverage = difficulty_value.get("evidence_coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    overall_evidence = str(
        coverage.get("overall_status", coverage.get("overall", ""))
    ).strip().lower()
    materialization = str(
        coverage.get("materialization_fidelity", "")
    ).strip().lower()
    # Do not retroactively reject legacy records that predate explicit
    # coverage fields.  New simulations always report them; a synthetic board
    # or an explicit inconclusive verdict needs a deliberate manual choice.
    difficulty_evidence_status = (
        "inconclusive"
        if materialization == "synthetic" or overall_evidence == "inconclusive"
        else "disproved"
        if overall_evidence in {"reject", "disproved", "failed"}
        else "proved"
    )

    validation_status = (
        "proved"
        if candidate_validation_passed is True
        else "disproved"
        if candidate_validation_passed is False
        else "inconclusive"
    )
    axes = {
        "candidate_rule_validation": validation_status,
        "whole_board_completion": hard_status,
        "difficulty_target": qualified_status,
        "duplicate_gradient_safety": trend_status,
        "difficulty_evidence": difficulty_evidence_status,
    }
    inconclusive_axes = [
        key for key, status in axes.items() if status == "inconclusive"
    ]
    disproved_axes = [key for key, status in axes.items() if status == "disproved"]
    return {
        "policy_version": "manual-risk-export-v2",
        "status": (
            "disproved"
            if disproved_axes
            else "inconclusive"
            if inconclusive_axes
            else "proved"
        ),
        "acknowledgement_required": bool(inconclusive_axes or disproved_axes),
        "axes": axes,
        "inconclusive_axes": inconclusive_axes,
        "disproved_axes": disproved_axes,
        "candidate_rule_validation": {
            "passed": candidate_validation_passed,
        },
        "hard_validation": {
            "status": hard_status,
            "reasons": list(hard_validation.get("reasons") or ()),
        },
        "difficulty": {
            "target": difficulty_value.get("target"),
            "qualified": qualified,
            "trend_safe": trend_safe,
            "materialization_fidelity": materialization or None,
            "overall_evidence": overall_evidence or None,
            "downgrade_reasons": list(
                difficulty_value.get("downgrade_reasons") or ()
            ),
        },
    }


def new_idempotency_key() -> str:
    """Create the only supported form of user-visible export retry token."""

    return str(uuid.uuid4())


def _validate_idempotency_key(value: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InputValidationError("导出幂等键必须是后端生成的 UUID") from exc
    canonical = str(parsed)
    if str(value) != canonical:
        raise InputValidationError("导出幂等键必须使用标准小写 UUID 格式")
    return canonical


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_timestamp(now: _datetime.datetime | None = None) -> str:
    value = now or _datetime.datetime.now(_datetime.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_datetime.timezone.utc)
    return value.astimezone(_datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _local_date(now: _datetime.datetime | None = None) -> str:
    value = now or _datetime.datetime.now().astimezone()
    return value.strftime("%Y%m%d")


def _nfc_casefold(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _confined_path(root: Path, path: Path) -> tuple[Path, Path, Path]:
    """Return lexical absolute paths without resolving attacker-controlled links."""

    absolute_root = Path(os.path.abspath(os.fspath(root)))
    absolute_path = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        raise ReconciliationRequiredError(
            "导出路径越过可信根目录", evidence={"path": str(path)}
        ) from exc
    return absolute_root, absolute_path, relative


def _reject_symlink_chain(root: Path, path: Path) -> None:
    """Reject every existing symlink from the trusted root through ``path``."""

    absolute_root, _absolute_path, relative = _confined_path(root, path)
    current = absolute_root
    try:
        root_status = os.lstat(current)
    except FileNotFoundError as exc:
        raise ReconciliationRequiredError(
            "导出可信根目录不存在", evidence={"path": str(root)}
        ) from exc
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise ReconciliationRequiredError(
            "导出可信根目录不是真实目录", evidence={"path": str(root)}
        )
    for part in relative.parts:
        current = current / part
        try:
            status = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(status.st_mode):
            raise ReconciliationRequiredError(
                "导出路径链包含符号链接", evidence={"path": str(current)}
            )


def _ensure_directory(path: Path, *, trusted_root: Path | None = None) -> None:
    if trusted_root is None:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ExportConflictError("导出路径不是目录", evidence={"path": str(path)})
        return

    absolute_root, absolute_path, relative = _confined_path(trusted_root, path)
    _reject_symlink_chain(absolute_root, absolute_root)
    current = absolute_root
    for part in relative.parts:
        current = current / part
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass
        status = os.lstat(current)
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ReconciliationRequiredError(
                "导出目录链包含符号链接或非目录节点",
                evidence={"path": str(current)},
            )
    if current != absolute_path:
        raise ReconciliationRequiredError("导出目录边界校验失败")


def _exclusive_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    trusted_root: Path | None = None,
) -> None:
    if trusted_root is not None:
        _reject_symlink_chain(trusted_root, path)
    data = canonical_json(payload).encode("utf-8")
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        raise
    _fsync_directory(path.parent)


def _atomic_mutable_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    trusted_root: Path | None = None,
) -> None:
    """Durably update only mutable control state; never formal artifacts."""

    _ensure_directory(path.parent, trusted_root=trusted_root)
    if trusted_root is not None:
        _reject_symlink_chain(trusted_root, path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(payload).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ReconciliationRequiredError(
            "导出工作区 JSON 不得为符号链接", evidence={"path": str(path)}
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconciliationRequiredError(
            "导出工作区包含损坏的 JSON 审计文件", evidence={"path": str(path)}
        ) from exc
    if not isinstance(value, dict):
        raise ReconciliationRequiredError(
            "导出工作区 JSON 根节点必须是对象", evidence={"path": str(path)}
        )
    return value


def _initialize_workspace(output_root: Path) -> tuple[str, Path]:
    _ensure_directory(output_root)
    control = output_root / CONTROL_DIRECTORY
    _ensure_directory(control, trusted_root=output_root)
    init_lock = control / "init.lock"
    _reject_symlink_chain(output_root, init_lock)
    if fcntl is None:
        raise ExportConflictError("当前平台缺少导出排他锁支持")
    init_descriptor = os.open(
        str(init_lock),
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(init_descriptor, "r+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        workspace_path = control / "workspace.json"
        if not workspace_path.exists():
            _exclusive_json(
                workspace_path,
                {
                    "format_version": WORKSPACE_FORMAT_VERSION,
                    "output_workspace_id": str(uuid.uuid4()),
                    "created_at": _utc_timestamp(),
                },
                trusted_root=output_root,
            )
        workspace = _read_json(workspace_path)
        if workspace.get("format_version") != WORKSPACE_FORMAT_VERSION:
            raise ReconciliationRequiredError("输出工作区版本不受支持")
        try:
            workspace_id = str(uuid.UUID(str(workspace["output_workspace_id"])))
        except (KeyError, ValueError, TypeError) as exc:
            raise ReconciliationRequiredError("输出工作区 ID 无效") from exc
        for name in ("transactions", "staging"):
            _ensure_directory(control / name, trusted_root=output_root)
        export_lock = control / "export.lock"
        _reject_symlink_chain(output_root, export_lock)
        descriptor = os.open(
            str(export_lock),
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.close(descriptor)
        return workspace_id, control


@contextlib.contextmanager
def _export_lock(control: Path):
    if fcntl is None:
        raise ExportConflictError("当前平台缺少导出排他锁支持")
    lock_path = control / "export.lock"
    _reject_symlink_chain(control, lock_path)
    descriptor = os.open(
        str(lock_path), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "r+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _replacement_map(selection: ExportSelection) -> dict[int, int]:
    return {
        item.tile_index: item.structure_tile_id
        for item in selection.generated.candidate.replacements
    }


def _puzzle_semantic_payload(selection: ExportSelection, content_hash: str) -> dict[str, Any]:
    candidate = selection.generated.candidate
    units = []
    by_index = {item.tile_index: item for item in candidate.replacements}
    for unit in candidate.units:
        units.append(
            {
                "unit_id": unit.unit_id,
                "role": unit.role,
                "actual_structure_step": unit.proof.minimum_structure_step,
                "threshold_type": unit.threshold_type,
                "tiles": [
                    {
                        "coordinate": [by_index[index].x, by_index[index].y, by_index[index].z],
                        "structure_type": by_index[index].structure_type,
                        "blocker_mask": mask,
                    }
                    for index, mask in zip(unit.selected_indices, unit.blocker_masks)
                ],
            }
        )
    tower = None
    if candidate.tower_group is not None:
        tower = [
            {
                "coordinate": [
                    selection.source.level.tiles[index].x,
                    selection.source.level.tiles[index].y,
                    selection.source.level.tiles[index].z,
                ],
                "structure_type": label,
            }
            for index, label in zip(
                candidate.tower_group.selected_indices,
                candidate.tower_group.assigned_types,
            )
        ]
    return {
        "content_semantic_hash": content_hash,
        "signature": candidate.plan_signature,
        "units": units,
        "tower": tower,
    }


def _recompute_validated_evidence(
    selection: ExportSelection,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recompute the dynamic evidence retained by formal export.

    ``validate_candidate`` independently checks the same values, but its stable
    report intentionally contains only compact conclusions.  Formal export
    needs the complete versioned simulation and reveal evidence for replay, so
    this boundary recomputes it once more and retains that recomputed value --
    never an unchecked UI/job payload.
    """

    candidate = selection.generated.candidate
    difficulty_profile = difficulty_profile_from_simulation(
        candidate.difficulty_simulation
    )
    difficulty = simulate_candidate_difficulty(
        selection.source.level,
        candidate.replacements,
        target=selection.generation_options.quality.difficulty_target,
        profile=difficulty_profile,
        requested_archetype=(
            selection.generation_options.quality.experience_archetype
        ),
    )
    if dict(candidate.difficulty_simulation) != difficulty:
        raise ExportConflictError(
            "候选携带的难度模拟与导出阶段独立重算不一致"
        )
    experience = evaluate_candidate_experience(
        selection.source.level,
        candidate,
        selection.generation_options.quality,
        difficulty,
    )
    if dict(candidate.experience_analysis) != experience:
        raise ExportConflictError(
            "候选携带的整板体验证据与导出阶段独立重算不一致"
        )
    return difficulty, experience


def _prepare_selections(
    selections: Sequence[ExportSelection],
    *,
    task_control: CooperativeTaskControl | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[_PreparedSelection, ...]:
    if not selections:
        raise InputValidationError("正式导出至少需要一个已选候选")
    prepared = []
    identities = set()
    total = len(selections)
    for selection_index, selection in enumerate(selections):
        candidate_id = selection.generated.candidate.candidate_id
        if progress_callback is not None:
            progress_callback(selection_index, total, candidate_id)
        if task_control is not None:
            task_control.checkpoint("export_prepare_selection")
        if selection.candidate_ordinal < 1:
            raise InputValidationError("候选序号必须大于 0")
        if selection.candidate_ordinal != selection.generated.candidate.candidate_index:
            raise InputValidationError("候选序号与候选内容身份不一致")
        validation = validate_candidate(
            selection.source.level,
            selection.plan,
            selection.generation_options,
            selection.generated.candidate,
            # Experience-rule and difficulty conclusions are review signals,
            # not file-export permission boundaries.  Identity, deterministic
            # regeneration and rewrite-integrity failures still raise here.
            require_exportable_experience=False,
        )
        validation_passed = bool(validation.passed)
        difficulty_simulation, experience_analysis = (
            _recompute_validated_evidence(selection)
        )
        current_override_record = inconclusive_evidence_record(
            difficulty_simulation,
            experience_analysis,
            candidate_validation_passed=validation_passed,
        )
        if (selection.manual_evidence_override is None
                and current_override_record["acknowledgement_required"]):
            raise ExportConflictError(
                "独立复验存在规则结论、难度风险或证据缺口，需要在应用内人工确认后再导出"
            )
        if selection.manual_evidence_override is not None:
            if not current_override_record["acknowledgement_required"]:
                raise ExportConflictError(
                    "人工证据确认与当前独立复验状态不匹配"
                )
            if config_hash(selection.manual_evidence_override) != config_hash(
                current_override_record
            ):
                raise ExportConflictError(
                    "人工确认时的证据状态已变化，请返回候选重新确认"
                )
        family = naming_family(selection.source.level.template_id, selection.plan)
        signature = selection.plan.signature
        semantic_bytes = rewrite_template(
            selection.source.level,
            SEMANTIC_ID_PLACEHOLDER,
            _replacement_map(selection),
        )
        content_hash = _sha256(semantic_bytes)
        puzzle_hash = config_hash(_puzzle_semantic_payload(selection, content_hash))
        generation_config_hash = config_hash(
            {
                "generation_options": selection.generation_options,
                "generation_seed": selection.generation_seed,
                "algorithm_version": ALGORITHM_VERSION,
            }
        )
        identity = (
            selection.source.level.sha256,
            selection.plan.config_hash,
            selection.generated.candidate.candidate_id,
        )
        if identity in identities:
            raise InputValidationError("同一候选不能在一次导出中重复选择")
        identities.add(identity)
        prepared.append(
            _PreparedSelection(
                selection=selection,
                validation=validation,
                family=family,
                signature=signature,
                content_semantic_hash=content_hash,
                puzzle_semantic_hash=puzzle_hash,
                config_hash=generation_config_hash,
                difficulty_simulation=difficulty_simulation,
                experience_analysis=experience_analysis,
            )
        )
        if progress_callback is not None:
            progress_callback(selection_index + 1, total, candidate_id)
    return tuple(prepared)


def _request_hash(
    workspace_id: str,
    request: ExportRequest,
    prepared: Sequence[_PreparedSelection],
) -> str:
    return config_hash(
        {
            "output_workspace_id": workspace_id,
            "profile_id": request.profile.profile_id,
            "profile_name": request.profile.name,
            "selections": [
                {
                    "source_sha256": item.selection.source.level.sha256,
                    "source_input_mode": item.selection.source.input_mode,
                    "source_locator": item.selection.source.source_locator,
                    "source_filename": item.selection.source.source_filename,
                    "resolved_plan_id": item.selection.plan.plan_id,
                    "resolved_plan_hash": item.selection.plan.config_hash,
                    "original_plan_id": item.selection.original_plan_id,
                    "candidate_id": item.selection.generated.candidate.candidate_id,
                    "candidate_ordinal": item.selection.candidate_ordinal,
                    "candidate_seed_ordinal": (
                        item.selection.generated.candidate.candidate_seed_ordinal
                    ),
                    "candidate_seed_sha256": (
                        item.selection.generated.candidate.candidate_seed_sha256
                    ),
                    "online_level_number": item.selection.online_level_number,
                    "level_id": item.selection.level_id,
                    "content_semantic_hash": item.content_semantic_hash,
                    "puzzle_semantic_hash": item.puzzle_semantic_hash,
                    "config_hash": item.config_hash,
                    "manual_evidence_override": (
                        None
                        if item.selection.manual_evidence_override is None
                        else dict(item.selection.manual_evidence_override)
                    ),
                }
                for item in prepared
            ],
            "allow_duplicate_as_new_version": request.allow_duplicate_as_new_version,
            "algorithm_version": ALGORITHM_VERSION,
            "rules_version": RULES_VERSION,
            "manifest_format_version": MANIFEST_FORMAT_VERSION,
            "revalidation_evidence_format_version": (
                REVALIDATION_EVIDENCE_FORMAT_VERSION
            ),
            "export_format_version": EXPORT_FORMAT_VERSION,
        }
    )


def _transaction_paths(control: Path, reports_root: Path, key: str) -> tuple[Path, Path]:
    return (
        _safe_target(control, f"transactions/{key}.json"),
        reports_root / PUBLIC_TRANSACTIONS_DIRECTORY / f"{key}.json",
    )


def _all_registry_records(output_root: Path) -> list[dict[str, Any]]:
    registry = _safe_target(
        output_root, f"{REPORTS_DIRECTORY}/{REGISTRY_DIRECTORY}"
    )
    if not registry.exists():
        return []
    records = []
    for path in sorted(registry.glob("*.json")):
        _reject_symlink_chain(output_root, path)
        record = _read_json(path)
        output_id = record.get("output_template_id")
        if not isinstance(output_id, str) or path.name != f"{output_id}.json":
            raise ReconciliationRequiredError(
                "名称注册分片的文件名与模板 ID 不一致",
                evidence={"path": str(path)},
            )
        record["_path"] = path
        records.append(record)
    return records


def _verify_revalidation_evidence(
    output_root: Path,
    manifest: Mapping[str, Any],
) -> Path | None:
    """Verify and return a manifest's immutable dynamic-evidence artifact.

    Version-1 manifests predate this artifact and remain readable.  Every
    version-2-or-newer manifest fails closed when the evidence, either hash, or
    any identity/version binding is missing or contradictory.
    """

    manifest_version = manifest.get("manifest_format_version", 1)
    if not isinstance(manifest_version, int) or isinstance(
        manifest_version, bool
    ):
        raise ReconciliationRequiredError("manifest 版本无效")
    if manifest_version < 2:
        return None
    descriptor = manifest.get("revalidation_evidence")
    if not isinstance(descriptor, Mapping):
        raise ReconciliationRequiredError("manifest 缺少独立复验证据索引")
    if descriptor.get("evidence_format_version") != REVALIDATION_EVIDENCE_FORMAT_VERSION:
        raise ReconciliationRequiredError("独立复验证据版本不受支持")
    if descriptor.get("kind") != "candidate_revalidation":
        raise ReconciliationRequiredError("独立复验证据类型无效")
    relative_path = descriptor.get("relative_path")
    if not isinstance(relative_path, str):
        raise ReconciliationRequiredError("独立复验证据路径无效")
    path = _safe_target(output_root, relative_path)
    if not path.is_file():
        raise ReconciliationRequiredError(
            "独立复验证据文件缺失",
            evidence={"path": str(path)},
        )
    output_id = manifest.get("output_template_id")
    if path.name != f"{output_id}_revalidation.json":
        raise ReconciliationRequiredError("独立复验证据文件名与模板 ID 不一致")
    data = path.read_bytes()
    if _sha256(data) != descriptor.get("evidence_sha256"):
        raise ReconciliationRequiredError("独立复验证据文件哈希不一致")
    document = _read_json(path)
    if (
        document.get("evidence_format_version")
        != REVALIDATION_EVIDENCE_FORMAT_VERSION
        or document.get("evidence_kind") != "candidate_revalidation"
    ):
        raise ReconciliationRequiredError("独立复验证据文档头无效")
    revalidation = document.get("revalidation")
    if not isinstance(revalidation, Mapping):
        raise ReconciliationRequiredError("独立复验证据主体无效")
    revalidation_sha256 = config_hash(revalidation)
    if (
        revalidation_sha256 != document.get("revalidation_sha256")
        or revalidation_sha256 != descriptor.get("revalidation_sha256")
    ):
        raise ReconciliationRequiredError("独立复验证据内容哈希不一致")
    provenance = revalidation.get("provenance")
    validation = manifest.get("validation")
    if not isinstance(provenance, Mapping) or not isinstance(validation, Mapping):
        raise ReconciliationRequiredError("复验证据与静态验证报告的绑定无效")
    validation_report_sha256 = config_hash(validation)
    if (
        validation_report_sha256
        != provenance.get("validation_report_sha256")
        or validation_report_sha256
        != descriptor.get("validation_report_sha256")
    ):
        raise ReconciliationRequiredError("复验证据与静态验证报告哈希不一致")
    identity = revalidation.get("identity")
    if not isinstance(identity, Mapping):
        raise ReconciliationRequiredError("独立复验证据缺少身份绑定")
    identity_bindings = {
        "candidate_id": manifest.get("candidate_id"),
        "source_template_id": manifest.get("source_template_id"),
        "source_sha256": manifest.get("source_sha256"),
        "output_template_id": manifest.get("output_template_id"),
        "output_sha256": manifest.get("output_sha256"),
        "content_semantic_hash": manifest.get("content_semantic_hash"),
        "puzzle_semantic_hash": manifest.get("puzzle_semantic_hash"),
        "resolved_output_plan_id": manifest.get("resolved_output_plan_id"),
        "generation_seed": manifest.get("generation_seed"),
        "candidate_seed_ordinal": manifest.get("candidate_seed_ordinal"),
        "candidate_seed_sha256": manifest.get("candidate_seed_sha256"),
    }
    if any(identity.get(key) != value for key, value in identity_bindings.items()):
        raise ReconciliationRequiredError("独立复验证据与 manifest 身份绑定不一致")
    versions = revalidation.get("versions")
    descriptor_versions = descriptor.get("versions")
    if (
        not isinstance(versions, Mapping)
        or not isinstance(descriptor_versions, Mapping)
        or dict(versions) != dict(descriptor_versions)
    ):
        raise ReconciliationRequiredError("独立复验证据版本绑定不一致")
    if (
        versions.get("algorithm_version") != manifest.get("algorithm_version")
        or versions.get("rules_version") != manifest.get("rules_version")
        or versions.get("export_format_version") != manifest.get("tool_version")
    ):
        raise ReconciliationRequiredError("复验证据与 manifest 工具版本不一致")
    difficulty = revalidation.get("difficulty_simulation")
    experience = revalidation.get("experience_analysis")
    if not isinstance(difficulty, Mapping) or not isinstance(experience, Mapping):
        raise ReconciliationRequiredError("独立复验证据缺少完整动态分析")
    difficulty_coverage = difficulty.get("evidence_coverage")
    difficulty_versions = (
        difficulty_coverage
        if isinstance(difficulty_coverage, Mapping)
        else difficulty
    )
    for version_key in (
        "common_seed_version",
        "materialization_version",
        "simulation_version",
    ):
        if difficulty_versions.get(version_key) != versions.get(version_key):
            raise ReconciliationRequiredError("难度模拟版本与复验索引不一致")
    if experience.get("version") != versions.get("experience_analysis_version"):
        raise ReconciliationRequiredError("整板体验版本与复验索引不一致")
    signature_evidence = experience.get("experience_signature_evidence")
    style_distance = experience.get("online_style_distance")
    if (
        not isinstance(signature_evidence, Mapping)
        or signature_evidence.get("version")
        != versions.get("experience_signature_version")
        or not isinstance(style_distance, Mapping)
        or style_distance.get("profile_version")
        != versions.get("online_style_profile_version")
    ):
        raise ReconciliationRequiredError("整板体验子版本与复验索引不一致")
    return path


def _audit_workspace(
    output_root: Path,
    control: Path,
    *,
    current_key: str,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Audit naming integrity without requiring archived outputs to stay put.

    Completed ledgers reserve identities forever. Their files may be moved by
    the user; only the active transaction requires every artifact to be local.
    Present historical files still must match their immutable ledger hashes.
    """

    completed_ledgers = []
    transaction_root = _safe_target(control, "transactions")
    staging_root = _safe_target(control, "staging")
    ledger_keys: set[str] = set()
    for ledger_path in sorted(transaction_root.glob("*.json")):
        _reject_symlink_chain(control, ledger_path)
        ledger = _read_json(ledger_path)
        ledger_keys.add(ledger_path.stem)
        if ledger.get("output_workspace_id") != workspace_id:
            raise ReconciliationRequiredError("事务账本归属于另一输出工作区")
        if ledger_path.stem != current_key and ledger.get("state") != "completed":
            raise ReconciliationRequiredError(
                "检测到其他未完成导出事务，必须先恢复或对账",
                evidence={"transaction": ledger_path.stem, "state": ledger.get("state")},
            )
        if ledger.get("state") == "completed":
            completed_ledgers.append(ledger)

    for ledger in completed_ledgers:
        _verify_transaction(output_root, ledger, allow_archived=True)
    completed_outputs = {}
    for ledger in completed_ledgers:
        for output in ledger["outputs"]:
            output_id = str(output["output_template_id"])
            if output_id in completed_outputs:
                raise ReconciliationRequiredError("历史事务存在重复模板 ID")
            completed_outputs[output_id] = output

    for staging_path in sorted(staging_root.iterdir()):
        _reject_symlink_chain(control, staging_path)
        if not staging_path.is_dir():
            raise ReconciliationRequiredError(
                "staging 根目录包含非目录项",
                evidence={"path": str(staging_path)},
            )
        if staging_path.name not in ledger_keys:
            raise ReconciliationRequiredError(
                "检测到无事务账本归属的 staging 目录",
                evidence={"idempotency_key": staging_path.name},
            )

    records = _all_registry_records(output_root)
    registered_ids = {record["output_template_id"] for record in records}
    for ledger in completed_ledgers:
        for output in ledger["outputs"]:
            if output["output_template_id"] in registered_ids:
                continue
            # A moved registry shard must not release an immutable identity.
            # Reconstruct reservations in memory only; never restore archives.
            archived = {
                **output,
                "family": f"aipuzzle{output['source_template_id']}{output['signature']}",
                "output_workspace_id": workspace_id,
                "_archived_registry": True,
            }
            suffix = f"/evidence/{output['output_template_id']}_revalidation.json"
            evidence = next((item for item in ledger["files"] if item["relative_path"].endswith(suffix)), None)
            if evidence is not None:
                archived["evidence_relative_path"] = evidence["relative_path"]
            records.append(archived)
    by_fold: dict[str, dict[str, Any]] = {}
    registered_manifest_paths = set()
    registered_bytes_paths = set()
    registered_evidence_paths = set()
    for record in records:
        output_id = str(record["output_template_id"])
        if record.get("output_workspace_id") != workspace_id:
            raise ReconciliationRequiredError("名称注册分片归属于另一输出工作区")
        folded = _nfc_casefold(output_id)
        if folded in by_fold:
            raise ReconciliationRequiredError("名称注册表存在大小写或 NFC 冲突")
        by_fold[folded] = record
        for key in ("source_template_id", "signature", "version", "output_relative_path", "manifest_relative_path", "output_sha256"):
            if key not in record:
                raise ReconciliationRequiredError("名称注册分片缺少必要字段", evidence={"field": key})
        family = record.get("family")
        version = record.get("version")
        if not isinstance(family, str) or not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ReconciliationRequiredError("名称注册分片的命名族或版本无效")
        expected_id = family if version == 1 else f"{family}v{version}"
        if output_id != expected_id:
            raise ReconciliationRequiredError("名称注册分片的模板 ID 与命名族/版本不一致")
        bytes_path = _safe_target(output_root, record["output_relative_path"])
        manifest_path = _safe_target(output_root, record["manifest_relative_path"])
        registered_bytes_paths.add(bytes_path.resolve())
        registered_manifest_paths.add(manifest_path.resolve())
        evidence_relative = record.get("evidence_relative_path")
        evidence_path = _safe_target(output_root, evidence_relative) if evidence_relative else None
        if evidence_path is not None:
            registered_evidence_paths.add(evidence_path.resolve())
        if (record.get("_archived_registry") or not bytes_path.is_file() or not manifest_path.is_file()
                or evidence_path is not None and not evidence_path.is_file()):
            completed = completed_outputs.get(output_id)
            if completed is not None and all(
                record.get(key) == completed.get(key)
                for key in ("output_relative_path", "manifest_relative_path", "output_sha256", "source_template_id", "signature", "version")
            ):
                continue
            raise ReconciliationRequiredError(
                "名称注册分片指向缺失的 bytes 或 manifest",
                evidence={"output_template_id": output_id},
            )
        data = bytes_path.read_bytes()
        if _sha256(data) != record["output_sha256"]:
            raise ReconciliationRequiredError("正式 bytes 与名称注册哈希不一致")
        try:
            parsed = parse_level_bytes(data, source_name=bytes_path.name)
        except ValueError as exc:
            raise ReconciliationRequiredError("正式 bytes 无法重新解析") from exc
        if parsed.template_id != output_id or bytes_path.name != f"{output_id}_m.bytes":
            raise ReconciliationRequiredError("正式 bytes 文件名、内部 ID 与注册表不一致")
        manifest = _read_json(manifest_path)
        if manifest.get("output_workspace_id") != workspace_id:
            raise ReconciliationRequiredError("manifest 归属于另一输出工作区")
        if manifest.get("output_template_id") != output_id or manifest.get("output_sha256") != record["output_sha256"]:
            raise ReconciliationRequiredError("正式 manifest 与注册表不一致")
        evidence_path = _verify_revalidation_evidence(output_root, manifest)
        if evidence_path is not None:
            descriptor = manifest["revalidation_evidence"]
            if (
                record.get("evidence_relative_path")
                != descriptor.get("relative_path")
                or record.get("evidence_sha256")
                != descriptor.get("evidence_sha256")
                or record.get("revalidation_sha256")
                != descriptor.get("revalidation_sha256")
            ):
                raise ReconciliationRequiredError("复验证据索引与名称注册表不一致")
            registered_evidence_paths.add(evidence_path.resolve())
        registered_bytes_paths.add(bytes_path.resolve())
        registered_manifest_paths.add(manifest_path.resolve())

    for path in output_root.iterdir():
        folded_name = _nfc_casefold(path.name)
        if not (
            path.is_file()
            and folded_name.startswith("aipuzzle")
            and folded_name.endswith("_m.bytes")
        ):
            continue
        if path.resolve() not in registered_bytes_paths:
            raise ReconciliationRequiredError("检测到孤立正式 bytes", evidence={"path": str(path)})
    reports = _safe_target(output_root, REPORTS_DIRECTORY)
    if reports.exists():
        for path in reports.glob("*/manifests/*_manifest.json"):
            _safe_target(output_root, path.relative_to(output_root).as_posix())
            if path.resolve() not in registered_manifest_paths:
                raise ReconciliationRequiredError("检测到孤立 manifest", evidence={"path": str(path)})
        for path in reports.glob("*/evidence/*_revalidation.json"):
            _safe_target(output_root, path.relative_to(output_root).as_posix())
            if path.resolve() not in registered_evidence_paths:
                raise ReconciliationRequiredError(
                    "检测到孤立复验证据",
                    evidence={"path": str(path)},
                )
        public_transactions = reports / PUBLIC_TRANSACTIONS_DIRECTORY
        if public_transactions.exists():
            _safe_target(
                output_root,
                public_transactions.relative_to(output_root).as_posix(),
            )
            for path in public_transactions.glob("*.json"):
                _safe_target(output_root, path.relative_to(output_root).as_posix())
                ledger_path = control / "transactions" / path.name
                if not ledger_path.is_file():
                    raise ReconciliationRequiredError(
                        "检测到没有可变账本归属的孤立事务清单",
                        evidence={"path": str(path)},
                    )
    return records


def _next_batch_id(output_root: Path, now: _datetime.datetime | None) -> str:
    date = _local_date(now)
    reports = _safe_target(output_root, REPORTS_DIRECTORY)
    occupied = set()
    if reports.exists():
        occupied.update(path.name for path in reports.iterdir() if path.is_dir() and _BATCH_ID.fullmatch(path.name))
    control = _safe_target(output_root, CONTROL_DIRECTORY)
    for path in (control / "transactions").glob("*.json"):
        record = _read_json(path)
        batch = record.get("batch_id")
        if isinstance(batch, str):
            occupied.add(batch)
    if date not in occupied:
        return date
    index = 1
    while f"{date}_{index:02d}" in occupied:
        index += 1
    return f"{date}_{index:02d}"


def _next_versions(
    records: Sequence[Mapping[str, Any]], prepared: Sequence[_PreparedSelection]
) -> list[int]:
    maxima: dict[tuple[str, str], int] = {}
    for record in records:
        key = (
            _nfc_casefold(str(record["source_template_id"])),
            _nfc_casefold(str(record["signature"])),
        )
        try:
            version = int(record["version"])
        except (TypeError, ValueError) as exc:
            raise ReconciliationRequiredError("名称注册分片版本号无效") from exc
        if version < 1:
            raise ReconciliationRequiredError("名称注册分片版本号无效")
        maxima[key] = max(maxima.get(key, 0), version)
    allocated = []
    for item in prepared:
        key = (
            _nfc_casefold(item.selection.source.level.template_id),
            _nfc_casefold(item.signature),
        )
        value = maxima.get(key, 0) + 1
        maxima[key] = value
        allocated.append(value)
    return allocated


def _duplicate_check(
    records: Sequence[Mapping[str, Any]], prepared: Sequence[_PreparedSelection], allow: bool
) -> None:
    if allow:
        return
    existing = {
        (str(item.get("content_semantic_hash")), str(item.get("puzzle_semantic_hash")))
        for item in records
    }
    duplicates = [
        item.family
        for item in prepared
        if (item.content_semantic_hash, item.puzzle_semantic_hash) in existing
    ]
    if duplicates:
        raise ExportConflictError(
            "候选与已有正式结果语义完全相同；需明确选择使用旧结果或另存新版本",
            evidence={"families": duplicates},
        )


def _family_pair(source_template_id: str, signature: str) -> tuple[str, str]:
    return (
        unicodedata.normalize("NFC", source_template_id),
        unicodedata.normalize("NFC", signature),
    )


def _validate_naming_namespace(
    records: Sequence[Mapping[str, Any]],
    prepared: Sequence[_PreparedSelection],
) -> None:
    """Reject delimiter-free family ambiguities before any formal write.

    Product naming intentionally concatenates source ID and signature without a
    separator.  Distinct pairs such as ``(base, s4s5)`` and ``(bases4, s5)``
    can therefore spell the same family.  Registry metadata is authoritative;
    string parsing must never guess which pair owns that family.
    """

    owners: dict[str, tuple[str, str]] = {}
    for record in records:
        source_id = str(record.get("source_template_id", ""))
        signature = str(record.get("signature", ""))
        family = str(record.get("family", ""))
        expected_family = f"aipuzzle{source_id}{signature}"
        if family != expected_family:
            raise ReconciliationRequiredError(
                "名称注册分片的命名族与源模板/签名不一致",
                evidence={"output_template_id": record.get("output_template_id")},
            )
        folded = _nfc_casefold(family)
        pair = _family_pair(source_id, signature)
        previous = owners.get(folded)
        if previous is not None and previous != pair:
            raise ReconciliationRequiredError(
                "历史名称注册表存在无分隔命名族归属冲突",
                evidence={"family": family, "owners": (previous, pair)},
            )
        owners[folded] = pair

    batch_owners: dict[str, tuple[str, str]] = {}
    for item in prepared:
        source_id = item.selection.source.level.template_id
        pair = _family_pair(source_id, item.signature)
        expected_family = f"aipuzzle{source_id}{item.signature}"
        if item.family != expected_family:
            raise ExportConflictError("待导出候选的命名族计算不一致")
        folded = _nfc_casefold(item.family)
        historical = owners.get(folded)
        if historical is not None and historical != pair:
            raise ExportConflictError(
                "待导出候选与历史结果形成无分隔命名族冲突",
                evidence={"family": item.family, "historical_owner": historical, "new_owner": pair},
            )
        previous = batch_owners.get(folded)
        if previous is not None and previous != pair:
            raise ExportConflictError(
                "同一批次的不同源模板/签名组合产生了相同命名族",
                evidence={"family": item.family, "owners": (previous, pair)},
            )
        batch_owners[folded] = pair


def _validate_output_id_namespace(
    records: Sequence[Mapping[str, Any]],
    prepared: Sequence[_PreparedSelection],
    versions: Sequence[int],
) -> None:
    occupied = {
        _nfc_casefold(str(record["output_template_id"])) for record in records
    }
    pending: dict[str, str] = {}
    for item, version in zip(prepared, versions):
        output_id = versioned_template_id(
            item.selection.source.level.template_id,
            item.selection.plan,
            version,
        )
        folded = _nfc_casefold(output_id)
        if folded in occupied or folded in pending:
            raise ExportConflictError(
                "预留的正式模板 ID 与历史或本批其他结果冲突",
                evidence={
                    "output_template_id": output_id,
                    "conflicting_output_template_id": pending.get(folded),
                },
            )
        pending[folded] = output_id


def _stage_artifact(staged: dict[str, bytes], relative_path: str, data: bytes) -> None:
    folded = _nfc_casefold(relative_path)
    conflict = next(
        (path for path in staged if _nfc_casefold(path) == folded),
        None,
    )
    if conflict is not None:
        raise ExportConflictError(
            "本批 staging 目标路径重复，已在正式写入前阻止",
            evidence={"path": relative_path, "conflict": conflict},
        )
    staged[relative_path] = data


def _required_version(
    payload: Mapping[str, Any], key: str, *, evidence_name: str
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExportConflictError(
            f"{evidence_name}缺少必需的版本字段 {key}"
        )
    return value


def _revalidation_versions(item: _PreparedSelection) -> dict[str, str]:
    difficulty = item.difficulty_simulation
    experience = item.experience_analysis
    difficulty_coverage = difficulty.get("evidence_coverage")
    difficulty_versions: Mapping[str, Any] = (
        difficulty_coverage
        if isinstance(difficulty_coverage, Mapping)
        else difficulty
    )
    signature_evidence = experience.get("experience_signature_evidence")
    style_distance = experience.get("online_style_distance")
    if not isinstance(signature_evidence, Mapping):
        raise ExportConflictError("整板体验证据缺少体验指纹版本")
    if not isinstance(style_distance, Mapping):
        raise ExportConflictError("整板体验证据缺少线上风格版本")
    return {
        "algorithm_version": _required_version(
            {
                "algorithm_version": (
                    item.selection.generated.candidate.algorithm_version
                )
            },
            "algorithm_version",
            evidence_name="候选生成证据",
        ),
        "rules_version": RULES_VERSION,
        "export_format_version": EXPORT_FORMAT_VERSION,
        "common_seed_version": _required_version(
            difficulty_versions,
            "common_seed_version",
            evidence_name="难度模拟证据",
        ),
        "materialization_version": _required_version(
            difficulty_versions,
            "materialization_version",
            evidence_name="难度模拟证据",
        ),
        "simulation_version": _required_version(
            difficulty_versions,
            "simulation_version",
            evidence_name="难度模拟证据",
        ),
        "experience_analysis_version": _required_version(
            experience,
            "version",
            evidence_name="整板体验证据",
        ),
        "experience_signature_version": _required_version(
            signature_evidence,
            "version",
            evidence_name="体验指纹证据",
        ),
        "online_style_profile_version": _required_version(
            style_distance,
            "profile_version",
            evidence_name="线上风格证据",
        ),
    }


def _build_revalidation_evidence(
    item: _PreparedSelection,
    output: ExportedTemplate,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Build one content-addressed evidence artifact and its manifest pointer."""

    versions = _revalidation_versions(item)
    validation_payload = item.validation.as_dict()
    validation_report_sha256 = config_hash(validation_payload)
    revalidation = {
        "identity": {
            "candidate_id": item.selection.generated.candidate.candidate_id,
            "source_template_id": output.source_template_id,
            "source_sha256": item.selection.source.level.sha256,
            "output_template_id": output.output_template_id,
            "output_sha256": output.output_sha256,
            "content_semantic_hash": item.content_semantic_hash,
            "puzzle_semantic_hash": item.puzzle_semantic_hash,
            "resolved_output_plan_id": item.selection.plan.plan_id,
            "generation_seed": item.selection.generation_seed,
            "candidate_seed_ordinal": (
                item.selection.generated.candidate.candidate_seed_ordinal
            ),
            "candidate_seed_sha256": (
                item.selection.generated.candidate.candidate_seed_sha256
            ),
        },
        "provenance": {
            "kind": "independently_recomputed_during_formal_export",
            "candidate_payload_exact_match_required": True,
            "validation_report_sha256": validation_report_sha256,
            "manual_evidence_override": (
                None
                if item.selection.manual_evidence_override is None
                else dict(item.selection.manual_evidence_override)
            ),
        },
        "versions": versions,
        # These are the complete recomputed objects.  Per-strategy reveal
        # curves live under duplicates/source_duplicates[*].strategies[*].
        # reveal_curve, so no second flattened copy is necessary.
        "difficulty_simulation": item.difficulty_simulation,
        "experience_analysis": item.experience_analysis,
    }
    revalidation_sha256 = config_hash(revalidation)
    document = {
        "evidence_format_version": REVALIDATION_EVIDENCE_FORMAT_VERSION,
        "evidence_kind": "candidate_revalidation",
        "revalidation_sha256": revalidation_sha256,
        "evidence_index": {
            "difficulty_simulation": "revalidation.difficulty_simulation",
            "candidate_per_strategy_reveal": (
                "revalidation.difficulty_simulation.duplicates[*].strategies.*.reveal_curve"
            ),
            "source_per_strategy_reveal": (
                "revalidation.difficulty_simulation.source_duplicates[*].strategies.*.reveal_curve"
            ),
            "experience_analysis": "revalidation.experience_analysis",
        },
        "revalidation": revalidation,
    }
    evidence_bytes = render_authoritative_json(document)
    descriptor = {
        "evidence_format_version": REVALIDATION_EVIDENCE_FORMAT_VERSION,
        "kind": "candidate_revalidation",
        "evidence_sha256": _sha256(evidence_bytes),
        "revalidation_sha256": revalidation_sha256,
        "validation_report_sha256": validation_report_sha256,
        "versions": versions,
    }
    return document, evidence_bytes, descriptor


def _report_row(item: _PreparedSelection, output: ExportedTemplate) -> dict[str, Any]:
    report = item.validation
    manual_record = item.selection.manual_evidence_override
    if manual_record is None:
        validation_status = "通过"
    elif not report.passed:
        validation_status = "规则结论未通过（用户确认导出）"
    elif str(manual_record.get("status", "")) == "disproved":
        validation_status = "未达推荐条件（用户确认导出）"
    else:
        validation_status = "依据不完整（用户确认导出）"
    conclusion = report.combination_conclusion
    if conclusion is None:
        conclusion_text = "仅塔内：不计算谜题组合"
    elif conclusion.contains_pressure_unit:
        conclusion_text = "包含 s8/s9：按单元高压语义与拓扑通过"
    else:
        conclusion_text = "7 格背包下允许的整单元顺序均可完成"
    return {
        "online_level_number": item.selection.online_level_number,
        "level_id": item.selection.level_id,
        "source_template_id": output.source_template_id,
        "output_template_id": output.output_template_id,
        "replaced_a_count": report.replaced_a_count,
        "replaced_b_count": report.replaced_b_count,
        "structure_types": [name for name, _count in report.structure_counts],
        "new_structure_count": report.replacement_count,
        "signature": output.signature,
        "puzzle_units": [
            {"unit_id": unit.unit_id, "actual_structure_step": unit.proof.minimum_structure_step}
            for unit in item.selection.generated.candidate.units
        ],
        "source_sha256": item.selection.source.level.sha256,
        "output_sha256": output.output_sha256,
        "generation_seed": item.selection.generation_seed,
        "validation_status": validation_status,
        "manual_evidence_override": (
            None
            if item.selection.manual_evidence_override is None
            else {"confirmed": True, **dict(item.selection.manual_evidence_override)}
        ),
        "failure_reason": "",
        "tower_counts": {
            "inside": report.tower_structure_count,
            "outside": report.non_tower_structure_count,
        },
        "nested_counts": {"A": report.nested_a_count, "B": report.nested_b_count},
        "source_total_tile_count": report.inventory.source_total_tile_count,
        "source_existing_structure_count": (
            report.inventory.source_existing_structure_count
        ),
        "output_structure_count": report.inventory.output_structure_count,
        "effective_structure_count_limit": (
            report.effective_constraints.effective_structure_count_limit
        ),
        "effective_max_structure_layer": (
            report.effective_constraints.effective_max_structure_layer
        ),
        "highest_structure_layer": report.highest_structure_layer,
        "adjacent_single_full_count": (
            report.coverage_audit.adjacent_single_full_count
        ),
        "cross_layer_single_full_count": (
            report.coverage_audit.cross_layer_single_full_count
        ),
        "joint_full_cover_count": report.coverage_audit.joint_full_cover_count,
        "initial_entry_count": report.initial_entry_count,
        "external_blocker_count": report.external_blocker_count,
        "combination_conclusion_text": conclusion_text,
    }


def _build_transaction(
    output_root: Path,
    workspace_id: str,
    request: ExportRequest,
    request_hash: str,
    prepared: Sequence[_PreparedSelection],
    records: Sequence[Mapping[str, Any]],
    batch_id: str,
    versions: Sequence[int],
    timestamp: str,
) -> tuple[dict[str, Any], dict[str, bytes], Tuple[ExportedTemplate, ...]]:
    staged: dict[str, bytes] = {}
    outputs = []
    manifests = []
    registry_records = []
    rows = []
    batch_prefix = f"{REPORTS_DIRECTORY}/{batch_id}"
    state_authority = {
        "kind": "mutable_transaction_ledger",
        "relative_path": (
            f"{CONTROL_DIRECTORY}/transactions/{request.idempotency_key}.json"
        ),
        "authoritative_fields": (
            "state",
            "state_history",
            "recovery_count",
            "last_recovery_result",
            "updated_at",
        ),
    }
    for item, version in zip(prepared, versions):
        source_id = item.selection.source.level.template_id
        output_id = versioned_template_id(source_id, item.selection.plan, version)
        if not _SAFE_ID.fullmatch(output_id):
            raise ExportConflictError("计算出的正式模板 ID 不安全")
        output_rel = f"{output_id}_m.bytes"
        manifest_rel = f"{batch_prefix}/manifests/{output_id}_manifest.json"
        evidence_rel = (
            f"{batch_prefix}/evidence/{output_id}_revalidation.json"
        )
        registry_rel = f"{REPORTS_DIRECTORY}/{REGISTRY_DIRECTORY}/{output_id}.json"
        output_bytes = rewrite_template(
            item.selection.source.level,
            output_id,
            _replacement_map(item.selection),
        )
        verification = verify_rewrite(
            item.selection.source.level,
            output_bytes,
            output_id,
            _replacement_map(item.selection),
        )
        output = ExportedTemplate(
            source_template_id=source_id,
            output_template_id=output_id,
            signature=item.signature,
            version=version,
            output_relative_path=output_rel,
            manifest_relative_path=manifest_rel,
            registry_relative_path=registry_rel,
            output_sha256=verification.output_sha256,
            content_semantic_hash=item.content_semantic_hash,
            puzzle_semantic_hash=item.puzzle_semantic_hash,
            config_hash=item.config_hash,
            candidate_id=item.selection.generated.candidate.candidate_id,
            resolved_plan_id=item.selection.plan.plan_id,
        )
        _evidence_document, evidence_bytes, evidence_descriptor = (
            _build_revalidation_evidence(item, output)
        )
        evidence_descriptor = {
            **evidence_descriptor,
            "relative_path": evidence_rel,
        }
        manifest = {
            "manifest_format_version": MANIFEST_FORMAT_VERSION,
            "output_workspace_id": workspace_id,
            "profile": {"profile_id": request.profile.profile_id, "name": request.profile.name},
            "output_template_id": output_id,
            "source_template_id": source_id,
            "input_mode": item.selection.source.input_mode,
            "source_locator": item.selection.source.source_locator,
            "source_filename": item.selection.source.source_filename,
            "source_sha256": item.selection.source.level.sha256,
            "source_tile_counts": {
                "A": sum(tile.tile_id == -10 for tile in item.selection.source.level.tiles),
                "B": sum(tile.tile_id == -11 for tile in item.selection.source.level.tiles),
            },
            "output_sha256": output.output_sha256,
            "content_semantic_hash": item.content_semantic_hash,
            "puzzle_semantic_hash": item.puzzle_semantic_hash,
            "config_hash": item.config_hash,
            "signature": item.signature,
            "version": version,
            "original_output_plan_id": item.selection.original_plan_id or item.selection.plan.source_plan_id,
            "resolved_output_plan_id": item.selection.plan.plan_id,
            "candidate_ordinal": item.selection.candidate_ordinal,
            "candidate_id": item.selection.generated.candidate.candidate_id,
            "candidate_seed_ordinal": (
                item.selection.generated.candidate.candidate_seed_ordinal
            ),
            "candidate_seed_sha256": (
                item.selection.generated.candidate.candidate_seed_sha256
            ),
            "generation_seed": item.selection.generation_seed,
            "resolved_parameters": asdict(item.selection.generation_options),
            "puzzle_units": [unit.as_dict() for unit in item.selection.generated.candidate.units],
            "replacements": [asdict(value) for value in item.selection.generated.candidate.replacements],
            "validation": item.validation.as_dict(),
            "manual_evidence_override": (
                None
                if item.selection.manual_evidence_override is None
                else {
                    "confirmed": True,
                    **dict(item.selection.manual_evidence_override),
                }
            ),
            "revalidation_evidence": evidence_descriptor,
            "tool_version": EXPORT_FORMAT_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "rules_version": RULES_VERSION,
            "batch_id": batch_id,
            "export_idempotency_key": request.idempotency_key,
            "created_at": timestamp,
            "execution_state_embedded": False,
            "execution_state_authority": state_authority,
        }
        registry_record = {
            "registry_format_version": 1,
            "output_workspace_id": workspace_id,
            "output_template_id": output_id,
            "source_template_id": source_id,
            "signature": item.signature,
            "family": item.family,
            "version": version,
            "output_relative_path": output_rel,
            "manifest_relative_path": manifest_rel,
            "evidence_relative_path": evidence_rel,
            "evidence_sha256": evidence_descriptor["evidence_sha256"],
            "revalidation_sha256": evidence_descriptor[
                "revalidation_sha256"
            ],
            "output_sha256": output.output_sha256,
            "content_semantic_hash": item.content_semantic_hash,
            "puzzle_semantic_hash": item.puzzle_semantic_hash,
            "transaction_id": request.idempotency_key,
            "created_at": timestamp,
        }
        _stage_artifact(staged, output_rel, output_bytes)
        _stage_artifact(staged, evidence_rel, evidence_bytes)
        _stage_artifact(staged, manifest_rel, render_authoritative_json(manifest))
        _stage_artifact(staged, registry_rel, render_authoritative_json(registry_record))
        outputs.append(output)
        manifests.append(manifest)
        registry_records.append(registry_record)
        rows.append(_report_row(item, output))

    batch_meta = {
        "batch_id": batch_id,
        "status": "validation_passed",
        "created_at": timestamp,
        "output_workspace_id": workspace_id,
        "idempotency_key": request.idempotency_key,
        "manual_evidence_override_count": sum(
            item.selection.manual_evidence_override is not None
            for item in prepared
        ),
        "execution_state_embedded": False,
        "execution_state_authority": state_authority,
    }
    _stage_artifact(
        staged,
        f"{batch_prefix}/{batch_id}_验证明细.json",
        render_authoritative_json({"batch": batch_meta, "outputs": manifests}),
    )
    _stage_artifact(
        staged,
        f"{batch_prefix}/{batch_id}_生成参数.json",
        render_authoritative_json({
            "batch": batch_meta,
            "request_payload_hash": request_hash,
            "parameters": [asdict(item.selection.generation_options) for item in prepared],
        }),
    )
    _stage_artifact(
        staged,
        f"{batch_prefix}/{batch_id}_对照表.csv",
        render_comparison_csv(rows),
    )
    _stage_artifact(
        staged,
        f"{batch_prefix}/{batch_id}_验证报告.md",
        render_batch_markdown(batch_meta, rows).encode("utf-8"),
    )
    _stage_artifact(
        staged,
        f"{batch_prefix}/{batch_id}_失败清单.json",
        render_authoritative_json({"batch_id": batch_id, "failures": []}),
    )

    public_transaction_rel = f"{REPORTS_DIRECTORY}/{PUBLIC_TRANSACTIONS_DIRECTORY}/{request.idempotency_key}.json"
    expected_before_public = [
        {"relative_path": path, "sha256": _sha256(data)}
        for path, data in sorted(staged.items())
    ]
    public_transaction = {
        "transaction_format_version": TRANSACTION_FORMAT_VERSION,
        "output_workspace_id": workspace_id,
        "idempotency_key": request.idempotency_key,
        "request_payload_hash": request_hash,
        "batch_id": batch_id,
        "outputs": [item.as_dict() for item in outputs],
        "files": expected_before_public,
        "transaction_manifest_relative_path": public_transaction_rel,
        "created_at": timestamp,
        "execution_state_embedded": False,
        "execution_state_authority": state_authority,
    }
    _stage_artifact(
        staged,
        public_transaction_rel,
        render_authoritative_json(public_transaction),
    )
    files = [
        {"relative_path": path, "sha256": _sha256(data)}
        for path, data in sorted(staged.items())
    ]
    ledger = {
        "transaction_format_version": TRANSACTION_FORMAT_VERSION,
        "output_workspace_id": workspace_id,
        "idempotency_key": request.idempotency_key,
        "request_payload_hash": request_hash,
        "state": "prepared",
        "state_history": [{"state": "prepared", "at": timestamp}],
        "batch_id": batch_id,
        "outputs": [item.as_dict() for item in outputs],
        "files": files,
        "created_paths": [],
        "created_at": timestamp,
        "updated_at": timestamp,
        "recovery_count": 0,
        "last_recovery_result": None,
    }
    return ledger, staged, tuple(outputs)


def _write_staging(control: Path, key: str, staged: Mapping[str, bytes]) -> Path:
    root = _safe_target(control, f"staging/{key}")
    if root.exists():
        raise ReconciliationRequiredError("新事务的 staging 目录已存在")
    _ensure_directory(root, trusted_root=control)
    _fsync_directory(root.parent)
    for relative_path, data in sorted(staged.items()):
        path = _safe_target(root, relative_path)
        _ensure_directory(path.parent, trusted_root=root)
        descriptor = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    _fsync_directory(root)
    return root


def _verify_staging(staging: Path, files: Sequence[Mapping[str, Any]]) -> None:
    for record in files:
        path = _safe_target(staging, record["relative_path"])
        if not path.is_file() or _sha256(path.read_bytes()) != record["sha256"]:
            raise ReconciliationRequiredError(
                "staging 文件缺失或哈希不一致", evidence={"path": str(path)}
            )


def _safe_target(output_root: Path, relative_path: Any) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise ReconciliationRequiredError("事务或注册记录包含无效相对路径")
    if "\x00" in relative_path or any(ord(character) < 32 for character in relative_path):
        raise ReconciliationRequiredError("事务或注册记录路径包含控制字符")
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReconciliationRequiredError("事务账本包含不安全相对路径")
    target = output_root.joinpath(*relative.parts)
    _confined_path(output_root, target)
    _reject_symlink_chain(output_root, target)
    resolved_root = output_root.resolve(strict=True)
    resolved_parent = target.parent.resolve(strict=False)
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise ReconciliationRequiredError("事务账本路径逃逸输出根目录")
    return target


def _place_files(output_root: Path, control: Path, ledger_path: Path, ledger: dict[str, Any]) -> None:
    staging = _safe_target(control, f"staging/{ledger['idempotency_key']}")
    _verify_staging(staging, ledger["files"])
    created = set(ledger.get("created_paths", []))
    for record in ledger["files"]:
        relative_path = str(record["relative_path"])
        source = _safe_target(staging, relative_path)
        target = _safe_target(output_root, relative_path)
        _ensure_directory(target.parent, trusted_root=output_root)
        try:
            os.link(str(source), str(target), follow_symlinks=False)
            descriptor = os.open(str(target), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory(target.parent)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or _sha256(target.read_bytes()) != record["sha256"]:
                raise ReconciliationRequiredError(
                    "目标路径已被其他内容占用，禁止覆盖",
                    evidence={"relative_path": relative_path},
                )
        created.add(relative_path)
        ledger["created_paths"] = sorted(created)
        ledger["updated_at"] = _utc_timestamp()
        _atomic_mutable_json(ledger_path, ledger, trusted_root=control)
    ledger["state"] = "files_written"
    ledger["updated_at"] = _utc_timestamp()
    ledger.setdefault("state_history", []).append(
        {"state": "files_written", "at": ledger["updated_at"]}
    )
    _atomic_mutable_json(ledger_path, ledger, trusted_root=control)


def _verify_transaction(output_root: Path, ledger: Mapping[str, Any], *, allow_archived: bool = False) -> None:
    expected = {str(item["relative_path"]): str(item["sha256"]) for item in ledger["files"]}
    for relative_path, digest in expected.items():
        path = _safe_target(output_root, relative_path)
        if allow_archived and not path.exists():
            continue
        if not path.is_file() or _sha256(path.read_bytes()) != digest:
            raise ReconciliationRequiredError(
                "正式导出文件缺失或哈希不一致",
                evidence={"relative_path": relative_path},
            )
    for output in ledger["outputs"]:
        output_relative = output["output_relative_path"]
        registry_relative = output["registry_relative_path"]
        manifest_relative = output["manifest_relative_path"]
        for relative_path in (output_relative, registry_relative, manifest_relative):
            if relative_path not in expected:
                raise ReconciliationRequiredError(
                    "事务输出路径未纳入本事务精确 allowlist",
                    evidence={"relative_path": relative_path},
                )
        path = _safe_target(output_root, output_relative)
        registry_path = _safe_target(output_root, registry_relative)
        manifest_path = _safe_target(output_root, manifest_relative)
        if allow_archived and any(not item.exists() for item in (path, registry_path, manifest_path)):
            continue
        parsed = parse_level_bytes(path.read_bytes(), source_name=path.name)
        if parsed.template_id != output["output_template_id"]:
            raise ReconciliationRequiredError("正式 bytes 内部 TemplateId 不一致")
        registry = _read_json(_safe_target(output_root, registry_relative))
        manifest = _read_json(_safe_target(output_root, manifest_relative))
        if registry.get("output_template_id") != output["output_template_id"]:
            raise ReconciliationRequiredError("名称注册表归属不一致")
        if manifest.get("output_template_id") != output["output_template_id"]:
            raise ReconciliationRequiredError("manifest 归属不一致")
        if output.get("candidate_id") is not None and (
            manifest.get("candidate_id") != output["candidate_id"]
        ):
            raise ReconciliationRequiredError("manifest 候选归属不一致")
        if output.get("resolved_plan_id") is not None and (
            manifest.get("resolved_output_plan_id") != output["resolved_plan_id"]
        ):
            raise ReconciliationRequiredError("manifest 方案归属不一致")
        if registry.get("output_sha256") != output["output_sha256"] or manifest.get("output_sha256") != output["output_sha256"]:
            raise ReconciliationRequiredError("名称注册表或 manifest 哈希不一致")


def _result_from_ledger(ledger: Mapping[str, Any], *, recovered: bool) -> ExportResult:
    return ExportResult(
        output_workspace_id=str(ledger["output_workspace_id"]),
        idempotency_key=str(ledger["idempotency_key"]),
        request_payload_hash=str(ledger["request_payload_hash"]),
        batch_id=str(ledger["batch_id"]),
        state=str(ledger["state"]),
        outputs=tuple(ExportedTemplate(**item) for item in ledger["outputs"]),
        recovered=recovered,
        recovery_count=int(ledger.get("recovery_count", 0)),
    )


def _resume(
    output_root: Path,
    control: Path,
    ledger_path: Path,
    ledger: dict[str, Any],
) -> ExportResult:
    state = ledger.get("state")
    if state not in _STATE_ORDER:
        raise ReconciliationRequiredError("事务账本状态无效")
    ledger["recovery_count"] = int(ledger.get("recovery_count", 0)) + 1
    ledger["last_recovery_result"] = "resume_started"
    ledger["updated_at"] = _utc_timestamp()
    _atomic_mutable_json(ledger_path, ledger, trusted_root=control)
    if _STATE_ORDER[state] < _STATE_ORDER["files_written"]:
        _place_files(output_root, control, ledger_path, ledger)
    _verify_transaction(output_root, ledger)
    ledger["state"] = "verified"
    ledger["last_recovery_result"] = "verified"
    ledger["updated_at"] = _utc_timestamp()
    ledger.setdefault("state_history", []).append(
        {"state": "verified", "at": ledger["updated_at"]}
    )
    _atomic_mutable_json(ledger_path, ledger, trusted_root=control)
    ledger["state"] = "completed"
    ledger["last_recovery_result"] = "completed"
    ledger["updated_at"] = _utc_timestamp()
    ledger.setdefault("state_history", []).append(
        {"state": "completed", "at": ledger["updated_at"]}
    )
    _atomic_mutable_json(ledger_path, ledger, trusted_root=control)
    return _result_from_ledger(ledger, recovered=True)


def export_selected(
    request: ExportRequest,
    *,
    now: _datetime.datetime | None = None,
    task_control: CooperativeTaskControl | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> ExportResult:
    """Formally export selected candidates using a recoverable transaction.

    ``now`` exists for deterministic integration tests; production callers
    should omit it.  A completed idempotent retry returns the original result.
    """

    if task_control is not None:
        task_control.checkpoint("export_validate_request")
    key = _validate_idempotency_key(request.idempotency_key)
    output_root = validate_output_root(
        request.profile.output_root,
        tile_repo_root=request.profile.tile_repo_root,
        template_dir=request.profile.template_dir,
        create=False,
    )
    prepared = _prepare_selections(
        request.selections,
        task_control=task_control,
        progress_callback=progress_callback,
    )
    if task_control is not None:
        task_control.checkpoint("export_initialize_workspace")
    workspace_id, control = _initialize_workspace(output_root)
    request_hash = _request_hash(workspace_id, request, prepared)
    reports_root = output_root / REPORTS_DIRECTORY
    ledger_path, _public_path = _transaction_paths(control, reports_root, key)

    with _export_lock(control):
        if ledger_path.exists():
            ledger = _read_json(ledger_path)
            if ledger.get("output_workspace_id") != workspace_id:
                raise ReconciliationRequiredError("事务账本工作区 ID 不一致")
            if ledger.get("request_payload_hash") != request_hash:
                raise ExportConflictError("同一幂等键已绑定不同的导出请求")
            if ledger.get("state") == "completed":
                _verify_transaction(output_root, ledger)
                if task_control is not None:
                    task_control.mark_completed()
                return _result_from_ledger(ledger, recovered=False)
            if task_control is not None:
                task_control.begin_uninterruptible("export_recover_commit")
            result = _resume(output_root, control, ledger_path, ledger)
            if task_control is not None:
                task_control.mark_completed()
            return result

        records = _audit_workspace(
            output_root,
            control,
            current_key=key,
            workspace_id=workspace_id,
        )
        if task_control is not None:
            task_control.checkpoint("export_workspace_audited")
        _validate_naming_namespace(records, prepared)
        _duplicate_check(records, prepared, request.allow_duplicate_as_new_version)
        batch_id = _next_batch_id(output_root, now)
        versions = _next_versions(records, prepared)
        _validate_output_id_namespace(records, prepared, versions)
        timestamp = _utc_timestamp(now)
        ledger, staged, _outputs = _build_transaction(
            output_root,
            workspace_id,
            request,
            request_hash,
            prepared,
            records,
            batch_id,
            versions,
            timestamp,
        )
        if task_control is not None:
            # From staging publication through ledger completion this
            # transaction must run atomically with respect to user controls.
            task_control.begin_uninterruptible("export_commit")
        _write_staging(control, key, staged)
        # The durable prepared ledger publishes a fully fsynced staging set.
        _exclusive_json(ledger_path, ledger, trusted_root=control)
        _place_files(output_root, control, ledger_path, ledger)
        _verify_transaction(output_root, ledger)
        ledger["state"] = "verified"
        ledger["updated_at"] = _utc_timestamp()
        ledger.setdefault("state_history", []).append(
            {"state": "verified", "at": ledger["updated_at"]}
        )
        _atomic_mutable_json(ledger_path, ledger, trusted_root=control)
        ledger["state"] = "completed"
        ledger["updated_at"] = _utc_timestamp()
        ledger.setdefault("state_history", []).append(
            {"state": "completed", "at": ledger["updated_at"]}
        )
        _atomic_mutable_json(ledger_path, ledger, trusted_root=control)
        result = _result_from_ledger(ledger, recovered=False)
        if task_control is not None:
            task_control.mark_completed()
        return result


__all__ = [
    "ExportRequest",
    "ExportResult",
    "ExportSelection",
    "ExportedTemplate",
    "export_selected",
    "inconclusive_evidence_record",
    "new_idempotency_key",
]
