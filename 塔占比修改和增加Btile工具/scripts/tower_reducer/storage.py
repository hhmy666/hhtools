"""Durable, recoverable local jobs and verified artifact capabilities.

The repository root is normally <tool>/运行数据/任务. Only this module can
register an artifact: disk flags alone never establish verification. Downloads
recheck the saved source, output and typed JSON plan using the independent
verifier. No pickle, arbitrary class loading or arbitrary download path exists.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal, DecimalException
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from types import MappingProxyType

from .distribution import DistributionOptions, DistributionPlan
from .inspection import InspectionIssue
from .naming import validate_template_id
from .placement import LayerCapacity, Placement, PlacementOptions, PlacementPlan
from .rewrite import RewritePlan
from .increase import IncreasePlan, TowerAddition
from .ordinary_add import OrdinaryAddPlan, OrdinaryAddition
from .translation import TranslationPlan
from .manual_edit import ManualEditPlan, ManualTile, TowerRemovalPlan, initial_plan
from .template_reader import decode_template
from .verification import verify_output


ACTIVE_STATES = frozenset(('created', 'queued', 'running', 'validating', 'generating', 'generated', 'verifying', 'exporting'))
JOB_PATTERN = re.compile(r'job-[0-9a-f]{32}\Z')
KEY_PATTERN = re.compile(r'tpl-[0-9a-f]{32}\Z')
ARTIFACT_PATTERN = re.compile(r'art-[0-9a-f]{32}\Z')
_PLAN_TYPES = {cls.__name__: cls for cls in (RewritePlan, DistributionOptions, DistributionPlan,
              Placement, PlacementOptions, PlacementPlan, LayerCapacity, InspectionIssue,
              IncreasePlan, TowerAddition, OrdinaryAddPlan, OrdinaryAddition, TranslationPlan, ManualEditPlan, ManualTile, TowerRemovalPlan)}
_ROOT_LOCKS = {}
_ROOT_LOCKS_GUARD = threading.Lock()
VERIFIED_STATES = frozenset(('verified', 'exported', 'succeeded'))


class FileOps:
    """Narrow fault/coordination hook. Raising aborts the current operation.

    Events: write, fsync_file, replace, fsync_dir, registration_readback,
    export_readback, export_before_commit. Hooks run before final safety checks.
    """
    def checkpoint(self, stage, path):
        pass


def digest(raw):
    return sha256(raw).hexdigest()


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute path without traversal required')
    return path


def open_directory(value, create=False, writable=False):
    """Return the fd obtained by ONE no-follow component walk; caller owns it."""
    path = absolute_path(value)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('directory component is not a real directory')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (info.st_dev, info.st_ino):
                os.close(child)
                raise ValueError('directory changed while opening')
            os.close(fd)
            fd = child
        if writable and not os.fstat(fd).st_mode & 0o222:
            raise ValueError('directory is not writable')
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def directory_fd(path, create=False, writable=False):
    fd = open_directory(path, create=create, writable=writable)
    try:
        yield fd
    finally:
        os.close(fd)


def safe_directory(value, create=False, writable=False):
    with directory_fd(value, create, writable):
        return absolute_path(value)


def directory_identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def assert_directory_identity(path, expected_fd):
    with directory_fd(path) as current:
        if directory_identity(current) != directory_identity(expected_fd):
            raise ValueError('directory changed since it was opened')


def directory_ancestry(value):
    """Physical ancestry plus unresolved tail, without following any links.

    Existing aliases (including case aliases on macOS) share dev/ino. Missing
    components are relative to the last existing identity, not an absolute
    spelling. Missing tails are case-folded only when an existing component
    proves case-insensitive lookup on the same device; sensitive volumes are
    never globally lowercased.
    """
    path = absolute_path(value)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    identities = [directory_identity(fd)]
    case_insensitive = False
    try:
        for index, part in enumerate(path.parts[1:], 1):
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                tail = path.parts[index:]
                return tuple(identities), tuple(part.casefold() for part in tail) if case_insensitive else tail
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('directory component is not a real directory')
            parent_device = os.fstat(fd).st_dev
            if info.st_dev != parent_device:
                case_insensitive = False
            elif part.swapcase() != part:
                try:
                    alias = os.stat(part.swapcase(), dir_fd=fd, follow_symlinks=False)
                    case_insensitive = case_insensitive or (alias.st_dev, alias.st_ino) == (info.st_dev, info.st_ino)
                except FileNotFoundError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if directory_identity(child) != (info.st_dev, info.st_ino):
                os.close(child)
                raise ValueError('directory changed while opening')
            os.close(fd); fd = child
            identities.append(directory_identity(fd))
        return tuple(identities), ()
    finally:
        os.close(fd)


def directories_overlap(first, second):
    first_chain, first_tail = directory_ancestry(first)
    second_chain, second_tail = directory_ancestry(second)
    if not first_tail and first_chain[-1] in second_chain:
        return True
    if not second_tail and second_chain[-1] in first_chain:
        return True
    if first_chain[-1] == second_chain[-1] and first_tail and second_tail:
        return first_tail[:len(second_tail)] == second_tail or second_tail[:len(first_tail)] == first_tail
    return False


def read_regular_at(parent_fd, name):
    if not isinstance(name, str) or Path(name).name != name or name in ('', '.', '..'):
        raise ValueError('regular file basename required')
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('expected regular file')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError('file changed while opening')
        raw = stream.read()
        after = os.fstat(stream.fileno())
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('file changed while reading')
        return raw


def read_regular(path):
    if isinstance(path, PreparedFile):
        return read_regular_at(path.parent_fd, path.name)
    path = absolute_path(path)
    with directory_fd(path.parent) as parent:
        return read_regular_at(parent, path.name)


def fsync_directory(path, ops=None):
    ops = ops or FileOps()
    ops.checkpoint('fsync_dir', Path(path))
    with directory_fd(path) as fd:
        os.fsync(fd)


@dataclass
class PreparedFile:
    """Own the opened directory for the entire prepare/commit/cleanup lifetime."""
    path: Path
    parent_fd: int
    applied: bool = False

    @property
    def name(self):
        return self.path.name

    @property
    def parent(self):
        return self.path.parent

    def __fspath__(self):
        return str(self.path)

    def set_readonly(self):
        fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
        try:
            os.fchmod(fd, 0o400)
            os.fsync(fd)
        finally:
            os.close(fd)


def prepare_file(target, raw, ops=None, parent_fd=None):
    """Create a unique same-directory temporary file, fsync, return its path."""
    target = absolute_path(target)
    ops = ops or FileOps()
    parent = open_directory(target.parent) if parent_fd is None else os.dup(parent_fd)
    temp = PreparedFile(target.parent / ('.tmp-' + secrets.token_hex(16)), parent)
    try:
        fd = os.open(temp.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                ops.checkpoint('write', temp)
                stream.write(raw)
                stream.flush()
                ops.checkpoint('fsync_file', temp)
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(temp.name, dir_fd=parent)
            raise
        return temp
    except BaseException:
        os.close(parent)
        raise


def discard_temp(temp):
    if temp is None or temp.parent_fd < 0:
        return
    try:
        os.unlink(temp.name, dir_fd=temp.parent_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(temp.parent_fd)
        temp.parent_fd = -1


def commit_file(temp, target, ops=None, before_replace=None, *, overwrite=True):
    ops = ops or FileOps()
    target = absolute_path(target)
    if temp.parent != target.parent or not temp.name.startswith('.tmp-'):
        raise ValueError('temporary file must belong to the destination directory')
    ops.checkpoint('replace', target)
    parent = temp.parent_fd
    assert_directory_identity(target.parent, parent)
    if before_replace is not None:
        before_replace()
    temporary_info = os.stat(temp.name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(temporary_info.st_mode):
        raise ValueError('temporary file is not a regular file')
    try:
        info = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('target is not a regular file')
    except FileNotFoundError:
        pass
    if overwrite:
        os.replace(temp.name, target.name, src_dir_fd=parent, dst_dir_fd=parent)
    else:
        # A hard-link publishes complete bytes atomically and fails with EEXIST
        # even if another process wins after the final evidence check.
        os.link(temp.name, target.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        temp.applied = True
        os.unlink(temp.name, dir_fd=parent)
    temp.applied = True
    ops.checkpoint('fsync_dir', target.parent)
    os.fsync(parent)


def atomic_write_bytes(path, raw, ops=None, readonly=False):
    temp = prepare_file(path, raw, ops)
    try:
        if readonly:
            temp.set_readonly()
        commit_file(temp, Path(path), ops)
    finally:
        discard_temp(temp)


def atomic_write_json(path, value, ops=None):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    atomic_write_bytes(path, raw, ops)


def audit_json(value):
    """Safe descriptive JSON; unknown runtime types become a type-only summary."""
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float:
        return value if value == value and abs(value) != float('inf') else {'unsupported_type': 'nonfinite_float'}
    if type(value) is Decimal:
        return {'decimal': str(value)}
    if type(value) in (tuple, list):
        return [audit_json(item) for item in value]
    if type(value) in (dict, MappingProxyType):
        return {str(key): audit_json(item) for key, item in value.items() if type(key) in (str, int)}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: audit_json(getattr(value, f.name)) for f in fields(value)}
    return {'unsupported_type': type(value).__name__}


def _pack(value):
    if type(value).__name__ in _PLAN_TYPES and _PLAN_TYPES[type(value).__name__] is type(value):
        return {'record': type(value).__name__, 'fields': {f.name: _pack(getattr(value, f.name)) for f in fields(value)}}
    if type(value) in (list, tuple):
        return {'tuple': [_pack(item) for item in value]}
    if type(value) in (dict, MappingProxyType):
        return {'mapping': [[_pack(key), _pack(item)] for key, item in value.items()]}
    if type(value) is Decimal:
        return {'decimal': str(value)}
    if type(value) is bytes:
        return {'bytes_hex': value.hex()}
    if type(value) in (str, int, float, bool, type(None)):
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError('unsupported persisted plan value')


def _unpack(value, depth=0):
    """Every malformed persisted tag is a ValueError, never a batch exception."""
    try:
        return _unpack_value(value, depth)
    except (DecimalException, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError('invalid persisted plan encoding') from exc


def _unpack_value(value, depth):
    if depth > 80:
        raise ValueError('persisted plan nesting limit exceeded')
    child = lambda item: _unpack(item, depth+1)
    if type(value) is not dict:
        if type(value) not in (str, int, float, bool, type(None)):
            raise ValueError('invalid plan value')
        json.dumps(value, allow_nan=False)
        return value
    if set(value) == {'record', 'fields'} and type(value['record']) is str and value['record'] in _PLAN_TYPES:
        cls = _PLAN_TYPES[value['record']]
        stored = value['fields']
        if type(stored) is not dict:
            raise ValueError('invalid persisted plan fields')
        expected = {f.name for f in fields(cls)}
        if cls is ManualEditPlan and expected-{'translation','quarter_turns'}<=set(stored)<=expected:
            stored=dict(stored)
            stored.setdefault('translation',{'tuple':[0,0]})
            stored.setdefault('quarter_turns',0)
        # Only the exact old IncreasePlan shape receives its historical
        # symmetry policy. New plans persist an explicit validated version;
        # partial/unknown fields and other record types stay strict.
        if cls is IncreasePlan and set(stored) == expected - {'symmetry_version'}:
            stored = dict(stored, symmetry_version='legacy-x-v1')
        if cls is PlacementPlan and set(stored)==expected-{'original_z_offset'}:
            stored=dict(stored,original_z_offset=0)
        # Pre-conversion artifacts contain the complete original record but
        # neither population field. Revalidate them under the legacy policy;
        # never accept one missing field, unknown fields or missing old fields.
        legacy_fields = expected - {'removed_counts', 'final_counts', 'replenishment_ids'}
        legacy = cls is DistributionPlan and set(stored) in (legacy_fields, expected-{'replenishment_ids'})
        # Older options predate the lower bound and explicit fill selection.
        # Omitted new fields use dataclass defaults; old fields and unknown
        # fields remain strict so recovery cannot widen unrelated permissions.
        if cls is DistributionOptions:
            legacy = (expected - {'min_ratio', 'replenish_type'} <= set(stored) <= expected)
        if set(stored) != expected and not legacy:
            raise ValueError('invalid persisted plan fields')
        return cls(**{key: child(item) for key, item in stored.items()})
    if set(value) == {'tuple'} and type(value['tuple']) is list:
        return tuple(child(item) for item in value['tuple'])
    if set(value) == {'mapping'} and type(value['mapping']) is list:
        result = {}
        for pair in value['mapping']:
            if type(pair) is not list or len(pair) != 2:
                raise ValueError('invalid mapping pair')
            key, item = child(pair[0]), child(pair[1])
            if key in result:
                raise ValueError('duplicate mapping key')
            result[key] = item
        return result
    if set(value) == {'decimal'} and type(value['decimal']) is str:
        number = Decimal(value['decimal'])
        if not number.is_finite():
            raise ValueError('nonfinite decimal')
        return number
    if set(value) == {'bytes_hex'} and type(value['bytes_hex']) is str:
        return bytes.fromhex(value['bytes_hex'])
    raise ValueError('unknown persisted plan record')


def validate_state(state, job_id):
    """Validate container contracts before any recovery code consumes them.

    Artifact contents are validated separately per template so one bad plan
    does not make healthy siblings unavailable.
    """
    if type(state) is not dict or state.get('version') != 1 or state.get('job_id') != job_id:
        raise ValueError('invalid job state root')
    for name in ('templates', 'artifacts', 'parameters'):
        if type(state.get(name)) is not dict:
            raise ValueError('invalid job mapping: '+name)
    for name in ('logs', 'exports'):
        if type(state.get(name)) is not list:
            raise ValueError('invalid job sequence: '+name)
    if type(state.get('status')) is not str or type(state.get('revision')) is not int or state['revision'] < 0:
        raise ValueError('invalid job status/revision')
    state.setdefault('history_artifacts', {})
    if type(state['history_artifacts']) is not dict:
        raise ValueError('invalid history artifacts')
    for key, row in state['templates'].items():
        if type(key) is not str or not KEY_PATTERN.fullmatch(key) or type(row) is not dict or row.get('key') != key:
            raise ValueError('invalid template row')
        for name in ('status', 'stage', 'source_sha256'):
            if type(row.get(name)) is not str:
                raise ValueError('invalid template text: '+name)
        if row.get('source_template_id') is not None and type(row['source_template_id']) is not str:
            raise ValueError('invalid source template id')
        for name in ('parameters', 'options', 'naming'):
            if type(row.get(name)) is not dict:
                raise ValueError('invalid template mapping: '+name)
        for name in ('original_names', 'source_parents', 'logs', 'output_artifacts', 'attempts'):
            if type(row.get(name)) is not list:
                raise ValueError('invalid template sequence: '+name)
        if any(type(name) is not str for name in row['original_names']+row['output_artifacts']):
            raise ValueError('invalid template names/artifacts')
        if any(parent is not None and type(parent) is not str for parent in row['source_parents']):
            raise ValueError('invalid source parents')
        for parent in row['source_parents']:
            if parent is not None:
                absolute_path(parent)
        if any(type(attempt) is not dict or type(attempt.get('output_artifacts')) is not list for attempt in row['attempts']):
            raise ValueError('invalid attempt history')
        row.setdefault('attempt_id', len(row['attempts']))
        if type(row['attempt_id']) is not int or row['attempt_id'] < 0:
            raise ValueError('invalid attempt id')
    return state


@dataclass(frozen=True)
class InputSnapshot:
    raw: bytes
    original_name: str
    source_parent: object = None


class AnalysisJob:
    LOG_LIMIT = 200

    def __init__(self, repository, state):
        self.repository = repository
        self.job_id = state['job_id']
        self._state = state
        self.lock = repository.lock

    @property
    def directory(self):
        self.repository._check_root()
        return safe_directory(self.repository.root / self.job_id)

    def snapshot(self):
        with self.lock:
            self._refresh()
            return deepcopy(self._state)

    def _refresh(self):
        self._state = validate_state(json.loads(read_regular(self.directory/'state.json')), self.job_id)

    def _save(self, state):
        state['revision'] = self._state.get('revision', 0) + 1
        state['updated_at_ns'] = time.time_ns()
        atomic_write_json(self.directory/'state.json', state, self.repository.file_ops)
        self._state = state

    def _row(self, key):
        self._refresh()
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise ValueError('invalid template key')
        if key not in self._state['templates']:
            raise KeyError('template_not_found')
        return self._state['templates'][key]

    def set_status(self, status):
        if status not in ACTIVE_STATES | {'interrupted', 'succeeded', 'partial_failed', 'failed', 'no_op'}:
            raise ValueError('invalid job status')
        with self.lock:
            state = self.snapshot(); state['status'] = status; self._save(state)

    def update_template(self, key, **updates):
        """API-safe progress/config updates cannot grant verification or artifacts."""
        allowed = {'status', 'stage', 'parameters', 'options', 'naming', 'error', 'progress', 'plan_summary', 'preflight'}
        if set(updates) - allowed or updates.get('status') in ('verified', 'exported', 'succeeded'):
            raise ValueError('protected template state')
        if 'status' in updates and updates['status'] not in ACTIVE_STATES | {'interrupted', 'failed', 'no_op', 'configured', 'planned', 'imported'}:
            raise ValueError('invalid template status')
        with self.lock:
            self._row(key)
            state = self.snapshot(); state['templates'][key].update(audit_json(updates)); self._save(state)

    def add_log(self, message, key=None):
        with self.lock:
            if key is not None:
                self._row(key)
            state = self.snapshot()
            entry = {'template_key': key, 'message': str(message)[:2000]}
            state['logs'] = (state['logs'] + [entry])[-self.LOG_LIMIT:]
            if key is not None:
                row = state['templates'][key]
                row['logs'] = (row['logs'] + [entry])[-self.LOG_LIMIT:]
            self._save(state)

    def start_attempt(self, key, parameters=None, naming=None):
        """Archive the prior attempt; a worker can then update only this row."""
        with self.lock:
            self._row(key)
            state = self.snapshot(); row = state['templates'][key]
            row['attempts'].append({name: deepcopy(row.get(name)) for name in
                                    ('status', 'stage', 'parameters', 'options', 'naming', 'error',
                                     'verification', 'plan_summary', 'output_artifacts', 'logs', 'attempt_id')})
            for artifact_id in row['output_artifacts']:
                if artifact_id in state['artifacts']:
                    state['history_artifacts'][artifact_id] = state['artifacts'].pop(artifact_id)
            row.update(status='queued', stage='queued', error=None, verification=None, logs=[],
                       output_artifacts=[], attempt_id=row['attempt_id']+1)
            if parameters is not None:
                row['parameters'] = audit_json(parameters)
            if naming is not None:
                row['naming'] = audit_json(naming)
            self._save(state)

    def read_source(self, key):
        row = self._row(key)
        raw = read_regular(self.directory/'templates'/key/'source.bytes')
        if digest(raw) != row['source_sha256']:
            raise ValueError('source snapshot SHA mismatch')
        return raw

    def renamed_output_bytes(self, artifact_id, new_id):
        """Read-only identity variant; preserve and independently verify geometry."""
        raw = self.read_artifact(artifact_id)
        record = self.snapshot()['artifacts'][artifact_id]
        plan = _unpack(record['plan'])
        source = self.read_source(record['template_key'])
        validate_template_id(new_id, decode_template(source).template_id)
        from .rewrite import rewrite_template
        output = rewrite_template(source, plan, new_id)
        if not verify_output(source, output, plan, new_id).ok:
            raise ValueError('renamed output failed independent verification')
        return output, plan

    def activate_renamed_output(self, artifact_id, new_id, base_id):
        """Keep later download/sync identity consistent with a versioned export."""
        with self.lock:
            record = self.snapshot()['artifacts'][artifact_id]
            key = record['template_key']
            summary = self.snapshot()['templates'][key]['plan_summary']
            output, plan = self.renamed_output_bytes(artifact_id, new_id)
            artifact = self.register_output(key, output, plan, new_id,
                                            undo_previous=record.get('undo_previous'))
            if artifact is None:
                raise ValueError('renamed output could not be registered')
            summary.update(new_id=new_id, output_sha256=artifact['sha256'], export_base_id=base_id)
            self.update_template(key, plan_summary=summary)
            return artifact

    def register_output(self, key, output_bytes, plan, new_id, *, undo_previous=None):
        """Return artifact metadata or None; only this template fails on rejection."""
        with self.lock:
            row = self._row(key)
            temp = None
            verification = None
            try:
                if undo_previous is not None:
                    predecessor = (self._state['artifacts'].get(undo_previous)
                                   or self._state['history_artifacts'].get(undo_previous))
                    if (not predecessor or predecessor.get('template_key') != key
                            or predecessor.get('attempt_id') != row['attempt_id']
                            or predecessor.get('source_sha256') != row['source_sha256']):
                        raise ValueError('invalid undo predecessor')
                validate_template_id(new_id, row['source_template_id'])
                if type(output_bytes) is not bytes or type(plan) not in (RewritePlan, IncreasePlan, OrdinaryAddPlan, TranslationPlan, ManualEditPlan):
                    raise ValueError('immutable output bytes and typed rewrite plan required')
                for artifact in self.artifact_records():
                    if artifact['template_key'] != key and artifact['new_id'].casefold() == new_id.casefold():
                        raise ValueError('batch output name conflict')
                source = self.read_source(key)
                directory = safe_directory(self.directory/'templates'/key/'outputs', create=True)
                artifact_id = 'art-' + secrets.token_hex(16)
                destination = directory/(artifact_id+'.bytes')
                temp = prepare_file(destination, output_bytes, self.repository.file_ops)
                self.repository.file_ops.checkpoint('registration_readback', temp)
                reread = read_regular(temp)
                verification = verify_output(source, reread, plan, new_id)
                if digest(reread) != digest(output_bytes) or not verification.ok:
                    raise ValueError('readback verification failed')
                packed = _pack(plan)
                # Prove the persisted representation can itself verify after restart.
                if not verify_output(source, reread, _unpack(packed), new_id).ok:
                    raise ValueError('persisted plan cannot verify output')
                temp.set_readonly()
                def unchanged_output():
                    checked = read_regular(temp)
                    if digest(checked) != digest(reread) or not verify_output(self.read_source(key), checked, plan, new_id).ok:
                        raise ValueError('verified temporary artifact changed before commit')
                commit_file(temp, destination, self.repository.file_ops, before_replace=unchanged_output)
                artifact = {'id': artifact_id, 'template_key': key, 'new_id': new_id,
                            'filename': new_id+'_m.bytes', 'relative_path': str(destination.relative_to(self.directory)),
                            'sha256': digest(reread), 'size': len(reread), 'verified': True,
                            'source_sha256': digest(source), 'plan': packed, 'attempt_id': row['attempt_id'],
                            'undo_previous': undo_previous}
                state = self.snapshot(); current = state['templates'][key]
                previous_summary = deepcopy(current.get('plan_summary') or {})
                option_plan = initial_plan(plan)
                if type(option_plan) is OrdinaryAddPlan:
                    plan_options = {'mode':'ordinary_add', 'ordinary_add':option_plan.options}
                elif type(option_plan) is IncreasePlan:
                    plan_options = {'mode':'increase', 'increase':option_plan.options}
                elif type(option_plan) is TowerRemovalPlan:
                    plan_options = deepcopy(current.get('options') or {})
                else:
                    plan_options = {'distribution':option_plan.distribution_options,'placement':option_plan.placement_options}
                current.update(status='verified', stage='verified', error=None, naming={'new_id': new_id},
                               verification=audit_json(verification), plan_summary=audit_json(plan),
                               options=audit_json(plan_options))
                for previous in current['output_artifacts']:
                    if previous in state['artifacts']:
                        state['history_artifacts'][previous] = state['artifacts'].pop(previous)
                        state['history_artifacts'][previous]['plan_summary'] = previous_summary
                current['output_artifacts'] = [artifact_id]
                state['artifacts'][artifact_id] = artifact
                self._save(state)
                return deepcopy(artifact)
            except Exception as exc:
                state = self.snapshot(); current = state['templates'][key]
                current.update(status='failed', stage='verification', error={'code': 'output_verification_failed',
                               'stage': 'verification', 'message': str(exc), 'recommendations': ['检查生成计划和输出后单独重跑该模板']})
                current['verification'] = audit_json(verification)
                self._save(state)
                return None
            finally:
                discard_temp(temp)

    def artifact_records(self):
        """Metadata only. Consumers must call read_artifact before using bytes."""
        with self.lock:
            state = self.snapshot()
            return tuple(deepcopy(state['artifacts'][artifact_id]) for row in state['templates'].values()
                         if row['status'] in VERIFIED_STATES for artifact_id in row['output_artifacts']
                         if artifact_id in state['artifacts'])

    def read_artifact(self, artifact_id, attempt_id=None):
        return self._read_artifact(artifact_id, attempt_id, require_verified=True)

    def undo_predecessor(self, current_id):
        """Only resolve the server-recorded direct predecessor of a live capability."""
        with self.lock:
            self.read_artifact(current_id)
            current = self._state['artifacts'][current_id]
            previous_id = current.get('undo_previous')
            if previous_id is None:
                return None
            raw = self._read_artifact(previous_id, undo_current=current_id)
            record = self._state['history_artifacts'][previous_id]
            if type(record.get('plan_summary', {})) is not dict:
                raise ValueError('invalid undo summary')
            return deepcopy(record), raw

    def can_undo(self, current_id):
        try:
            return self.undo_predecessor(current_id) is not None
        except (ValueError, KeyError, OSError, TypeError):
            return False

    def _read_artifact(self, artifact_id, attempt_id=None, require_verified=True, *, undo_current=None):
        if not isinstance(artifact_id, str) or not ARTIFACT_PATTERN.fullmatch(artifact_id):
            raise ValueError('invalid artifact id')
        with self.lock:
            self._refresh()
            if attempt_id is not None and (type(attempt_id) is not int or attempt_id < 0):
                raise ValueError('invalid history attempt id')
            records = self._state['artifacts'] if attempt_id is None and undo_current is None else self._state['history_artifacts']
            record = records.get(artifact_id)
            if record is None:
                raise KeyError('artifact_not_found')
            if type(record) is not dict or any(type(record.get(name)) is not str for name in
                    ('id', 'template_key', 'new_id', 'relative_path', 'filename', 'sha256', 'source_sha256')):
                raise ValueError('invalid artifact manifest fields')
            key = record['template_key']; row = self._row(key)
            if undo_current is not None:
                current = self._state['artifacts'].get(undo_current) or {}
                allowed = (row['status'] in VERIFIED_STATES and row['output_artifacts'] == [undo_current]
                           and current.get('undo_previous') == artifact_id
                           and current.get('template_key') == key
                           and current.get('attempt_id') == record.get('attempt_id') == row['attempt_id']
                           and current.get('source_sha256') == record.get('source_sha256') == row['source_sha256'])
            elif attempt_id is None:
                allowed = ((not require_verified or row['status'] in VERIFIED_STATES)
                           and artifact_id in row['output_artifacts']
                           and record.get('attempt_id', 0) == row['attempt_id'])
            else:
                allowed = record.get('attempt_id', 0) == attempt_id and any(
                    item.get('attempt_id') == attempt_id and artifact_id in item['output_artifacts'] for item in row['attempts'])
            validate_template_id(record['new_id'], row['source_template_id'])
            expected_path = 'templates/'+key+'/outputs/'+artifact_id+'.bytes'
            if (record['id'] != artifact_id or record['relative_path'] != expected_path or record.get('verified') is not True
                    or record['filename'] != record['new_id']+'_m.bytes' or not allowed):
                raise ValueError('invalid artifact manifest')
            raw = read_regular(self.directory/expected_path)
            source = self.read_source(key)
            if digest(raw) != record['sha256'] or digest(source) != record['source_sha256'] or len(raw) != record['size']:
                raise ValueError('artifact SHA or size mismatch')
            report = verify_output(source, raw, _unpack(record['plan']), record['new_id'])
            if not report.ok:
                raise ValueError('artifact independent verification failed')
            return raw

    def save_backup(self, raw, metadata):
        directory = safe_directory(self.directory/'backups'/('backup-'+secrets.token_hex(16)), create=True)
        atomic_write_bytes(directory/'original.bytes', raw, self.repository.file_ops, readonly=True)
        if digest(read_regular(directory/'original.bytes')) != digest(raw):
            raise ValueError('backup readback SHA mismatch')
        result = dict(metadata, relative_path=str((directory/'original.bytes').relative_to(self.directory)), sha256=digest(raw))
        atomic_write_json(directory/'metadata.json', result, self.repository.file_ops)
        return result

    def record_export(self, rows):
        with self.lock:
            state = self.snapshot()
            state['exports'] = (state['exports'] + [audit_json(rows)])[-50:]
            self._save(state)


class JobRepository:
    def __init__(self, root, file_ops=None):
        self.root = safe_directory(root, create=True)
        with directory_fd(self.root) as fd:
            self._identity = directory_identity(fd)
        self.file_ops = file_ops or FileOps()
        self._jobs = {}
        with _ROOT_LOCKS_GUARD:
            self.lock = _ROOT_LOCKS.setdefault(self._identity, threading.RLock())
        self.diagnostics = []

    def _check_root(self):
        with directory_fd(self.root) as fd:
            if directory_identity(fd) != self._identity:
                raise ValueError('repository root changed')

    def create(self, inputs, parameters=None):
        with self.lock:
            self._check_root()
            job_id = 'job-' + secrets.token_hex(16)
            directory = self.root/job_id
            with directory_fd(self.root) as root_fd:
                if directory_identity(root_fd) != self._identity:
                    raise ValueError('repository root changed')
                os.mkdir(job_id, mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            state = {'version': 1, 'job_id': job_id, 'status': 'queued', 'revision': 0, 'updated_at_ns': time.time_ns(),
                     'parameters': audit_json(parameters or {}), 'templates': {}, 'artifacts': {}, 'history_artifacts': {}, 'logs': [], 'exports': []}
            by_sha = {}
            for item in inputs:
                if not isinstance(item, InputSnapshot) or type(item.raw) is not bytes:
                    raise ValueError('inputs must be immutable InputSnapshot bytes')
                parent = str(absolute_path(item.source_parent)) if item.source_parent is not None else None
                if parent is not None:
                    directory_ancestry(parent)
                checksum = digest(item.raw)
                if checksum in by_sha:
                    row = state['templates'][by_sha[checksum]]
                    row['original_names'].append(str(item.original_name))
                    if parent not in row['source_parents']:
                        row['source_parents'].append(parent)
                    continue
                key = 'tpl-' + secrets.token_hex(16); by_sha[checksum] = key
                folder = safe_directory(directory/'templates'/key, create=True)
                atomic_write_bytes(folder/'source.bytes', item.raw, self.file_ops, readonly=True)
                source_id, error = None, None
                try:
                    source_id = decode_template(item.raw).template_id
                except (ValueError, TypeError) as exc:
                    error = {'code': 'invalid_template', 'stage': 'import', 'message': str(exc)}
                state['templates'][key] = {'key': key, 'source_sha256': checksum, 'source_template_id': source_id,
                    'original_names': [str(item.original_name)], 'source_parents': [parent],
                    'status': 'failed' if error else 'queued', 'stage': 'import', 'parameters': audit_json(parameters or {}),
                    'options': {}, 'naming': {}, 'error': error, 'logs': [], 'verification': None,
                    'output_artifacts': [], 'attempts': [], 'attempt_id': 0}
            atomic_write_json(directory/'state.json', state, self.file_ops)
            fsync_directory(self.root, self.file_ops)
            job = AnalysisJob(self, state); self._jobs[job_id] = job
            return job

    def get(self, job_id):
        if not isinstance(job_id, str) or not JOB_PATTERN.fullmatch(job_id):
            raise ValueError('invalid job id')
        with self.lock:
            self._check_root()
            try:
                directory = safe_directory(self.root/job_id)
                if job_id not in self._jobs:
                    state = validate_state(json.loads(read_regular(directory/'state.json')), job_id)
                    # The manifest/key schema is job-wide. Each template's disk
                    # directory is checked by its own read/recovery boundary so
                    # a missing or linked template cannot hide healthy siblings.
                    self._jobs[job_id] = AnalysisJob(self, state)
                return self._jobs[job_id]
            except FileNotFoundError as exc:
                raise KeyError('job_not_found') from exc

    load = get

    def discover(self, limit=200, scan_limit=1000):
        """Startup only: mark active work interrupted; never resume execution."""
        if type(limit) is not int or type(scan_limit) is not int or min(limit, scan_limit) < 1:
            raise ValueError('positive discovery limits required')
        self._check_root()
        self.diagnostics = []
        def diagnose(value):
            if len(self.diagnostics) < 100:
                self.diagnostics.append(value)
        candidates = []
        with directory_fd(self.root) as root_fd, os.scandir(root_fd) as entries:
            for scanned, entry in enumerate(entries):
                if scanned >= scan_limit:
                    diagnose({'code': 'scan_truncated', 'scan_limit': scan_limit,
                              'recovery': 'unscanned jobs are not resumed; request explicitly or increase scan_limit'})
                    break
                name = None
                try:
                    name = entry.name
                    if JOB_PATTERN.fullmatch(name):
                        candidates.append((entry.stat(follow_symlinks=False).st_mtime_ns, name))
                except OSError as exc:
                    diagnose({'job_id': name, 'code': 'invalid_job_entry', 'message': str(exc)})
        candidates.sort(reverse=True)
        result = []
        for _, name in candidates:
            if len(result) >= limit:
                diagnose({'code': 'result_truncated', 'limit': limit, 'recent_scope': 'bounded scanned entries'})
                break
            try:
                job = self.get(name)
                with job.lock:
                    state = job.snapshot(); changed = False
                    invalid_current = False
                    if state['status'] in ACTIVE_STATES:
                        state['status'] = 'interrupted'; changed = True
                    for key, row in state['templates'].items():
                        try:
                            job.read_source(key)
                            for artifact_id in row['output_artifacts']:
                                job._read_artifact(artifact_id, require_verified=False)
                        except (ValueError, KeyError, OSError, TypeError) as exc:
                            for artifact_id in row['output_artifacts']:
                                record = state['artifacts'].pop(artifact_id, None)
                                if record is not None:
                                    row.setdefault('unavailable_artifacts', {})[artifact_id] = record
                            row.update(status='failed', output_artifacts=[], error={'code': 'recovery_verification_failed',
                                       'stage': 'recovery', 'message': str(exc)})
                            changed = True
                            invalid_current = True
                        if row['status'] in ACTIVE_STATES:
                            row['interrupted_stage'] = row['status']; row['status'] = 'interrupted'; changed = True
                    if invalid_current:
                        state['status'] = 'partial_failed' if any(row['status'] in VERIFIED_STATES for row in state['templates'].values()) else 'failed'
                    if changed:
                        job._save(state)
                result.append(job)
            except (ValueError, KeyError, OSError, TypeError) as exc:
                diagnose({'job_id': name, 'code': 'invalid_job_state', 'message': str(exc)})
        return result
