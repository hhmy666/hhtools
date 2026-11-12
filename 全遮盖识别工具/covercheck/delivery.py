"""Read-only delivery previews and independent, no-overwrite local commits.

The server keeps the returned private plan; only rows/targets are JSON output.
Files contains renamed bytes for ZIP delivery. No board rule blocks delivery.
"""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import secrets
import stat
import unicodedata

from .engine import analyze_bytes
from .repair import rewrite_tiles


def _key(value):
    return unicodedata.normalize("NFKC", value).casefold()


def _safe_export_name(value):
    if not isinstance(value, str):
        raise ValueError("请填写新模板命名格式")
    normalized = unicodedata.normalize("NFKC", value)
    if (not value or value != value.strip() or normalized in (".", "..")
            or len(value.encode("utf-8")) > 220 or normalized.endswith((".", " "))
            or any(unicodedata.category(char).startswith("C") or char in '/\\<>:"|?*{}'
                   for char in normalized)
            or normalized.lower().endswith(".bytes")
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", normalized)):
        raise ValueError("新模板名称需要是普通文件名，不能包含路径、保留名或.bytes后缀")
    return value


def _identity(info):
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class _Root:
    path: Path
    identities: tuple


def _path(value):
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("导出目录需要是绝对路径")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path) or path == Path(path.anchor):
        raise ValueError("导出目录需要是无路径穿越的绝对目录")
    return path


def _open_root(path, expected=None, created=None, create=False):
    """One fd-relative no-follow walk, binding every existing directory inode."""
    created = created if created is not None else {}
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    identities = [_identity(os.fstat(fd))]
    try:
        if expected is not None and identities[0] != expected.identities[0]:
            raise ValueError("目录已变化，请重新预览")
        current = Path(path.anchor)
        for position, part in enumerate(path.parts[1:], 1):
            current = current / part
            wanted = expected.identities[position] if expected is not None else None
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if wanted is not None:
                    raise ValueError("目录已变化，请重新预览")
                if not create:
                    identities.extend([None] * (len(path.parts) - position))
                    os.close(fd)
                    return None, _Root(path, tuple(identities))
                # Exclusive mkdir: a directory appearing after preview is a conflict.
                os.mkdir(part, 0o700, dir_fd=fd)
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                created[str(current)] = _identity(info)
                os.fsync(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("导出目录不能包含符号链接或非目录节点")
            identity = _identity(info)
            if expected is not None and identity != (wanted or created.get(str(current))):
                raise ValueError("目录已变化，请重新预览")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if _identity(os.fstat(child)) != identity:
                os.close(child)
                raise ValueError("目录在检查期间变化，请重新预览")
            os.close(fd)
            fd = child
            identities.append(identity)
        return fd, _Root(path, tuple(identities))
    except BaseException:
        os.close(fd)
        raise


def _collisions(fd, filename):
    return [name for name in os.listdir(fd) if _key(name) == _key(filename)]


def _source_id(name):
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith("_m.bytes"):
        return name[:-8]
    return name[:-6] if name.lower().endswith(".bytes") else name


def plan_delivery(entries, targets, pattern="fixed{原模板ID}", reserved_ids=()):
    """Prepare bytes/names without creating directories or changing any file.

    Entries have index/name/template_id/raw. Targets must include output; optional
    names (e.g. template/tile) denote explicitly selected sync directories.
    Every existing filename occupies its normalized name, regardless of content.
    reserved_ids can include original IDs from unselected task sources.
    """
    if not isinstance(entries, list) or not entries:
        raise ValueError("请选择已保存且有修改的模板")
    if not isinstance(targets, dict) or "output" not in targets:
        raise ValueError("需要指定工具输出目录")
    if not isinstance(pattern, str):
        raise ValueError("请填写新模板命名格式")
    _safe_export_name(pattern.replace("{原模板ID}", "sample").replace("{id}", "sample"))
    roots, occupied, seen_paths = {}, set(), set()
    for target, value in targets.items():
        if not isinstance(target, str) or not target:
            raise ValueError("导出目标名称不能为空")
        path = _path(value)
        if _key(str(path)) in seen_paths:
            raise ValueError("导出目标目录不能重复")
        seen_paths.add(_key(str(path)))
        fd, root = _open_root(path)
        roots[target] = root
        if fd is not None:
            try:
                occupied.update(_key(name) for name in os.listdir(fd))
            finally:
                os.close(fd)
    reserved, analyses, indices = {_key(value) for value in reserved_ids if value}, [], set()
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("index")) is not int or entry["index"] in indices:
            raise ValueError("模板index需要是不重复的整数")
        indices.add(entry["index"])
        analysis = analyze_bytes(entry["raw"])
        if entry.get("template_id") != analysis["template_id"]:
            raise ValueError("模板ID与待导出字节不一致")
        analyses.append(analysis)
        reserved.update(_key(value) for value in (analysis["template_id"], _source_id(entry["name"])) if value)
    rows, prepared, files = [], [], {}
    for entry, analysis in zip(entries, analyses):
        desired = _safe_export_name(pattern.replace("{原模板ID}", analysis["template_id"])
                                   .replace("{id}", analysis["template_id"]))
        new_id, version = desired, 1
        while _key(new_id) in reserved or _key(new_id + "_m.bytes") in occupied:
            version += 1
            new_id = _safe_export_name(desired + "_v" + str(version))
        reserved.add(_key(new_id))
        filename = new_id + "_m.bytes"
        occupied.add(_key(filename))
        raw = rewrite_tiles(entry["raw"], analysis["tiles"], new_id=new_id)
        row = {"index": entry["index"], "name": entry["name"], "new_id": new_id, "filename": filename,
               "targets": [{"target": target, "path": str(root.path / filename), "status": "create"}
                           for target, root in roots.items()]}
        rows.append(row)
        files[filename] = raw
        prepared.append({"row": deepcopy(row), "raw": raw, "sha256": sha256(raw).hexdigest()})
    return {"rows": rows, "targets": {name: str(root.path) for name, root in roots.items()},
            "files": files, "_prepared": prepared, "_roots": roots}


class _WriteFailed(Exception):
    def __init__(self, error, applied):
        super().__init__(str(error))
        self.applied = applied


def _publish(root, created, filename, raw, expected_sha):
    fd, _ = _open_root(root.path, root, created, create=True)
    temp, published, applied = ".delivery-" + secrets.token_hex(16), None, False
    try:
        if _collisions(fd, filename):
            raise ValueError("目标文件已变化，请重新预览")
        file_fd = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(file_fd, "w+b") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            if sha256(stream.read()).hexdigest() != expected_sha:
                raise ValueError("导出临时文件校验失败")
            identity = _identity(os.fstat(stream.fileno()))
            check_fd, _ = _open_root(root.path, root, created)
            try:
                if check_fd is None or _identity(os.fstat(check_fd)) != _identity(os.fstat(fd)):
                    raise ValueError("目录已变化，请重新预览")
            finally:
                if check_fd is not None:
                    os.close(check_fd)
            if _collisions(fd, filename) or _identity(os.stat(temp, dir_fd=fd, follow_symlinks=False)) != identity:
                raise ValueError("目标文件已变化，请重新预览")
            # Link publishes a complete inode atomically and fails on any existing leaf.
            os.link(temp, filename, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            published = _identity(os.stat(filename, dir_fd=fd, follow_symlinks=False))
            applied = True
            try:
                check_fd, _ = _open_root(root.path, root, created)
                try:
                    if (check_fd is None or _identity(os.fstat(check_fd)) != _identity(os.fstat(fd))
                            or published != identity or _collisions(fd, filename) != [filename]):
                        raise ValueError("目标在写入期间变化，请重新预览")
                finally:
                    if check_fd is not None:
                        os.close(check_fd)
            except Exception:
                # Undo only the inode just published, never another writer's leaf.
                if _identity(os.stat(filename, dir_fd=fd, follow_symlinks=False)) == published:
                    os.unlink(filename, dir_fd=fd)
                    applied = False
                raise
            os.fsync(fd)
    except Exception as error:
        raise _WriteFailed(error, applied) from error
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise _WriteFailed("临时文件清理失败：{}".format(error), applied) from error
        finally:
            os.close(fd)


def commit_delivery(plan, progress=None):
    """Commit a retained private preview; report each file/target independently.

    A failed fsync after publication has applied=True. Changed destinations need
    another preview; this function never silently allocates a different filename.
    Progress receives one dict, and notification errors cannot interrupt writes.
    """
    roots, prepared = plan["_roots"], plan["_prepared"]
    rows = [deepcopy(item["row"]) for item in prepared]
    total, completed, created, blocked = sum(len(row["targets"]) for row in rows), 0, {}, {}
    def notify(phase, current=""):
        if progress is not None:
            try:
                progress({"completed": completed, "total": total, "current": current, "phase": phase})
            except Exception:
                pass
    notify("preflight")
    # Validate every chosen root and planned leaf before the first write.
    for target, root in roots.items():
        fd = None
        try:
            fd, _ = _open_root(root.path, root)
            if fd is not None:
                for row in rows:
                    if _collisions(fd, row["filename"]):
                        blocked[target, row["filename"]] = "目标文件已变化，请重新预览"
        except (OSError, ValueError) as error:
            for row in rows:
                blocked[target, row["filename"]] = str(error)
        finally:
            if fd is not None:
                os.close(fd)
    for row, item in zip(rows, prepared):
        for target in row["targets"]:
            try:
                error = blocked.get((target["target"], row["filename"]))
                if error:
                    raise ValueError(error)
                if sha256(item["raw"]).hexdigest() != item["sha256"]:
                    raise ValueError("导出字节已变化，请重新预览")
                _publish(roots[target["target"]], created, row["filename"], item["raw"], item["sha256"])
                target.update(status="written", applied=True)
            except Exception as error:
                target.update(status="failed", applied=getattr(error, "applied", False), error=str(error))
            completed += 1
            notify("writing", target["path"])
    notify("complete")
    return {"ok": all(target["status"] == "written" for row in rows for target in row["targets"]),
            "rows": rows, "completed": completed, "total": total}
