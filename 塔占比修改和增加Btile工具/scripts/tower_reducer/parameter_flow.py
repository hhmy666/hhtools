"""HTTP-facing parameter flow. Only server-proven template counts may be used."""
from copy import deepcopy
from pathlib import Path
from datetime import datetime
import os
import re
import secrets
import threading
import json
import time

from .parameter_config import ParameterConfigService, _snapshot
from .settings import load_settings
from .storage import ARTIFACT_PATTERN, KEY_PATTERN, digest, read_regular, _unpack, atomic_write_json, safe_directory
from .template_reader import decode_template
from .verification import verify_output
from .inspection import inspect_template
from .protobuf_wire import replace_length_delimited


def _api_error(code, message, status=409):
    from .api import ApiError
    return ApiError(status, code, message)


def _request(payload, allowed, required=()):
    if type(payload) is not dict or set(payload) - set(allowed) or not set(required) <= set(payload):
        raise _api_error('invalid_request', '请求字段不符合接口规则', 400)


def _template_id(value):
    return type(value) is str and 0 < len(value) <= 180 and re.fullmatch(r'[A-Za-z0-9_-]+', value) is not None


def _counts(raw):
    doc = decode_template(raw)
    return doc.template_id, {'A': sum(t.tile_id == -10 for t in doc.tiles),
                             'B': sum(t.tile_id == -11 for t in doc.tiles)}


class SourceAmbiguity(ValueError):
    def __init__(self, identities, message='匹配到多个原模板，请选择原模板，或指定前后缀后重新计算。'):
        super().__init__(message)
        self.candidates = [{'original_template_id': identity, 'label': identity} for identity in sorted(set(identities))]


def _identity_digest(raw):
    """An identity copy may change only the effective internal ID field."""
    document = decode_template(raw)
    return digest(b''.join(replace_length_delimited(field, b'parameter-identity')
        if index == document.template_id_field_index else field.raw
        for index, field in enumerate(document.fields)))


def _mapped_operation(source_raw, output_raw):
    """Prove an unregistered increase using bytes, never a caller mode flag.

    An explicit ID mapping authorizes the ID change and ordinary tower
    additions only. Moving/recoloring old tiles requires a registered plan.
    This grants parameter calculation, not verified download/sync capability.
    """
    source, output = decode_template(source_raw), decode_template(output_raw)
    if len(output.tiles) <= len(source.tiles):
        # Independent source matching cannot inherit a previous plan's grant.
        # Retain every metadata/unknown field while allowing tile relocation.
        metadata = lambda doc: [field.raw for index, field in enumerate(doc.fields)
            if index != doc.template_id_field_index and field.number not in (6, 7)]
        if metadata(source) != metadata(output):
            raise ValueError('模板摘要与源结构不一致，未知字段或元数据发生变化，请使用已验收产物')
        return 'reduce'
    if len(output.fields) <= len(source.fields) or output.template_id == source.template_id:
        raise ValueError('增加模板缺少可验证的源新ID及追加结构')
    for index, field in enumerate(source.fields):
        expected = (replace_length_delimited(field, output.template_id.encode('utf-8'))
                    if index == source.template_id_field_index else field.raw)
        if output.fields[index].raw != expected:
            raise ValueError('增加模板修改了未经授权的原牌、原塔或未知字段；请通过生成任务验收')
    added_tops = set(range(len(source.fields), len(output.fields)))
    for field in output.fields[len(source.fields):]:
        if field.number not in (6, 7) or field.wire_type != 2:
            raise ValueError('增加模板只能追加普通牌与塔记录')
    added_tiles = [tile for tile in output.tiles if tile.top_field_index in added_tops]
    for tile in added_tiles:
        numbers = [field.number for field in tile.fields]
        if (tile.tile_id not in (-10, -11) or tile.debuff_data_index is not None
                or set(numbers) not in ({1, 2, 4}, {1, 2, 3, 4})
                or len(set(numbers)) != len(numbers)
                or any(field.wire_type != 0 for field in tile.fields)):
            raise ValueError('新增牌应为无特殊字段的普通 A/B 牌')
    for stack in output.stacks:
        if stack.top_field_index not in added_tops:
            continue
        numbers = [field.number for field in stack.fields]
        if (set(numbers) not in ({1, 2}, {1, 2, 3}) or len(set(numbers)) != len(numbers)
                or any(field.wire_type != 0 for field in stack.fields)
                or stack.direction not in (0, 1, 2, 3)):
            raise ValueError('新增塔记录含非法字段或方向')
    inspection = inspect_template(output)
    anchors = {(stack.x, stack.y) for stack in output.stacks}
    if (any((tile.x, tile.y) not in anchors for tile in added_tiles)
            or any(issue.severity == 'error' for issue in inspection.issues)):
        raise ValueError('新增牌未形成可验证的连续塔结构')
    return 'increase'


class ParameterFlow:
    def __init__(self, application):
        self.app = application
        self.service = ParameterConfigService(application.repository.root.parent)
        self.selections, self.calculations, self.plans = {}, {}, {}
        self.lock = threading.RLock()
        self.task_lock = threading.RLock()
        self.tasks = {}
        self.task_root = safe_directory(self.service.state_root / 'tasks', create=True)
        for path in sorted(self.task_root.glob('parameter-task-*.json'))[-128:]:
            try:
                saved = json.loads(read_regular(path).decode('utf-8'))
                task = saved['task']
                if (type(task) is not dict or type(task.get('task_id')) is not str
                        or not re.fullmatch(r'parameter-task-[0-9a-f]{32}', task['task_id'])
                        or path.name != task['task_id'] + '.json'
                        or type(task.get('request_id')) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', task['request_id'])
                        or type(saved.get('signature')) is not str or not re.fullmatch(r'[0-9a-f]{64}', saved['signature'])
                        or task.get('status') not in ('running', 'succeeded', 'failed')
                        or task.get('action') not in ('resolve', 'calculate', 'preview', 'commit')
                        or type(saved.get('payload')) is not dict):
                    continue
                restored = []
                for name, prefix in (('selections', 'selection'), ('calculations', 'calc'), ('plans', 'plan')):
                    cache = saved.get(name, {})
                    if type(cache) is not dict or any(type(key) is not str or not re.fullmatch(prefix + r'-[0-9a-f]{32}', key)
                        or type(value) is not dict or type(value.get('proofs')) is not list or type(value.get('entries')) is not list
                        for key, value in cache.items()):
                        raise ValueError('invalid persisted parameter context')
                    restored.append(cache)
                if task['status'] == 'running':
                    if task['action'] == 'commit':
                        saved['resume_commit'] = True
                    else:
                        task.update(status='failed', phase='interrupted', error={
                            'code': 'parameter_interrupted', 'message': '本地服务已重启，本次处理已中断，请重新计算。'})
                self.tasks[task['task_id']] = saved
                for cache, values in zip((self.selections, self.calculations, self.plans), restored):
                    cache.update(values)
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def task_status(self, task_id):
        recover = None
        with self.task_lock:
            saved = self.tasks.get(task_id)
            if saved is None:
                raise _api_error('parameter_task_not_found', '未找到处理记录，请重新计算。', 404)
            if saved.pop('resume_commit', False):
                recover = saved['payload']
        if recover is not None:
            self._launch_task(task_id, 'commit', recover)
        with self.task_lock:
            return deepcopy(saved['task'])

    def _task_contexts(self, saved, body):
        """Persist only proofs belonging to this operation, not the entire cache."""
        task = saved['task']
        result = task.get('result') or {}
        selection = task.get('selection') or {}
        keys = {'selections': selection.get('selection_id') or body.get('selection_id'),
                'calculations': result.get('calculation_id') or body.get('calculation_id'),
                'plans': result.get('plan_id') or body.get('plan_id')}
        with self.lock:
            for cache, name in ((self.selections, 'selections'), (self.calculations, 'calculations'), (self.plans, 'plans')):
                key = keys[name]
                if key in cache:
                    saved[name] = {key: deepcopy(cache[key])}

    def start(self, payload):
        _request(payload, ('request_id', 'action', 'payload'), ('request_id', 'action', 'payload'))
        request_id, action, body = payload['request_id'], payload['action'], payload['payload']
        if (type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id)
                or action not in ('resolve', 'calculate', 'preview', 'commit') or type(body) is not dict):
            raise _api_error('invalid_request', '参数处理请求不合法', 400)
        signature = digest(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())
        with self.task_lock:
            for saved in self.tasks.values():
                if saved['task']['request_id'] == request_id:
                    if saved['signature'] != signature:
                        raise _api_error('parameter_request_changed', '处理范围已变化，请重新计算。')
                    return deepcopy(saved['task'])
            if sum(saved['task']['status'] == 'running' for saved in self.tasks.values()) >= 4:
                raise _api_error('parameter_busy', '正在处理其他参数任务，请稍后重试。', 429)
            while len(self.tasks) >= 128:
                old = next((key for key, saved in self.tasks.items() if saved['task']['status'] != 'running'), None)
                if old is None:
                    break
                self.tasks.pop(old)
                (self.task_root / (old + '.json')).unlink(missing_ok=True)
            task_id = 'parameter-task-' + secrets.token_hex(16)
            task = {'task_id': task_id, 'request_id': request_id, 'action': action,
                    'status': 'running', 'phase': 'preparing', 'completed': 0, 'total': 0,
                    'overall_progress': {'completed': 0, 'total': 10000},
                    'current_filename': None, 'result': None, 'selection': None, 'error': None,
                    'started_at': time.time(), 'updated_at': time.time()}
            self.tasks[task_id] = {'task': task, 'signature': signature, 'payload': deepcopy(body)}
            self._task_contexts(self.tasks[task_id], body)
            atomic_write_json(self.task_root / (task_id + '.json'), self.tasks[task_id])

        self._launch_task(task_id, action, deepcopy(body))
        return self.task_status(task_id)

    def _launch_task(self, task_id, action, body):
        task = self.tasks[task_id]['task']
        # This is cumulative processing work, not an elapsed-time estimate.
        # A selected source has already been resolved; job sources need no
        # artifact-name index. Reserve the final 1% for durable result storage.
        source = body.get('source') if action == 'calculate' else body if action == 'resolve' else None
        phases = []
        if source is not None:
            if type(source) is not dict or source.get('mode') != 'job':
                phases.append('indexing')
            phases.append('resolving')
        if action != 'resolve':
            phases.extend(('verifying', {'calculate': 'calculating', 'preview': 'previewing', 'commit': 'committing'}[action]))
        previous = task.get('overall_progress') or {}
        task['overall_progress'] = {'completed': min(9900, max(0, previous.get('completed', 0))), 'total': 10000,
                                    'stage_count': len(phases)}

        def cumulative_progress(state):
            previous = state['overall_progress']['completed']
            if state.get('status') == 'succeeded':
                return {'completed': 10000, 'total': 10000, 'stage_count': len(phases)}
            phase = state.get('phase')
            if phase not in phases:
                return {'completed': previous, 'total': 10000, 'stage_count': len(phases)}
            total, completed = state.get('total', 0), state.get('completed', 0)
            fraction = min(1, max(0, completed / total)) if total > 0 else 0
            current = int(9900 * (phases.index(phase) + fraction) / len(phases))
            # Indexing may be re-entered from resolving to recognize a renamed
            # artifact. Its local counter must never reset overall progress.
            return {'completed': max(previous, min(9900, current)), 'total': 10000, 'stage_count': len(phases)}

        def progress(**update):
            with self.task_lock:
                task.update(update, updated_at=time.time())
                task['overall_progress'] = cumulative_progress(task)

        def run():
            terminal = None
            try:
                if action == 'calculate':
                    _request(body, ('source', 'selection_id', 'level_start', 'level_end', 'level_id_rule', 'description', 'preserve_level_prefix'))
                    calculation = dict(body)
                    source = calculation.pop('source', None)
                    if source is not None:
                        if 'selection_id' in calculation:
                            raise _api_error('invalid_request', '模板来源重复', 400)
                        selection = self.resolve(source, progress=progress)
                        progress(selection=selection)
                        calculation['selection_id'] = selection['selection_id']
                        if 'level_id_rule' not in calculation:
                            entries = [row for row in selection['entries'] if row['status'] != 'failed']
                            calculation['level_id_rule'] = ('moreB{原关卡ID}' if entries and all(row.get('template_mode') == 'ordinary_add' for row in entries)
                                else 'moreT{原关卡ID}' if entries and all(row.get('operation') == 'increase' for row in entries) else 'lessT{原关卡ID}')
                    result = self.calculate(calculation, progress=progress)
                elif action == 'resolve':
                    result = self.resolve(body, progress=progress)
                    progress(selection=result)
                else:
                    result = getattr(self, action)(body, progress=progress)
                terminal = {'status':'succeeded', 'phase':'completed', 'current_filename':None, 'result':result, 'error':None}
            except Exception as exc:
                error = {'code': getattr(exc, 'code', 'parameter_processing_failed'),
                         'message': getattr(exc, 'message', '参数处理失败，请检查模板和配置后重试。')}
                terminal = {'status':'failed', 'phase':'failed', 'error':error}
            finally:
                with self.task_lock:
                    saved = deepcopy(self.tasks[task_id])
                saved['task'].update(terminal, updated_at=time.time())
                saved['task']['overall_progress'] = cumulative_progress(saved['task'])
                self._task_contexts(saved, body)
                try:
                    # Status remains readable as running during durable IO. Only
                    # publish success once the restart recovery record is saved.
                    atomic_write_json(self.task_root / (task_id + '.json'), saved)
                except Exception:
                    terminal = {'status':'failed', 'phase':'failed', 'result':terminal.get('result'), 'error':{
                        'code':'parameter_state_unavailable', 'message':'处理记录无法保存，请核对处理结果；写入操作请检查配置与备份。'}}
                progress(**terminal)

        try:
            threading.Thread(target=run, name=task_id, daemon=True).start()
        except Exception:
            progress(status='failed', phase='failed', error={'code': 'parameter_busy', 'message': '无法启动处理，请重新计算。'})

    def _config_root(self):
        settings = load_settings(self.app.settings_path)
        if settings.load_error:
            raise _api_error('settings_recovery_required', '本机设置损坏，请先在设置中恢复')
        return settings.config_root or str(self.app.discovery_root / 'ulinkconfig' / 'csv')

    def context(self):
        root = Path(self._config_root())
        target = root / 'config_level.csv'
        if not target.is_file():
            target = root / 'csv' / 'config_level.csv'
        jobs = []
        for job in self.app.recent_jobs:
            try:
                state = job.snapshot()
                label = datetime.fromtimestamp(state['updated_at_ns'] / 1_000_000_000).strftime('%m-%d %H:%M')
                first = next((row.get('source_template_id') for row in state['templates'].values() if row.get('source_template_id')), '')
                jobs.append({'job_id': job.job_id, 'status': state['status'],
                    'template_count': len(state['templates']),
                    'label': '{} · {} 个模板 · {}'.format(label, len(state['templates']), first)})
            except (OSError, ValueError):
                continue
        settings = load_settings(self.app.settings_path)
        return {'config_root': str(root), 'config_path': str(target),
                'config_ready': target.is_file() and not target.is_symlink(),
                'jobs': jobs, 'output_root': settings.export_root}

    def _store(self, cache, identity, item):
        with self.lock:
            while len(cache) >= 256:
                cache.pop(next(iter(cache)))
            cache[identity] = deepcopy(item)

    def _known(self, cache, identity, prefix):
        if type(identity) is not str or not re.fullmatch(prefix + r'-[0-9a-f]{32}', identity):
            raise _api_error('invalid_request', '操作编号不合法，请重新计算', 400)
        with self.lock:
            item = cache.get(identity)
            if item is None:
                raise _api_error('parameter_context_expired', '操作记录已失效，请重新读取模板并计算')
            return deepcopy(item)

    def _artifact(self, job, record, historical=False):
        """Archived exports can be used only after the same independent check.

        No browser path or cached geometry summary is ever treated as proof.
        Historical export IDs do not grant the ordinary download capability.
        """
        if not historical:
            return job.read_artifact(record['id'])
        key, identity = record.get('template_key'), record.get('id')
        if (type(key) is not str or not KEY_PATTERN.fullmatch(key)
                or type(identity) is not str or not ARTIFACT_PATTERN.fullmatch(identity)):
            raise ValueError('invalid archived artifact')
        path = 'templates/' + key + '/outputs/' + identity + '.bytes'
        if (record.get('relative_path') != path or record.get('verified') is not True
                or record.get('filename') != record.get('new_id', '') + '_m.bytes'):
            raise ValueError('invalid archived artifact manifest')
        source = job.read_source(key)
        raw = read_regular(job.directory / path)
        if (digest(source) != record.get('source_sha256') or digest(raw) != record.get('sha256')
                or len(raw) != record.get('size')
                or not verify_output(source, raw, _unpack(record['plan']), record['new_id']).ok):
            raise ValueError('archived artifact verification failed')
        return raw

    def _entry(self, job, record, historical=False):
        raw = self._artifact(job, record, historical)
        source = job.read_source(record['template_key'])
        original_id, before = _counts(source)
        new_id, after = _counts(raw)
        if new_id != record['new_id']:
            raise ValueError('output ID mismatch')
        from .rewrite import RewritePlan
        plan = _unpack(record['plan'])
        from .manual_edit import initial_plan, ManualEditPlan, TowerRemovalPlan
        from .translation import TranslationPlan
        cursor=plan;edited=False
        while type(cursor) in (ManualEditPlan,TranslationPlan):
            edited=edited or type(cursor) is ManualEditPlan
            cursor=cursor.base_plan
        plan = initial_plan(plan)
        operation = 'reduce'
        template_mode = 'reduce'
        if type(plan) not in (RewritePlan,TowerRemovalPlan):
            from .increase import IncreasePlan
            from .ordinary_add import OrdinaryAddPlan
            if type(plan) not in (IncreasePlan, OrdinaryAddPlan):
                raise ValueError('unsupported verified parameter plan')
            operation = 'increase'
            template_mode = 'ordinary_add' if type(plan) is OrdinaryAddPlan else 'increase'
        if edited:
            operation = 'manual'
            template_mode = 'ordinary_add'
        evidence = {'kind': 'artifact', 'job_id': job.job_id, 'artifact_id': record['id'],
                    'historical': historical, 'source_sha256': digest(source), 'artifact_sha256': digest(raw)}
        entry = {'original_template_id': original_id, 'new_template_id': new_id,
                 'operation': operation, 'template_mode': template_mode,
                 'source_tile_counts': before, 'result_tile_counts': after, 'status': 'verified',
                 'source_sha256': evidence['source_sha256'], 'artifact_sha256': evidence['artifact_sha256']}
        return entry, evidence

    def _artifact_index(self, progress=None):
        index = {('sources',): {}, ('all_artifacts',): [], ('progress',): progress}
        jobs = list(self.app.recent_jobs)
        for number, job in enumerate(jobs):
            if progress:
                progress(phase='indexing', completed=number, total=len(jobs), current_filename=job.job_id)
            state = job.snapshot()
            for key, row in state.get('templates', {}).items():
                identity = row.get('source_template_id')
                if _template_id(identity):
                    index[('sources',)].setdefault(identity, []).append((job, key, row.get('source_sha256')))
            for historical, name in ((False, 'artifacts'), (True, 'history_artifacts')):
                for record in state.get(name, {}).values():
                    index.setdefault(record.get('new_id'), []).append((job, historical, record))
                    index[('all_artifacts',)].append((job, historical, record))
        return index

    def _renamed_artifact_index(self, index):
        if ('renamed',) in index:
            return index[('renamed',)]
        renamed = {}
        records = index.get(('all_artifacts',), [])
        progress = index.get(('progress',))
        for number, (job, historical, record) in enumerate(records):
            if progress:
                progress(phase='indexing', completed=number, total=len(records), current_filename=record.get('new_id'))
            key, identity = record.get('template_key'), record.get('id')
            if (type(key) is not str or not KEY_PATTERN.fullmatch(key)
                    or type(identity) is not str or not ARTIFACT_PATTERN.fullmatch(identity)):
                continue
            relative = 'templates/' + key + '/outputs/' + identity + '.bytes'
            if record.get('relative_path') != relative or record.get('verified') is not True:
                continue
            try:
                artifact = read_regular(job.directory / relative)
                if digest(artifact) == record.get('sha256') and len(artifact) == record.get('size'):
                    renamed.setdefault(_identity_digest(artifact), []).append((job, historical, record))
            except (OSError, ValueError, TypeError):
                continue
        index[('renamed',)] = renamed
        return renamed

    def _find_artifact(self, raw, artifact_index=None, original_id=None):
        new_id, _ = _counts(raw)
        checksum = digest(raw)
        matches = []
        if artifact_index is None:
            artifact_index = self._artifact_index()
        verified = artifact_index.setdefault(('verified_entries',), {})
        def entry_for(job, record, historical):
            key = (job.job_id, record['id'], historical)
            if key not in verified:
                verified[key] = self._entry(job, record, historical)
            return deepcopy(verified[key])
        for job, historical, record in artifact_index.get(new_id, []):
            if record.get('sha256') == checksum:
                try:
                    entry, proof = entry_for(job, record, historical)
                    matches.append((entry, proof))
                except (KeyError, OSError, ValueError, TypeError):
                    continue
        if not matches:
            for job, historical, record in self._renamed_artifact_index(artifact_index).get(_identity_digest(raw), []):
                try:
                    entry, proof = entry_for(job, record, historical)
                    entry = dict(entry, new_template_id=new_id, artifact_sha256=checksum,
                        match_kind='renamed_artifact', matched_artifact_template_id=record['new_id'])
                    matches.append((entry, proof))
                except (KeyError, OSError, ValueError, TypeError):
                    continue
        if not matches:
            return None
        if original_id is not None:
            filtered = [item for item in matches if item[0]['original_template_id'] == original_id]
            if not filtered:
                # An explicit additional binding needs independent source
                # evidence; it cannot borrow another source's verified plan.
                return None
            matches = filtered
        identities = {(item[0]['original_template_id'], item[0]['source_sha256']) for item in matches}
        if len(identities) != 1:
            raise SourceAmbiguity([item[0] for item in identities], '同一新模板对应多个源记录，请选择原模板；同ID不同版本需先核对源版本。')
        return matches[0]

    def _source_index(self, artifact_index):
        if ('source_ids',) in artifact_index:
            return artifact_index[('source_ids',)]
        identities = set(artifact_index.get(('sources',), {}))
        try:
            snapshot = _snapshot(self._config_root())
            column = snapshot['fields'].index('TemplateId')
            identities.update(row[column] for row in snapshot['data'] if _template_id(row[column]))
        except (OSError, ValueError):
            pass
        settings = load_settings(self.app.settings_path)
        if not settings.load_error:
            for source in ('tile', 'template'):
                if not getattr(settings, source + '_confirmed'):
                    continue
                root = getattr(settings, source + '_root')
                if not root:
                    continue
                descriptor = None
                try:
                    descriptor = self.app._open_repository_directory(Path(root))
                    with os.scandir(descriptor) as files:
                        for row in files:
                            if row.name.endswith('_m.bytes') and row.is_file(follow_symlinks=False) and _template_id(row.name[:-8]):
                                identities.add(row.name[:-8])
                except (OSError, ValueError):
                    continue
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
        artifact_index[('source_ids',)] = identities
        return identities

    def _match_source_id(self, new_id, index, strip_prefix='', strip_suffix=''):
        identities = self._source_index(index)
        if strip_prefix or strip_suffix:
            candidate = new_id
            if strip_prefix:
                if not candidate.startswith(strip_prefix):
                    raise ValueError('新模板ID未以指定前缀“' + strip_prefix + '”开头，请核对前缀或单独选择原模板。')
                candidate = candidate[len(strip_prefix):]
            if strip_suffix:
                if not candidate.endswith(strip_suffix):
                    raise ValueError('新模板ID未以指定后缀“' + strip_suffix + '”结尾，请核对后缀或单独选择原模板。')
                candidate = candidate[:-len(strip_suffix)]
            if not _template_id(candidate) or candidate not in identities:
                raise ValueError('剥除前后缀后未找到真实原模板“' + candidate + '”，请核对源库或选择原模板。')
            return candidate
        if new_id in identities:
            return new_id
        candidates = {identity for identity in identities if identity in new_id}
        # A native samplev2 beats its contained sample; never strip v2/v3 blindly.
        candidates = {identity for identity in candidates if not any(identity != other and identity in other for other in candidates)}
        if len(candidates) > 1:
            raise SourceAmbiguity(candidates)
        if not candidates:
            raise ValueError('未找到该新模板的真实原模板，请指定前后缀，或填写“原ID=新ID”关联。')
        return next(iter(candidates))

    def _source_bytes(self, identity, index):
        candidates = []
        for origin in ('tile', 'template'):
            try:
                raw, path = self._read_named(identity, origin)
                candidates.append((raw, {'kind':'path', 'path':path, 'artifact_sha256':digest(raw)}, origin))
            except (ValueError, OSError):
                continue
        if not candidates:
            seen = set()
            for job, key, checksum in index.get(('sources',), {}).get(identity, []):
                if checksum in seen:
                    continue
                try:
                    raw = job.read_source(key)
                    if _counts(raw)[0] != identity or digest(raw) != checksum:
                        continue
                    candidates.append((raw, {'kind':'source', 'job_id':job.job_id, 'template_key':key,
                        'source_sha256':checksum}, '任务源快照'))
                    seen.add(checksum)
                except (OSError, ValueError, KeyError):
                    continue
        if not candidates:
            raise ValueError('已识别原模板“' + identity + '”，但源库与任务快照中未找到可读取的原文件，请核对源库。')
        if len({digest(item[0]) for item in candidates}) != 1:
            raise SourceAmbiguity([identity], '原模板“' + identity + '”在不同来源的内容不同，请先核对源库版本；未自动选择其中一份。')
        return candidates[0][:2]

    def _read_named(self, name, source):
        if not _template_id(name):
            raise ValueError('模板ID只允许字母数字下划线连字符，不含后缀或路径')
        settings = load_settings(self.app.settings_path)
        if settings.load_error:
            raise ValueError('本机设置损坏，请先恢复设置')
        root = settings.export_root if source == 'output' else getattr(settings, source + '_root', '')
        if source in ('tile', 'template') and not getattr(settings, source + '_confirmed'):
            raise ValueError('请先在设置中确认所选模板库路径')
        if not root or not Path(root).is_absolute():
            raise ValueError('请先在设置中配置所选读取目录')
        fd = self.app._open_repository_directory(Path(root))
        try:
            raw, error = self.app._read_repository_template(fd, name, self.app.limits.file_bytes)
        finally:
            os.close(fd)
        if error:
            raise ValueError(error['message'])
        if decode_template(raw).template_id != name:
            raise ValueError('文件名与内部模板ID不一致，请核对文件')
        return raw, str(Path(root) / (name + '_m.bytes'))

    def _external_entry(self, raw, source_map, proof, artifact_index=None, strip_prefix='', strip_suffix=''):
        new_id, after = _counts(raw)
        explicit = source_map.get(new_id)
        automatic = explicit is None
        artifact_index = artifact_index if artifact_index is not None else self._artifact_index()
        if automatic and (strip_prefix or strip_suffix):
            explicit = self._match_source_id(new_id, artifact_index, strip_prefix, strip_suffix)
        found = self._find_artifact(raw, artifact_index, explicit)
        if found:
            if explicit is not None and explicit != found[0]['original_template_id']:
                raise ValueError('指定原模板与已验收生成记录不一致，未使用该关联')
            entry, artifact_proof = found
            # A registry match proves provenance, not continued identity of the
            # file the user actually chose. Keep both snapshots bound.
            return entry, dict(artifact_proof, selected_proof=proof)
        if explicit is None:
            explicit = self._match_source_id(new_id, artifact_index, strip_prefix, strip_suffix)
        if not _template_id(explicit):
            raise ValueError('指定的原模板ID不合法')
        source_raw, source_proof = self._source_bytes(explicit, artifact_index)
        original_id, before = _counts(source_raw)
        operation = _mapped_operation(source_raw, raw)
        # This is an explicit user mapping, not a guess derived from lessT.
        proof = dict(proof, source_proof=source_proof, source_sha256=digest(source_raw))
        if not inspect_template(decode_template(source_raw)).needs_optimization and before == after:
            return {'original_template_id': original_id, 'new_template_id': None,
                    'source_tile_counts': before, 'result_tile_counts': after, 'status': 'no_tower'}, proof
        return {'original_template_id': original_id, 'new_template_id': new_id,
                'operation': operation,
                'source_tile_counts': before, 'result_tile_counts': after, 'status': 'verified',
                'match_kind': 'automatic_source' if automatic else 'explicit_source',
                'message': '按' + ('自动识别' if automatic else '明确指定') + '的真实源模板与新模板计算；未推断图案难度等价'}, proof

    def resolve(self, payload, progress=None):
        _request(payload, ('mode','job_id','template_ids','import_id','source','source_map','strip_prefix','strip_suffix'), ('mode',))
        mode = payload['mode']
        if mode not in ('job', 'ids', 'files'):
            raise _api_error('invalid_request', '请选择模板来源', 400)
        source_map = payload.get('source_map', {})
        if type(source_map) is not dict or any(not _template_id(k) or not (
                _template_id(v) or (type(v) is list and 0 < len(v) <= 100 and all(_template_id(item) for item in v)))
                for k,v in source_map.items()):
            raise _api_error('invalid_request', '源新模板关联格式不合法', 400)
        affixes = []
        for name in ('strip_prefix', 'strip_suffix'):
            value = payload.get(name, '')
            if type(value) is not str or len(value) > 180 or (value.strip() and not _template_id(value.strip())):
                raise _api_error('invalid_request', '前后缀只允许字母数字下划线连字符，或留空自动识别', 400)
            affixes.append(value.strip())
        entries, proofs = [], []
        if mode == 'job':
            _request(payload, ('mode','job_id'), ('mode','job_id'))
            job = self.app._job(payload['job_id'])
            state = job.snapshot()
            if progress:
                progress(phase='resolving', completed=0, total=len(state['templates']))
            for number, (key, row) in enumerate(state['templates'].items()):
                if progress:
                    progress(completed=number, current_filename=row.get('source_template_id') or key)
                try:
                    if row.get('output_artifacts') and row['status'] in ('verified','exported','succeeded'):
                        entry, proof = self._entry(job, state['artifacts'][row['output_artifacts'][-1]])
                    else:
                        raw = job.read_source(key)
                        original_id, counts = _counts(raw)
                        additive = ((row.get('parameters') or {}).get('mode') in ('increase', 'ordinary_add')
                                    and row.get('status') != 'no_op')
                        no_tower = not inspect_template(decode_template(raw)).needs_optimization and not additive
                        entry = {'original_template_id': original_id, 'new_template_id': None,
                                 'source_tile_counts': counts, 'result_tile_counts': counts,
                                 'status': 'no_tower' if no_tower else 'failed',
                                 'message': '原模板无塔，无需配置新参数' if no_tower else '该模板尚无已验收新产物，请完成生成或单独重跑'}
                        proof = {'kind':'source', 'job_id':job.job_id, 'template_key':key, 'source_sha256':digest(raw)}
                    entries.append(entry); proofs.append(proof)
                except (KeyError, OSError, ValueError, TypeError):
                    entries.append({'original_template_id':row.get('source_template_id'), 'new_template_id':None,
                                    'status':'failed','message':'源新模板证据无法通过校验，请从运行记录重新生成'})
                if progress:
                    progress(completed=number + 1)
        else:
            raws = []
            if mode == 'files':
                _request(payload, ('mode','import_id','source_map','strip_prefix','strip_suffix'), ('mode','import_id'))
                folder, manifest = self.app._load_import(payload['import_id'])
                for row in manifest['files']:
                    if row.get('duplicate_of'):
                        continue
                    if row.get('status') == 'failed':
                        entries.append({'original_template_id':row.get('original_name'), 'new_template_id':None,
                                        'status':'failed','message':row.get('error',{}).get('message','模板无法读取')})
                        continue
                    raws.append((row, {'kind':'import','import_id':payload['import_id'],'file_id':row['file_id'],
                                      'artifact_sha256':row.get('sha256'), 'original_name':row.get('original_name')}))
            else:
                _request(payload, ('mode','template_ids','source','source_map','strip_prefix','strip_suffix'), ('mode','template_ids'))
                ids = payload['template_ids']
                source = payload.get('source', 'output')
                if type(ids) is not list or not ids or any(type(item) is not str for item in ids) or source not in ('output','tile','template'):
                    raise _api_error('invalid_request', '请输入模板ID并选择读取来源', 400)
                for identity in dict.fromkeys(ids):
                    raws.append((identity, {'kind':'named', 'source':source, 'original_name':identity}))
            artifact_index = self._artifact_index(progress)
            if progress:
                progress(phase='resolving', completed=0, total=len(raws), current_filename=None)
            for number, (reference, proof) in enumerate(raws):
                raw = None
                if progress:
                    progress(completed=number, current_filename=proof.get('original_name') or Path(proof.get('path', '')).name)
                try:
                    if proof['kind'] == 'import':
                        raw = self.app._import_snapshot(folder, reference)
                    else:
                        raw, path = self._read_named(reference, proof['source'])
                        proof = {'kind':'path', 'path':path, 'artifact_sha256':digest(raw), 'original_name':reference}
                    new_id = _counts(raw)[0]
                    requested = source_map.get(new_id)
                    bindings = list(dict.fromkeys(requested)) if type(requested) is list else [requested]
                    for original in bindings:
                        try:
                            mapping = {new_id:original} if original is not None else {}
                            entry, evidence = self._external_entry(raw, mapping, proof, artifact_index, *affixes)
                            entries.append(entry); proofs.append(evidence)
                        except (KeyError, OSError, ValueError, TypeError) as exc:
                            entries.append({'original_template_id':original, 'new_template_id':new_id, 'status':'failed',
                                'message':str(exc) if isinstance(exc, ValueError) else '源新模板证据无法读取',
                                **({'source_candidates':exc.candidates} if isinstance(exc, SourceAmbiguity) else {})})
                except (KeyError, OSError, ValueError, TypeError) as exc:
                    try:
                        identity = _counts(raw)[0] if raw is not None else proof.get('original_name')
                    except (ValueError, TypeError):
                        identity = proof.get('original_name') or '无法解析的模板'
                    entries.append({'original_template_id': source_map.get(identity),
                        'new_template_id': identity, 'status':'failed',
                        'message': str(exc) if isinstance(exc, ValueError) else '源新模板证据无法读取',
                        **({'source_candidates':exc.candidates} if isinstance(exc, SourceAmbiguity) else {})})
                if progress:
                    progress(completed=number + 1)
        identity = 'selection-' + secrets.token_hex(16)
        self._store(self.selections, identity, {'entries':entries, 'proofs':proofs})
        return {'selection_id':identity, 'entries':entries, 'summary':{'template_count':len(entries),
                'ready_count':sum(row['status']=='verified' for row in entries),
                'failed_count':sum(row['status']=='failed' for row in entries)}}

    def _verify(self, context, progress=None, _cache=None):
        cache = {} if _cache is None else _cache
        if progress:
            progress(phase='verifying', completed=0, total=len(context['proofs']), current_filename=None)
        for number, proof in enumerate(context['proofs']):
            if progress:
                progress(completed=number, current_filename=proof.get('original_name') or proof.get('artifact_id') or Path(proof.get('path', '')).name)
            try:
                if proof.get('selected_proof'):
                    self._verify({'proofs': [proof['selected_proof']]}, _cache=cache)
                if proof.get('source_proof'):
                    self._verify({'proofs': [proof['source_proof']]}, _cache=cache)
                if proof['kind'] == 'artifact':
                    key = ('job', proof['job_id'])
                    if key not in cache:
                        job = self.app._job(proof['job_id'])
                        cache[key] = (job, job.snapshot())
                    job, state = cache[key]
                    record = state['history_artifacts' if proof['historical'] else 'artifacts'][proof['artifact_id']]
                    raw = self._artifact(job, record, proof['historical'])
                elif proof['kind'] == 'source':
                    raw = self.app._job(proof['job_id']).read_source(proof['template_key'])
                elif proof['kind'] == 'import':
                    key = ('import', proof['import_id'])
                    if key not in cache:
                        folder, manifest = self.app._load_import(proof['import_id'])
                        cache[key] = (folder, {row['file_id']: row for row in manifest['files']})
                    folder, rows = cache[key]
                    row = rows[proof['file_id']]
                    raw = self.app._import_snapshot(folder, row)
                else:
                    raw = read_regular(Path(proof['path']))
                expected = proof.get('artifact_sha256', proof.get('source_sha256'))
                if digest(raw) != expected:
                    raise ValueError('template changed')
                if proof.get('original_path') and digest(read_regular(Path(proof['original_path']))) != proof['source_sha256']:
                    raise ValueError('source changed')
            except (OSError, KeyError, ValueError, TypeError, StopIteration):
                raise _api_error('parameter_evidence_changed', '源模板或新模板已变化，请重新读取并计算参数')
            if progress:
                progress(completed=number + 1)

    def calculate(self, payload, progress=None):
        _request(payload, ('selection_id','level_start','level_end','level_id_rule','description','preserve_level_prefix'), ('selection_id',))
        context = self._known(self.selections, payload['selection_id'], 'selection')
        self._verify(context, progress)
        if progress:
            progress(phase='calculating', completed=0, total=0, current_filename=None)
        try:
            result = self.service.calculate(self._config_root(), context['entries'], **{
                name:payload[name] for name in ('level_start','level_end','level_id_rule','description','preserve_level_prefix') if name in payload})
        except ValueError as exc:
            raise _api_error('parameter_calculation_failed', str(exc))
        self._store(self.calculations, result['calculation_id'], context)
        return result

    def preview(self, payload, progress=None):
        _request(payload, ('calculation_id','selected_row_ids','level_id_rule','description','preserve_level_prefix'), ('calculation_id','selected_row_ids'))
        context = self._known(self.calculations, payload['calculation_id'], 'calc')
        self._verify(context, progress)
        if progress:
            progress(phase='previewing', completed=0, total=0, current_filename=None)
        try:
            result = self.service.preview(payload['calculation_id'], payload['selected_row_ids'],
                config_root=self._config_root(), **{name:payload[name] for name in ('level_id_rule','description','preserve_level_prefix') if name in payload})
        except ValueError as exc:
            raise _api_error('parameter_preview_failed', str(exc))
        self._store(self.plans, result['plan_id'], context)
        return result

    def commit(self, payload, progress=None):
        _request(payload, ('plan_id',), ('plan_id',))
        context = self._known(self.plans, payload['plan_id'], 'plan')
        self._verify(context, progress)
        if progress:
            progress(phase='committing', completed=0, total=0, current_filename=None)
        result = self.service.commit(payload['plan_id'], config_root=self._config_root())
        result['directories'] = self.app.directory_actions.issue([
            ('配置仓库', str(Path(result['config_path']).parent))]) if result.get('status')=='committed' else []
        return result

    def download(self, calculation_id, format):
        self._known(self.calculations, calculation_id, 'calc')
        if format not in ('xlsx','csv'):
            raise _api_error('invalid_request', '只支持XLSX或CSV报表', 400)
        try:
            return self.service.export(calculation_id, format)
        except ValueError as exc:
            raise _api_error('parameter_download_failed', str(exc))
