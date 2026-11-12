"""Atomic, persistent draft and job state storage."""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .source_service import json_safe


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_identifier(prefix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{timestamp}-{uuid.uuid4().hex[:10]}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(json_safe(value), ensure_ascii=False, indent=2, allow_nan=False)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class DraftRepository:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def allocate(self, kind: str) -> tuple[str, Path]:
        with self._lock:
            draft_id = new_identifier(f"draft-{kind}")
            directory = self.root / draft_id
            directory.mkdir(parents=False, exist_ok=False)
            return draft_id, directory

    def save(self, draft_id: str, record: Mapping[str, Any]) -> None:
        with self._lock:
            atomic_write_json(self.directory(draft_id) / "draft.json", record)

    def load(self, draft_id: str) -> dict[str, Any]:
        path = self.directory(draft_id) / "draft.json"
        if not path.is_file():
            raise FileNotFoundError("预检草稿不存在或已失效")
        with self._lock, path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("预检草稿格式无效")
        return value

    def directory(self, draft_id: str) -> Path:
        if not _safe_id(draft_id, "draft-"):
            raise ValueError("草稿 ID 无效")
        path = (self.root / draft_id).resolve(strict=False)
        if path.parent != self.root.resolve():
            raise ValueError("草稿 ID 无效")
        return path


def _safe_id(value: object, prefix: str) -> bool:
    text = str(value or "")
    return text.startswith(prefix) and len(text) <= 80 and all(
        char.isascii() and (char.isalnum() or char in "-_") for char in text
    )


class JobRepository:
    MAX_LOGS = 240

    def __init__(self, root: Path, output_root: Path):
        self.root = root
        self.output_root = output_root
        self.root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def directory(self, job_id: str) -> Path:
        if not _safe_id(job_id, "job-"):
            raise ValueError("任务 ID 无效")
        path = (self.root / job_id).resolve(strict=False)
        if path.parent != self.root.resolve():
            raise ValueError("任务 ID 无效")
        return path

    def output_directory(self, job_id: str) -> Path:
        self.directory(job_id)  # validates the identifier
        path = (self.output_root / job_id).resolve(strict=False)
        if path.parent != self.output_root.resolve():
            raise ValueError("任务 ID 无效")
        return path

    def create_from_draft(self, draft: Mapping[str, Any], draft_dir: Path) -> dict[str, Any]:
        with self._lock:
            job_id = new_identifier("job")
            directory = self.directory(job_id)
            directory.mkdir(parents=False, exist_ok=False)
            input_dir = directory / "input" / "player"
            shutil.copytree(draft_dir / "input", input_dir)
            output_dir = self.output_directory(job_id)
            output_dir.mkdir(parents=True, exist_ok=False)
            timestamp = now_iso()
            source_files = [str(input_dir / Path(path).name) for path in draft["source_files"]]
            state = {
                "job_id": job_id,
                "created_at": timestamp,
                "updated_at": timestamp,
                "status": "queued",
                "stage": "player",
                "progress": {
                    "phase": "queued",
                    "current": 0,
                    "total": None,
                    "percent": None,
                    "message": "已创建任务，等待分析。",
                },
                "logs": [
                    {"at": timestamp, "level": "info", "message": "已从通过预检的数据创建任务。"}
                ],
                "source": {
                    "player": {
                        "files": [Path(path).name for path in source_files],
                        "audit": draft.get("audit", {}),
                        "settings": draft.get("settings", {}),
                        "draft_id": draft.get("draft_id"),
                    }
                },
                "source_files": {"player": source_files},
                "settings": {"player": draft.get("settings", {})},
                "summary": {},
                "previews": {},
                "artifacts": [],
                "engine_results": {},
                "error": None,
            }
            self._save_unlocked(job_id, state)
            return state

    def load(self, job_id: str) -> dict[str, Any]:
        path = self.directory(job_id) / "state.json"
        if not path.is_file():
            raise FileNotFoundError("任务不存在")
        with self._lock, path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            raise ValueError("任务状态文件无效")
        return state

    def save(self, job_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            value = dict(state)
            value["updated_at"] = now_iso()
            self._save_unlocked(job_id, value)
            return value

    def update(
        self, job_id: str, mutator: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        with self._lock:
            state = self.load(job_id)
            mutator(state)
            state["updated_at"] = now_iso()
            self._save_unlocked(job_id, state)
            return state

    def append_log(self, job_id: str, message: str, *, level: str = "info") -> dict[str, Any]:
        clean = " ".join(str(message).split())[:1200]

        def mutate(state: dict[str, Any]) -> None:
            logs = state.setdefault("logs", [])
            logs.append({"at": now_iso(), "level": level, "message": clean})
            if len(logs) > self.MAX_LOGS:
                del logs[:-self.MAX_LOGS]

        return self.update(job_id, mutate)

    def discover(self, *, limit: int = 12, recover_interrupted: bool = True) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        with self._lock:
            for directory in self.root.iterdir():
                if not directory.is_dir() or not _safe_id(directory.name, "job-"):
                    continue
                try:
                    state = self.load(directory.name)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if recover_interrupted and state.get("status") in {"queued", "running"}:
                    state["status"] = "interrupted"
                    state["error"] = {
                        "code": "server_restarted",
                        "message": "本地服务在任务运行期间退出；请使用原数据重新运行。",
                    }
                    state.setdefault("logs", []).append(
                        {"at": now_iso(), "level": "warning", "message": "检测到上次未完成的任务，已标记为中断。"}
                    )
                    state["updated_at"] = now_iso()
                    self._save_unlocked(directory.name, state)
                jobs.append(state)
        jobs.sort(key=lambda value: str(value.get("updated_at", "")), reverse=True)
        return jobs[:limit]

    def attach_online_draft(
        self, job_id: str, draft: Mapping[str, Any], draft_dir: Path
    ) -> dict[str, Any]:
        with self._lock:
            state = self.load(job_id)
            online_input = (
                self.directory(job_id)
                / "input"
                / "online"
                / str(draft.get("draft_id") or new_identifier("draft-online"))
            )
            shutil.copytree(draft_dir / "input", online_input)
            files = [str(online_input / Path(path).name) for path in draft["source_files"]]
            state.setdefault("source", {})["online"] = {
                "files": [Path(path).name for path in files],
                "audit": draft.get("audit", {}),
                "settings": draft.get("settings", {}),
                "draft_id": draft.get("draft_id"),
            }
            state.setdefault("source_files", {})["online"] = files
            state.setdefault("settings", {})["online"] = draft.get("settings", {})
            state["updated_at"] = now_iso()
            self._save_unlocked(job_id, state)
            return state

    def _save_unlocked(self, job_id: str, state: Mapping[str, Any]) -> None:
        atomic_write_json(self.directory(job_id) / "state.json", state)
