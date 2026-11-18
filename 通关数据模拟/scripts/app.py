"""Local HTTP API and background job queue for the player simulation tool."""

from __future__ import annotations

import argparse
import csv
import json
import math
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from cost_model import estimate as estimate_cost
from fuuu import enrich
from parameters import int_field, options
from scenario_inputs import parse_entries
from sources import (
    ROOT, DATA, MODELS, default_repository, model_path, read_template,
    remember_repository, repository_directory, upload_templates,
)

MAX_BYTES = 40 * 1024 * 1024
MAX_SCENARIOS = 1000
VALID_ORIGIN_HOST = "127.0.0.1"
STATIC = ROOT / "frontend"
JOBS = {}
LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="player-simulation")


@lru_cache(maxsize=8)
def _model(path, mtime):
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    return payload


def player_model():
    path = model_path()
    if not path.is_file():
        return {}
    payload = _model(path, path.stat().st_mtime_ns)
    behavior = MODELS / "replay-behavior-model.json"
    cost = MODELS / "online-cost-model.json"
    tower_model = MODELS / "tower-choice-model.json"
    if behavior.is_file():
        payload = {**payload, "behavior": _model(behavior, behavior.stat().st_mtime_ns)}
    if cost.is_file():
        payload = {**payload, "cost_calibration": _model(cost, cost.stat().st_mtime_ns)}
    if tower_model.is_file():
        payload = {**payload, "tower_choices": _model(tower_model, tower_model.stat().st_mtime_ns)}
    return payload


def _calibration_warning(model):
    calibration = model.get("cost_calibration")
    if not calibration:
        return "当前没有适用的线上消耗校准样本。"
    mae = calibration.get("heldout_mae_observed_features")
    cells = calibration.get("joined_cells")
    level_range = calibration.get("level_range")
    if mae is None or not level_range:
        return ""
    return f"消耗校准：历史 {level_range[0]}–{level_range[1]} 关，{cells:,} 个副本样本格；按真实回放特征留出误差约 {mae:.0f}，实跑到线上误差尚需验证。"


def player_settings():
    model = player_model()
    return {
        "repository": default_repository(),
        "model_available": len(model.get("player_policies", [])) >= 1000,
        "profile_count": len(model.get("player_policies", [])),
        "model_version": model.get("feature_version"),
        "verified_replays": model.get("train_replays"),
        "behavior_replays": model.get("behavior", {}).get("audit", {}).get("used_rows"),
        "behavior_level_range": model.get("behavior", {}).get("levels"),
        "cost_holdout_warning": _calibration_warning(model),
    }


def _text(payload, name):
    text = payload.get(name, "")
    if not isinstance(text, str):
        raise ValueError(f"{name} 格式有误。")
    return text.strip()


def _merge_input(paste, config, uploads):
    if not config:
        return parse_entries(paste, uploads)
    if not paste:
        return parse_entries(config, uploads)
    requested = parse_entries(paste)
    # A config_level upload plus a simple paste list selects those rows from
    # the table. For actual mixed paste rows, keep both sets instead.
    config_rows = parse_entries(config, uploads)
    if all(row.parameters is None for row in requested):
        keys = {row.template_id for row in requested}
        level_keys = keys & {row.level_id for row in config_rows}
        template_keys = keys - level_keys
        rows = [row for row in config_rows if row.level_id in level_keys or row.template_id in template_keys]
        covered = level_keys | {row.template_id for row in rows}
        missing = sorted(keys - covered)
        if missing:
            raise ValueError("上传的配置表或模板文件没有这些 ID：" + "、".join(missing[:15]))
        return rows
    return config_rows + requested


def prepare(payload):
    upload = payload.get("template_files", [])
    if not isinstance(upload, list):
        raise ValueError("模板文件列表格式有误。")
    uploaded = upload_templates(upload)
    scenarios = _merge_input(_text(payload, "paste"), _text(payload, "file_text"), uploaded)
    if len(scenarios) > MAX_SCENARIOS:
        raise ValueError(f"已识别 {len(scenarios):,} 个副本。一次最多模拟 {MAX_SCENARIOS:,} 个；请粘贴本次需要的模板 ID 或关卡 ID，筛选上传配置。")
    trials = int_field(payload.get("trials"), "模拟局数", 1000, 20000, 1000)
    metric = payload.get("target_metric")
    recommendation = any(entry.parameters is None for entry in scenarios)
    ranges = payload.get("ranges", {})
    if not isinstance(ranges, dict):
        raise ValueError("搜索范围格式有误。")
    target = None
    if recommendation:
        if metric not in ("mean_cost", "naked_win_rate"):
            raise ValueError("缺少副本参数，请选择预期人均消耗或裸胜率作为推荐目标。")
        try:
            target = float(payload.get("target_value"))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("请输入有效的推荐目标。") from exc
        if not math.isfinite(target) or target < 0 or (metric == "naked_win_rate" and target > 100):
            raise ValueError("裸胜率须在 0%–100%，预期人均消耗须为非负数。")
        if metric == "naked_win_rate":
            target /= 100
    baseline = payload.get("baseline_help")
    if baseline is not None and baseline != "":
        baseline = int_field(baseline, "DDA 帮助值", -500, 500)
    else:
        baseline = None
    model = player_model()
    if len(model.get("player_policies", [])) < 1000:
        raise ValueError("当前真实点击模型不足 1,000 种玩家风格，无法开始模拟。")
    raw_repository = _text(payload, "repository") or default_repository()
    needs_repository = any(entry.template_id not in uploaded for entry in scenarios)
    path = repository_directory(raw_repository) if raw_repository and needs_repository else None
    boards = dict(uploaded)
    for entry in scenarios:
        if entry.template_id not in boards:
            boards[entry.template_id] = read_template(entry.template_id, path)
    entropy = secrets.randbits(63)
    if recommendation:
        plans = [options(boards[entry.template_id], ranges, entropy + index) if entry.parameters is None else None
                 for index, entry in enumerate(scenarios)]
    else:
        plans = [None] * len(scenarios)
    if path is not None:
        remember_repository(path)
    warnings = ["裸胜率由回放行为模型估算；目前上线校准仍有限，新模板和塔牌的预测误差可能较大。"]
    resolved = []
    for entry in scenarios:
        entry, warning = enrich(entry)
        resolved.append(entry)
        if warning and warning not in warnings:
            warnings.append(warning)
    scenarios = resolved
    for template_id, level in boards.items():
        if template_id != level.template_id:
            warnings.append(f"{template_id} 的文件内部 ID 为 {level.template_id}；本次使用该文件的实际牌面。")
    return scenarios, boards, plans, trials, metric, target, model, entropy, baseline, warnings


def _update(job_id, **fields):
    with LOCK:
        JOBS[job_id].update(fields)


def _estimate(engine, board, entry, params, trials, entropy, baseline, cost_model, progress):
    result = engine(board, entry, params, trials, entropy, baseline, progress=progress)
    if cost_model:
        value = estimate_cost(result, cost_model)
        if value is not None:
            result["mean_cost"] = value
            result["cost_source"] = "线上资源消耗校准"
    return result


def execute(job_id, prepared):
    try:
        from simulation import build_engine

        scenarios, boards, plans, trials, metric, target, model, entropy, baseline, warnings = prepared
        engine = build_engine(model)
        total_units = sum(1 if plan is None else len(plan) + min(5, len(plan)) for plan in plans)
        completed = 0
        rows = []

        def run(board, entry, params, count, random_seed):
            nonlocal completed
            _update(job_id, message=f"{entry.template_id} · 副本 {entry.duplicate} · {params} · {count:,} 局")
            def running(n, total):
                if total:
                    _update(job_id, progress=(completed + min(n / total, 1)) / total_units * 100)
            try:
                return _estimate(engine, board, entry, params, count, random_seed, baseline,
                                 model.get("cost_calibration"), running)
            finally:
                completed += 1
                _update(job_id, progress=completed / total_units * 100)

        def simulate_scenario(index, entry, plan):
            board = boards[entry.template_id]
            trial_seed = (entropy + index * 123019) % (2**63)
            extra = {}
            if plan is None:
                params = entry.parameters
                result = run(board, entry, params, trials, trial_seed)
            else:
                def rank(item):
                    params, values = item
                    estimate = values.get(metric)
                    return (abs(estimate - target) if estimate is not None else math.inf, str(params))
                nonlocal completed
                screen = []
                rejected = []
                for params in plan:
                    try:
                        result = run(board, entry, params, min(160, trials), trial_seed)
                        if result.get(metric) is not None:
                            screen.append((params, result))
                    except ValueError as exc:
                        # A single infeasible A/B/Seed combination does not stop
                        # other combinations for this template.
                        rejected.append(str(exc))
                if not screen:
                    raise ValueError(rejected[0] if rejected else "没有可估算目标的参数组合。")
                leaders = sorted(screen, key=rank)[:5]
                full = [(params, run(board, entry, params, trials, trial_seed ^ 0x39D3B)) for params, _ in leaders]
                completed += min(5, len(plan)) - len(full)
                params, result = min(full, key=rank)
                if result.get(metric) is None:
                    raise ValueError(f"{entry.template_id} 缺少估算该目标所需的模型数据，无法推荐参数。")
                extra = {
                    "target_metric": metric, "target_value": target,
                    "target_delta": result[metric] - target,
                    "screened_parameters": len(plan),
                    "skipped_parameters": len(rejected),
                    "verified_parameters": len(full),
                    "alternatives": [
                        {**v, "parameters": str(p), "target_delta": v[metric] - target}
                        for p, v in sorted(full, key=rank)[1:]
                    ],
                }
            return {**result, **extra, "template_id": entry.template_id, "duplicate": entry.duplicate,
                    "fuuu_profile": entry.fuuu, "level_id": entry.level_id, "parameters": str(params),
                    "trials": trials, "recommended": plan is not None}

        errors = 0
        for index, (entry, plan) in enumerate(zip(scenarios, plans)):
            start = completed
            try:
                row = simulate_scenario(index, entry, plan)
            except (ValueError, OSError) as exc:
                errors += 1
                detail = f"{entry.template_id} · 副本 {entry.duplicate}：{exc}"
                warnings.append(detail)
                row = {"template_id": entry.template_id, "duplicate": entry.duplicate, "level_id": entry.level_id,
                       "fuuu_profile": entry.fuuu, "parameters": str(entry.parameters) if entry.parameters else "—",
                       "notes": [detail]}
                completed = start + (1 if plan is None else len(plan) + min(5, len(plan)))
            rows.append(row)
            _update(job_id, rows=rows.copy(), warnings=warnings.copy(), progress=completed / total_units * 100)
        success = len(rows) - errors
        summary = f"已完成 {success} 个副本"
        if errors:
            summary += f"，{errors} 个未能模拟"
        _update(job_id, status="done" if success else "error", progress=100, message=summary, warnings=warnings)
    except (ValueError, OSError) as exc:
        _update(job_id, status="error", message=str(exc))
    except Exception:
        traceback.print_exc()
        _update(job_id, status="error", message="模拟中断。终端已记录错误；本任务已完成的副本仍可查看。")


def choose_directory():
    if sys.platform != "darwin":
        raise ValueError("当前系统不支持目录选择窗口，请手动输入路径。")
    script = 'POSIX path of (choose folder with prompt "请选择模板仓库目录")'
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("没能打开目录选择窗口，请手动输入路径。") from exc
    if result.returncode:
        if "-128" in result.stderr:
            return {"cancelled": True, "repository": default_repository()}
        raise ValueError("系统没有打开目录选择窗口，请手动输入路径。")
    path = repository_directory(result.stdout.strip())
    remember_repository(path)
    return {"repository": str(path)}


class Handler(BaseHTTPRequestHandler):
    server_version = "TileSimulation/1"

    def _allowed(self):
        host = f"{VALID_ORIGIN_HOST}:{self.server.server_port}"
        if self.headers.get("Host") != host or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        return not origin or origin == f"http://{host}"

    def _respond(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, payload):
        self._respond(status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8")

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/jobs/"):
            super().log_message(fmt, *args)

    def do_GET(self):
        if not self._allowed():
            return self._json(403, {"error": "请从本地工具地址访问。"})
        route = urlsplit(self.path).path
        if route == "/api/settings":
            return self._json(200, player_settings())
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})", route)
        if match:
            with LOCK:
                row = JOBS.get(match[1])
                snapshot = row.copy() if row else None
            return self._json(200, snapshot) if snapshot else self._json(404, {"error": "找不到模拟任务。"})
        path = STATIC / "index.html" if route == "/" else STATIC / route.lstrip("/")
        # Resolve only ordinary assets under the static tree (no directory listings).
        path = path.resolve()
        if path.is_relative_to(STATIC) and path.is_file() and path.suffix.lower() in {
            ".html", ".js", ".css", ".woff2", ".woff", ".svg", ".ico", ".png",
        }:
            if path.suffix == ".js":
                kind = "application/javascript; charset=utf-8"
            else:
                kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return self._respond(200, path.read_bytes(), kind)
        return self._json(404, {"error": "找不到页面。"})

    def do_POST(self):
        if not self._allowed():
            return self._json(403, {"error": "请从本地工具地址访问。"})
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"error": "请提交 JSON。"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= MAX_BYTES:
                return self._json(413, {"error": "内容为空或大于 40 MB。"})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求格式有误。")
            route = urlsplit(self.path).path
            if route == "/api/choose-directory":
                return self._json(200, choose_directory())
            if route != "/api/simulate":
                return self._json(404, {"error": "找不到操作。"})
            with LOCK:
                full = sum(row["status"] == "running" for row in JOBS.values()) >= 3
            if full:
                return self._json(409, {"error": "当前工作队列已满，请稍后重试。"})
            prepared = prepare(payload)
            job_id = uuid.uuid4().hex
            created = time.time()
            with LOCK:
                JOBS[job_id] = {
                    "status": "running", "message": "等待模拟", "progress": 0,
                    "rows": [], "warnings": prepared[-1], "created_at": created,
                    "cost_holdout_warning": _calibration_warning(prepared[6]),
                }
                while len(JOBS) > 24:
                    finished = next((key for key, job in JOBS.items() if job["status"] != "running"), None)
                    if not finished:
                        break
                    del JOBS[finished]
            EXECUTOR.submit(execute, job_id, prepared)
            return self._json(202, {"job_id": job_id})
        except (ValueError, OSError, csv.Error) as exc:
            return self._json(400, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--config-dir", help="ulinkconfig 仓库根目录或 csv 目录；默认自动发现")
    args = parser.parse_args()
    if args.config_dir:
        os.environ["TILE_SIM_CONFIG"] = args.config_dir
    with ThreadingHTTPServer((VALID_ORIGIN_HOST, args.port), Handler) as server:
        address = f"http://{VALID_ORIGIN_HOST}:{server.server_port}/"
        print(f"通关数据模拟 · {address}\n请保持此终端打开。", flush=True)
        if args.open:
            threading.Timer(0.2, webbrowser.open, args=(address,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n正在关闭；当前模拟将结束。", flush=True)


if __name__ == "__main__":
    main()
