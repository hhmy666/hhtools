"""Recoverable synchronization to the two confirmed local template bindings.

The public boundary accepts only the logical binding names ``tile`` and
``template``.  Paths are resolved from persisted local settings.  A template
is the transaction boundary: all selected repositories for that template are
prepared before the first replace, and a later failure compensates already
applied repositories in reverse order.  Sibling templates remain independent.

This module intentionally contains no Git integration.
"""

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time

from .export_service import target_evidence
from .path_binding import TEMPLATE_SUFFIX, TILE_SUFFIX, validate_binding
from .settings import DEFAULT_SETTINGS_PATH, load_settings
from .storage import (
    JOB_PATTERN,
    PreparedFile,
    absolute_path,
    assert_directory_identity,
    atomic_write_json,
    digest,
    directory_fd,
    directory_identity,
    read_regular,
    read_regular_at,
    safe_directory,
)
from .template_reader import decode_template


PLAN_PATTERN = re.compile(r"plan-[0-9a-f]{32}\Z")
TRANSACTION_PATTERN = re.compile(r"txn-[0-9a-f]{32}\Z")
CONFLICT_PATTERN = re.compile(r"conflict-[0-9a-f]{32}\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
TEMP_PATTERN = re.compile(r"\.tmp-[0-9a-f]{32}\Z")
QUARANTINE_PATTERN = re.compile(r"\.sync-quarantine-[0-9a-f]{32}\Z")
TEMPLATE_KEY_PATTERN = re.compile(r"tpl-[0-9a-f]{32}\Z")
ARTIFACT_PATTERN = re.compile(r"art-[0-9a-f]{32}\Z")
_BINDINGS = {
    "tile": ("tile_root", "tile_confirmed", TILE_SUFFIX),
    "template": ("template_root", "template_confirmed", TEMPLATE_SUFFIX),
}
_SUCCESS = frozenset(("synced", "skipped_same_sha"))
_PLAN_STATES = frozenset(("ready", "committing", "committed", "rolled_back", "recovery_required", "invalidated"))
_JOURNAL_STATES = frozenset(
    ("preparing", "prepared", "applying", "rollback_pending", "rolling_back", "committed", "rolled_back", "recovery_required")
)
_TERMINAL_JOURNALS = frozenset(("committed", "rolled_back", "recovery_required"))
_RECOVERABLE_JOURNALS = frozenset(("preparing", "prepared", "applying", "rollback_pending", "rolling_back"))
_PLAN_DIGEST_FIELDS = (
    "version", "plan_id", "job_id", "created_at_ns", "selected_bindings", "bindings", "rows"
)


def _load_atomic_rename():
    """Return one same-directory, dir-fd based atomic rename primitive.

    Python 3.9 exposes only replacing rename semantics.  Synchronization needs
    two stronger operations: create-if-absent and exchange-without-destroying
    either object.  Darwin provides both through ``renameatx_np`` and Linux
    through ``renameat2``.  Unsupported systems fail closed before a commit.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        return function, 0x00000004, 0x00000002
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        function.restype = ctypes.c_int
        return function, 1, 2
    return None, None, None


_ATOMIC_RENAME, _RENAME_NOREPLACE, _RENAME_EXCHANGE = _load_atomic_rename()


def _atomic_rename(fd, source_name, target_name, flag):
    if _ATOMIC_RENAME is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace/exchange rename is unavailable")
    if (
        type(fd) is not int
        or type(source_name) is not str
        or type(target_name) is not str
        or Path(source_name).name != source_name
        or Path(target_name).name != target_name
        or source_name == target_name
    ):
        raise ValueError("atomic rename requires distinct basenames and one held directory fd")
    ctypes.set_errno(0)
    result = _ATOMIC_RENAME(
        fd,
        os.fsencode(source_name),
        fd,
        os.fsencode(target_name),
        flag,
    )
    if result != 0:
        number = ctypes.get_errno() or errno.EIO
        raise OSError(number, os.strerror(number), target_name)


def _atomic_noreplace(fd, source_name, target_name):
    _atomic_rename(fd, source_name, target_name, _RENAME_NOREPLACE)


def _atomic_exchange(fd, first_name, second_name):
    _atomic_rename(fd, first_name, second_name, _RENAME_EXCHANGE)


def _error(code, message, stage, evidence=None, recommendations=None):
    defaults = {
        "planning": ["检查已保存并确认的模板库绑定后重新预览"],
        "confirmation": ["在当前预览中逐项选择“备份后替换”"],
        "validation": ["目标或设置已改变，请重新生成同步预览"],
        "preparing": ["检查目录权限或磁盘状态后单独重试该模板"],
        "applying": ["本模板已回滚；检查故障证据后重新预览"],
        "recovery": ["暂停继续同步，根据 journal 和备份人工核对该模板"],
    }
    return {
        "code": str(code),
        "stage": str(stage),
        "message": str(message),
        "evidence": deepcopy(evidence or {}),
        "recommendations": list(recommendations or defaults.get(stage, ["核对审计证据后重试"])),
    }


def _canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return digest(raw)


def _preview_digest(value):
    """Digest the immutable preview capability."""
    return _canonical_digest({name: value.get(name) for name in _PLAN_DIGEST_FIELDS})


def _plan_digest(value):
    """Digest the complete persisted lifecycle record."""
    body = dict(value)
    body.pop("plan_digest", None)
    return _canonical_digest(body)


def _commit_digest(value):
    """Digest the immutable preview plus the one consumed confirmation set.

    The plan's full digest necessarily changes as the lifecycle advances.  A
    transaction journal therefore binds to this stable committing capability,
    while ``plan_digest`` protects the current persisted lifecycle record.
    """
    return _canonical_digest(
        {
            "version": value.get("version"),
            "plan_id": value.get("plan_id"),
            "job_id": value.get("job_id"),
            "preview_digest": value.get("preview_digest"),
            "consumed_conflict_ids": value.get("consumed_conflict_ids"),
            "committing_status": "committing",
        }
    )


def _identity(value, length):
    return (
        type(value) is list
        and len(value) == length
        and all(type(item) is int and item >= 0 for item in value)
    )


def _evidence_shape(value):
    if type(value) is not dict or type(value.get("exists")) is not bool:
        return False
    if value["exists"]:
        return SHA_PATTERN.fullmatch(value.get("sha256", "")) is not None and _identity(value.get("identity"), 5)
    return value.get("sha256") is None and value.get("identity") is None


def _strict_bindings(selected):
    if type(selected) not in (tuple, list) or not 1 <= len(selected) <= 2:
        raise ValueError("selected_bindings must contain one or two logical binding keys")
    if any(type(key) is not str or key not in _BINDINGS for key in selected):
        raise ValueError("only tile/template logical bindings are accepted")
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate binding key")
    return tuple(selected)


def _safe_filename(value):
    if type(value) is not str or Path(value).name != value or not value.endswith("_m.bytes"):
        raise ValueError("artifact filename must be a single _m.bytes basename")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("artifact filename must be ASCII") from exc
    return value


@dataclass(frozen=True)
class SyncPlan:
    plan_id: str
    job_id: str
    selected_bindings: tuple
    rows: tuple
    plan_digest: str

    def to_dict(self):
        return {
            "plan_id": self.plan_id,
            "job_id": self.job_id,
            "selected_bindings": list(self.selected_bindings),
            "rows": deepcopy(list(self.rows)),
            "plan_digest": self.plan_digest,
        }


@dataclass(frozen=True)
class SyncReport:
    plan_id: str
    job_id: str
    status: str
    rows: tuple

    @property
    def ok(self):
        return self.status == "committed" and bool(self.rows) and all(row.get("status") in _SUCCESS for row in self.rows)

    def to_dict(self):
        return {
            "plan_id": self.plan_id,
            "job_id": self.job_id,
            "status": self.status,
            "rows": deepcopy(list(self.rows)),
            "ok": self.ok,
        }


class SyncService:
    """Plan, commit and recover synchronization for one JobRepository."""

    def __init__(self, repository, settings_path=DEFAULT_SETTINGS_PATH):
        self.repository = repository
        self.settings_path = absolute_path(settings_path)
        self._plans = {}
        self.diagnostics = []

    def _diagnose(self, code, message, evidence=None):
        if len(self.diagnostics) < 100:
            self.diagnostics.append(
                {
                    "code": str(code),
                    "message": str(message),
                    "evidence": deepcopy(evidence or {}),
                }
            )
        elif len(self.diagnostics) == 100:
            self.diagnostics.append(
                {"code": "sync_diagnostics_truncated", "message": "additional synchronization diagnostics omitted", "evidence": {}}
            )

    def _settings_bindings(self, selected, writable):
        settings = load_settings(self.settings_path)
        if settings.load_error:
            raise ValueError(settings.load_error)
        snapshots = {}
        opened = {}
        stack = ExitStack()
        try:
            for key in selected:
                root_field, confirmed_field, suffix = _BINDINGS[key]
                path = getattr(settings, root_field)
                if getattr(settings, confirmed_field) is not True:
                    raise ValueError("{} binding is not confirmed".format(key))
                result = validate_binding(path, suffix)
                if not result.valid:
                    raise ValueError("{} binding is invalid: {}".format(key, result.reason))
                fd = stack.enter_context(directory_fd(result.path, writable=writable))
                identity = list(directory_identity(fd))
                snapshots[key] = {
                    "binding_key": key,
                    "path": str(result.path),
                    "confirmed": True,
                    "identity": identity,
                }
                opened[key] = fd
            identities = [tuple(row["identity"]) for row in snapshots.values()]
            if len(set(identities)) != len(identities):
                raise ValueError("selected bindings resolve to the same directory")
            return settings, snapshots, opened, stack
        except BaseException:
            stack.close()
            raise

    def _plan_directory(self, job, plan_id, create=False):
        if type(plan_id) is not str or not PLAN_PATTERN.fullmatch(plan_id):
            raise ValueError("invalid sync plan id")
        return safe_directory(job.directory / "sync" / plan_id, create=create)

    def _journal_directory(self, job, plan_id, transaction_id, create=False):
        if type(transaction_id) is not str or not TRANSACTION_PATTERN.fullmatch(transaction_id):
            raise ValueError("invalid sync transaction id")
        return safe_directory(
            self._plan_directory(job, plan_id, create=create) / "transactions" / transaction_id,
            create=create,
        )

    def _persist_plan(self, job, data):
        if data.get("version") != 2 or data.get("status") not in _PLAN_STATES:
            raise ValueError("invalid sync plan lifecycle")
        data["updated_at_ns"] = time.time_ns()
        data["plan_digest"] = _plan_digest(data)
        directory = self._plan_directory(job, data["plan_id"], create=True)
        atomic_write_json(directory / "plan.json", data, self.repository.file_ops)
        loaded = json.loads(read_regular(directory / "plan.json"))
        if loaded != data:
            raise ValueError("sync plan persistence readback mismatch")
        self._plans[data["plan_id"]] = job.job_id

    def _persist_report(self, job, plan_id, report):
        directory = self._plan_directory(job, plan_id, create=True)
        atomic_write_json(directory / "report.json", report.to_dict(), self.repository.file_ops)

    def _persist_journal(self, job, journal):
        directory = self._journal_directory(
            job, journal["plan_id"], journal["transaction_id"], create=True
        )
        journal["updated_at_ns"] = time.time_ns()
        atomic_write_json(directory / "journal.json", journal, self.repository.file_ops)

    def _validate_plan(self, raw, job, plan_id):
        if type(raw) is not dict or raw.get("version") != 2:
            raise ValueError("invalid sync plan version")
        if raw.get("plan_id") != plan_id or raw.get("job_id") != job.job_id:
            raise ValueError("sync plan identity mismatch")
        if raw.get("status") not in _PLAN_STATES:
            raise ValueError("invalid sync plan state")
        consumed = raw.get("consumed_conflict_ids")
        if type(consumed) is not list or any(not CONFLICT_PATTERN.fullmatch(item or "") for item in consumed):
            raise ValueError("invalid consumed conflict ids")
        if raw.get("preview_digest") != _preview_digest(raw):
            raise ValueError("sync preview digest mismatch")
        commit_plan_digest = raw.get("commit_plan_digest")
        if commit_plan_digest is not None and not SHA_PATTERN.fullmatch(commit_plan_digest or ""):
            raise ValueError("invalid committing plan digest")
        if raw["status"] == "ready":
            if commit_plan_digest is not None or consumed:
                raise ValueError("ready plan already consumed")
        elif raw["status"] in ("committing", "committed", "rolled_back", "recovery_required"):
            if commit_plan_digest is None or commit_plan_digest != _commit_digest(raw):
                raise ValueError("plan is not bound to its committing capability")
        elif commit_plan_digest is not None and commit_plan_digest != _commit_digest(raw):
            raise ValueError("invalidated plan has an invalid committing capability")
        if type(raw.get("created_at_ns")) is not int or type(raw.get("updated_at_ns")) is not int:
            raise ValueError("invalid sync plan timestamps")
        selected = _strict_bindings(raw.get("selected_bindings"))
        bindings = raw.get("bindings")
        rows = raw.get("rows")
        if type(bindings) is not dict or set(bindings) != set(selected) or type(rows) is not list or not rows:
            raise ValueError("invalid sync plan shape")
        for key in selected:
            binding = bindings[key]
            if (
                type(binding) is not dict
                or binding.get("binding_key") != key
                or binding.get("confirmed") is not True
                or type(binding.get("path")) is not str
                or not _identity(binding.get("identity"), 2)
            ):
                raise ValueError("invalid sync plan binding")
            absolute_path(binding["path"])
        transactions = set()
        filenames = set()
        for row in rows:
            if type(row) is not dict:
                raise ValueError("invalid sync plan row")
            if not TEMPLATE_KEY_PATTERN.fullmatch(row.get("template_key", "")):
                raise ValueError("invalid sync template key")
            if not TRANSACTION_PATTERN.fullmatch(row.get("transaction_id", "")) or row["transaction_id"] in transactions:
                raise ValueError("invalid or duplicate transaction id")
            transactions.add(row["transaction_id"])
            if row.get("status") not in ("planned", "blocked"):
                raise ValueError("invalid planned row state")
            if row["status"] == "blocked":
                if type(row.get("targets")) is not list or type(row.get("error")) not in (dict, type(None)):
                    raise ValueError("invalid blocked plan row")
                blocked_targets = row["targets"]
                if not blocked_targets:
                    # Artifact-level planning failures have no external
                    # capability at all.  Every artifact field must therefore
                    # remain empty instead of carrying partially trusted data.
                    if any(
                        row.get(name) is not None
                        for name in ("artifact_id", "new_id", "filename", "new_sha256", "size")
                    ):
                        raise ValueError("blocked artifact row has partial capability fields")
                    continue
                if len(blocked_targets) != len(selected):
                    raise ValueError("invalid blocked target count")
                if (
                    type(row.get("attempt_id")) is not int
                    or row["attempt_id"] < 0
                    or not ARTIFACT_PATTERN.fullmatch(row.get("artifact_id", ""))
                    or type(row.get("new_id")) is not str
                    or not SHA_PATTERN.fullmatch(row.get("new_sha256", ""))
                    or type(row.get("size")) is not int
                    or row["size"] < 0
                ):
                    raise ValueError("invalid blocked artifact fields")
                blocked_filename = _safe_filename(row.get("filename"))
                if (
                    blocked_filename != row["new_id"] + "_m.bytes"
                    or blocked_filename.casefold() in filenames
                ):
                    raise ValueError("invalid blocked output filename")
                filenames.add(blocked_filename.casefold())
                blocked_seen = set()
                for target in blocked_targets:
                    if type(target) is not dict:
                        raise ValueError("invalid blocked sync target")
                    key = target.get("binding_key")
                    classification = target.get("classification")
                    if key not in selected or key in blocked_seen:
                        raise ValueError("invalid blocked sync target identity")
                    blocked_seen.add(key)
                    binding = bindings[key]
                    if (
                        target.get("binding_path") != binding["path"]
                        or target.get("binding_identity") != binding["identity"]
                        or target.get("filename") != blocked_filename
                        or classification not in ("blocked", "create", "same_sha", "conflict")
                        or type(target.get("error")) not in (dict, type(None))
                    ):
                        raise ValueError("blocked sync target does not match binding")
                    evidence = target.get("target_evidence")
                    conflict_id = target.get("conflict_id")
                    if classification == "blocked":
                        if evidence is not None or conflict_id is not None or type(target.get("error")) is not dict:
                            raise ValueError("invalid blocked target evidence")
                        continue
                    if not _evidence_shape(evidence):
                        raise ValueError("invalid blocked-row target evidence")
                    if classification == "create" and evidence["exists"]:
                        raise ValueError("blocked-row create target unexpectedly exists")
                    if classification in ("same_sha", "conflict") and not evidence["exists"]:
                        raise ValueError("blocked-row existing target lacks evidence")
                    if classification == "same_sha" and evidence["sha256"] != row["new_sha256"]:
                        raise ValueError("blocked-row same-SHA target mismatch")
                    if classification == "conflict":
                        if not CONFLICT_PATTERN.fullmatch(conflict_id or "") or evidence["sha256"] == row["new_sha256"]:
                            raise ValueError("invalid blocked-row conflict target")
                    elif conflict_id is not None:
                        raise ValueError("unexpected blocked-row conflict id")
                continue
            if (
                type(row.get("attempt_id")) is not int
                or row["attempt_id"] < 0
                or not ARTIFACT_PATTERN.fullmatch(row.get("artifact_id", ""))
                or type(row.get("new_id")) is not str
                or not SHA_PATTERN.fullmatch(row.get("new_sha256", ""))
                or type(row.get("size")) is not int
                or row["size"] < 0
            ):
                raise ValueError("invalid sync plan artifact fields")
            filename = _safe_filename(row.get("filename"))
            if filename.casefold() in filenames or filename != row["new_id"] + "_m.bytes":
                raise ValueError("invalid or duplicate sync filename")
            filenames.add(filename.casefold())
            targets = row.get("targets")
            if type(targets) is not list or len(targets) != len(selected):
                raise ValueError("invalid sync target count")
            seen = set()
            for target in targets:
                if type(target) is not dict:
                    raise ValueError("invalid sync target")
                key = target.get("binding_key")
                classification = target.get("classification")
                if key not in selected or key in seen or classification not in ("create", "same_sha", "conflict"):
                    raise ValueError("invalid sync target identity")
                seen.add(key)
                binding = bindings[key]
                if (
                    target.get("binding_path") != binding["path"]
                    or target.get("binding_identity") != binding["identity"]
                    or target.get("filename") != filename
                    or not _evidence_shape(target.get("target_evidence"))
                ):
                    raise ValueError("sync target does not match binding")
                evidence = target["target_evidence"]
                conflict_id = target.get("conflict_id")
                if classification == "create" and evidence["exists"]:
                    raise ValueError("create target unexpectedly exists in preview")
                if classification in ("same_sha", "conflict") and not evidence["exists"]:
                    raise ValueError("existing target missing preview evidence")
                if classification == "same_sha" and evidence["sha256"] != row["new_sha256"]:
                    raise ValueError("same-SHA target mismatch")
                if classification == "conflict":
                    if not CONFLICT_PATTERN.fullmatch(conflict_id or "") or evidence["sha256"] == row["new_sha256"]:
                        raise ValueError("invalid conflict target")
                elif conflict_id is not None:
                    raise ValueError("unexpected conflict id")
        expected_consumed = sorted(
            target["conflict_id"]
            for row in rows
            if row["status"] == "planned"
            for target in row["targets"]
            if target["classification"] == "conflict"
        )
        if commit_plan_digest is None:
            if consumed:
                raise ValueError("uncommitted plan consumed conflict confirmations")
        elif consumed != expected_consumed:
            raise ValueError("committing plan confirmation set mismatch")
        supplied = raw.get("plan_digest")
        if type(supplied) is not str or supplied != _plan_digest(raw):
            raise ValueError("sync plan digest mismatch")
        return selected

    def _load_plan_at(self, job, plan_id):
        raw = json.loads(read_regular(self._plan_directory(job, plan_id) / "plan.json"))
        selected = self._validate_plan(raw, job, plan_id)
        supplied = raw["plan_digest"]
        self._plans[plan_id] = job.job_id
        return raw, SyncPlan(plan_id, job.job_id, selected, tuple(deepcopy(raw["rows"])), supplied)

    def _find_plan(self, plan_id):
        if type(plan_id) is not str or not PLAN_PATTERN.fullmatch(plan_id):
            raise ValueError("invalid sync plan id")
        job_id = self._plans.get(plan_id)
        if job_id is not None:
            job = self.repository.get(job_id)
            return job, self._load_plan_at(job, plan_id)
        self.repository._check_root()
        candidates = 0
        with directory_fd(self.repository.root) as root_fd, os.scandir(root_fd) as entries:
            for entry in entries:
                try:
                    name = entry.name
                    if not JOB_PATTERN.fullmatch(name) or not entry.is_dir(follow_symlinks=False):
                        continue
                    if candidates >= 1000:
                        self._diagnose("sync_plan_lookup_truncated", "plan lookup job limit reached", {"plan_id": plan_id})
                        break
                    candidates += 1
                    job = self.repository.get(name)
                    try:
                        loaded = self._load_plan_at(job, plan_id)
                    except FileNotFoundError:
                        continue
                    return job, loaded
                except (OSError, ValueError, KeyError):
                    continue
        raise KeyError("sync_plan_not_found")

    def create_sync_plan(self, job, selected_bindings):
        selected = _strict_bindings(selected_bindings)
        with job.lock:
            _, bindings, binding_fds, stack = self._settings_bindings(selected, writable=True)
            try:
                state = job.snapshot()
                rows = []
                used_names = set()
                for template_key, template in state["templates"].items():
                    if template.get('status') == 'no_op' and not template.get('output_artifacts'):
                        continue  # No generated identity/file to synchronize.
                    row = {
                        "template_key": template_key,
                        "attempt_id": template.get("attempt_id"),
                        "artifact_id": None,
                        "new_id": None,
                        "filename": None,
                        "new_sha256": None,
                        "size": None,
                        "transaction_id": "txn-" + secrets.token_hex(16),
                        "targets": [],
                        "status": "planned",
                        "error": None,
                    }
                    try:
                        artifact_ids = template.get("output_artifacts", [])
                        if len(artifact_ids) != 1:
                            raise ValueError("template has no single current verified artifact")
                        artifact = state["artifacts"][artifact_ids[0]]
                        raw = job.read_artifact(artifact_ids[0])
                        filename = _safe_filename(artifact["filename"])
                        if filename.casefold() in used_names:
                            raise ValueError("duplicate output filename")
                        used_names.add(filename.casefold())
                        if decode_template(raw).template_id != artifact["new_id"]:
                            raise ValueError("artifact internal TemplateId mismatch")
                        row.update(
                            artifact_id=artifact["id"],
                            new_id=artifact["new_id"],
                            filename=filename,
                            new_sha256=digest(raw),
                            size=len(raw),
                        )
                        for key in selected:
                            destination = Path(bindings[key]["path"]) / filename
                            target = {
                                "binding_key": key,
                                "binding_path": bindings[key]["path"],
                                "binding_identity": bindings[key]["identity"],
                                "filename": filename,
                                "classification": "blocked",
                                "target_evidence": None,
                                "conflict_id": None,
                                "error": None,
                            }
                            try:
                                evidence = target_evidence(destination, binding_fds[key])
                                classification = (
                                    "create"
                                    if not evidence["exists"]
                                    else "same_sha"
                                    if evidence["sha256"] == row["new_sha256"]
                                    else "conflict"
                                )
                                target.update(classification=classification, target_evidence=evidence)
                                if classification == "conflict":
                                    target["conflict_id"] = "conflict-" + secrets.token_hex(16)
                            except (OSError, ValueError, TypeError) as exc:
                                target["error"] = _error(
                                    "invalid_target", exc, "planning", {"binding_key": key, "filename": filename}
                                )
                            row["targets"].append(target)
                        identities = [
                            tuple(target["target_evidence"]["identity"][:2])
                            for target in row["targets"]
                            if target["target_evidence"] and target["target_evidence"]["exists"]
                        ]
                        if len(identities) != len(set(identities)):
                            raise ValueError("selected targets are the same physical file")
                        if any(target["classification"] == "blocked" for target in row["targets"]):
                            row["status"] = "blocked"
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        row["status"] = "blocked"
                        row["error"] = _error(
                            "artifact_or_target_invalid", exc, "planning", {"template_key": template_key}
                        )
                    rows.append(row)
                plan_id = "plan-" + secrets.token_hex(16)
                body = {
                    "version": 2,
                    "plan_id": plan_id,
                    "job_id": job.job_id,
                    "created_at_ns": time.time_ns(),
                    "updated_at_ns": time.time_ns(),
                    "status": "ready",
                    "consumed_conflict_ids": [],
                    "commit_plan_digest": None,
                    "selected_bindings": list(selected),
                    "bindings": bindings,
                    "rows": rows,
                }
                body["preview_digest"] = _preview_digest(body)
                body["plan_digest"] = _plan_digest(body)
                self._persist_plan(job, body)
                return SyncPlan(plan_id, job.job_id, selected, tuple(deepcopy(rows)), body["plan_digest"])
            finally:
                stack.close()

    def _settings_match_plan(self, plan_data):
        selected = tuple(plan_data["selected_bindings"])
        _, current, fds, stack = self._settings_bindings(selected, writable=True)
        try:
            if current != plan_data["bindings"]:
                raise ValueError("saved binding settings or directory identity changed")
            return fds, stack
        except BaseException:
            stack.close()
            raise

    def _result_for_invalidated(self, job, plan_data, code, message):
        plan_data["status"] = "invalidated"
        self._persist_plan(job, plan_data)
        rows = []
        for row in plan_data["rows"]:
            rows.append(
                {
                    "template_key": row["template_key"],
                    "artifact_id": row.get("artifact_id"),
                    "status": "blocked",
                    "targets": deepcopy(row.get("targets", [])),
                    "error": _error(code, message, "validation", {"plan_id": plan_data["plan_id"]}),
                }
            )
        report = SyncReport(plan_data["plan_id"], job.job_id, "invalidated", tuple(rows))
        self._persist_report(job, plan_data["plan_id"], report)
        return report

    def _journal_path(self, job, plan_id, transaction_id):
        return self._journal_directory(job, plan_id, transaction_id) / "journal.json"

    def _load_existing_journal(self, job, row, plan_id):
        try:
            journal = json.loads(read_regular(self._journal_path(job, plan_id, row["transaction_id"])))
        except FileNotFoundError:
            return None
        plan_data, _ = self._load_plan_at(job, plan_id)
        self._validate_recovery_journal(journal, plan_data, row)
        return journal

    def _journal_for_row(self, plan_data, row):
        targets = []
        for target in row["targets"]:
            targets.append(
                dict(
                    deepcopy(target),
                    backup=None,
                    temp_name=None,
                    temp_state="none",
                    temp_identity=None,
                    temp_sha256=None,
                    applied=False,
                    applied_identity=None,
                    rolled_back=False,
                    rollback_error=None,
                    temp_quarantine_name=None,
                    temp_quarantine_state="none",
                    restore_temp_name=None,
                    restore_temp_state="none",
                    restore_temp_identity=None,
                    restore_temp_sha256=None,
                    restore_quarantine_name=None,
                    restore_quarantine_state="none",
                    rollback_quarantine_name=None,
                    rollback_quarantine_state="none",
                )
            )
        return {
            "version": 2,
            "plan_id": plan_data["plan_id"],
            "plan_digest": plan_data["commit_plan_digest"],
            "preview_digest": plan_data["preview_digest"],
            "job_id": plan_data["job_id"],
            "transaction_id": row["transaction_id"],
            "template_key": row["template_key"],
            "attempt_id": row["attempt_id"],
            "artifact_id": row["artifact_id"],
            "new_id": row["new_id"],
            "filename": row["filename"],
            "new_sha256": row["new_sha256"],
            "size": row["size"],
            "state": "preparing",
            "targets": targets,
            "error": None,
            "created_at_ns": time.time_ns(),
        }

    @staticmethod
    def _same_evidence(current, planned):
        return current == planned

    @staticmethod
    def _temp_identity(temp):
        info = os.stat(temp.name, dir_fd=temp.parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("prepared item is not a regular file")
        return [info.st_dev, info.st_ino, info.st_size]

    @staticmethod
    def _close_prepared(temp):
        """Release only the held directory fd; never delete by a mutable name."""
        if temp is not None and temp.parent_fd >= 0:
            os.close(temp.parent_fd)
            temp.parent_fd = -1

    @staticmethod
    def _entry_evidence(fd, name):
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("synchronization entry is not a regular file")
        raw = read_regular_at(fd, name)
        after = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_size) != (after.st_dev, after.st_ino, after.st_size):
            raise ValueError("synchronization entry changed while reading")
        return {
            "sha256": digest(raw),
            "identity": [info.st_dev, info.st_ino, info.st_size],
        }

    @staticmethod
    def _entry_matches(evidence, expected_sha, expected_identity=None):
        if evidence is None or evidence.get("sha256") != expected_sha:
            return False
        if expected_identity is None:
            return True
        return evidence.get("identity", [])[:2] == list(expected_identity)[:2]

    def _prepare_named_file(self, destination, raw, parent_fd, name):
        """Task7 PreparedFile semantics with a journaled server basename."""
        if not TEMP_PATTERN.fullmatch(name):
            raise ValueError("invalid pre-registered temporary basename")
        parent = os.dup(parent_fd)
        temp = PreparedFile(Path(destination).parent / name, parent)
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    self.repository.file_ops.checkpoint("write", temp)
                    stream.write(raw)
                    stream.flush()
                    self.repository.file_ops.checkpoint("fsync_file", temp)
                    os.fsync(stream.fileno())
            except BaseException:
                # The durable journal already owns this unpredictable basename.
                # Leave it for evidence-checked quarantine cleanup; deleting by
                # name here would reopen a check/delete replacement race.
                raise
            try:
                self.repository.file_ops.checkpoint("sync_temp_created", temp)
            except BaseException:
                os.close(parent)
                temp.parent_fd = -1
                raise
            return temp
        except BaseException:
            if temp.parent_fd >= 0:
                os.close(parent)
                temp.parent_fd = -1
            raise

    @staticmethod
    def _row_by_transaction(plan_data, transaction_id):
        matches = [row for row in plan_data["rows"] if row.get("transaction_id") == transaction_id]
        if len(matches) != 1:
            raise ValueError("journal transaction is not uniquely bound to plan")
        return matches[0]

    def _read_bound_artifact(self, job, row):
        state = job.snapshot()
        template = state["templates"].get(row["template_key"])
        if type(template) is not dict:
            raise ValueError("bound template no longer exists")
        if (
            template.get("attempt_id") == row["attempt_id"]
            and template.get("output_artifacts") == [row["artifact_id"]]
        ):
            raw = job.read_artifact(row["artifact_id"])
        else:
            raw = job.read_artifact(row["artifact_id"], attempt_id=row["attempt_id"])
        if len(raw) != row["size"] or digest(raw) != row["new_sha256"]:
            raise ValueError("bound artifact bytes changed")
        decoded = decode_template(raw)
        if decoded.template_id != row["new_id"] or row["filename"] != row["new_id"] + "_m.bytes":
            raise ValueError("bound artifact identity changed")
        return raw

    @staticmethod
    def _matches_applied(evidence, target, new_sha):
        identity = evidence.get("identity") if evidence else None
        prepared = target.get("temp_identity")
        return bool(
            evidence
            and evidence.get("exists")
            and evidence.get("sha256") == new_sha
            and identity
            and prepared
            and identity[:2] == prepared[:2]
        )

    def _validate_current_row(self, job, row, binding_fds):
        state = job.snapshot()
        template = state["templates"].get(row["template_key"])
        if (
            type(template) is not dict
            or template.get("attempt_id") != row["attempt_id"]
            or template.get("output_artifacts") != [row["artifact_id"]]
        ):
            raise ValueError("job attempt or artifact selection changed")
        raw = self._read_bound_artifact(job, row)
        for target in row["targets"]:
            if target["classification"] == "blocked":
                continue
            key = target["binding_key"]
            try:
                current = target_evidence(Path(target["binding_path"]) / row["filename"], binding_fds[key])
            except (OSError, ValueError, TypeError) as exc:
                raise TargetChanged("target type or bytes changed after preview") from exc
            if not self._same_evidence(current, target["target_evidence"]):
                raise TargetChanged("target changed after preview")
        return raw

    def _prepared_barrier(self, job, plan_data, row, raw, journal, prepared, binding_fds):
        # Re-open saved settings and compare actual directory identities after
        # every backup/temp is durable, but before the first formal replace.
        current_fds, current_stack = self._settings_match_plan(plan_data)
        try:
            for key in binding_fds:
                if directory_identity(current_fds[key]) != directory_identity(binding_fds[key]):
                    raise TargetChanged("binding identity changed during preparation")
        finally:
            current_stack.close()
        checked = self._validate_current_row(job, row, binding_fds)
        if checked != raw:
            raise ValueError("artifact changed during preparation")
        for target in journal["targets"]:
            if target["classification"] == "same_sha":
                continue
            key = target["binding_key"]
            assert_directory_identity(target["binding_path"], binding_fds[key])
            temp = prepared[key]
            if self._temp_identity(temp) != target["temp_identity"]:
                raise ValueError("prepared artifact identity changed before barrier")
            reread = read_regular(temp)
            if digest(reread) != row["new_sha256"] or decode_template(reread).template_id != row["new_id"]:
                raise ValueError("prepared artifact bytes changed before barrier")
            if target["classification"] == "conflict":
                backup = target.get("backup")
                if type(backup) is not dict:
                    raise ValueError("conflict backup missing at barrier")
                old = read_regular(job.directory / backup["relative_path"])
                if digest(old) != target["target_evidence"]["sha256"] or digest(old) != backup.get("sha256"):
                    raise ValueError("conflict backup changed before barrier")

    def _prepare_transaction(self, job, plan_data, row, raw, binding_fds):
        journal = self._journal_for_row(plan_data, row)
        prepared = {}
        try:
            self._persist_journal(job, journal)
            for target in journal["targets"]:
                if target["classification"] == "same_sha":
                    continue
                key = target["binding_key"]
                destination = Path(target["binding_path"]) / row["filename"]
                if target["classification"] == "conflict":
                    old = read_regular_at(binding_fds[key], row["filename"])
                    if digest(old) != target["target_evidence"]["sha256"]:
                        raise TargetChanged("old bytes changed before backup")
                    target["backup"] = job.save_backup(
                        old,
                        {
                            "plan_id": plan_data["plan_id"],
                            "transaction_id": row["transaction_id"],
                            "template_key": row["template_key"],
                            "binding_key": key,
                            "relative_name": row["filename"],
                            "binding_path": target["binding_path"],
                            "old_identity": target["target_evidence"]["identity"],
                        },
                    )
                    # Persist the backup capability before creating any
                    # externally visible temporary entry.  A process death in
                    # the preparation phase can then still prove what belongs
                    # to this transaction.
                    self._persist_journal(job, journal)
                target["temp_name"] = ".tmp-" + secrets.token_hex(16)
                target["temp_state"] = "intended"
                # The server-generated basename is durable before O_EXCL
                # creation, closing the otherwise unrecoverable orphan window.
                self._persist_journal(job, journal)
                self.repository.file_ops.checkpoint("sync_before_temp_create", destination)
                temp = self._prepare_named_file(
                    destination,
                    raw,
                    binding_fds[key],
                    target["temp_name"],
                )
                prepared[key] = temp
                reread = read_regular(temp)
                if digest(reread) != row["new_sha256"] or decode_template(reread).template_id != row["new_id"]:
                    raise ValueError("prepared artifact verification failed")
                target["temp_identity"] = self._temp_identity(temp)
                target["temp_sha256"] = digest(reread)
                target["temp_state"] = "created"
                self._persist_journal(job, journal)
            for key in prepared:
                self.repository.file_ops.checkpoint("sync_prepare_dir_fsync", Path(plan_data["bindings"][key]["path"]))
                os.fsync(binding_fds[key])
            self._prepared_barrier(job, plan_data, row, raw, journal, prepared, binding_fds)
            journal["state"] = "prepared"
            self._persist_journal(job, journal)
            return journal, prepared
        except Exception as exc:
            journal["state"] = "rollback_pending"
            journal["error"] = _error("sync_prepare_failed", exc, "preparing")
            try:
                self._persist_journal(job, journal)
                failures = []
                for target in reversed(journal["targets"]):
                    if target["classification"] == "same_sha":
                        continue
                    try:
                        self._cleanup_recorded_temp(
                            target,
                            binding_fds[target["binding_key"]],
                            row["new_sha256"],
                            row["new_id"],
                            job,
                            journal,
                        )
                    except Exception as cleanup_error:
                        failures.append(_error("prepare_temp_cleanup_failed", cleanup_error, "recovery"))
                journal["state"] = "recovery_required" if failures else "rolled_back"
                if failures:
                    journal["error"] = _error("sync_prepare_cleanup_incomplete", exc, "recovery", {"failures": failures})
                self._persist_journal(job, journal)
            except Exception as audit_error:
                journal["state"] = "recovery_required"
                journal["audit_error"] = _error("sync_prepare_audit_failed", audit_error, "preparing")
                try:
                    self._persist_journal(job, journal)
                except Exception:
                    pass
            raise TransactionPreparationFailed(str(exc), journal) from exc

    def _apply_transaction(self, job, plan_data, row, journal, prepared, binding_fds):
        try:
            for target in journal["targets"]:
                if target["classification"] == "same_sha":
                    continue
                key = target["binding_key"]
                destination = Path(target["binding_path"]) / row["filename"]
                journal["state"] = "applying"
                target["apply_state"] = "applying"
                self._persist_journal(job, journal)

                # Preserve the historical checkpoint contract for deterministic
                # faults, but never rely on it as a compare-and-swap boundary.
                # The actual namespace operation below is either no-replace or
                # exchange and therefore cannot destroy a last-moment object.
                self.repository.file_ops.checkpoint("replace", destination)
                current_fds, current_stack = self._settings_match_plan(plan_data)
                try:
                    if directory_identity(current_fds[key]) != directory_identity(binding_fds[key]):
                        raise TargetChanged("binding changed immediately before atomic install")
                finally:
                    current_stack.close()
                assert_directory_identity(target["binding_path"], binding_fds[key])
                current = target_evidence(destination, binding_fds[key])
                if current != target["target_evidence"]:
                    raise TargetChanged("target changed immediately before atomic install")
                reread = read_regular(prepared[key])
                if digest(reread) != row["new_sha256"] or decode_template(reread).template_id != row["new_id"]:
                    raise ValueError("prepared artifact changed before atomic install")
                if self._temp_identity(prepared[key]) != target["temp_identity"]:
                    raise ValueError("prepared artifact identity changed")
                self.repository.file_ops.checkpoint("sync_before_commit", destination)
                if target["classification"] == "create":
                    self.repository.file_ops.checkpoint("sync_before_atomic_create", destination)
                    _atomic_noreplace(binding_fds[key], prepared[key].name, row["filename"])
                else:
                    self.repository.file_ops.checkpoint("sync_before_atomic_install_exchange", destination)
                    _atomic_exchange(binding_fds[key], prepared[key].name, row["filename"])
                prepared[key].applied = True
                self.repository.file_ops.checkpoint("fsync_dir", Path(target["binding_path"]))
                os.fsync(binding_fds[key])
                current = target_evidence(destination, binding_fds[key])
                if not self._matches_applied(current, target, row["new_sha256"]):
                    raise ValueError("committed target verification failed")
                if target["classification"] == "conflict":
                    displaced = self._entry_evidence(binding_fds[key], prepared[key].name)
                    planned = target["target_evidence"]
                    if not self._entry_matches(displaced, planned["sha256"], planned["identity"]):
                        # The exchange has preserved the unexpected object at
                        # the temporary name.  Exchange back so the external
                        # namespace is restored, while preserving both objects.
                        _atomic_exchange(binding_fds[key], prepared[key].name, row["filename"])
                        prepared[key].applied = False
                        self.repository.file_ops.checkpoint("fsync_dir", Path(target["binding_path"]))
                        os.fsync(binding_fds[key])
                        raise TargetChanged("conflict target changed at atomic exchange")
                target["applied"] = True
                target["applied_identity"] = current["identity"]
                target["apply_state"] = "applied"
                self._persist_journal(job, journal)
            # No mutable temporary basename is deleted.  Any displaced object
            # is atomically moved to a pre-journaled no-replace quarantine and
            # retained as recovery evidence.
            for target in journal["targets"]:
                if target["classification"] == "same_sha":
                    continue
                self._cleanup_recorded_temp(
                    target,
                    binding_fds[target["binding_key"]],
                    row["new_sha256"],
                    row["new_id"],
                    job,
                    journal,
                )
            journal["state"] = "committed"
            self._persist_journal(job, journal)
            return journal
        except Exception as exc:
            for target in journal["targets"]:
                if target["classification"] == "same_sha":
                    continue
                key = target["binding_key"]
                try:
                    current = target_evidence(Path(target["binding_path"]) / row["filename"], binding_fds[key])
                    if self._matches_applied(current, target, row["new_sha256"]):
                        target["applied"] = True
                        target["applied_identity"] = current["identity"]
                except (OSError, ValueError):
                    pass
            journal["state"] = "rollback_pending"
            journal["error"] = _error(
                "sync_apply_failed",
                exc,
                "applying",
                {"applied_bindings": [t["binding_key"] for t in journal["targets"] if t.get("applied")]},
            )
            try:
                self._persist_journal(job, journal)
            except Exception as audit_error:
                journal["audit_error"] = _error("rollback_pending_audit_failed", audit_error, "recovery")
            return self._rollback(job, journal, binding_fds)

    def _restore_unknown_quarantine(self, fd, source_name, quarantine_name):
        """Move unknown evidence back only if the source basename is absent."""
        try:
            _atomic_noreplace(fd, quarantine_name, source_name)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                return False
            if exc.errno == errno.ENOENT:
                return False
            raise
        os.fsync(fd)
        return True

    def _select_owned_entry(self, fd, source_name, quarantine_name, candidates):
        """Return (location, evidence, expected) without mutating either name."""
        source = self._entry_evidence(fd, source_name)
        quarantine = self._entry_evidence(fd, quarantine_name) if quarantine_name else None
        if source is not None:
            for expected_sha, expected_identity in candidates:
                if self._entry_matches(source, expected_sha, expected_identity):
                    return "source", source, (expected_sha, expected_identity)
            return "unknown_source", source, None
        if quarantine is not None:
            for expected_sha, expected_identity in candidates:
                if self._entry_matches(quarantine, expected_sha, expected_identity):
                    return "quarantine", quarantine, (expected_sha, expected_identity)
            return "unknown_quarantine", quarantine, None
        return "absent", None, None

    def _quarantine_remove_owned(
        self,
        job,
        journal,
        target,
        fd,
        source_name,
        expected_sha,
        expected_identity,
        name_field,
        state_field,
        checkpoint,
    ):
        """Atomically remove an owned basename without deleting any object.

        The quarantine destination is journaled first and uses native
        no-replace rename.  Known transaction bytes remain under that hidden
        name as recovery evidence.  Unknown bytes are restored to the source
        when possible and are never unlinked.
        """
        quarantine = target.get(name_field)
        if quarantine is None:
            quarantine = ".sync-quarantine-" + secrets.token_hex(16)
            target[name_field] = quarantine
            target[state_field] = "intended"
            self._persist_journal(job, journal)
        if not QUARANTINE_PATTERN.fullmatch(quarantine):
            raise ValueError("invalid quarantine basename")

        location, _, expected = self._select_owned_entry(
            fd,
            source_name,
            quarantine,
            ((expected_sha, expected_identity),),
        )
        if location == "absent":
            target[state_field] = "absent"
            self._persist_journal(job, journal)
            return False
        if location == "unknown_source":
            raise ValueError("source object is not owned by this transaction")
        if location == "unknown_quarantine":
            self._restore_unknown_quarantine(fd, source_name, quarantine)
            target[state_field] = "restored_unknown"
            self._persist_journal(job, journal)
            raise ValueError("quarantine basename is occupied by an unowned object")
        if location == "source":
            self.repository.file_ops.checkpoint(checkpoint, Path(target["binding_path"]) / source_name)
            try:
                _atomic_noreplace(fd, source_name, quarantine)
            except OSError as exc:
                if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    raise ValueError("quarantine basename became occupied; no object was moved") from exc
                raise
            target[state_field] = "moved"
            self._persist_journal(job, journal)
        moved = self._entry_evidence(fd, quarantine)
        if not self._entry_matches(moved, expected[0], expected[1]):
            self._restore_unknown_quarantine(fd, source_name, quarantine)
            raise ValueError("quarantined object is not owned by this transaction")
        self.repository.file_ops.checkpoint("fsync_dir", Path(target["binding_path"]))
        os.fsync(fd)
        target[state_field] = "retained"
        self._persist_journal(job, journal)
        return True

    def _cleanup_recorded_temp(self, target, fd, new_sha, new_id, job, journal):
        name = target.get("temp_name")
        if not name:
            return
        if not TEMP_PATTERN.fullmatch(name):
            raise ValueError("invalid recorded temporary basename")
        quarantine = target.get("temp_quarantine_name")
        candidates = [(new_sha, target.get("temp_identity"))]
        planned = target.get("target_evidence")
        if target.get("classification") == "conflict" and type(planned) is dict and planned.get("exists"):
            candidates.append((planned["sha256"], planned["identity"]))
        location, _, expected = self._select_owned_entry(fd, name, quarantine, candidates)
        if location == "absent":
            target["temp_state"] = "removed"
            return
        if expected is None:
            if location == "unknown_quarantine":
                self._restore_unknown_quarantine(fd, name, quarantine)
            raise ValueError("recorded temporary name contains unowned bytes")
        self._quarantine_remove_owned(
            job,
            journal,
            target,
            fd,
            name,
            expected[0],
            expected[1],
            "temp_quarantine_name",
            "temp_quarantine_state",
            "sync_before_temp_cleanup",
        )
        target["temp_state"] = "removed"

    def _cleanup_restore_temp(self, target, fd, old_sha, job, journal):
        name = target.get("restore_temp_name")
        if not name:
            return
        quarantine = target.get("restore_quarantine_name")
        candidates = [(old_sha, target.get("restore_temp_identity"))]
        applied_identity = target.get("applied_identity") or target.get("temp_identity")
        journal_new_sha = journal.get("new_sha256")
        if journal_new_sha:
            candidates.append((journal_new_sha, applied_identity))
        location, _, expected = self._select_owned_entry(fd, name, quarantine, candidates)
        if location == "absent":
            target["restore_temp_state"] = "removed"
            return
        if expected is None:
            if location == "unknown_quarantine":
                self._restore_unknown_quarantine(fd, name, quarantine)
            raise ValueError("restore temporary name contains unowned bytes")
        self._quarantine_remove_owned(
            job,
            journal,
            target,
            fd,
            name,
            expected[0],
            expected[1],
            "restore_quarantine_name",
            "restore_quarantine_state",
            "sync_before_restore_temp_cleanup",
        )
        target["restore_temp_state"] = "removed"

    def _rollback_target(self, job, journal, target, fd):
        destination = Path(target["binding_path"]) / journal["filename"]
        assert_directory_identity(target["binding_path"], fd)
        planned = target["target_evidence"]
        current = target_evidence(destination, fd)
        if planned["exists"]:
            if current == planned or (current.get("exists") and current.get("sha256") == planned["sha256"]):
                self._cleanup_restore_temp(target, fd, planned["sha256"], job, journal)
                target["rolled_back"] = True
                return
            if not self._matches_applied(current, target, journal["new_sha256"]):
                raise ValueError("target has unknown bytes/identity; refusing to overwrite")
            backup = target.get("backup")
            if type(backup) is not dict or type(backup.get("relative_path")) is not str:
                raise ValueError("verified backup is missing")
            backup_path = job.directory / backup["relative_path"]
            old = read_regular(backup_path)
            if digest(old) != planned["sha256"] or digest(old) != backup.get("sha256"):
                raise ValueError("backup SHA mismatch")
            if target.get("restore_temp_name") is None:
                target["restore_temp_name"] = ".tmp-" + secrets.token_hex(16)
                target["restore_temp_state"] = "intended"
                self._persist_journal(job, journal)
            restore_name = target["restore_temp_name"]
            try:
                os.stat(restore_name, dir_fd=fd, follow_symlinks=False)
                restore = PreparedFile(destination.parent / restore_name, os.dup(fd))
            except FileNotFoundError:
                restore = self._prepare_named_file(destination, old, fd, restore_name)
            restored_temp = read_regular(restore)
            if digest(restored_temp) != planned["sha256"]:
                self._close_prepared(restore)
                raise ValueError("restore temporary SHA mismatch")
            target["restore_temp_identity"] = self._temp_identity(restore)
            target["restore_temp_sha256"] = digest(restored_temp)
            target["restore_temp_state"] = "created"
            self._persist_journal(job, journal)
            try:
                self.repository.file_ops.checkpoint("sync_rollback_replace", destination)
                assert_directory_identity(target["binding_path"], fd)
                evidence = target_evidence(destination, fd)
                if not self._matches_applied(evidence, target, journal["new_sha256"]):
                    raise ValueError("target changed before rollback exchange")
                if self._temp_identity(restore) != target["restore_temp_identity"]:
                    raise ValueError("restore temporary identity changed")
                self.repository.file_ops.checkpoint("sync_before_atomic_restore_exchange", destination)
                _atomic_exchange(fd, restore.name, journal["filename"])
                restore.applied = True
                self.repository.file_ops.checkpoint("fsync_dir", Path(target["binding_path"]))
                os.fsync(fd)

                restored = target_evidence(destination, fd)
                displaced = self._entry_evidence(fd, restore.name)
                restored_ok = bool(restored.get("exists") and restored.get("sha256") == planned["sha256"])
                displaced_ok = self._entry_matches(
                    displaced,
                    journal["new_sha256"],
                    target.get("applied_identity") or target.get("temp_identity"),
                )
                if not (restored_ok and displaced_ok):
                    # Both names still exist.  A second exchange restores the
                    # object observed at the final checkpoint and preserves the
                    # restore bytes at their temporary name.
                    _atomic_exchange(fd, restore.name, journal["filename"])
                    restore.applied = False
                    self.repository.file_ops.checkpoint("fsync_dir", Path(target["binding_path"]))
                    os.fsync(fd)
                    raise TargetChanged("target changed at atomic rollback exchange")
                target["restore_temp_state"] = "created"
                self._persist_journal(job, journal)
                self._cleanup_restore_temp(target, fd, planned["sha256"], job, journal)
            finally:
                self._close_prepared(restore)
            restored = target_evidence(destination, fd)
            if not restored["exists"] or restored["sha256"] != planned["sha256"]:
                raise ValueError("rollback restore verification failed")
            target["restore_temp_state"] = "removed"
            self._persist_journal(job, journal)
        else:
            quarantine = target.get("rollback_quarantine_name")
            quarantine_exists = False
            if quarantine is not None:
                if not QUARANTINE_PATTERN.fullmatch(quarantine):
                    raise ValueError("invalid rollback quarantine basename")
                try:
                    os.stat(quarantine, dir_fd=fd, follow_symlinks=False)
                    quarantine_exists = True
                except FileNotFoundError:
                    pass
            if not current["exists"] and not quarantine_exists:
                target["rolled_back"] = True
                return
            if current["exists"] and not self._matches_applied(current, target, journal["new_sha256"]):
                raise ValueError("created target has unknown bytes/identity; refusing to delete")
            self._quarantine_remove_owned(
                job,
                journal,
                target,
                fd,
                journal["filename"],
                journal["new_sha256"],
                target.get("applied_identity") or target.get("temp_identity"),
                "rollback_quarantine_name",
                "rollback_quarantine_state",
                "sync_rollback_delete",
            )
            if target_evidence(destination, fd)["exists"]:
                raise ValueError("rollback delete verification failed")
        target["rolled_back"] = True

    def _rollback(self, job, journal, binding_fds):
        failures = []
        for target in reversed(journal["targets"]):
            if target["classification"] == "same_sha":
                continue
            key = target["binding_key"]
            try:
                journal["state"] = "rolling_back"
                try:
                    self._persist_journal(job, journal)
                except Exception as persist_error:
                    failures.append(_error("rollback_audit_failed", persist_error, "recovery"))
                self._rollback_target(job, journal, target, binding_fds[key])
                self._cleanup_recorded_temp(
                    target,
                    binding_fds[key],
                    journal["new_sha256"],
                    journal["new_id"],
                    job,
                    journal,
                )
                try:
                    self._persist_journal(job, journal)
                except Exception as persist_error:
                    failures.append(_error("rollback_audit_failed", persist_error, "recovery"))
            except Exception as exc:
                target["rollback_error"] = _error(
                    "rollback_target_failed",
                    exc,
                    "recovery",
                    {"binding_key": key, "filename": journal["filename"]},
                )
                failures.append(target["rollback_error"])
                try:
                    self._persist_journal(job, journal)
                except Exception as persist_error:
                    failures.append(_error("rollback_audit_failed", persist_error, "recovery"))
        journal["state"] = "recovery_required" if failures else "rolled_back"
        if failures:
            journal["error"] = _error(
                "rollback_incomplete",
                "one or more target rollback actions could not be proven",
                "recovery",
                {"failures": failures},
            )
        try:
            self._persist_journal(job, journal)
        except Exception as exc:
            journal["state"] = "recovery_required"
            journal["audit_error"] = _error("rollback_final_audit_failed", exc, "recovery")
        return journal

    def _row_report(self, row, journal, status=None, error=None):
        return {
            "template_key": row["template_key"],
            "artifact_id": row.get("artifact_id"),
            "filename": row.get("filename"),
            "transaction_id": row.get("transaction_id"),
            "status": status or ("synced" if journal and journal.get("state") == "committed" else "failed"),
            "targets": deepcopy(journal["targets"] if journal else row.get("targets", [])),
            "error": deepcopy(error if error is not None else (journal.get("error") if journal else row.get("error"))),
        }

    def _report_status(self, rows):
        return "committed" if rows and all(row["status"] in _SUCCESS for row in rows) else "partial_failed"

    def _load_persisted_report(self, job, plan_data):
        value = json.loads(read_regular(self._plan_directory(job, plan_data["plan_id"]) / "report.json"))
        if (
            type(value) is not dict
            or value.get("plan_id") != plan_data["plan_id"]
            or value.get("job_id") != job.job_id
            or type(value.get("status")) is not str
            or type(value.get("rows")) is not list
        ):
            raise ValueError("invalid persisted sync report")
        return SyncReport(value["plan_id"], value["job_id"], value["status"], tuple(deepcopy(value["rows"])))

    def commit_sync(self, plan_id, conflict_confirmations=None):
        job, (plan_data, _) = self._find_plan(plan_id)
        confirmations = {} if conflict_confirmations is None else conflict_confirmations
        if type(confirmations) is not dict:
            raise ValueError("conflict confirmations must be a mapping")
        known = {
            target["conflict_id"]
            for row in plan_data["rows"]
            for target in row["targets"]
            if target.get("classification") == "conflict"
        }
        if any(
            type(key) is not str
            or key not in known
            or type(value) is not str
            or value != "backup_and_replace"
            for key, value in confirmations.items()
        ):
            raise ValueError("unknown, stale or invalid conflict confirmation")

        with job.lock:
            plan_data, _ = self._load_plan_at(job, plan_id)
            if plan_data["status"] in ("committed", "rolled_back", "recovery_required", "invalidated"):
                try:
                    return self._load_persisted_report(job, plan_data)
                except Exception as exc:
                    self._diagnose("invalid_sync_report", exc, {"job_id": job.job_id, "plan_id": plan_id})
                    rows = tuple(
                        self._row_report(
                            row,
                            None,
                            "recovery_required" if plan_data["status"] == "recovery_required" else "failed",
                            _error("terminal_plan_report_missing", exc, "recovery"),
                        )
                        for row in plan_data["rows"]
                    )
                    return SyncReport(plan_id, job.job_id, plan_data["status"], rows)
            if plan_data["status"] == "committing":
                rows = tuple(
                    self._row_report(
                        row,
                        None,
                        "recovery_required",
                        _error("pending_transaction_requires_recovery", "run startup recovery before a new commit", "recovery"),
                    )
                    for row in plan_data["rows"]
                )
                return SyncReport(plan_id, job.job_id, "recovery_required", rows)
            required_all = {
                target["conflict_id"]
                for row in plan_data["rows"]
                if row["status"] == "planned"
                for target in row["targets"]
                if target["classification"] == "conflict"
            }
            if not required_all.issubset(confirmations):
                rows = []
                for row in plan_data["rows"]:
                    required = {
                        target["conflict_id"]
                        for target in row.get("targets", [])
                        if target.get("classification") == "conflict"
                    }
                    error = None
                    status = "blocked" if row["status"] == "blocked" or not required.issubset(confirmations) else "ready"
                    if not required.issubset(confirmations):
                        error = _error(
                            "conflict_confirmation_required",
                            "every conflict needs an explicit current-plan confirmation",
                            "confirmation",
                            {"required_conflict_ids": sorted(required)},
                        )
                    rows.append(self._row_report(row, None, status, error or row.get("error")))
                report = SyncReport(plan_id, job.job_id, "confirmation_required", tuple(rows))
                try:
                    self._persist_report(job, plan_id, report)
                except Exception as exc:
                    self._diagnose("sync_report_persist_failed", exc, {"job_id": job.job_id, "plan_id": plan_id})
                return report
            try:
                binding_fds, stack = self._settings_match_plan(plan_data)
            except (OSError, ValueError, TypeError) as exc:
                return self._result_for_invalidated(job, plan_data, "settings_changed", exc)
            if _ATOMIC_RENAME is None:
                stack.close()
                return self._result_for_invalidated(
                    job,
                    plan_data,
                    "atomic_sync_unavailable",
                    "this operating system lacks no-replace/exchange rename; no target was written",
                )
            plan_data["status"] = "committing"
            plan_data["consumed_conflict_ids"] = sorted(required_all)
            plan_data["commit_plan_digest"] = _commit_digest(plan_data)
            self._persist_plan(job, plan_data)
            rows = []
            try:
                for row in plan_data["rows"]:
                    prepared = {}
                    row_result = None
                    try:
                        if row["status"] == "blocked":
                            row_result = self._row_report(row, None, "blocked", row.get("error") or _error("plan_row_blocked", "row was blocked during planning", "planning"))
                            continue
                        existing = self._load_existing_journal(job, row, plan_id)
                        if existing is not None:
                            if existing["state"] == "committed":
                                row_result = self._row_report(row, existing, "synced")
                            elif existing["state"] == "rolled_back":
                                row_result = self._row_report(row, existing, "rolled_back")
                            else:
                                row_result = self._row_report(row, existing, "recovery_required")
                            continue
                        raw = self._validate_current_row(job, row, binding_fds)
                        if all(target["classification"] == "same_sha" for target in row["targets"]):
                            row_result = self._row_report(row, None, "skipped_same_sha", None)
                            continue
                        journal, prepared = self._prepare_transaction(job, plan_data, row, raw, binding_fds)
                        journal = self._apply_transaction(job, plan_data, row, journal, prepared, binding_fds)
                        if journal["state"] == "committed":
                            row_result = self._row_report(row, journal, "synced")
                        elif journal["state"] == "rolled_back":
                            row_result = self._row_report(row, journal, "rolled_back")
                        else:
                            row_result = self._row_report(row, journal, "recovery_required")
                    except TransactionPreparationFailed as exc:
                        status = "recovery_required" if exc.journal.get("state") == "recovery_required" else "rolled_back"
                        row_result = self._row_report(row, exc.journal, status, exc.journal["error"])
                    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                        code = "target_changed" if isinstance(exc, TargetChanged) else "template_transaction_failed"
                        row_result = self._row_report(row, None, "failed", _error(code, exc, "validation"))
                    finally:
                        cleanup_failures = []
                        for temp in prepared.values():
                            try:
                                self._close_prepared(temp)
                            except Exception as exc:
                                cleanup_failures.append(_error("prepared_handle_cleanup_failed", exc, "recovery"))
                        if cleanup_failures:
                            row_result = self._row_report(
                                row,
                                None,
                                "recovery_required",
                                _error("template_cleanup_incomplete", "prepared handle cleanup failed", "recovery", {"failures": cleanup_failures}),
                            )
                        if row_result is None:
                            row_result = self._row_report(
                                row,
                                None,
                                "failed",
                                _error("template_transaction_incomplete", "template transaction ended without a result", "recovery"),
                            )
                        rows.append(row_result)
            finally:
                stack.close()
            if rows and all(row["status"] in _SUCCESS for row in rows):
                final_state = "committed"
                report_status = "committed"
            elif any(row["status"] == "recovery_required" for row in rows):
                final_state = "recovery_required"
                report_status = "partial_failed"
            elif any(row["status"] == "blocked" for row in rows):
                final_state = "invalidated"
                report_status = "partial_failed"
            else:
                final_state = "rolled_back"
                report_status = "partial_failed"
            report = SyncReport(plan_id, job.job_id, report_status, tuple(rows))
            try:
                self._persist_report(job, plan_id, report)
            except Exception as exc:
                self._diagnose("sync_report_persist_failed", exc, {"job_id": job.job_id, "plan_id": plan_id})
                final_state = "recovery_required"
                report = SyncReport(plan_id, job.job_id, "audit_failed", tuple(rows))
            plan_data["status"] = final_state
            try:
                self._persist_plan(job, plan_data)
            except Exception as exc:
                self._diagnose("sync_plan_persist_failed", exc, {"job_id": job.job_id, "plan_id": plan_id})
            return report

    def _iter_plan_jobs(self, max_jobs=1000, max_plans=1000):
        self.repository._check_root()
        yielded = 0
        jobs = 0
        with directory_fd(self.repository.root) as root_fd, os.scandir(root_fd) as entries:
            for entry in entries:
                try:
                    name = entry.name
                    if not JOB_PATTERN.fullmatch(name) or not entry.is_dir(follow_symlinks=False):
                        continue
                    if jobs >= max_jobs or yielded >= max_plans:
                        self._diagnose(
                            "sync_plan_scan_truncated",
                            "job or plan scan limit reached",
                            {"max_jobs": max_jobs, "max_plans": max_plans},
                        )
                        return
                    jobs += 1
                    job = self.repository.get(name)
                    sync_root = job.directory / "sync"
                    try:
                        with directory_fd(sync_root) as sync_fd, os.scandir(sync_fd) as plans:
                            for plan_entry in plans:
                                plan_id = plan_entry.name
                                if not PLAN_PATTERN.fullmatch(plan_id) or not plan_entry.is_dir(follow_symlinks=False):
                                    continue
                                yielded += 1
                                yield job, plan_id
                                if yielded >= max_plans:
                                    self._diagnose(
                                        "sync_plan_scan_truncated",
                                        "plan scan limit reached",
                                        {"max_jobs": max_jobs, "max_plans": max_plans},
                                    )
                                    return
                    except FileNotFoundError:
                        continue
                except (OSError, ValueError, KeyError) as exc:
                    self._diagnose("invalid_sync_plan_entry", exc, {"job_id": getattr(entry, "name", None)})
                    continue

    def _iter_journals(self, job, plan_id, max_transactions=1000):
        root = self._plan_directory(job, plan_id) / "transactions"
        try:
            with directory_fd(root) as root_fd, os.scandir(root_fd) as entries:
                candidates = 0
                for entry in entries:
                    name = entry.name
                    if TRANSACTION_PATTERN.fullmatch(name) and entry.is_dir(follow_symlinks=False):
                        if candidates >= max_transactions:
                            self._diagnose(
                                "sync_journal_scan_truncated",
                                "transaction scan limit reached",
                                {"job_id": job.job_id, "plan_id": plan_id, "limit": max_transactions},
                            )
                            return
                        candidates += 1
                        try:
                            value = json.loads(read_regular(root / name / "journal.json"))
                            yield value
                        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                            self._diagnose(
                                "invalid_sync_journal",
                                exc,
                                {"job_id": job.job_id, "plan_id": plan_id, "transaction_id": name},
                            )
                            continue
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as exc:
            self._diagnose(
                "invalid_sync_journal_directory",
                exc,
                {"job_id": job.job_id, "plan_id": plan_id},
            )

    def _open_recovery_bindings(self, journal):
        opened = {}
        stack = ExitStack()
        try:
            for target in journal["targets"]:
                if target["classification"] == "same_sha":
                    continue
                key = target["binding_key"]
                if key in opened:
                    continue
                _, _, suffix = _BINDINGS[key]
                result = validate_binding(target["binding_path"], suffix)
                if not result.valid:
                    raise ValueError("historical binding is invalid: {}".format(result.reason))
                fd = stack.enter_context(directory_fd(result.path, writable=True))
                if list(directory_identity(fd)) != target["binding_identity"]:
                    raise ValueError("historical binding directory identity changed")
                opened[key] = fd
            return opened, stack
        except BaseException:
            stack.close()
            raise

    def _missing_journal_is_provably_unstarted(self, job, row):
        """A missing expected journal is safe only while every preview is exact.

        This check intentionally uses the immutable plan paths and directory
        identities, not current settings.  It is read-only: any ambiguity is a
        recovery requirement, never permission to mutate an external target.
        """
        self._read_bound_artifact(job, row)
        opened = {}
        stack = ExitStack()
        try:
            for target in row["targets"]:
                key = target["binding_key"]
                if key not in opened:
                    _, _, suffix = _BINDINGS[key]
                    result = validate_binding(target["binding_path"], suffix)
                    if not result.valid:
                        raise ValueError("historical binding is invalid: {}".format(result.reason))
                    fd = stack.enter_context(directory_fd(result.path, writable=False))
                    if list(directory_identity(fd)) != target["binding_identity"]:
                        raise ValueError("historical binding directory identity changed")
                    opened[key] = fd
                current = target_evidence(
                    Path(target["binding_path"]) / row["filename"],
                    opened[key],
                )
                if current != target["target_evidence"]:
                    return False
            return True
        finally:
            stack.close()

    def _validate_recovery_journal(self, journal, plan_data, row):
        """Strictly bind persisted capabilities before any external operation."""
        if plan_data["commit_plan_digest"] is None:
            raise ValueError("transaction journal exists for an unconsumed preview")
        if type(journal) is not dict or journal.get("version") != 2:
            raise ValueError("invalid recovery journal root")
        if journal.get("state") not in _JOURNAL_STATES:
            raise ValueError("invalid recovery journal state")
        exact = {
            "plan_id": plan_data["plan_id"],
            "plan_digest": plan_data["commit_plan_digest"],
            "preview_digest": plan_data["preview_digest"],
            "job_id": plan_data["job_id"],
            "transaction_id": row["transaction_id"],
            "template_key": row["template_key"],
            "attempt_id": row["attempt_id"],
            "artifact_id": row["artifact_id"],
            "new_id": row["new_id"],
            "filename": row["filename"],
            "new_sha256": row["new_sha256"],
            "size": row["size"],
        }
        if any(journal.get(name) != value for name, value in exact.items()):
            raise ValueError("recovery journal does not match immutable plan row")
        filename = _safe_filename(journal.get("filename"))
        if not SHA_PATTERN.fullmatch(journal.get("new_sha256", "")):
            raise ValueError("invalid recovery output SHA")
        if type(journal.get("targets")) is not list or len(journal["targets"]) != len(row["targets"]):
            raise ValueError("invalid recovery targets")
        seen = set()
        for target, planned_target in zip(journal["targets"], row["targets"]):
            if type(target) is not dict:
                raise ValueError("invalid recovery target")
            key = target.get("binding_key")
            if key not in _BINDINGS or key in seen:
                raise ValueError("invalid recovery binding key")
            seen.add(key)
            for name in (
                "binding_key", "binding_path", "binding_identity", "filename",
                "classification", "target_evidence", "conflict_id",
            ):
                if target.get(name) != planned_target.get(name):
                    raise ValueError("recovery target does not match plan")
            absolute_path(target.get("binding_path"))
            identity = target.get("binding_identity")
            if not _identity(identity, 2):
                raise ValueError("invalid recovery binding identity")
            if target.get("classification") not in ("create", "same_sha", "conflict"):
                raise ValueError("invalid recovery classification")
            evidence = target.get("target_evidence")
            if not _evidence_shape(evidence):
                raise ValueError("invalid recovery target evidence")
            temp_name = target.get("temp_name")
            temp_identity = target.get("temp_identity")
            temp_state = target.get("temp_state")
            if temp_state not in ("none", "intended", "created", "removed"):
                raise ValueError("invalid recovery temporary state")
            if temp_state == "none":
                if temp_name is not None or temp_identity is not None or target.get("temp_sha256") is not None:
                    raise ValueError("unexpected temporary evidence")
            else:
                if type(temp_name) is not str or not TEMP_PATTERN.fullmatch(temp_name):
                    raise ValueError("invalid recovery temporary basename")
                if temp_identity is not None and not _identity(temp_identity, 3):
                    raise ValueError("invalid recovery temporary identity")
                if temp_state == "created" and (
                    not _identity(temp_identity, 3) or target.get("temp_sha256") != journal["new_sha256"]
                ):
                    raise ValueError("incomplete created temporary evidence")
            applied_identity = target.get("applied_identity")
            if applied_identity is not None and not _identity(applied_identity, 5):
                raise ValueError("invalid applied target identity")
            if type(target.get("applied")) is not bool or type(target.get("rolled_back")) is not bool:
                raise ValueError("invalid recovery target flags")
            restore_state = target.get("restore_temp_state")
            restore_name = target.get("restore_temp_name")
            restore_identity = target.get("restore_temp_identity")
            if restore_state not in ("none", "intended", "created", "removed"):
                raise ValueError("invalid restore temporary state")
            if restore_state == "none":
                if restore_name is not None or restore_identity is not None or target.get("restore_temp_sha256") is not None:
                    raise ValueError("unexpected restore temporary evidence")
            else:
                if not TEMP_PATTERN.fullmatch(restore_name or ""):
                    raise ValueError("invalid restore temporary basename")
                if restore_identity is not None and not _identity(restore_identity, 3):
                    raise ValueError("invalid restore temporary identity")
                if restore_state == "created" and (
                    not _identity(restore_identity, 3)
                    or target.get("restore_temp_sha256") != evidence.get("sha256")
                ):
                    raise ValueError("incomplete restore temporary evidence")
            for name_field, state_field in (
                ("temp_quarantine_name", "temp_quarantine_state"),
                ("restore_quarantine_name", "restore_quarantine_state"),
                ("rollback_quarantine_name", "rollback_quarantine_state"),
            ):
                quarantine_name = target.get(name_field)
                quarantine_state = target.get(state_field)
                if quarantine_state not in (
                    "none", "intended", "moved", "retained", "absent", "restored_unknown"
                ):
                    raise ValueError("invalid quarantine state")
                if quarantine_state == "none" and quarantine_name is not None:
                    raise ValueError("unexpected quarantine basename")
                if quarantine_state != "none" and not QUARANTINE_PATTERN.fullmatch(quarantine_name or ""):
                    raise ValueError("invalid quarantine basename")
            backup = target.get("backup")
            if backup is not None:
                if type(backup) is not dict or type(backup.get("relative_path")) is not str:
                    raise ValueError("invalid recovery backup")
                relative = Path(backup["relative_path"])
                if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "backups":
                    raise ValueError("recovery backup escaped the job")
                if (
                    target["classification"] != "conflict"
                    or backup.get("sha256") != evidence.get("sha256")
                    or backup.get("plan_id") != plan_data["plan_id"]
                    or backup.get("transaction_id") != row["transaction_id"]
                    or backup.get("template_key") != row["template_key"]
                    or backup.get("binding_key") != key
                    or backup.get("relative_name") != filename
                    or backup.get("binding_path") != target["binding_path"]
                    or backup.get("old_identity") != evidence.get("identity")
                ):
                    raise ValueError("recovery backup does not match plan")

    def recover_pending(self, max_jobs=1000, max_plans=1000, max_transactions=1000):
        recovered = []
        for job, plan_id in self._iter_plan_jobs(max_jobs, max_plans):
            try:
                with job.lock:
                    try:
                        plan_data, _ = self._load_plan_at(job, plan_id)
                    except Exception as exc:
                        self._diagnose(
                            "invalid_sync_plan",
                            exc,
                            {"job_id": job.job_id, "plan_id": plan_id},
                        )
                        continue
                    plan_results = []
                    journals_by_transaction = {}
                    diagnostics_start = len(self.diagnostics)
                    invalid_journal = False
                    missing_safe = set()
                    missing_unsafe = set()
                    expected_rows = [
                        row
                        for row in plan_data["rows"]
                        if row["status"] == "planned"
                        and not all(target["classification"] == "same_sha" for target in row["targets"])
                    ]
                    if len(expected_rows) > max_transactions:
                        self._diagnose(
                            "sync_journal_scan_truncated",
                            "expected transaction limit reached",
                            {"job_id": job.job_id, "plan_id": plan_id, "limit": max_transactions},
                        )
                        invalid_journal = True
                    for row in expected_rows[:max_transactions]:
                        transaction_id = row["transaction_id"]
                        try:
                            journal = json.loads(
                                read_regular(self._journal_path(job, plan_id, transaction_id))
                            )
                            self._validate_recovery_journal(journal, plan_data, row)
                        except FileNotFoundError:
                            # Commit durably consumes the plan before creating
                            # any journal or changing an external file. A
                            # validated, unconsumed preview has no transaction
                            # to recover, even when another plan changed its
                            # old target evidence. Existing journals still go
                            # through the strict capability validator above.
                            if (plan_data["status"] in ("ready", "invalidated")
                                    and plan_data["commit_plan_digest"] is None):
                                continue
                            try:
                                unchanged = self._missing_journal_is_provably_unstarted(job, row)
                            except Exception as exc:
                                unchanged = False
                                missing_reason = str(exc)
                            else:
                                missing_reason = "one or more external targets differ from the immutable preview"
                            if unchanged:
                                missing_safe.add(transaction_id)
                            else:
                                missing_unsafe.add(transaction_id)
                                self._diagnose(
                                    "missing_expected_sync_journal",
                                    "expected transaction journal is missing and the transaction cannot be proven unstarted",
                                    {
                                        "job_id": job.job_id,
                                        "plan_id": plan_id,
                                        "transaction_id": transaction_id,
                                        "reason": missing_reason,
                                    },
                                )
                            continue
                        except Exception as exc:
                            self._diagnose(
                                "invalid_sync_journal",
                                exc,
                                {"job_id": job.job_id, "plan_id": plan_id, "transaction_id": transaction_id},
                            )
                            invalid_journal = True
                            continue
                        journals_by_transaction[transaction_id] = journal
                        state = journal["state"]
                        if state in _TERMINAL_JOURNALS:
                            plan_results.append(state)
                            if state == "recovery_required":
                                item = {
                                    "job_id": job.job_id,
                                    "plan_id": plan_id,
                                    "transaction_id": transaction_id,
                                    "template_key": journal["template_key"],
                                    "status": state,
                                    "error": deepcopy(journal.get("error")),
                                }
                                recovered.append(item)
                            continue
                        if state not in _RECOVERABLE_JOURNALS or plan_data["status"] != "committing":
                            self._diagnose(
                                "sync_lifecycle_mismatch",
                                "pending journal is not paired with a committing plan",
                                {"job_id": job.job_id, "plan_id": plan_id, "transaction_id": transaction_id},
                            )
                            continue
                        try:
                            self._read_bound_artifact(job, row)
                            binding_fds, stack = self._open_recovery_bindings(journal)
                            try:
                                journal = self._rollback(job, journal, binding_fds)
                            finally:
                                stack.close()
                        except Exception as exc:
                            journal["state"] = "recovery_required"
                            journal["error"] = _error(
                                "recovery_binding_or_rollback_failed", exc, "recovery", {"plan_id": plan_id}
                            )
                            try:
                                self._persist_journal(job, journal)
                            except Exception as persist_error:
                                self._diagnose(
                                    "recovery_audit_failed",
                                    persist_error,
                                    {"job_id": job.job_id, "plan_id": plan_id, "transaction_id": transaction_id},
                                )
                        item = {
                            "job_id": job.job_id,
                            "plan_id": plan_id,
                            "transaction_id": transaction_id,
                            "template_key": journal["template_key"],
                            "status": journal["state"],
                            "error": deepcopy(journal.get("error")),
                        }
                        recovered.append(item)
                        plan_results.append(journal["state"])
                        journals_by_transaction[transaction_id] = journal
                    if any(
                        item.get("code") in ("invalid_sync_journal", "invalid_sync_journal_directory", "sync_journal_scan_truncated")
                        and item.get("evidence", {}).get("plan_id") == plan_id
                        for item in self.diagnostics[diagnostics_start:]
                    ):
                        invalid_journal = True
                    if plan_data["status"] == "committing":
                        try:
                            report_rows = []
                            for row in plan_data["rows"]:
                                journal = journals_by_transaction.get(row["transaction_id"])
                                if row["status"] == "blocked":
                                    report_rows.append(self._row_report(row, None, "blocked", row.get("error")))
                                elif journal is None and all(t["classification"] == "same_sha" for t in row["targets"]):
                                    report_rows.append(self._row_report(row, None, "skipped_same_sha", None))
                                elif journal is not None and journal["state"] == "committed":
                                    report_rows.append(self._row_report(row, journal, "synced"))
                                elif journal is not None and journal["state"] == "rolled_back":
                                    report_rows.append(self._row_report(row, journal, "rolled_back"))
                                elif row["transaction_id"] in missing_unsafe:
                                    report_rows.append(
                                        self._row_report(
                                            row,
                                            None,
                                            "recovery_required",
                                            _error(
                                                "missing_expected_sync_journal",
                                                "expected journal is missing and external state no longer matches the preview",
                                                "recovery",
                                                {"transaction_id": row["transaction_id"]},
                                            ),
                                        )
                                    )
                                elif journal is None and invalid_journal:
                                    report_rows.append(
                                        self._row_report(
                                            row,
                                            None,
                                            "recovery_required",
                                            _error(
                                                "unverifiable_transaction_journal",
                                                "one or more transaction journals could not be verified",
                                                "recovery",
                                            ),
                                        )
                                    )
                                elif row["transaction_id"] in missing_safe:
                                    # The exact expected journal is absent and
                                    # every artifact, directory identity and
                                    # target still equals the immutable preview.
                                    report_rows.append(
                                        self._row_report(
                                            row,
                                            None,
                                            "rolled_back",
                                            _error(
                                                "transaction_not_started",
                                                "commit stopped before the transaction journal was created",
                                                "recovery",
                                            ),
                                        )
                                    )
                                    recovered.append(
                                        {
                                            "job_id": job.job_id,
                                            "plan_id": plan_id,
                                            "transaction_id": row["transaction_id"],
                                            "template_key": row["template_key"],
                                            "status": "rolled_back",
                                            "error": deepcopy(report_rows[-1]["error"]),
                                        }
                                    )
                                elif journal is None:
                                    report_rows.append(
                                        self._row_report(
                                            row,
                                            None,
                                            "recovery_required",
                                            _error(
                                                "unverifiable_transaction_journal",
                                                "the expected transaction journal was not safely classified",
                                                "recovery",
                                            ),
                                        )
                                    )
                                else:
                                    report_rows.append(self._row_report(row, journal, "recovery_required"))
                            row_states = [item["status"] for item in report_rows]
                            if invalid_journal or "recovery_required" in row_states:
                                plan_data["status"] = "recovery_required"
                            elif row_states and all(state in _SUCCESS for state in row_states):
                                plan_data["status"] = "committed"
                            elif "blocked" in row_states:
                                plan_data["status"] = "invalidated"
                            else:
                                plan_data["status"] = "rolled_back"
                            recovery_report = SyncReport(
                                plan_id,
                                job.job_id,
                                "committed" if plan_data["status"] == "committed" else "partial_failed",
                                tuple(report_rows),
                            )
                            self._persist_report(job, plan_id, recovery_report)
                            self._persist_plan(job, plan_data)
                        except Exception as exc:
                            self._diagnose(
                                "recovery_plan_audit_failed",
                                exc,
                                {"job_id": job.job_id, "plan_id": plan_id},
                            )
            except Exception as exc:
                self._diagnose(
                    "sync_recovery_item_failed",
                    exc,
                    {"job_id": getattr(job, "job_id", None), "plan_id": plan_id},
                )
        return tuple(recovered)


class TargetChanged(ValueError):
    pass


class TransactionPreparationFailed(ValueError):
    def __init__(self, message, journal):
        super().__init__(message)
        self.journal = journal


_FUNCTION_SERVICES = {}


def create_sync_plan(job, selected_bindings, settings_path=DEFAULT_SETTINGS_PATH):
    """Documented function interface for simple in-process callers.

    Servers should still own one long-lived ``SyncService`` so startup recovery
    and submission share the same repository lock.  This bounded registry only
    connects the two documented convenience calls; it never stores paths from
    a request.
    """
    service = SyncService(job.repository, settings_path)
    plan = service.create_sync_plan(job, selected_bindings)
    _FUNCTION_SERVICES[plan.plan_id] = service
    while len(_FUNCTION_SERVICES) > 256:
        _FUNCTION_SERVICES.pop(next(iter(_FUNCTION_SERVICES)))
    return plan


def commit_sync(plan_id, conflict_confirmations):
    """Commit a plan created by the matching convenience function."""
    if type(plan_id) is not str or not PLAN_PATTERN.fullmatch(plan_id):
        raise ValueError("invalid sync plan id")
    service = _FUNCTION_SERVICES.get(plan_id)
    if service is None:
        raise KeyError("sync_plan_service_not_available; use a persistent SyncService after restart")
    return service.commit_sync(plan_id, conflict_confirmations)
