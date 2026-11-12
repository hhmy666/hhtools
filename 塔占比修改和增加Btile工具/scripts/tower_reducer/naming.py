"""Literal naming rules: one recognized token, no expression evaluation."""

from dataclasses import dataclass
import re


TOKEN = '{原模板ID}'
DEFAULT_RULE = 'lessT' + TOKEN


def validate_template_id(value, original_id=None):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value)
            or len(value.encode('utf-8')) > 180 or value == original_id):
        raise ValueError('新模板名称只能用英文字母、数字、下划线或连字符，长度为1–180个字符，且不能与原模板名称相同')
    return value


def _rule(rule, batch=False):
    if not isinstance(rule, str) or not rule:
        raise ValueError('命名规则不能为空')
    count = rule.count(TOKEN)
    literal = rule.replace(TOKEN, '')
    if count > 1 or '{' in literal or '}' in literal:
        raise ValueError('命名中只能使用一个 {原模板ID}；也可以直接填写固定名称，同名会自动加 v2、v3')


def allocate_template_id(base, occupied=(), original_id=None):
    """Choose a case-insensitive unique name without changing an existing owner."""
    validate_template_id(base, original_id)
    folded = {name.casefold() for name in occupied}
    if base.casefold() not in folded:
        return base
    for version in range(2, 10001):
        suffix = 'v' + str(version)
        candidate = base[:180-len(suffix)] + suffix
        if candidate.casefold() not in folded and candidate != original_id:
            return candidate
    raise ValueError('同名模板太多，请换一个名称后重试')


def render_template_id(rule, original_id, override=None):
    _rule(rule)
    if not isinstance(original_id, str) or not original_id:
        raise ValueError('源ID不能为空')
    result = override if override is not None else rule.replace(TOKEN, original_id)
    return validate_template_id(result, original_id)


@dataclass(frozen=True)
class NameReport:
    rows: tuple
    conflicts: tuple

    @property
    def ok(self):
        return all(row['ok'] for row in self.rows)


def validate_batch_names(rows, rule=DEFAULT_RULE):
    """Rows contain original_id and optional override/key; order is preserved."""
    rows = tuple(rows)
    results = []
    rule_error = None
    try:
        _rule(rule, batch=len(rows) > 1)
    except ValueError as exc:
        rule_error = str(exc)
    for i, row in enumerate(rows):
        result = {'index': i, 'key': row.get('key', str(i)), 'original_id': row.get('original_id'),
                  'new_id': None, 'ok': False, 'error': None}
        try:
            if rule_error:
                raise ValueError(rule_error)
            result['new_id'] = render_template_id(rule, row.get('original_id'), row.get('override'))
            result['ok'] = True
        except ValueError as exc:
            result['error'] = {'code': 'invalid_rule' if rule_error else 'invalid_id', 'message': str(exc)}
        results.append(result)
    # Reserve all requested names before resolving collisions so an automatic
    # suffix cannot take another row's explicit "v2" name.
    reserved = {row['new_id'].casefold() for row in results if row['ok']}
    assigned = set()
    for row in results:
        if not row['ok']:
            continue
        base = row['new_id']
        if base.casefold() in assigned:
            row['new_id'] = allocate_template_id(base, reserved | assigned, row['original_id'])
        assigned.add(row['new_id'].casefold())
    return NameReport(tuple(results), ())
