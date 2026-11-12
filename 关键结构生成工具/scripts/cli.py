#!/usr/bin/env python3
"""Developer CLI for the headless key-structure generation core.

The future local web UI will call the same service functions.  This CLI exists
so parsing, planning, generation and validation can be exercised before any UI
style or interaction is committed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping


SCRIPT_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = SCRIPT_ROOT.parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from puzzle_core.config_io import (  # noqa: E402
    ConfigError,
    load_batch_request,
    resolve_batch_config,
)
from puzzle_core.errors import InputValidationError, PuzzleToolError  # noqa: E402
from puzzle_core.planning import canonical_json  # noqa: E402
from services.import_service import (  # noqa: E402
    inspect_loaded_template,
    load_template_file,
    load_template_id,
)
from services.job_service import generate_batch  # noqa: E402
from services.path_service import is_within, real_path  # noqa: E402
from services.profile_service import ProfileService  # noqa: E402
from services.export_service import (  # noqa: E402
    ExportRequest,
    export_selected,
    new_idempotency_key,
)
from services.selection_service import (  # noqa: E402
    load_selection_payload,
    resolve_export_selections,
)
from services.snapshot_service import SourceSnapshotStore  # noqa: E402


def _json_ready(value: Any) -> Any:
    """Use the core canonicalizer to normalize enums and dataclasses."""

    return json.loads(canonical_json(value))


def _emit(payload: Any, *, stream: Any = sys.stdout) -> None:
    json.dump(
        _json_ready(payload),
        stream,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    stream.write("\n")
    stream.flush()


def _active_profile(service: ProfileService, *, initialize: bool) -> Any:
    profile = service.active_profile()
    if profile is None and initialize:
        profile = service.initialize(TOOL_ROOT)
    if profile is None:
        raise ConfigError(
            "profile_not_initialized",
            "请先执行 profile init 完成首次输出目录确认",
        )
    return profile


def _parse_file_inputs(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for index, raw in enumerate(values):
        if "=" not in raw:
            raise ConfigError(
                "invalid_file_mapping",
                "拖入文件映射格式必须是 模板ID=/path/to/file_m.bytes",
                f"--file-input[{index}]",
            )
        key, raw_path = raw.split("=", 1)
        key = key.strip()
        if not key or not raw_path.strip():
            raise ConfigError(
                "invalid_file_mapping",
                "模板 ID 和文件路径都不能为空",
                f"--file-input[{index}]",
            )
        if key in result:
            raise ConfigError(
                "duplicate_file_mapping",
                f"模板 {key!r} 被映射了多次",
                f"--file-input[{index}]",
            )
        result[key] = Path(raw_path).expanduser()
    return result


def _write_new(path: Path, data: bytes) -> None:
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        str(destination),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        raise


def _plan_payload(config: Any) -> dict[str, Any]:
    return {
        "source_config_hash": config.source_config_hash,
        "batch_seed": config.batch.batch_seed,
        "batch_seed_source": config.batch_seed_source,
        "locked_config_snapshot": json.loads(config.normalized_config_json)
        if config.normalized_config_json
        else None,
        "batch": asdict(config.batch),
        "work_items": [
            {
                "template_key": item.template_key,
                "plan": asdict(item.plan),
                "generation_options_preview": asdict(item.generation_options),
                "generation_parameter_spec": None
                if item.parameter_binding is None
                else asdict(item.parameter_binding),
                "parameter_source": item.parameter_source,
            }
            for item in config.work_items
        ],
    }


def _command_profile(args: argparse.Namespace) -> int:
    service = ProfileService()
    if args.profile_action == "init":
        profile = service.initialize(TOOL_ROOT, name=args.name)
        _emit(service.status(profile.profile_id))
        return 0
    profile = _active_profile(service, initialize=False)
    if args.profile_action == "show":
        _emit(service.status(profile.profile_id))
    elif args.profile_action == "set-source":
        changed = service.configure_source(
            profile.profile_id,
            Path(args.tile_repo_root),
            None if args.template_dir is None else Path(args.template_dir),
        )
        _emit(service.status(changed.profile_id))
    elif args.profile_action == "set-output":
        changed = service.configure_output(profile.profile_id, Path(args.output_root))
        _emit(service.status(changed.profile_id))
    else:  # argparse prevents this
        raise AssertionError(args.profile_action)
    return 0


def _command_inspect(args: argparse.Namespace) -> int:
    if bool(args.template_id) == bool(args.file):
        raise ConfigError(
            "input_choice",
            "--template-id 与 --file 必须且只能提供一个",
        )
    if args.file:
        loaded = load_template_file(Path(args.file))
    else:
        service = ProfileService()
        profile = _active_profile(service, initialize=False)
        loaded = load_template_id(profile, args.template_id)
    _emit(inspect_loaded_template(loaded).as_dict())
    return 0


def _load_resolved_config(
    path: str,
    *,
    batch_seed_override: int | None = None,
) -> Any:
    return resolve_batch_config(
        load_batch_request(Path(path), batch_seed_override=batch_seed_override)
    )


def _command_plan(args: argparse.Namespace) -> int:
    _emit(
        _plan_payload(
            _load_resolved_config(
                args.config,
                batch_seed_override=getattr(args, "batch_seed", None),
            )
        )
    )
    return 0


def _command_generate(args: argparse.Namespace) -> int:
    config = _load_resolved_config(
        args.config,
        batch_seed_override=getattr(args, "batch_seed", None),
    )
    service = ProfileService()
    profile = _active_profile(service, initialize=True)
    file_inputs = _parse_file_inputs(args.file_input)
    snapshot = None
    loaded_inputs = None
    if file_inputs:
        snapshot = SourceSnapshotStore(service.settings_path.parent).create(
            file_inputs,
            batch_seed=config.batch.batch_seed,
            source_config_hash=config.source_config_hash,
            normalized_config_json=config.normalized_config_json,
            profile=profile,
        )
        loaded_inputs = snapshot.sources
    result = generate_batch(
        config,
        profile,
        loaded_inputs=loaded_inputs,
        source_snapshot_id=None if snapshot is None else snapshot.snapshot_set_id,
    )
    payload = result.as_dict()
    if args.report:
        report_path = real_path(args.report)
        protected_roots = [
            real_path(value)
            for value in (profile.tile_repo_root, profile.template_dir)
            if value is not None
        ]
        if any(is_within(report_path, root) for root in protected_roots):
            raise ConfigError(
                "report_inside_source",
                "候选报告不能写入只读 Tile 仓库",
                "--report",
            )
        output_root = real_path(profile.output_root)
        if is_within(report_path, output_root):
            raise ConfigError(
                "report_inside_output",
                "候选报告不能写入正式输出工作区",
                "--report",
            )
        if any(report_path == real_path(value) for value in file_inputs.values()):
            raise ConfigError(
                "report_over_source",
                "候选报告不能覆盖拖入的源模板",
                "--report",
            )
        report_bytes = (
            json.dumps(
                _json_ready(payload),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        _write_new(report_path, report_bytes)
        payload = {
            "report_path": str(report_path),
            **payload,
        }
    _emit(payload)
    return 0 if result.failed == 0 else 3


def _command_new_export_key(_args: argparse.Namespace) -> int:
    _emit({"idempotency_key": new_idempotency_key()})
    return 0


def _command_export(args: argparse.Namespace) -> int:
    """Reproduce explicitly reviewed candidates, then formally export them."""

    if args.idempotency_key is not None and not args.idempotency_key:
        raise ConfigError(
            "empty_idempotency_key",
            "显式传入的导出幂等键不能为空",
            "--idempotency-key",
        )
    service = ProfileService()
    profile = _active_profile(service, initialize=False)
    reviewed_snapshot = None
    reviewed_snapshot_id = getattr(args, "review_snapshot", None)
    supplied_seed = getattr(args, "batch_seed", None)
    if reviewed_snapshot_id:
        reviewed_snapshot = SourceSnapshotStore(service.settings_path.parent).load(
            reviewed_snapshot_id,
            profile=profile,
        )
        if supplied_seed is not None and supplied_seed != reviewed_snapshot.batch_seed:
            raise ConfigError(
                "batch_seed_conflict",
                "--batch-seed 与审核快照中已锁定的种子不一致",
                "--batch-seed",
            )
        config = _load_resolved_config(
            args.config,
            batch_seed_override=reviewed_snapshot.batch_seed,
        )
        if (
            config.source_config_hash != reviewed_snapshot.source_config_hash
            or config.normalized_config_json != reviewed_snapshot.normalized_config_json
        ):
            raise InputValidationError(
                "当前配置与审核时锁定的规范化配置不一致"
            )
    else:
        config = _load_resolved_config(
            args.config,
            batch_seed_override=supplied_seed,
        )
        if getattr(config, "batch_seed_source", "provided") == "generated":
            raise ConfigError(
                "unlocked_export_seed",
                (
                    "配置省略了 batch_seed；正式导出必须用 --batch-seed 传回"
                    "候选审核报告中锁定的批次种子"
                ),
                "--batch-seed",
            )
    file_inputs = _parse_file_inputs(args.file_input)
    if reviewed_snapshot is not None:
        overlap = sorted(set(file_inputs) & set(reviewed_snapshot.sources))
        if overlap:
            raise ConfigError(
                "ambiguous_source_input",
                "同一模板不能同时使用审核快照和 --file-input",
                "--file-input",
            )
    choices = load_selection_payload(Path(args.selection))
    generated = generate_batch(
        config,
        profile,
        file_inputs=file_inputs,
        loaded_inputs=None if reviewed_snapshot is None else reviewed_snapshot.sources,
        source_snapshot_id=reviewed_snapshot_id,
    )
    selections = resolve_export_selections(config, generated, choices)
    key = args.idempotency_key
    if key is None:
        key = new_idempotency_key()
        # stderr is a flushed progress channel; stdout remains one final JSON result.
        _emit(
            {"event": "export_idempotency_key", "idempotency_key": key},
            stream=sys.stderr,
        )
    result = export_selected(
        ExportRequest(
            profile=profile,
            selections=selections,
            idempotency_key=key,
            allow_duplicate_as_new_version=args.duplicate_as_new_version,
        )
    )
    _emit(result.as_dict())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="关键结构生成工具",
        description="无界面核心：模板体检、批量方案展开、候选生成与独立验证。",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    profile = subcommands.add_parser("profile", help="首次配置与路径持久化")
    profile_sub = profile.add_subparsers(dest="profile_action", required=True)
    initialize = profile_sub.add_parser("init", help="初始化默认 Profile")
    initialize.add_argument("--name", default="默认环境")
    profile_sub.add_parser("show", help="显示当前 Profile 和路径状态")
    source = profile_sub.add_parser("set-source", help="配置只读 Tile 仓库")
    source.add_argument("tile_repo_root")
    source.add_argument("--template-dir")
    output = profile_sub.add_parser("set-output", help="更换可写输出根目录")
    output.add_argument("output_root")
    profile.set_defaults(handler=_command_profile)

    inspect = subcommands.add_parser("inspect", help="只读解析和体检一个模板")
    inspect.add_argument("--template-id")
    inspect.add_argument("--file")
    inspect.set_defaults(handler=_command_inspect)

    plan = subcommands.add_parser("plan", help="展开批次/单模板/多成品方案")
    plan.add_argument("--config", required=True)
    plan.add_argument(
        "--batch-seed",
        type=int,
        help="当配置省略 batch_seed 时，传入先前审核报告中已锁定的种子",
    )
    plan.set_defaults(handler=_command_plan)

    generate = subcommands.add_parser("generate", help="搜索候选并独立复验，不正式导出")
    generate.add_argument("--config", required=True)
    generate.add_argument(
        "--batch-seed",
        type=int,
        help="当配置省略 batch_seed 时，传入先前审核报告中已锁定的种子",
    )
    generate.add_argument(
        "--file-input",
        action="append",
        default=[],
        metavar="TEMPLATE_ID=PATH",
        help="指定拖入文件入口，可重复传入",
    )
    generate.add_argument(
        "--report",
        help="可选候选审核 JSON；已存在时拒绝覆盖",
    )
    generate.set_defaults(handler=_command_generate)

    new_key = subcommands.add_parser(
        "new-export-key",
        help="由后端生成可用于安全重试的导出幂等键",
    )
    new_key.set_defaults(handler=_command_new_export_key)

    export = subcommands.add_parser(
        "export",
        help="重现并复验明确选中的候选，再事务化正式导出",
    )
    export.add_argument("--config", required=True)
    export.add_argument(
        "--batch-seed",
        type=int,
        help="配置省略 batch_seed 时必传审核报告中的已锁定种子",
    )
    export.add_argument("--selection", required=True)
    export.add_argument(
        "--file-input",
        action="append",
        default=[],
        metavar="TEMPLATE_ID=PATH",
        help="指定拖入文件入口，可重复传入",
    )
    export.add_argument(
        "--review-snapshot",
        help=(
            "generate 报告返回的源快照 ID；原拖入路径移动后"
            "仍可用同一审核源重算导出"
        ),
    )
    export.add_argument(
        "--idempotency-key",
        help="技术重试时复用 new-export-key 生成的 UUID；省略则本次自动生成",
    )
    export.add_argument(
        "--duplicate-as-new-version",
        action="store_true",
        help="经人工确认，语义重复候选仍另存为下一个 vN",
    )
    export.set_defaults(handler=_command_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except PuzzleToolError as exc:
        _emit({"error": exc.detail.as_dict()}, stream=sys.stderr)
        return 2
    except ConfigError as exc:
        _emit({"error": exc.as_dict()}, stream=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        _emit(
            {
                "error": {
                    "code": type(exc).__name__,
                    "message": str(exc),
                }
            },
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
