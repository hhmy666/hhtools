"""Evidence-first exports and ZIPs containing only registered verified bytes.

Each file commits independently. A directory fsync error after replace is
reported with applied=True: complete bytes may already exist, but durability
was not confirmed. This is intentionally not a multi-file transaction.
"""

from dataclasses import dataclass
import io
import os
from pathlib import Path
import stat
import zipfile

from .storage import (absolute_path, assert_directory_identity, commit_file, digest, discard_temp,
                      directories_overlap, directory_ancestry, directory_fd, open_directory,
                      prepare_file, read_regular, read_regular_at, safe_directory)
from .template_reader import decode_template
from .naming import validate_template_id, validate_batch_names, render_template_id, allocate_template_id


@dataclass(frozen=True)
class ExportReport:
    job_id: str
    target_dir: str
    rows: tuple

    @property
    def ok(self):
        return all(row.get('status') in ('exported', 'skipped') for row in self.rows)

    def to_dict(self):
        return {'job_id': self.job_id, 'target_dir': self.target_dir, 'rows': list(self.rows), 'ok': self.ok}


def _error(code, message, stage='export'):
    return {'code': code, 'stage': stage, 'message': str(message),
            'recommendations': ['检查目录权限或重新预览冲突后重试该模板']}


def _target_root(job, value, create=False):
    target = absolute_path(value)
    for row in job.snapshot()['templates'].values():
        for source_parent in row['source_parents']:
            if source_parent is None:
                continue
            if directories_overlap(target, source_parent):
                raise ValueError('input and output directories must not contain each other')
    # Preview does not create directories. Validate every existing ancestor.
    _, missing = directory_ancestry(target)
    ancestor = target if not missing else Path(*target.parts[:-len(missing)])
    safe_directory(ancestor, writable=True)
    if create:
        safe_directory(target, create=True, writable=True)
    elif not missing:
        safe_directory(target, writable=True)
    return target


def target_evidence(path, parent_fd=None):
    """Existence/type/SHA and inode evidence, using no-follow regular reads."""
    if parent_fd is None:
        try:
            with directory_fd(path.parent) as fd:
                return target_evidence(path, fd)
        except FileNotFoundError:
            return {'exists': False, 'sha256': None, 'identity': None}
    try:
        info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return {'exists': False, 'sha256': None, 'identity': None}
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('target must be a regular file, never a link/directory/device')
    raw = read_regular_at(parent_fd, path.name)
    after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('target changed while planning')
    return {'exists': True, 'sha256': digest(raw), 'identity': list(identity)}


def selected_template_keys(job, keys):
    if keys is None:
        return None
    known=job.snapshot()['templates']
    if (not isinstance(keys,(list,tuple)) or not keys or len(keys)>len(known)
            or any(type(key) is not str or key not in known for key in keys)
            or len(set(keys))!=len(keys)):
        raise ValueError('请选择当前任务内不重复的模板')
    return set(keys)


def delivery_names(job, template_keys=None, naming_rule=None):
    selected=selected_template_keys(job,template_keys)
    if naming_rule is None:
        return {}
    state=job.snapshot()
    rows=[{'key':r['template_key'],'original_id':state['templates'][r['template_key']]['source_template_id']}
          for r in job.artifact_records() if selected is None or r['template_key'] in selected]
    report=validate_batch_names(rows,naming_rule)
    if not report.ok:
        raise ValueError(next(row['error']['message'] for row in report.rows if not row['ok']))
    return {row['key']:row['new_id'] for row in report.rows}


def plan_exports(job, target_dir, *, auto_version=False, template_keys=None, naming_rule=None, progress=None):
    """Read-only preview. Missing destination directories remain missing."""
    selected=selected_template_keys(job,template_keys)
    names=delivery_names(job,template_keys,naming_rule)
    root_error = None
    try:
        target = _target_root(job, target_dir)
    except (ValueError, OSError, TypeError) as exc:
        target = Path(str(target_dir)); root_error = _error('invalid_target_directory', exc, 'planning')
    rows = []
    state = job.snapshot()
    total = sum(selected is None or key in selected for key in state['templates'])
    def notify(row=None):
        if progress is not None:
            progress(phase='verifying', verified=len(rows), verification_total=total,
                     current_filename=row.get('relative_name') if row else None)
    notify()
    occupied = set()
    if auto_version and not root_error:
        try:
            with directory_fd(target) as fd:
                occupied = {name.casefold() for name in os.listdir(fd)}
        except FileNotFoundError:
            pass
    reserved = ({(name+'_m.bytes').casefold() for name in names.values()} if naming_rule is not None
                else {record['filename'].casefold() for record in job.artifact_records()})
    for key, template in state['templates'].items():
        if selected is not None and key not in selected:
            continue
        row = {'template_key': key, 'artifact_id': None, 'relative_name': None, 'new_sha256': None,
               'expected_old_sha': None, 'classification': 'blocked', 'status': 'blocked', 'error': root_error}
        artifact = state['artifacts'].get(next(iter(template['output_artifacts']), None), {})
        notify({'relative_name': artifact.get('filename') or template['source_template_id']})
        try:
            if root_error:
                continue
            if template['status'] == 'no_op' and not template['output_artifacts']:
                row.update(classification='no_op', status='skipped', error=None)
                continue
            ids = template['output_artifacts']
            if len(ids) != 1:
                raise ValueError('template has no registered verified artifact')
            artifact = state['artifacts'][ids[0]]
            raw = job.read_artifact(ids[0])
            row.update(artifact_id=ids[0], relative_name=artifact['filename'], new_sha256=digest(raw))
            if auto_version:
                base = (render_template_id(naming_rule, template['source_template_id']) if naming_rule is not None
                        else template.get('plan_summary', {}).get('export_base_id') or artifact['new_id'])
                validate_template_id(base, template['source_template_id'])
                own_name = names.get(key) or artifact['new_id']
                blocked = occupied | (reserved - {(own_name+'_m.bytes').casefold()})
                name = allocate_template_id(base, (item[:-len('_m.bytes')] for item in blocked
                                                   if item.endswith('_m.bytes')), template['source_template_id'])
                if name != artifact['new_id']:
                    raw, _ = job.renamed_output_bytes(ids[0], name)
                row.update(relative_name=name+'_m.bytes', new_id=name, base_id=base,
                           original_name=base+'_m.bytes', renamed=name != base, new_sha256=digest(raw))
                occupied.add(row['relative_name'].casefold())
            evidence = target_evidence(target/row['relative_name'])
            classification = 'create' if not evidence['exists'] else ('same_sha' if evidence['sha256']==digest(raw) else 'conflict')
            row.update(classification=classification, status='planned', expected_old_sha=evidence['sha256'], target_evidence=evidence)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            row['error'] = _error('artifact_or_target_invalid', exc, 'planning')
        finally:
            rows.append(row)
            notify(row)
    return ExportReport(job.job_id, str(target), tuple(rows))


class TargetChanged(ValueError):
    pass


def export_outputs(job, target_dir, conflict_decisions=None, *, auto_version=False, expected_rows=None, expected_revision=None, template_keys=None, naming_rule=None, progress=None):
    """Export independently, with explicit SHA-bound backup-and-replace decisions.

    decisions = {template_key: {job_id, relative_name, expected_old_sha,
                                action: 'skip'|'backup_and_replace'}}
    Use plan_exports for a write-free UI preview. Every call obtains fresh
    evidence, and repeats it immediately before committing each output.
    """
    decisions = conflict_decisions if isinstance(conflict_decisions, dict) else {}
    with job.lock:
        if expected_revision is not None and job.snapshot()['revision'] != expected_revision:
            raise TargetChanged('任务已变化，请重新预览导出')
        def preflight(**update):
            if progress is not None:
                update.update(phase='preflight', completed=0, total=update['verification_total'])
                progress(**update)
        preview = plan_exports(job, target_dir, auto_version=auto_version,template_keys=template_keys,
                               naming_rule=naming_rule, progress=preflight if progress is not None else None)
        rows = [dict(row) for row in preview.rows]
        if expected_rows is not None:
            identity = lambda row: (row.get('template_key'), row.get('artifact_id'), row.get('relative_name'),
                                    row.get('new_sha256'), row.get('classification'), row.get('target_evidence'))
            if [identity(row) for row in rows] != [identity(row) for row in expected_rows]:
                raise TargetChanged('目标或产物已变化，请重新预览导出')
        ops = job.repository.file_ops
        completed = 0
        def notify(phase, row=None):
            if progress is not None:
                progress(phase=phase, completed=completed, total=len(rows),
                         current_filename=row.get('relative_name') if row else None)
        notify('verifying')
        for row in rows:
            if row['classification'] in ('blocked', 'no_op'):
                completed += 1
                notify('verifying', row)
                continue
            key = row['template_key']
            if row['classification'] == 'conflict':
                base = {'job_id': job.job_id, 'relative_name': row['relative_name'],
                        'expected_old_sha': row['expected_old_sha']}
                decision = decisions.get(key)
                if decision == dict(base, action='skip'):
                    row.update(status='skipped', applied=False)
                    row.pop('error', None)
                    completed += 1
                    notify('verifying', row)
                    continue
                if decision != dict(base, action='backup_and_replace'):
                    row.update(status='blocked', error=_error('conflict_confirmation_required', '请针对当前文件与旧SHA确认备份后替换'))
                    completed += 1
                    notify('verifying', row)
                    continue
            temp, applied, target_fd = None, False, None
            try:
                notify('verifying', row)
                target = _target_root(job, target_dir, create=True)
                target_fd = open_directory(target, writable=True)
                destination = target/row['relative_name']
                def unchanged():
                    _target_root(job, target)
                    assert_directory_identity(target, target_fd)
                    if target_evidence(destination, target_fd) != row['target_evidence']:
                        raise TargetChanged('target changed; preview and confirm again')
                unchanged()
                if auto_version and naming_rule is None and row['new_id'] != job.snapshot()['artifacts'][row['artifact_id']]['new_id']:
                    artifact = job.activate_renamed_output(row['artifact_id'], row['new_id'], row['base_id'])
                    row['artifact_id'] = artifact['id']
                raw = job.read_artifact(row['artifact_id'])
                if naming_rule is not None and row['new_id'] != decode_template(raw).template_id:
                    raw,_=job.renamed_output_bytes(row['artifact_id'],row['new_id'])
                if digest(raw) != row['new_sha256']:
                    raise TargetChanged('artifact changed after planning')
                if row['classification'] == 'same_sha':
                    row['status'] = 'skipped'
                    continue
                notify('writing', row)
                temp = prepare_file(destination, raw, ops, parent_fd=target_fd)
                ops.checkpoint('export_readback', temp)
                reread = read_regular(temp)
                expected_id = row['relative_name'][:-len('_m.bytes')]
                if digest(reread) != row['new_sha256'] or decode_template(reread).template_id != expected_id:
                    raise ValueError('export temporary SHA/TemplateId mismatch')
                if row['classification'] == 'conflict':
                    unchanged()
                    old = read_regular_at(target_fd, destination.name)
                    if digest(old) != row['expected_old_sha']:
                        raise TargetChanged('old content changed before backup')
                    row['backup'] = job.save_backup(old, {'template_key': key, 'relative_name': row['relative_name'],
                                                         'target_dir': str(target), 'job_id': job.job_id})
                ops.checkpoint('export_before_commit', destination)
                def check_commit():
                    unchanged()
                    final_bytes = read_regular(temp)
                    if digest(final_bytes) != row['new_sha256'] or decode_template(final_bytes).template_id != expected_id:
                        raise ValueError('export temporary artifact changed before commit')
                try:
                    commit_file(temp, destination, ops, before_replace=check_commit, overwrite=not auto_version)
                    applied = True
                except Exception:
                    # A directory fsync can fail after a complete atomic replace.
                    applied = temp.applied
                    raise
                row.update(status='exported', applied=True)
            except Exception as exc:
                row.update(status='failed', applied=applied,
                           error=_error('target_changed' if isinstance(exc, (TargetChanged, FileExistsError)) else 'export_failed', exc))
            finally:
                try:
                    discard_temp(temp)
                except (ValueError, OSError) as exc:
                    # Cleanup uses the original fd even after a parent rename.
                    # Preserve evidence if permissions or I/O prevent unlink.
                    row['cleanup_error'] = _error('temporary_cleanup_blocked', exc, 'cleanup')
                if target_fd is not None:
                    os.close(target_fd)
                completed += 1
                notify('writing', row)
        try:
            job.record_export(rows)
        except Exception as exc:
            # Do not rewrite successful file outcomes as though nothing was applied.
            for row in rows:
                row['audit_error'] = _error('export_audit_failed', exc, 'persisting')
        return ExportReport(job.job_id, preview.target_dir, tuple(rows))


def build_zip(job, template_keys=None, naming_rule=None):
    """Return ZIP bytes; no caller-supplied member paths or filesystem inputs."""
    with job.lock:
        selected=selected_template_keys(job,template_keys)
        names=delivery_names(job,template_keys,naming_rule)
        output = io.BytesIO()
        seen = set()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for record in job.artifact_records():
                if selected is not None and record['template_key'] not in selected:
                    continue
                raw = job.read_artifact(record['id'])
                name = record['filename']
                if record['template_key'] in names:
                    new_id=names[record['template_key']]
                    raw,_=job.renamed_output_bytes(record['id'],new_id)
                    name=new_id+'_m.bytes'
                # read_artifact also proves strict ASCII new_id and exact basename.
                if name.casefold() in seen or Path(name).name != name or '/' in name or '\\' in name:
                    raise ValueError('unsafe or duplicate ZIP member')
                seen.add(name.casefold())
                archive.writestr(name, raw)
        return output.getvalue()
