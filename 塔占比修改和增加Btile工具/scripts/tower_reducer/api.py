"""Versioned, loopback-only application services for the local HTTP API.

The HTTP adapter lives in :mod:`scripts.app`.  This module deliberately owns
the stateful parts of the API (imports, background jobs, confirmation plans
and settings) so tests can inject a repository, clock and Task-8 sync service.
No browser supplied value is ever interpreted as a synchronization path.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from .distribution import DistributionOptions, plan_tower_distribution
from .directory_actions import DirectoryActions, DirectoryActionError
from .export_service import build_zip, export_outputs, plan_exports, target_evidence, TargetChanged
from .inspection import inspect_template
from .increase import (DEFAULT_INCREASE_OPTIONS, _build_increase, build_increase,
                       normalise_increase_options, recommend_increase)
from .increase_verification import verify_increase
from .ordinary_add import (DEFAULT_ORDINARY_ADD_OPTIONS, OrdinaryAddPlan, build_ordinary_add,
                           normalize_ordinary_add_options)
from .naming import render_template_id, validate_batch_names, allocate_template_id
from .path_binding import (TEMPLATE_SUFFIX, TILE_SUFFIX, discover_template_roots,
                           validate_binding)
from .placement import PlacementOptions, SearchBudget, plan_joint_layout
from .parameter_advice import verified_parameter_advice
from .rewrite import RewritePlan, rewrite_template
from .preview import comparison
from .settings import LocalSettings, load_settings, save_settings
from .storage import InputSnapshot, atomic_write_bytes, atomic_write_json, digest, read_regular
from .template_reader import decode_template
from .translation import TranslationPlan, translation_bounds, validate_translation


PROTOCOL_VERSION = 1
IMPORT_RE = re.compile(r"import-[0-9a-f]{32}\Z")
FILE_RE = re.compile(r"file-[0-9a-f]{32}\Z")
JOB_RE = re.compile(r"job-[0-9a-f]{32}\Z")
TEMPLATE_RE = re.compile(r"tpl-[0-9a-f]{32}\Z")
PLAN_TTL_SECONDS = 300.0


class ApiError(Exception):
    """Stable public failure.  Internal exception text must not cross HTTP."""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.details = details


@dataclass(frozen=True)
class ApiLimits:
    file_bytes: int = 32 * 1024 * 1024
    file_count: int = 20
    request_bytes: int = 128 * 1024 * 1024
    active_uploads: int = 2
    active_jobs: int = 2
    download_bytes: int = 64 * 1024 * 1024
    zip_uncompressed_bytes: int = 256 * 1024 * 1024
    active_zip_downloads: int = 1
    confirmation_plans: int = 256

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value < 1:
                raise ValueError("{} must be a positive integer".format(name))


DEFAULT_OPTIONS = {
    "mode": "reduce",
    "increase": DEFAULT_INCREASE_OPTIONS,
    "ordinary_add": DEFAULT_ORDINARY_ADD_OPTIONS,
    "ratio": 0.25,
    "min_ratio": 0.0,
    "replenish_type": "auto",
    "retained_counts": None,
    "strategy": "top_first",
    "max_tiles_per_layer": 20,
    "max_added_layers": 2,
    "seed": 0,
    "naming_rule": "lessT{原模板ID}",
}
OPTION_FIELDS = frozenset(DEFAULT_OPTIONS) | {"new_id"}


def _safe_error(error, fallback_code="processing_failed"):
    """Convert algorithm failures to bounded, path-free descriptive JSON."""
    code = getattr(error, "code", fallback_code)
    message = getattr(error, "message", "处理未完成，请调整参数后单独重试")
    recommendations = getattr(error, "recommendations", ())
    return {
        "code": str(code)[:100],
        "message": str(message)[:500],
        "recommendations": [str(item)[:500] for item in tuple(recommendations)[:10]],
    }


_PUBLIC_FAILURES = {
    "source_geometry_conflict": ("placement", "原普通牌存在紧邻层完全覆盖，保留原牌位置时无法消除", "增加搜索时间、层数或改变补牌位置无法消除原牌的完全覆盖；可使用删塔补牌手动调整，最终结果仍需通过结构验收。"),
    "minority_source_type_absent": ("placement", "原非塔区没有所需同类牌，无法满足少数补位邻近要求", "可查看已验收的参数调整方案；不会把缺少同类承接的方案按通过交付。"),
    "minority_proximity_unavailable": ("placement", "当前合法布局没有足够邻近原同类普通牌的位置用于少数补位", "本次未找到合格的邻近补位，不代表所有布局都不可行；可查看已独立验收的参数建议。"),
    "invalid_filename": ("import", "文件名不符合导入规则", "请重新选择 *_m.bytes 文件"),
    "file_too_large": ("import", "文件超过单文件上限", "请拆分批次或检查源文件"),
    "invalid_template": ("import", "无法解析模板文件", "请检查模板文件是否完整"),
    "invalid_template_id": ("import", "模板ID格式不合法", "ID只允许字母数字下划线连字符，不含文件后缀/路径"),
    "template_not_found": ("import", "模板库中未找到该ID", "请检查模板ID或切换模板库"),
    "template_read_rejected": ("import", "无法读取该模板文件", "请检查文件权限，模板必须是普通文件且不能使用符号链接"),
    "invalid_options": ("placement", "配置参数不合法", "请调整参数后单独重试该模板"),
    "invalid_distribution": ("distribution", "塔牌分配未通过校验", "请调整参数后单独重试该模板"),
    "invalid_ratio": ("distribution", "塔占比参数不合法", "请将塔占比设置在0%到100%之间，且上限不高于原模板塔占比"),
    "invalid_ratio_range": ("distribution", "塔占比范围不合法", "下限需小于上限；下限和上限都为0时表示清空塔"),
    "reduce_ratio_above_source": ("distribution", "塔占比上限高于当前原模板塔占比", "降低该模板上限或单独设置参数；批次中各模板按自己的原塔占比校验"),
    "invalid_replenish_type": ("distribution", "非塔补牌类型不合法", "请选择自动、A或B"),
    "tower_ratio_below_minimum": ("distribution", "保留塔牌数量低于下限", "请增加逐堆保留数量，或调整塔占比下限"),
    "tower_ratio_range_unattainable": ("distribution", "当前塔占比范围无法满足", "请按原塔牌数量及三消约束调整范围；工具不会增加塔内牌或自动降低下限"),
    "invalid_target_length": ("distribution", "逐堆保留数量与塔堆数不一致", "请核对逐堆参数"),
    "manual_out_of_range": ("distribution", "逐堆保留数量超出原塔范围", "请降低逐堆保留数量"),
    "manual_symmetry": ("distribution", "逐堆参数破坏原有对称关系", "请让镜像塔使用相同保留数量"),
    "manual_center_parity": ("distribution", "中心塔参数不符合对称约束", "请调整中心塔保留数量"),
    "tower_ratio_exceeded": ("distribution", "结果未达到塔占比要求", "请降低逐堆保留数量"),
    "single_move_type_unavailable": ("distribution", "旧结果使用了已取消的同类型搬移限制", "点击“单独调整并重新运行”，保留原参数重算；现在允许移出A牌后补入B牌，不需要为旧限制增加层数或提高塔占比"),
    "final_type_counts_not_multiple_of_three": ("distribution", "当前逐塔保留数会使补牌后的A/B数量不是3的倍数", "选择下方已验收方案，按其逐塔保留数重新生成；不提高塔占比上限"),
    "source_inspection_mismatch": ("inspection", "模板检查结果与源文件不一致", "请重新导入模板"),
    "precheck_failed": ("inspection", "源模板结构预检未通过", "请检查模板问题后重新导入"),
    "no_surface": ("placement", "没有可用的非塔承接面", "请调整补牌策略或新增层数"),
    "search_limit": ("placement", "本次试算未完成，暂未找到满足当前参数的补牌方案", "可单独重新运行，或查看已验收的调整方案"),
    "capacity_exhausted": ("placement", "本次未找到符合当前参数的完整补牌方案", "请查看已验收的参数调整方案"),
    "output_verification_failed": ("verification", "输出未通过独立验收", "请调整参数后单独重试该模板"),
    "recovery_verification_failed": ("recovery", "启动恢复时未能验证当前产物", "请重新运行该模板"),
    "interrupted": ("recovery", "上次运行被中断", "请单独重试该模板"),
    "processing_failed": ("processing", "模板处理未完成", "请调整参数后单独重试该模板"),
}
_PUBLIC_STAGES = frozenset({
    "import", "queued", "decode", "inspect", "inspection", "distribution",
    "placement", "generating", "register_output", "verification", "recovery",
    "processing",
})

# These errors originate in the append-only engine, not arbitrary persisted
# exception strings. Keep recovery guidance public without exposing internals.
_INCREASE_ERRORS = {
    "invalid_source": ("原模板的塔数据无法安全增加", "请检查塔牌是否从第1层连续排列且只包含普通A/B牌"),
    "no_existing_towers": ("原模板没有塔，不能给现有塔加层", "将增加方式改为“新增塔堆”"),
    "ratio_unattainable": ("原塔占比已达到或超过目标上限，增加塔牌只会让占比继续上升", "将增加模式的目标范围设在原塔占比以上，或改用减少模式；调整摆放位置不能解决占比冲突"),
    "grouping_conflict": ("未补牌的另一种牌数量不是3的倍数", "改选需要补齐数量的A或B；不能同时满足时请先检查原模板"),
    "quantity_outside_geometry": ("要求增加的牌数超过本次边界搜索范围", "减少新增数量，或调整方向、区域和塔堆数后重新预览"),
    "no_feasible_layout": ("本次未找到同时满足数量、对称和固定安全范围（X=2–14、Y=2–16）的摆法", "请查看已验收的参数调整方案；本次未找到方案不代表模板在其他参数下无解，固定安全范围不会放宽"),
    "new_tower_positions_unavailable": ("当前区域内，固定安全范围（X=2–14、Y=2–16）中没有满足原阵型约束的新增塔对称位置", "当前设置不能新增塔堆；原塔能否加层请以已验收方案为准，固定安全范围不会放宽"),
    "existing_tower_layout_unavailable": ("当前平移、方向或逐塔位置设置后的原塔不满足固定安全范围（X=2–14、Y=2–16）或原阵型占用约束", "撤回或调整本次平移、方向或逐塔位置改动后重新试算；如有已验收恢复方案，可采用其参数。本结论只适用于当前设置，固定安全范围不会放宽"),
    "stack_count_symmetry_unavailable": ("当前新增堆数不满足阵型的对称摆放要求", "将新增堆数设为0（自动），或调整堆数后重新预览"),
    "resource_budget": ("模板规模超过本次可处理范围，尚未判断有无可行方案", "请单独处理该模板并缩小摆放区域或指定塔堆数"),
    "search_budget": ("自动摆放达到本次尝试时限或次数上限，暂未找到完整方案", "可指定塔堆数、缩小摆放区域，或单独重新预览；本次尝试未完成，尚未判断有无可行方案"),
    "verification_failed": ("新增塔方案未通过独立验收", "请调整位置、数量或方向后重新预览；本次不会生成可导出的文件"),
    "invalid_increase": ("增加塔的参数或原模板结构不符合规则", "请检查数量、逐塔设置、锁定和原模板结构后重新预览"),
}
for _increase_code, (_increase_message, _increase_next) in _INCREASE_ERRORS.items():
    _PUBLIC_FAILURES['increase_' + _increase_code] = ('placement', _increase_message, _increase_next)

_ORDINARY_ADD_ERRORS = {
    'already_in_range': ('原模板总数已满足目标范围，无需新增', '如仍需补牌，可将数量目标改为新增张数18，再重新试算；不会自动改变目标范围'),
    'target_total_unattainable': ('目标总数范围与原模板或上下分配不匹配', '将范围设为高于原模板的总张数，确保其中有3的倍数；自定义上下张数之和也须使最终总数落入范围'),
    'invalid_ordinary_add': ('普通牌补入参数不合法', '请检查普通A/B类型、数量、放置策略和新增层范围'),
    'grouping_conflict': ('补入后的A/B数量无法同时满足3的倍数', '请调整补入类型和数量；工具保留原有牌的类型与数量'),
    'no_non_tower_surface': ('原模板没有可用于补入普通牌的非塔承接面', '请检查原模板的非塔牌结构'),
    'no_feasible_layout': ('本次未找到同时满足数量、对称、逐层承接和层数范围的摆法', '请查看已验收推荐方案；不会自动改变你的数量、补入位置或层数'),
    'search_limit': ('自动补牌暂未完成，不能据此判定放不下', '自动会尝试不同张数、上下分配和层数，但仍受对称、承接与安全边界约束；本次计算未完成，不是参数填写错误。可单独重试，或改为仅上方/仅下方补入后试算'),
    'verification_failed': ('普通牌补入结果未通过独立验收', '请调整参数后单独重试，本次不会生成可导出的文件'),
}
for _ordinary_code, (_ordinary_message, _ordinary_next) in _ORDINARY_ADD_ERRORS.items():
    _PUBLIC_FAILURES['ordinary_add_' + _ordinary_code] = ('placement', _ordinary_message, _ordinary_next)


def _public_failure(value, summary=None):
    """Project persisted failures to a path-free browser contract."""
    if type(value) is not dict:
        return None
    raw_code = value.get("code")
    if raw_code == 'search_budget' and type(summary) is dict and summary.get('operation') == 'increase':
        raw_code = 'increase_search_budget'
    code = raw_code if raw_code in _PUBLIC_FAILURES else "processing_failed"
    default_stage, message, recommendation = _PUBLIC_FAILURES[code]
    raw_stage = value.get("stage")
    stage = raw_stage if raw_stage in _PUBLIC_STAGES else default_stage
    context = summary.get('failure_context', {}) if type(summary) is dict else {}
    if code in ('capacity_exhausted','search_limit','minority_proximity_unavailable','minority_source_type_absent') and type(summary) is dict:
        advice=summary.get('parameter_advice') or {}
        if type(advice) is not dict or not advice.get('schemes'):
            recommendation='本次未找到通过独立验收的推荐参数，不能据此断言所有摆法都不可行；不会推荐未经验证的参数。'
    if code=='source_geometry_conflict' and type(context) is dict:
        covers=context.get('source_full_cover')
        if type(covers) in (list,tuple):
            valid=[item for item in covers[:6] if type(item) in (list,tuple) and len(item)==6
                   and all(type(number) is int and -10000<=number<=10000 for number in item)]
            if valid:
                message+='：'+ '；'.join('({}, {})，第{}→{}层'.format(item[2],item[3],item[4]+1,item[5]+1) for item in valid)
    if code == 'final_type_counts_not_multiple_of_three' and type(context) is dict:
        counts = context.get('final_counts')
        if (type(counts) in (tuple, list) and len(counts) == 2
                and all(type(count) is int and 0 <= count <= 10000 for count in counts)):
            message = '当前补牌方案完成后为A {}张、B {}张，不能同时满足3的倍数。'.format(*counts)
            source_counts = context.get('source_counts')
            if (type(source_counts) in (tuple, list) and len(source_counts) == 2
                    and all(type(count) is int and 0 <= count <= 10000 for count in source_counts)
                    and sum(source_counts) % 3):
                message = '原模板A/B合计{}张，不是3的倍数；保持总数不变时，无法使A/B数量同时为3的倍数。'.format(sum(source_counts))
                recommendation = '增加层数、改变补牌位置或降低塔占比都不能解决数量问题；请在编辑器检查原模板A/B数量后重新导入，工具不会自动增删牌'
            else:
                advice = summary.get('parameter_advice')
                if type(advice) is not dict or not advice.get('schemes'):
                    recommendation = '本次未找到已验收的具体保留数；可将“塔保留方式”改为“按占比自动分配”重新试算，塔占比上限保持不变；增加层数不能解决A/B数量问题'
    return {"code": code, "stage": stage, "message": message,
            "recommendations": [recommendation]}


def verified_increase_parameter_advice(raw, options, new_id, *, max_seconds=8, max_trials=12):
    """Offer explicit existing-tower changes only after an exact audited replay.

    All trials share one deadline. Failure to finish these bounded trials is
    never a proof that a different increase request is geometrically impossible.
    Trial bytes remain in memory and never enter the output artifact registry.
    """
    original = normalise_increase_options(options)
    source_sha = digest(raw)
    advice = {'operation': 'increase', 'status': 'not_applicable', 'schemes': [],
              'source_sha256': source_sha, 'checked_trials': 0}
    if not decode_template(raw).stacks:
        return advice
    deadline = time.monotonic() + max(0, max_seconds)
    if max_trials <= 0 or max_seconds <= 0:
        advice['status'] = 'limited'
        return advice

    base = {**original, 'operation': 'existing'}
    candidates = [base]
    # Preserve the requested quantity first. If it cannot fit, only count-mode
    # alternatives lower it, in visible three-card steps, within the trial cap.
    if original['target_mode'] == 'count':
        below = ((int(original['added_tiles']) - 1) // 3) * 3
        for index in range(max_trials - 1):
            count = below - index * 3
            if count <= 0:
                break
            candidates.append({**base, 'added_tiles': count})
    seen_outputs = set()
    limited = False
    for candidate in candidates:
        if time.monotonic() >= deadline:
            limited = True
            break
        advice['checked_trials'] += 1
        built = _build_increase(raw, candidate, new_id, deadline)
        if not built.get('ok'):
            limited |= (built.get('error') or {}).get('code') in ('search_budget', 'resource_budget')
            continue
        if built.get('output') is None:
            continue
        plan = built['plan']
        actual_added = sum(t.final_count - t.original_count for t in plan.towers)
        exact = normalise_increase_options({
            **candidate, 'translate_x': plan.translation[0], 'translate_y': plan.translation[1],
            'tower_counts': [tower.final_count for tower in plan.towers],
            'tower_positions': [{'index': tower.index, 'x': tower.x, 'y': tower.y}
                                for tower in plan.towers],
            'tower_directions': [{'index': tower.index, 'direction': tower.direction}
                                 for tower in plan.towers],
        })
        if (exact['target_mode'] == 'multiplier'
                and Decimal(str(exact['total_multiplier'])) * plan.original_tile_count
                != plan.original_tile_count + actual_added):
            exact['target_mode'] = 'count'
        if exact['target_mode'] == 'count':
            # The planner may return a nearer feasible quantity. Make that
            # compromise an explicit option change before offering adoption.
            exact['added_tiles'] = actual_added
        if time.monotonic() >= deadline:
            limited = True
            break
        replay = _build_increase(raw, exact, new_id, deadline)
        if (not replay.get('ok') or replay.get('output') is None
                or replay['output'] != built['output']):
            limited |= (replay.get('error') or {}).get('code') in ('search_budget', 'resource_budget')
            continue
        if normalise_increase_options(replay['plan'].options) != exact:
            continue
        # Do not trust a successful planner response as the public safety proof.
        report = verify_increase(raw, replay['output'], replay['plan'], new_id)
        if time.monotonic() >= deadline:
            limited = True
            break
        if not report.ok:
            continue
        output_sha = digest(replay['output'])
        if output_sha in seen_outputs:
            continue
        changes = [{'field': field, 'from': original[field], 'to': value}
                   for field, value in exact.items() if original[field] != value]
        if not changes:
            continue
        seen_outputs.add(output_sha)
        facts, towers = report.facts, replay['plan'].towers
        title = '改为原塔加层' if original['operation'] != 'existing' else '原塔加层'
        if exact['added_tiles'] != original['added_tiles']:
            title += '，调整请求新增数量'
        advice['schemes'].append({
            'verified': True, 'title': title, 'options': exact,
            'parameters': {'mode': 'increase', 'increase': exact}, 'changes': changes,
            'source_sha256': source_sha, 'output_sha256': output_sha,
            'facts': {'added_tiles': facts['added_tiles'], 'final_total': facts['final_total'],
                      'final_tower_tiles': facts['tower_tiles'],
                      'tower_ratio': facts['tower_tiles'] / facts['final_total'],
                      'new_tower_count': sum(t.original_count == 0 for t in towers),
                      'extended_tower_count': sum(t.original_count > 0 and t.final_count > t.original_count
                                                  for t in towers),
                      'tower_counts': [t.final_count for t in towers],
                      'final_counts': list(facts['final_counts']),
                      'translation': list(replay['plan'].translation)},
        })
        # A same-quantity fix fully answers the failed request. Smaller counts
        # are fallback choices only when that request could not be verified.
        if exact['added_tiles'] == original['added_tiles'] or len(advice['schemes']) >= 2:
            break
    if advice['checked_trials'] >= max_trials and not advice['schemes']:
        limited = True
    advice['status'] = 'ready' if advice['schemes'] else ('limited' if limited else 'not_found')
    return advice


def _public_sync_recovery(rows):
    """Expose recovery outcome without journal paths or nested diagnostics."""
    result = []
    for row in rows:
        if type(row) is not dict:
            result.append({"status": "recovery_required"})
            continue
        status = row.get("status")
        if status not in ("rolled_back", "committed", "recovery_required", "recovery_failed"):
            status = "recovery_required"
        item = {"status": status}
        plan_id = row.get("plan_id")
        if type(plan_id) is str and re.fullmatch(r"plan-[0-9a-f]{32}", plan_id):
            item["plan_id"] = plan_id
        result.append(item)
    return result


def _strict_object(value, allowed, required=()):
    if type(value) is not dict or set(value) - set(allowed) or not set(required).issubset(value):
        raise ApiError(400, "invalid_request", "请求字段不符合接口规则")
    return value


def _draft_transform(payload):
    if 'transform' in payload and 'translation' in payload:
        raise ApiError(400,'invalid_transform','整体变换与旧版平移参数不能同时提交')
    value=payload.get('transform',payload.get('translation',{'dx':0,'dy':0}))
    required=('quarter_turns','dx','dy') if 'transform' in payload else ('dx','dy')
    _strict_object(value,required,required)
    if any(type(value[n]) is not int or not -32<=value[n]<=32 for n in ('dx','dy')):
        raise ApiError(400,'invalid_translation','整体平移量需为-32到32之间的整数')
    q=value.get('quarter_turns',0)
    if type(q) is not int or q not in range(4):
        raise ApiError(400,'invalid_transform','整体旋转量需为0到3之间的整数')
    return value,q


def _validate_naming_rule(value, *, require_placeholder=False):
    """Validate user-editable rule syntax before saving or reserving a job."""
    token = "{原模板ID}"
    if type(value) is not str or not value or len(value.encode("utf-8")) > 300:
        raise ApiError(400, "invalid_naming_rule", "命名规则不能为空且不得超过300字节")
    count = value.count(token)
    literal = value.replace(token, "")
    if count > 1:
        raise ApiError(400, "invalid_naming_rule", "命名中最多使用一个 {原模板ID}；也可填写固定名称，同名会自动加 v2、v3")
    if not re.fullmatch(r"[A-Za-z0-9_-]*", literal):
        raise ApiError(400, "invalid_naming_rule", "命名规则的前后缀仅可使用ASCII字母、数字、下划线和连字符，不支持中文或空格")
    if not literal and count:
        raise ApiError(400, "invalid_naming_rule", "命名规则需添加前缀或后缀，生成的新ID必须不同于源ID")
    if len(literal) + int(bool(count)) > 180:
        raise ApiError(400, "invalid_naming_rule", "命名规则生成的新ID不得超过180字节，请缩短前后缀")
    return value


def _validated_template_name(options, source_id):
    try:
        return render_template_id(options["naming_rule"], source_id,
                                  override=options.get("new_id"))
    except ValueError as error:
        # These are deliberate, path-free naming errors, not internal failures.
        raise ApiError(400, "invalid_template_name", "模板命名无效：{}".format(error))


def _allocated_template_name(job, key, options, source_id, *, reserve=True):
    """Reserve identity before generation; registration still verifies uniqueness."""
    base = _validated_template_name(options, source_id)
    with job.lock:
        state = job.snapshot()
        occupied = {record['new_id'] for record in state['artifacts'].values()
                    if record['template_key'] != key}
        for other_key, row in state['templates'].items():
            if other_key == key or row['output_artifacts']:
                continue
            name = (row.get('naming') or {}).get('new_id') or (row.get('parameters') or {}).get('new_id')
            if name:
                occupied.add(name)
        name = allocate_template_id(base, occupied, source_id)
        if reserve:
            job.update_template(key, naming={'new_id': name})
        return name


def _validate_ratio_range(options):
    if options.get("mode", "reduce") == "increase":
        return
    lower, upper = options.get("min_ratio", 0.0), options.get("ratio", 0.25)
    if not (lower == upper == 0) and lower >= upper:
        raise ApiError(400, "invalid_request", "塔占比下限需小于上限；两者都为0时表示清空塔")


def _validate_reduce_source_ratio(options, document, inspection):
    """Check each immutable source after merging its own effective parameters."""
    if options.get('mode', 'reduce') != 'reduce' or not inspection.needs_optimization:
        return
    if options['ratio'] > inspection.tower_ratio:
        raise ApiError(400, 'reduce_ratio_above_source',
                       '模板 {} 的塔占比上限不能高于原模板塔占比（{:.4f}%）；请降低该模板上限或单独设置参数'.format(
                           document.template_id, inspection.tower_ratio * 100))


def _validate_options(value, *, partial=True):
    _strict_object(value, OPTION_FIELDS)
    result = {} if partial else deepcopy(DEFAULT_OPTIONS)
    for name, item in value.items():
        if name == "mode":
            if type(item) is not str or item not in ("reduce", "increase", "ordinary_add"):
                raise ApiError(400, "invalid_request", "请选择减少塔、增加塔或补入普通牌")
            result[name] = item
        elif name == "ordinary_add":
            if type(item) is not dict:
                raise ApiError(400, "invalid_request", "普通牌补入参数需为对象")
            if partial:
                # Validate the complete nested range after merging with the
                # actual batch defaults, rather than unrelated engine defaults.
                result[name] = deepcopy(item)
                continue
            try:
                normal = normalize_ordinary_add_options(item)
            except (ValueError, TypeError, OverflowError):
                raise ApiError(400, "invalid_request", "普通牌补入参数不合法，请检查数量、策略及新增层范围")
            result[name] = {key: normal[key] for key in item} if partial else normal
        elif name == "increase":
            if type(item) is not dict:
                raise ApiError(400, "invalid_request", "增加塔参数需为对象")
            try:
                normal = normalise_increase_options(
                    {"min_ratio": 0, "max_ratio": 1, **item} if partial else item)
            except (ValueError, TypeError, OverflowError):
                raise ApiError(400, "invalid_request", "增加塔参数不合法，请检查数量、占比范围及逐塔设置")
            result[name] = {key: normal[key] for key in item} if partial else normal
        elif name in ("ratio", "min_ratio"):
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not 0 <= item <= 1:
                raise ApiError(400, "invalid_request", "塔占比下限和上限需为0到100%之间的数值")
            result[name] = float(item)
        elif name == "replenish_type":
            if type(item) is not str or item not in ("auto", "A", "B", "majority_A", "majority_B", "preserve"):
                raise ApiError(400, "invalid_request", "非塔补牌类型需为自动、A或B")
            result[name] = item
        elif name == "retained_counts":
            if item is not None and (type(item) is not list or any(type(n) is not int or n < 0 for n in item)):
                raise ApiError(400, "invalid_request", "逐堆保留数量需为非负整数数组")
            result[name] = None if item is None else tuple(item)
        elif name == "strategy":
            if item not in ("top_first", "bottom_first", "balanced", "seeded_random"):
                raise ApiError(400, "invalid_request", "补牌策略不受支持")
            result[name] = item
        elif name in ("max_tiles_per_layer", "max_added_layers", "seed"):
            if type(item) is not int or (name == "max_tiles_per_layer" and item < 1) or (
                    name == "max_added_layers" and item < 0):
                raise ApiError(400, "invalid_request", "整数参数不合法")
            result[name] = item
        elif name == "naming_rule":
            result[name] = _validate_naming_rule(item)
        elif name == "new_id":
            if (type(item) is not str or not re.fullmatch(r"[A-Za-z0-9_-]+", item)
                    or len(item.encode("utf-8")) > 180):
                raise ApiError(400, "invalid_request", "单模板新ID不合法")
            result[name] = item
    if not partial:
        if result["mode"] == "increase" and "naming_rule" not in value:
            result["naming_rule"] = "moreT{原模板ID}"
        elif result["mode"] == "ordinary_add" and "naming_rule" not in value:
            result["naming_rule"] = "moreB{原模板ID}"
        _validate_ratio_range(result)
    return result


def _effective_options(defaults, overrides):
    """Merge nested options then validate cross-field constraints together."""
    result = deepcopy(defaults)
    for name, value in overrides.items():
        if name in ("increase", "ordinary_add"):
            result[name] = {**result.get(name, {}), **value}
        else:
            result[name] = value
    if result.get('mode') == 'increase':
        try:
            result['increase'] = normalise_increase_options(result.get('increase', {}))
        except (ValueError, TypeError, OverflowError):
            raise ApiError(400, 'invalid_request', '增加塔范围或逐塔配置不合法，请检查当前模板的实际参数')
    elif result.get('mode') == 'ordinary_add':
        try:
            result['ordinary_add'] = normalize_ordinary_add_options(result.get('ordinary_add', {}))
        except (ValueError, TypeError, OverflowError):
            raise ApiError(400, 'invalid_request', '普通牌补入范围配置不合法，请检查当前模板的实际参数')
    _validate_ratio_range(result)
    return result


def _result_dict(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if type(value) is dict:
        return dict(value)
    raise ValueError("service returned unsupported result")


def _inspection_summary(inspection):
    stacks = [{"anchor": list(stack.anchor), "count": stack.count,
               "z_levels": list(stack.z_levels)} for stack in inspection.stacks]
    return {
        "template_id": inspection.template_id,
        "total_tiles": inspection.total_tiles,
        # Compatibility aliases keep the transport explicit while older UI
        # builds migrate from tile_count/tower_stacks to the canonical names.
        "tile_count": inspection.total_tiles,
        "a_count": inspection.a_count,
        "b_count": inspection.b_count,
        "non_tower_a_count": inspection.non_tower_a_count,
        "non_tower_b_count": inspection.non_tower_b_count,
        "tower_count": len(inspection.stacks),
        "tower_tile_count": inspection.tower_tile_count,
        "tower_ratio": inspection.tower_ratio,
        "stacks": stacks,
        "tower_stacks": stacks,
        "non_tower_layers": list(inspection.non_tower_layers),
        "symmetry": list(inspection.symmetry),
        "profiles": [[list(offset) for offset in profile] for profile in inspection.profiles],
        "issues": [{"code": issue.code, "severity": issue.severity,
                    "message": issue.message, "evidence": _plain(issue.evidence)}
                   for issue in inspection.issues],
    }


def _plain(value):
    """Bounded JSON projection for already-audited structural evidence."""
    if type(value) in (str, int, float, bool, type(None)):
        return value
    if type(value) in (tuple, list):
        return [_plain(item) for item in value[:200]]
    if type(value) is dict:
        return {str(key): _plain(item) for key, item in list(value.items())[:200]
                if type(key) in (str, int)}
    return {"type": type(value).__name__}


def _verification_summary(value):
    if type(value) is not dict:
        return {"status": "unavailable", "ok": False, "facts": {}, "issues": []}
    errors = value.get("errors") if type(value.get("errors")) is list else []
    warnings = value.get("warnings") if type(value.get("warnings")) is list else []
    issues = []
    for severity, rows in (("error", errors), ("warning", warnings)):
        for row in rows:
            if type(row) is dict:
                issues.append({"severity": severity, "code": str(row.get("code", "unknown"))[:100],
                               "message": str(row.get("message", ""))[:500],
                               "evidence": _plain(row.get("evidence", []))})
    ok = not errors
    return {"status": "passed" if ok else "failed", "ok": ok,
            "facts": _plain(value.get("facts", {})), "issues": issues}


def _add_geometry_warnings(data,source,output=None,plan=None):
    from .verification import source_geometry_warnings
    source_warnings=source_geometry_warnings(source)
    output_warnings=source_geometry_warnings(source,output,plan) if output is not None else []
    data.update(source_geometry_warnings=source_warnings,output_geometry_warnings=output_warnings)
    for name,warnings in (('source',source_warnings),('output',output_warnings)):
        board=data.get(name)
        if board is None:continue
        if not warnings:continue
        board['geometry_warnings']=warnings
        marked={item[field] for item in warnings for field in ('lower_index','upper_index')}
        for tile in board['tiles']:
            if tile['index'] in marked:tile['inherited_full_cover']=True
    return data


def _plan_summary(inspection, distribution=None, placement=None, failure=None):
    retained = list(getattr(distribution, "retained_counts", ()))
    destinations = []
    for item in getattr(placement, "placements", ()):
        destinations.append({"tile_index": item.tile_index, "x": item.x, "y": item.y,
                             "z": item.z, "profile": list(item.profile),
                             "overlaps": _plain(item.overlaps)})
    recommendations = list(getattr(failure, "recommendations", ()))
    source_counts = {'A': inspection.a_count, 'B': inspection.b_count}
    removed = getattr(distribution, 'removed_counts', None)
    final = getattr(distribution, 'final_counts', None)
    has_type_counts = (type(removed) in (tuple, list) and len(removed) == 2
                       and type(final) in (tuple, list) and len(final) == 2)
    failure_context = {}
    for item in getattr(failure, 'evidence', ()):
        if type(item) not in (tuple, list) or len(item) != 2:
            continue
        name, value = item
        if name in ('source_counts', 'removed_counts', 'final_counts', 'remainders', 'retained_counts'):
            if (type(value) in (tuple, list) and len(value) <= 200
                    and all(type(number) is int and 0 <= number <= 10000 for number in value)):
                failure_context[name] = list(value)
        elif name == 'replenish_type' and value in ('A', 'B'):
            failure_context[name] = value
        elif name == 'replenish_count' and type(value) is int and 0 <= value <= 10000:
            failure_context[name] = value
        elif name=='source_full_cover' and type(value) in (tuple,list):
            failure_context[name]=[list(item) for item in value[:200] if type(item) in (tuple,list)
                and len(item)==6 and all(type(n) is int and -10000<=n<=10000 for n in item)]
    if failure_context:
        failure_context['code'] = getattr(failure, 'code', '')
    return {
        "status": "failed" if failure is not None else "planned",
        "source_tower_counts": [stack.count for stack in inspection.stacks],
        "retained_counts": retained,
        "source_tower_tile_count": inspection.tower_tile_count,
        "result_tower_tile_count": sum(retained) if distribution is not None else None,
        "source_tower_ratio": inspection.tower_ratio,
        "result_tower_ratio": getattr(distribution, "final_ratio", None),
        "move_type": getattr(distribution, "move_type", None),
        "move_count": getattr(distribution, "move_count", None),
        "source_tile_counts": source_counts,
        "non_tower_a_count": inspection.non_tower_a_count,
        "non_tower_b_count": inspection.non_tower_b_count,
        "removed_tile_counts": dict(zip(('A', 'B'), removed)) if has_type_counts else None,
        "added_tile_counts": ({kind: final[i]-source_counts[kind]+removed[i]
                               for i,kind in enumerate(('A', 'B'))} if has_type_counts else None),
        "result_tile_counts": dict(zip(('A', 'B'), final)) if has_type_counts else None,
        "tile_count_changes": ({kind: value - source_counts[kind]
                                for kind, value in zip(('A', 'B'), final)} if has_type_counts else None),
        "failure_context": failure_context or None,
        "added_layers": getattr(placement, "added_layers", None),
        "original_z_offset": getattr(placement, "original_z_offset", 0),
        "strategy": getattr(placement, "strategy", None),
        "seed": getattr(placement, "seed", None),
        "destinations": destinations,
        "audit": {"symmetry": list(inspection.symmetry),
                  "non_tower_layers": list(inspection.non_tower_layers),
                  "tower_count_before": len(inspection.stacks),
                  "tower_count_after": (sum(count > 0 for count in retained)
                                        if distribution is not None else None)},
        "recommendations": [str(item)[:500] for item in recommendations[:10]],
        "placement_failure": ({"requested": failure.requested, "placed": failure.planned,
                               "unplaced": failure.shortage}
                              if failure is not None and hasattr(failure, 'shortage') else None),
    }


class Application:
    """Dependency-injected local application state used by the HTTP server."""

    def __init__(self, *, tool_root, repository, settings_path, discovery_root,
                 sync_service=None, limits=None, csrf_token=None, clock=None):
        self.tool_root = Path(tool_root).resolve()
        self.frontend_root = (self.tool_root / "frontend").resolve()
        self.repository = repository
        self.settings_path = Path(settings_path).resolve()
        self.discovery_root = Path(discovery_root).resolve()
        self.limits = limits or ApiLimits()
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)
        self.clock = clock or time.time
        self.directory_actions = DirectoryActions(clock=self.clock)
        self.import_root = (self.repository.root.parent / "导入").resolve()
        self.import_root.mkdir(parents=True, exist_ok=True)
        self._uploads = threading.BoundedSemaphore(self.limits.active_uploads)
        self._zips = threading.BoundedSemaphore(self.limits.active_zip_downloads)
        self._increase_previews = threading.BoundedSemaphore(1)
        self._resource_lock = threading.Lock()
        self._active_uploads = 0
        self._active_zips = 0
        self._active_jobs = 0
        self._workers = {}
        self._worker_queues = {}
        self._worker_keys = {}
        self._worker_lock = threading.RLock()
        self._plan_lock = threading.Lock()
        self._export_plans = {}
        self._export_task_lock = threading.Lock()
        self._export_tasks = {}
        self._export_task_signatures = {}
        self._sync_plans = {}
        self._source_non_tower_counts = {}
        from .parameter_flow import ParameterFlow
        self.parameter_flow = ParameterFlow(self)
        self.ready = False
        self.sync_mutation_ready = False

        # Task 8 is deliberately delegated rather than copied here.
        if sync_service is None:
            from .sync_service import SyncService
            sync_service = SyncService(repository, settings_path=self.settings_path)
        self.sync_service = sync_service

        sync_recovery = []
        sync_recovery_failed = False
        try:
            recovered = self.sync_service.recover_pending()
            sync_recovery = list(recovered or ())
        except Exception:
            # Fail closed for mutation while preserving read-only startup.
            sync_recovery = [{"status": "recovery_failed"}]
            sync_recovery_failed = True
        sync_diagnostics = list(getattr(self.sync_service, "diagnostics", ()) or ())
        sync_recovery_required = any(
            type(row) is not dict or row.get("status") == "recovery_required"
            for row in sync_recovery
        )
        # A diagnostic means some journal or report could not be proven.  The
        # browser may keep reading local results, but no new repository write
        # authority is issued until a clean restart/recovery pass.
        self.sync_mutation_ready = not (
            sync_recovery_failed or sync_recovery_required or bool(sync_diagnostics)
        )
        try:
            self.recent_jobs = list(self.repository.discover())
        except Exception:
            self.recent_jobs = []
        self.recovery = {
            "sync": _public_sync_recovery(sync_recovery),
            "sync_locked": not self.sync_mutation_ready,
            "sync_diagnostic_count": len(sync_diagnostics),
            "invalid_jobs": sum(1 for row in self.repository.diagnostics
                                if row.get("code") == "invalid_job_state"),
        }
        self.ready = True

    # ----- resources and bootstrap -----

    def acquire_upload(self):
        if not self._uploads.acquire(blocking=False):
            raise ApiError(429, "upload_busy", "已有两个导入任务正在读取，请稍后重试")
        with self._resource_lock:
            self._active_uploads += 1

    def release_upload(self):
        with self._resource_lock:
            self._active_uploads = max(0, self._active_uploads - 1)
        self._uploads.release()

    def _reserve_job_capacity_locked(self):
        if self._active_jobs >= self.limits.active_jobs:
            raise ApiError(429, "job_busy", "后台分析任务已达到并发上限，请稍后重试")
        self._active_jobs += 1

    def _release_job_capacity_locked(self):
        self._active_jobs = max(0, self._active_jobs - 1)

    def _cleanup_plans_locked(self):
        now = float(self.clock())
        for plans in (self._export_plans, self._sync_plans):
            for plan_id, plan in list(plans.items()):
                state = plan.get("state", "ready")
                if state == "consumed" or (state == "ready" and now > plan.get("expires", 0)):
                    plans.pop(plan_id, None)

    def _store_plan(self, plans, plan_id, plan):
        with self._plan_lock:
            self._cleanup_plans_locked()
            if len(self._export_plans) + len(self._sync_plans) >= self.limits.confirmation_plans:
                raise ApiError(429, "plan_busy", "待确认计划已达到上限，请完成或等待旧计划过期")
            if plan_id in self._export_plans or plan_id in self._sync_plans:
                raise ApiError(409, "plan_collision", "确认计划编号冲突，请重新预览")
            value = dict(plan)
            value["state"] = "ready"
            plans[plan_id] = value

    def _reserve_plan(self, plans, plan_id, token, job_id=None):
        if type(plan_id) is not str or type(token) is not str:
            raise ApiError(409, "plan_expired", "确认计划不存在或已失效")
        supplied = sha256(token.encode()).digest()
        with self._plan_lock:
            self._cleanup_plans_locked()
            plan = plans.get(plan_id)
            if (not plan or plan.get("state") != "ready"
                    or not hmac.compare_digest(plan["token_hash"], supplied)
                    or (job_id is not None and plan.get("job_id") != job_id)):
                raise ApiError(409, "plan_expired", "确认计划不存在或已失效")
            plan["state"] = "committing"
            return plan

    def _finish_plan(self, plans, plan_id, plan, *, reusable):
        with self._plan_lock:
            current = plans.get(plan_id)
            if current is not plan or current.get("state") != "committing":
                return
            if reusable and float(self.clock()) <= current.get("expires", 0):
                current["state"] = "ready"
            else:
                current["state"] = "consumed"
                plans.pop(plan_id, None)

    def _ensure_sync_mutation_ready(self):
        if not self.sync_mutation_ready:
            raise ApiError(409, "sync_recovery_required",
                           "上次同步状态尚未完成可信恢复，已锁定新的同步操作")

    def _lock_sync_mutation(self):
        self.sync_mutation_ready = False
        self.recovery["sync_locked"] = True

    def bootstrap(self):
        with self._resource_lock:
            resources = {"active_uploads": self._active_uploads,
                         "active_zip_downloads": self._active_zips}
        with self._worker_lock:
            resources["active_jobs"] = self._active_jobs
        with self._plan_lock:
            self._cleanup_plans_locked()
            resources["confirmation_plans"] = len(self._export_plans) + len(self._sync_plans)
        recent = []
        for job in self.recent_jobs[:20]:
            try:
                state = job.snapshot()
                updated_ns = state.get("updated_at_ns")
                updated_at = None
                if type(updated_ns) is int and updated_ns >= 0:
                    updated_at = datetime.fromtimestamp(
                        updated_ns / 1_000_000_000, timezone.utc
                    ).isoformat().replace("+00:00", "Z")
                item = {"job_id": job.job_id, "status": state["status"],
                        "revision": state["revision"],
                        "template_count": len(state.get("templates", {}))}
                if updated_at is not None:
                    item["updated_at"] = updated_at
                recent.append(item)
            except Exception:
                continue
        return {
            "protocol": PROTOCOL_VERSION,
            "status": {"ready": self.ready, "local_only": True,
                       "sync_mutation_ready": self.sync_mutation_ready},
            "csrf_token": self.csrf_token,
            "limits": asdict(self.limits),
            "capabilities": ["multi_file_import", "template_id_import", "bulk_import_merge", "background_jobs", "rerun_template",
                             "two_phase_export", "safe_sync", "zip_download", "increase_towers", "increase_preview"],
            "resources": resources,
            "recent_jobs": recent,
            "recovery": self.recovery,
        }

    # ----- imports -----

    @staticmethod
    def _valid_upload_name(name):
        return (type(name) is str and 0 < len(name.encode("utf-8")) <= 255
                and Path(name).name == name and "/" not in name and "\\" not in name
                and name.endswith("_m.bytes"))

    @staticmethod
    def _open_repository_directory(root):
        """Open each fixed root component without traversing symbolic links.

        Keeping the final directory descriptor binds subsequent file reads to
        this checked directory even if a path is renamed while the request is
        running.  Browser input never contributes to this path.
        """
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root.anchor, flags)
        try:
            for component in root.parts[1:]:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _read_repository_template(self, repository_fd, template_id, remaining_bytes):
        filename = template_id + "_m.bytes"
        descriptor = None
        try:
            descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=repository_fd)
            evidence = os.fstat(descriptor)
            if not stat.S_ISREG(evidence.st_mode):
                return None, {"code": "template_read_rejected", "message": "模板必须是普通文件，不能使用符号链接"}
            if evidence.st_size > self.limits.file_bytes:
                return None, {"code": "file_too_large", "message": "文件超过单文件上限"}
            if evidence.st_size > remaining_bytes:
                raise ApiError(413, "import_too_large", "当前内部读取批次过大，工具将自动拆为更小批次")
            chunks = []
            size = 0
            ceiling = min(self.limits.file_bytes, remaining_bytes)
            while size <= ceiling:
                chunk = os.read(descriptor, min(1024 * 1024, ceiling - size + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > self.limits.file_bytes:
                return None, {"code": "file_too_large", "message": "文件超过单文件上限"}
            if size > remaining_bytes:
                raise ApiError(413, "import_too_large", "模板读取总大小超过单次导入上限，请拆分批次")
            return b"".join(chunks), None
        except FileNotFoundError:
            return None, {"code": "template_not_found", "message": "模板库中未找到该ID"}
        except OSError:
            return None, {"code": "template_read_rejected", "message": "无法读取模板，请检查文件权限和符号链接"}
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def import_ids(self, payload):
        _strict_object(payload, ("template_ids", "source_repository", "expected_binding"),
                       ("template_ids", "source_repository"))
        expected = payload.get("expected_binding")
        if expected is not None and (type(expected) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ApiError(400, "invalid_request", "模板库绑定编号不合法")
        requested = payload["template_ids"]
        source = payload["source_repository"]
        if (type(requested) is not list or not requested
                or any(type(value) is not str for value in requested)
                or source not in ("tile", "template")):
            raise ApiError(400, "invalid_request", "请提供模板ID数组并选择模板库")
        template_ids = list(dict.fromkeys(requested))
        if len(template_ids) > self.limits.file_count:
            raise ApiError(413, "too_many_files", "内部读取批次过大，请刷新页面使用自动分批导入")
        settings = load_settings(self.settings_path)
        suffix = TILE_SUFFIX if source == "tile" else TEMPLATE_SUFFIX
        root_value = getattr(settings, source + "_root")
        if (settings.load_error or not getattr(settings, source + "_confirmed")
                or not root_value or not validate_binding(root_value, suffix).valid):
            raise ApiError(409, "binding_not_confirmed", "请先在设置中确认所选模板库路径")
        self.acquire_upload()
        repository_fd = None
        try:
            try:
                repository_fd = self._open_repository_directory(Path(root_value))
            except OSError:
                raise ApiError(409, "binding_not_confirmed", "模板库路径已变化，请在设置中重新确认")
            info = os.fstat(repository_fd)
            binding = sha256(json.dumps([source, root_value, info.st_dev, info.st_ino]).encode()).hexdigest()
            if expected is not None and binding != expected:
                raise ApiError(409, "binding_changed", "读取来源已变化，请重新导入，避免合并不同模板库的数据")
            files, metadata = [], []
            total_bytes = 0
            for template_id in template_ids:
                valid = (len(template_id) <= 180
                         and re.fullmatch(r"[A-Za-z0-9_-]+", template_id) is not None)
                if not valid:
                    raw, error = None, {
                        "code": "invalid_template_id",
                        "message": "ID只允许字母数字下划线连字符，不含文件后缀/路径",
                    }
                else:
                    raw, error = self._read_repository_template(
                        repository_fd, template_id, self.limits.request_bytes - total_bytes)
                if raw is not None:
                    total_bytes += len(raw)
                else:
                    # Distinct failed IDs need distinct snapshots so normal
                    # SHA deduplication never collapses unrelated error rows.
                    raw = b"\x00rejected-template-id:" + sha256(
                        template_id.encode("utf-8", errors="surrogatepass")
                    ).hexdigest().encode("ascii")
                display_id = template_id.encode("utf-8", errors="replace").decode("utf-8")
                files.append((display_id[:180] + "_m.bytes", raw))
                info = {"source_repository": source, "requested_template_id": display_id[:255]}
                if error is not None:
                    info["error"] = error
                    info["size"] = 0
                metadata.append(info)
            result = self.import_files(files, _metadata=metadata, _context={
                "mode": "ids", "source_repository": source, "source_binding": binding})
            result["source_binding"] = binding
            return result
        finally:
            if repository_fd is not None:
                os.close(repository_fd)
            self.release_upload()

    def import_files(self, files, *, _metadata=None, _context=None, _inspect=True):
        if type(files) is not list or not files:
            raise ApiError(400, "invalid_multipart", "未找到模板文件")
        if len(files) > self.limits.file_count:
            raise ApiError(413, "too_many_files", "内部上传批次过大，请刷新页面使用自动分批导入")
        import_id = "import-" + secrets.token_hex(16)
        folder = self.import_root / import_id
        files_dir = folder / "files"
        files_dir.mkdir(parents=True, exist_ok=False)
        rows = []
        first_sha = {}
        for index, (original_name, raw) in enumerate(files):
            file_id = "file-" + secrets.token_hex(16)
            checksum = digest(raw)
            row = {"file_id": file_id, "original_name": original_name,
                   "sha256": checksum, "size": len(raw), "status": "ready"}
            info = _metadata[index] if _metadata is not None else {}
            row.update({key: value for key, value in info.items() if key != "error"})
            if "error" in info:
                row.update(status="failed", error=info["error"])
            elif not self._valid_upload_name(original_name):
                row.update(status="failed", error={"code": "invalid_filename",
                                                   "message": "文件名必须是无路径的 *_m.bytes"})
            elif len(raw) > self.limits.file_bytes:
                row.update(status="failed", error={"code": "file_too_large",
                                                   "message": "文件超过单文件上限"})
            elif _inspect:
                try:
                    document = decode_template(raw)
                    inspection = inspect_template(document)
                    row["inspection"] = _inspection_summary(inspection)
                    if not inspection.needs_optimization:
                        row["status"] = "no_tower"
                except Exception:
                    row.update(status="failed", error={"code": "invalid_template",
                                                       "message": "无法解析模板文件"})
            if checksum in first_sha:
                row["duplicate_of"] = first_sha[checksum]
            else:
                first_sha[checksum] = file_id
            # Opaque server filenames are used even for rejected rows so a job
            # can retain an auditable per-file failure without trusting names.
            atomic_write_bytes(files_dir / (file_id + ".bytes"), raw, readonly=True)
            rows.append(row)
        manifest = {"version": 1, "import_id": import_id, "files": rows}
        manifest["context"] = _context or {"mode": "files"}
        atomic_write_json(folder / "manifest.json", manifest)
        return {"import_id": import_id, "files": rows}

    def _load_import(self, import_id):
        if type(import_id) is not str or not IMPORT_RE.fullmatch(import_id):
            raise ApiError(400, "invalid_request", "导入编号不合法")
        folder = self.import_root / import_id
        try:
            manifest = json.loads(read_regular(folder / "manifest.json").decode("utf-8"))
        except FileNotFoundError:
            raise ApiError(404, "import_not_found", "未找到该批导入数据")
        except Exception:
            raise ApiError(409, "import_state_invalid", "导入数据已损坏，请重新导入")
        if type(manifest) is not dict or manifest.get("import_id") != import_id or type(manifest.get("files")) is not list:
            raise ApiError(409, "import_state_invalid", "导入数据已损坏，请重新导入")
        return folder, manifest

    def _import_snapshot(self, folder, row):
        if type(row) is not dict:
            raise ApiError(409, "import_state_invalid", "导入数据已损坏，请重新导入")
        file_id = row.get("file_id")
        if type(file_id) is not str or not FILE_RE.fullmatch(file_id):
            raise ApiError(409, "import_state_invalid", "导入数据已损坏，请重新导入")
        try:
            raw = read_regular(folder / "files" / (file_id + ".bytes"))
        except (OSError, ValueError):
            raise ApiError(409, "import_state_invalid", "导入数据已损坏，请重新导入")
        if digest(raw) != row.get("sha256"):
            raise ApiError(409, "import_state_invalid", "导入数据已损坏，请重新导入")
        return raw

    def import_source_preview(self, import_id, file_id):
        """Read an original board only through its registered import snapshot."""
        folder, manifest = self._load_import(import_id)
        if type(file_id) is not str or not FILE_RE.fullmatch(file_id):
            raise ApiError(400, "invalid_request", "模板编号不合法，请重新导入")
        if any(type(item) is not dict for item in manifest["files"]):
            raise ApiError(409, "import_state_invalid", "导入数据已损坏，请重新导入")
        rows = [item for item in manifest["files"] if item.get("file_id") == file_id]
        if len(rows) > 1:
            raise ApiError(409, "import_state_invalid", "导入数据已损坏，请重新导入")
        if not rows or rows[0].get("status") not in ("ready", "no_tower"):
            raise ApiError(404, "template_not_found", "未找到可预览的已导入模板")
        source = self._import_snapshot(folder, rows[0])
        try:
            data = comparison(source)
        except (ValueError, OSError, KeyError, TypeError):
            raise ApiError(409, "preview_unavailable", "模板未能通过预览校验，请重新导入")
        data.update(protocol=PROTOCOL_VERSION, import_id=import_id, file_id=file_id)
        return data

    def import_rejections(self, payload):
        """Keep oversized selections as failed rows without transmitting bytes.

        Names and sizes are client claims, not authority to read any path.
        Every row is unconditionally failed and cannot produce artifacts.
        """
        _strict_object(payload, ("files",), ("files",))
        requested = payload["files"]
        if type(requested) is not list or not requested or len(requested) > self.limits.file_count:
            raise ApiError(400, "invalid_request", "超限文件清单不合法")
        files, metadata = [], []
        for row in requested:
            _strict_object(row, ("name", "size"), ("name", "size"))
            name, size = row["name"], row["size"]
            if (not self._valid_upload_name(name) or len(name) > 255
                    or type(size) is not int or size <= self.limits.file_bytes):
                raise ApiError(400, "invalid_request", "超限文件清单不合法")
            files.append((name, b"\x00oversized-selection:" + secrets.token_bytes(32)))
            metadata.append({"size": size, "error": {"code": "file_too_large", "message":
                "该文件超过单文件 {} MB 读取限制，已跳过；请检查是否选错文件，其他模板可继续".format(
                    self.limits.file_bytes // (1024 * 1024))}})
        self.acquire_upload()
        try:
            return self.import_files(files, _metadata=metadata)
        finally:
            self.release_upload()

    def merge_imports(self, payload):
        """Merge immutable internal chunks into one user batch, one file at a time."""
        _strict_object(payload, ("import_ids",), ("import_ids",))
        ids = payload["import_ids"]
        if (type(ids) is not list or not ids
                or any(type(item) is not str or not IMPORT_RE.fullmatch(item) for item in ids)):
            raise ApiError(400, "invalid_request", "请提供有效的导入批次编号")
        ids = list(dict.fromkeys(ids))
        self.acquire_upload()
        try:
            chunks = [self._load_import(item) for item in ids]
            context = chunks[0][1].get("context")
            if any(manifest.get("context") != context for _, manifest in chunks):
                raise ApiError(409, "binding_changed", "读取来源已变化，请重新导入，避免合并不同模板库的数据")
            import_id = "import-" + secrets.token_hex(16)
            folder = self.import_root / import_id
            files_dir = folder / "files"
            files_dir.mkdir(parents=True, exist_ok=False)
            rows, first_sha, file_ids = [], {}, set()
            for source_folder, manifest in chunks:
                for source_row in manifest["files"]:
                    raw = self._import_snapshot(source_folder, source_row)
                    row = dict(source_row)
                    file_id = row["file_id"]
                    if file_id in file_ids:
                        raise ApiError(409, "import_state_invalid", "导入数据已损坏，请重新导入")
                    file_ids.add(file_id)
                    row.pop("duplicate_of", None)
                    if row["sha256"] in first_sha:
                        row["duplicate_of"] = first_sha[row["sha256"]]
                    else:
                        first_sha[row["sha256"]] = file_id
                    atomic_write_bytes(files_dir / (file_id + ".bytes"), raw, readonly=True)
                    rows.append(row)
            atomic_write_json(folder / "manifest.json", {
                "version": 1, "import_id": import_id, "files": rows,
                "context": context, "source_import_ids": ids})
            return {"import_id": import_id, "files": rows}
        finally:
            self.release_upload()

    # ----- background jobs -----

    def create_job(self, payload):
        _strict_object(payload, ("import_id", "defaults", "overrides"),
                       ("import_id", "defaults", "overrides"))
        if type(payload["defaults"]) is not dict or type(payload["overrides"]) is not dict:
            raise ApiError(400, "invalid_request", "分析参数结构不合法")
        defaults = _validate_options(payload["defaults"], partial=False)
        if defaults['mode'] == 'increase' and 'naming_rule' not in payload['defaults']:
            defaults['naming_rule'] = load_settings(self.settings_path).increase_naming_rule
        folder, manifest = self._load_import(payload["import_id"])
        file_ids = {row.get("file_id") for row in manifest["files"]}
        if (any(type(key) is not str or key not in file_ids for key in payload["overrides"])
                or any(type(value) is not dict for value in payload["overrides"].values())):
            raise ApiError(400, "invalid_request", "逐文件参数指向了未知文件")
        override_map = {key: _validate_options(value) for key, value in payload["overrides"].items()}
        options_by_sha = {}
        requested_names = {}
        import_error_by_sha = {}
        batch = sum(row.get("status") != "failed" and not row.get("duplicate_of")
                    and (defaults['mode'] in ('increase', 'ordinary_add') or row.get("status") != 'no_tower')
                    for row in manifest["files"]) > 1
        for row in manifest["files"]:
            file_id = row.get("file_id")
            raw = self._import_snapshot(folder, row)
            effective = _effective_options(defaults, override_map.get(file_id, {}))
            if row.get('status') != 'failed':
                source_doc = decode_template(raw)
                _validate_reduce_source_ratio(effective, source_doc, inspect_template(source_doc))
            if row.get("status") != "failed" and (effective['mode'] in ('increase', 'ordinary_add') or row.get('status') != 'no_tower'):
                _validate_naming_rule(effective["naming_rule"], require_placeholder=batch)
                source_id = decode_template(raw).template_id
                candidate = _validated_template_name(effective, source_id)
                requested_names.setdefault(row['sha256'], {'key':row['sha256'], 'original_id':source_id,
                                                          'override':candidate})
            options_by_sha.setdefault(row["sha256"], effective)
            if row.get("status") == "failed":
                import_error_by_sha.setdefault(row["sha256"], row.get("error"))
        for named in validate_batch_names(requested_names.values()).rows:
            options_by_sha[named['key']] = dict(options_by_sha[named['key']], new_id=named['new_id'])
        # Do not retain the entire merged upload in memory. Recheck each
        # immutable snapshot when the repository consumes it (including SHA).
        inputs = (InputSnapshot(self._import_snapshot(folder, row),
                                row.get("original_name", "uploaded_m.bytes"))
                  for row in manifest["files"])
        with self._worker_lock:
            self._reserve_job_capacity_locked()
        capacity_owned = True
        job = None
        created_keys = ()
        try:
            job = self.repository.create(inputs, parameters=defaults)
            self.recent_jobs.insert(0, job)
            state = job.snapshot()
            created_keys = tuple(state["templates"])
            for key, row in state["templates"].items():
                updates = {"parameters": options_by_sha.get(row["source_sha256"], defaults)}
                if row["source_sha256"] in import_error_by_sha:
                    updates.update(status="failed", stage="import",
                                   error=import_error_by_sha[row["source_sha256"]])
                job.update_template(key, **updates)
            self._set_progress(job, 0, len(state["templates"]))
            capacity_owned = False  # _start_worker now owns or releases it.
            self._start_worker(job, created_keys)
            return {"job_id": job.job_id, "status": "queued"}
        except ApiError:
            if capacity_owned:
                with self._worker_lock:
                    self._release_job_capacity_locked()
            if job is not None:
                self._mark_job_failed(job, created_keys, "job_start_failed")
            raise
        except Exception:
            if capacity_owned:
                with self._worker_lock:
                    self._release_job_capacity_locked()
            if job is not None:
                self._mark_job_failed(job, created_keys, "job_start_failed")
                raise ApiError(409, "job_start_failed", "分析任务未能启动，请重试")
            raise ApiError(409, "job_create_failed", "无法创建分析任务，请重试")

    def _set_progress(self, job, completed, total):
        with job.lock:
            state = job.snapshot()
            state["progress"] = {"completed_templates": int(completed),
                                 "total_templates": int(total)}
            job._save(state)

    def _mark_job_failed(self, job, keys, code="processing_failed"):
        try:
            state = job.snapshot()
            for key in keys:
                row = state.get("templates", {}).get(key, {})
                if row.get("status") in ("queued", "running", "generating", "generated"):
                    job.update_template(
                        key, status="failed", stage="processing",
                        error={"code": code, "stage": "processing",
                               "message": "后台任务未完成"},
                    )
            job.set_status("failed")
        except Exception:
            pass

    def _start_worker(self, job, keys, prepare=None):
        """Start a capacity-reserved worker behind a preparation gate.

        The caller owns one active-job slot.  A thread is proven startable
        before rerun history is mutated; the worker cannot observe state until
        preparation has completed while the lifecycle lock is still held.
        """
        gate = threading.Event()
        cancelled = threading.Event()
        worker_owns_capacity = [True]

        def withdraw():
            if self._workers.get(job.job_id) is worker:
                self._workers.pop(job.job_id, None)
                self._worker_queues.pop(job.job_id, None)
                self._worker_keys.pop(job.job_id, None)
                if worker_owns_capacity[0]:
                    worker_owns_capacity[0] = False
                    self._release_job_capacity_locked()

        def run():
            try:
                gate.wait()
                while not cancelled.is_set():
                    with self._worker_lock:
                        queue = self._worker_queues[job.job_id]
                        if not queue:
                            # Withdrawal and submission use the same lock: a
                            # request either joins this worker or starts a new
                            # one, never a queue that has already stopped.
                            withdraw()
                            return
                        batch = queue.popleft()
                    try:
                        self._run_job(job, batch)
                    except Exception:
                        self._mark_job_failed(job, batch)
                    finally:
                        with self._worker_lock:
                            # A completed key can already be queued again while
                            # later rows of the initial batch are computing.
                            self._worker_keys[job.job_id] = {
                                key for pending in self._worker_queues[job.job_id]
                                for key in pending
                            }
                            self._refresh_job_progress(job)
            finally:
                with self._worker_lock:
                    withdraw()
        worker = threading.Thread(target=run, name="tower-job-" + job.job_id[-8:], daemon=True)
        with self._worker_lock:
            if job.job_id in self._workers:
                self._release_job_capacity_locked()
                raise ApiError(409, "job_busy", "该任务正在运行")
            self._workers[job.job_id] = worker
            self._worker_queues[job.job_id] = deque((tuple(keys),))
            self._worker_keys[job.job_id] = set(keys)
            try:
                worker.start()
            except Exception:
                withdraw()
                raise
            try:
                if prepare is not None:
                    prepare()
            except Exception:
                # Cancel before opening the gate.  Capacity is released here,
                # so a failed request has no hidden running reservation.
                cancelled.set()
                withdraw()
                gate.set()
                raise
            gate.set()

    def _refresh_job_progress(self, job):
        """Summarize all rows, including work accepted during the current batch."""
        with job.lock:
            state = job.snapshot()
            previous_progress, previous_status = state.get('progress'), state.get('status')
            statuses = [row['status'] for row in state['templates'].values()]
            active = {'queued', 'running', 'generating', 'generated', 'verifying', 'validating'}
            state['progress'] = {
                'completed_templates': sum(status not in active for status in statuses),
                'total_templates': len(statuses),
            }
            if any(status in active for status in statuses):
                state['status'] = 'running'
            else:
                failures = any(status in ('failed', 'interrupted') for status in statuses)
                successes = any(status in ('verified', 'succeeded', 'no_op', 'exported') for status in statuses)
                state['status'] = 'partial_failed' if failures and successes else ('failed' if failures else 'succeeded')
            if state['progress'] != previous_progress or state['status'] != previous_status:
                job._save(state)

    def _run_job(self, job, keys):
        job.set_status("running")
        for key in keys:
            try:
                row = job.snapshot()["templates"][key]
                if row["status"] == "failed" and row.get("stage") == "import":
                    continue
                self._run_template(job, key)
            except Exception:
                try:
                    job.update_template(key, status="failed", stage="processing",
                                        error={"code": "processing_failed",
                                               "message": "模板处理未完成，可单独重试"})
                except Exception:
                    pass
            finally:
                with self._worker_lock:
                    self._worker_keys.get(job.job_id, set()).discard(key)
                    self._refresh_job_progress(job)

    def _run_template(self, job, key):
        row = job.snapshot()["templates"][key]
        options = dict(DEFAULT_OPTIONS)
        options.update(row.get("parameters") or {})
        job.update_template(key, status="running", stage="decode")
        raw = job.read_source(key)
        document = decode_template(raw)
        inspection = inspect_template(document)
        if options.get('mode') == 'increase':
            self._run_increase(job, key, raw, document, inspection, options)
            return
        if options.get('mode') == 'ordinary_add':
            self._run_ordinary_add(job, key, raw, document, inspection, options)
            return
        if not inspection.needs_optimization:
            summary = _plan_summary(inspection)
            summary.update(status="no_op", new_id=None, result_tower_ratio=0,
                           move_count=0, added_layers=0, source_sha256=digest(raw))
            job.update_template(key, status="no_op", stage="complete", naming={},
                                error=None, plan_summary=summary)
            job.add_log("原模板没有塔牌，已跳过；不命名、不生成新文件", key)
            return
        try:
            new_id = _allocated_template_name(job, key, options, document.template_id)
        except ApiError as error:
            job.update_template(key, status="failed", stage="naming",
                                error={"code": error.code, "stage": "naming", "message": error.message,
                                       "recommendations": ["修改命名规则或单模板新ID：仅使用ASCII字母、数字、下划线或连字符，长度不超过180字节且不同于源ID"]})
            return
        job.update_template(key, status="running", stage="inspect")
        retained = options.get("retained_counts")
        distribution_options = DistributionOptions(options["ratio"],
            None if retained is None else tuple(retained),
            min_ratio=options["min_ratio"], replenish_type=options["replenish_type"])
        job.update_template(key, status="running", stage="distribution")
        distribution = plan_tower_distribution(document, inspection, distribution_options)
        placement_options = PlacementOptions(options["strategy"], options["max_tiles_per_layer"],
                                             options["max_added_layers"], options["seed"])
        if not getattr(distribution, "ok", False):
            summary = _plan_summary(inspection, failure=distribution)
            self._add_parameter_advice(job, key, raw, distribution_options, placement_options, new_id, summary)
            job.update_template(key, status="failed", stage="distribution",
                                error=_safe_error(distribution),
                                plan_summary=summary)
            return
        job.update_template(key, status="generating", stage="placement")
        search_attempts=[]
        # Retry inconclusive bounded searches with the same user constraints.
        # Each fresh budget permits deeper branches; a finite final cap still
        # reports an incomplete search rather than global infeasibility.
        for state_limit,seconds in ((200000,8),(800000,20),(2000000,32)):
            if search_attempts:
                job.update_template(key,status='generating',stage='placement_expanded')
                job.add_log('正在扩大搜索范围，尝试其他合法摆法',key)
            started=time.monotonic()
            joint=plan_joint_layout(document,inspection,distribution_options,placement_options,
                                    search_budget=SearchBudget(state_limit,started+seconds))
            search_attempts.append({'state_limit':state_limit,'seconds_limit':seconds,
                'elapsed_seconds':round(time.monotonic()-started,3),
                'checked_distributions':joint.checked_distributions,'complete':joint.complete,
                'result':'verified_candidate' if joint.ok else getattr(joint.placement,'code','failed')})
            if joint.ok or getattr(joint.placement,'code',None)!='search_limit':
                break
        distribution, placement = joint.distribution, joint.placement
        if not getattr(placement, "ok", False):
            summary = _plan_summary(inspection, distribution, failure=placement)
            summary['layout_search'] = {'checked_distributions': joint.checked_distributions, 'complete': joint.complete,
                                        'attempts':search_attempts}
            if getattr(placement,'code',None)!='source_geometry_conflict':
                self._add_parameter_advice(job, key, raw, distribution_options, placement_options, new_id, summary)
            job.update_template(key, status="failed", stage="placement",
                                error=_safe_error(placement),
                                plan_summary=summary)
            return
        plan = RewritePlan(distribution, placement, distribution_options, placement_options)
        output = rewrite_template(raw, plan, new_id)
        job.update_template(key, status="generated", stage="register_output")
        artifact = job.register_output(key, output, plan, new_id)
        if artifact is None:
            return
        # register_output owns verification and the artifact capability.  The
        # API then publishes a smaller player-facing summary rather than the
        # complete persisted dataclass graph.
        summary = _plan_summary(inspection, distribution, placement)
        summary['layout_search'] = {'checked_distributions': joint.checked_distributions, 'complete': joint.complete,
                                    'attempts':search_attempts}
        summary.update(status="verified", new_id=new_id,
                       source_sha256=digest(raw), output_sha256=artifact["sha256"])
        job.update_template(key, plan_summary=summary)
        job.add_log("已完成生成与独立验证", key)

    def _run_increase(self, job, key, raw, document, inspection, options):
        new_id = _allocated_template_name(job, key, options, document.template_id)
        job.update_template(key, status='generating', stage='placement')
        job.add_log('正在计算新增塔数量、对称位置和完整展开边界', key)
        result = build_increase(raw, options['increase'], new_id)
        summary = dict(result.get('summary') or {})
        summary.update(operation='increase', source_sha256=digest(raw),
                       source_tower_ratio=inspection.tower_ratio,
                       non_tower_a_count=inspection.non_tower_a_count,
                       non_tower_b_count=inspection.non_tower_b_count)
        if not result.get('ok'):
            error = result.get('error') or {}
            code = error.get('code')
            safe_code = code if code in _INCREASE_ERRORS else 'invalid_increase'
            message, recommendation = _INCREASE_ERRORS[safe_code]
            if safe_code in {'no_feasible_layout', 'new_tower_positions_unavailable',
                             'stack_count_symmetry_unavailable', 'quantity_outside_geometry',
                             'search_budget'}:
                job.add_log('正在试算原塔加层的具体参数，并进行独立验收', key)
                try:
                    summary['parameter_advice'] = verified_increase_parameter_advice(
                        raw, options['increase'], new_id)
                except Exception:
                    summary['parameter_advice'] = {'operation': 'increase',
                        'status': 'unavailable', 'schemes': [], 'source_sha256': digest(raw)}
            summary['recommendations'] = []
            job.update_template(key, status='failed', stage='placement', plan_summary=summary,
                error={'code':'increase_'+safe_code, 'stage':'placement',
                       'message':message, 'recommendations':[recommendation]})
            return
        if result['output'] is None:
            summary.update(status='no_op', new_id=None, result_tower_ratio=inspection.tower_ratio)
            job.update_template(key, status='no_op', stage='complete', naming={}, error=None,
                                plan_summary=summary)
            job.add_log('当前目标无需增加塔牌；未命名、未生成新文件', key)
            return
        job.update_template(key, status='generated', stage='register_output')
        artifact = job.register_output(key, result['output'], result['plan'], new_id)
        if artifact is None:
            return
        # Keep aliases used by historical result views alongside explicit
        # increase facts. Registration remains the artifact trust boundary.
        summary.update(status='verified', new_id=new_id, output_sha256=artifact['sha256'],
                       result_tower_ratio=summary['final_ratio'])
        job.update_template(key, plan_summary=summary)
        job.add_log('已完成增加塔牌与独立验收', key)

    def _run_ordinary_add(self, job, key, raw, document, inspection, options):
        ordinary=options['ordinary_add']
        source_summary=dict(operation='ordinary_add', original_total=inspection.total_tiles,
            original_counts=[inspection.a_count,inspection.b_count],
            original_tower_tiles=inspection.tower_tile_count, source_tower_ratio=inspection.tower_ratio,
            source_sha256=digest(raw), tile_type=ordinary['tile_type'])
        if (ordinary['target_mode']=='total_range' and
                ordinary['min_total_tiles']<=inspection.total_tiles<=ordinary['max_total_tiles']):
            message=f"原模板已有 {inspection.total_tiles} 张，已满足目标总数 {ordinary['min_total_tiles']}–{ordinary['max_total_tiles']} 张；未新增，不生成新文件。"
            source_summary.update(no_op=True,status='no_op',new_id=None,final_total=inspection.total_tiles,
                final_counts=[inspection.a_count,inspection.b_count],added_tiles=0,
                final_tower_tiles=inspection.tower_tile_count,result_tower_ratio=inspection.tower_ratio,
                recommendations=[message,f"如仍需补入 {ordinary['tile_type']}，可将“数量目标”改为“新增张数”，填写 18（目标总数将为 {inspection.total_tiles+18}）；调整后重新试算摆放。"])
            job.update_template(key,status='no_op',stage='complete',naming={},error=None,plan_summary=source_summary)
            job.add_log(message,key)
            return
        new_id = _allocated_template_name(job, key, options, document.template_id)
        job.update_template(key, status='generating', stage='placement')
        job.add_log('正在计算普通牌补入位置与新增层范围', key)
        result = build_ordinary_add(raw, options['ordinary_add'], new_id)
        if (result.get('ok') and (type(result.get('plan')) is not OrdinaryAddPlan
                or dict(result['plan'].options) != options['ordinary_add'])):
            result = {'ok':False, 'error':{'code':'verification_failed'}}
        summary = {**source_summary, **dict(result.get('summary') or {})}
        summary.update(operation='ordinary_add', source_sha256=digest(raw),
                       source_tower_ratio=inspection.tower_ratio,
                       non_tower_a_count=inspection.non_tower_a_count,
                       non_tower_b_count=inspection.non_tower_b_count,
                       tile_type=options['ordinary_add']['tile_type'])
        if not result.get('ok'):
            code = (result.get('error') or {}).get('code')
            safe = code if code in _ORDINARY_ADD_ERRORS else 'invalid_ordinary_add'
            message, recommendation = _ORDINARY_ADD_ERRORS[safe]
            if safe in ('no_feasible_layout', 'search_limit', 'target_total_unattainable'):
                from .ordinary_add_advice import recommend_ordinary_add
                job.add_log('正在验证替代参数；原参数保持不变', key)
                summary['parameter_advice'] = recommend_ordinary_add(raw, ordinary, new_id)
            job.update_template(key, status='failed', stage='placement', plan_summary=summary,
                error={'code':'ordinary_add_' + safe, 'stage':'placement',
                       'message':message, 'recommendations':[recommendation]})
            return
        job.update_template(key, status='generated', stage='register_output')
        artifact = job.register_output(key, result['output'], result['plan'], new_id)
        if artifact is None:
            return
        output_inspection = inspect_template(decode_template(result['output']))
        summary.update(status='verified', new_id=new_id, output_sha256=artifact['sha256'],
                       original_total=inspection.total_tiles,
                       final_total=output_inspection.total_tiles,
                       added_tiles=output_inspection.total_tiles-inspection.total_tiles,
                       result_tower_ratio=output_inspection.tower_ratio,
                       original_counts=[inspection.a_count, inspection.b_count],
                       final_counts=[output_inspection.a_count, output_inspection.b_count])
        job.update_template(key, plan_summary=summary)
        job.add_log('已完成普通牌补入与独立验收', key)

    def ordinary_add_preview(self, payload):
        """Audit a temporary ordinary-add candidate without publishing bytes."""
        _strict_object(payload, ('import_id', 'file_id', 'ordinary_add'),
                       ('import_id', 'file_id', 'ordinary_add'))
        options = _validate_options({'mode':'ordinary_add', 'ordinary_add':payload['ordinary_add']},
                                    partial=False)['ordinary_add']
        folder, manifest = self._load_import(payload['import_id'])
        file_id = payload['file_id']
        if type(file_id) is not str or not FILE_RE.fullmatch(file_id):
            raise ApiError(400, 'invalid_request', '模板编号不合法，请重新导入')
        row = next((item for item in manifest['files'] if item.get('file_id') == file_id), None)
        if row is None or row.get('status') == 'failed':
            raise ApiError(404, 'template_not_found', '未找到可预览的已导入模板')
        raw = self._import_snapshot(folder, row)
        new_id = _validated_template_name({'naming_rule':'moreB{原模板ID}'},
                                           decode_template(raw).template_id)
        if not self._increase_previews.acquire(blocking=False):
            raise ApiError(429, 'preview_busy', '另一个摆放预览正在计算，请稍后重试')
        try:
            result = build_ordinary_add(raw, options, new_id)
            if result.get('ok'):
                from .verification import verify_output
                if (type(result.get('plan')) is not OrdinaryAddPlan
                        or dict(result['plan'].options) != options
                        or not verify_output(raw, result.get('output'), result.get('plan'), new_id).ok):
                    result = {'ok':False, 'error':{'code':'verification_failed'}}
            output = result.get('output') if result.get('ok') else None
            offset = result['plan'].original_z_offset if result.get('ok') else 0
            preview = comparison(raw, output, original_z_offset=offset)
            error = None
            if not result.get('ok'):
                code = (result.get('error') or {}).get('code')
                safe = code if code in _ORDINARY_ADD_ERRORS else 'invalid_ordinary_add'
                message, recommendation = _ORDINARY_ADD_ERRORS[safe]
                error = {'code':'ordinary_add_' + safe, 'message':message,
                         'recommendations':[recommendation]}
            return {'protocol':PROTOCOL_VERSION, 'ok':bool(result.get('ok')), **preview,
                    'summary':dict(result.get('summary') or {}), 'options':options,
                    'source_sha256':digest(raw), 'error':error}
        finally:
            self._increase_previews.release()

    def increase_preview(self, payload):
        _strict_object(payload, ('import_id', 'file_id', 'increase', 'new_id'),
                       ('import_id', 'file_id', 'increase'))
        options = _validate_options({'increase':payload['increase']}, partial=False)['increase']
        folder, manifest = self._load_import(payload['import_id'])
        file_id = payload['file_id']
        if type(file_id) is not str or not FILE_RE.fullmatch(file_id):
            raise ApiError(400, 'invalid_request', '模板编号不合法，请重新导入')
        row = next((item for item in manifest['files'] if item.get('file_id') == file_id), None)
        if row is None or row.get('status') == 'failed':
            raise ApiError(404, 'template_not_found', '未找到可预览的已导入模板')
        raw = self._import_snapshot(folder, row)
        source_id = decode_template(raw).template_id
        name_options = {'naming_rule':load_settings(self.settings_path).increase_naming_rule}
        if 'new_id' in payload:
            name_options.update(_validate_options({'new_id':payload['new_id']}))
        new_id = _validated_template_name(name_options, source_id)
        if not self._increase_previews.acquire(blocking=False):
            raise ApiError(429, 'preview_busy', '另一个摆放预览正在计算，请稍后重试')
        try:
            result = recommend_increase(raw, options, new_id)
            schemes = []
            for scheme in result.get('schemes', [])[:4]:
                output = scheme.get('output')
                schemes.append({'scheme_id':'scheme-'+secrets.token_hex(12),
                    'label':str(scheme['label'])[:120], 'options':scheme['options'],
                    'summary':scheme['summary'], 'preview':comparison(raw, output)})
            error = result.get('error')
            if error:
                code = error.get('code')
                safe = code if code in _INCREASE_ERRORS else 'invalid_increase'
                message, recommendation = _INCREASE_ERRORS[safe]
                error = {'code':'increase_'+safe,'message':message,'recommendations':[recommendation]}
            return {'protocol':PROTOCOL_VERSION, 'status':'ready' if schemes else 'unavailable',
                    'source_sha256':digest(raw), 'schemes':schemes, 'error':error}
        finally:
            self._increase_previews.release()

    def _add_parameter_advice(self, job, key, raw, distribution, placement, new_id, summary):
        job.add_log("正在试算可用参数调整方案，并进行独立验收", key)
        try:
            advice = verified_parameter_advice(raw, distribution, placement, new_id)
        except Exception:
            advice = {'status': 'unavailable', 'schemes': []}
        summary['parameter_advice'] = advice
        # Search traces belong to audit evidence, not user-facing instructions.
        summary['recommendations'] = []

    def _job(self, job_id):
        if type(job_id) is not str or not JOB_RE.fullmatch(job_id):
            raise ApiError(400, "invalid_request", "任务编号不合法")
        try:
            return self.repository.get(job_id)
        except KeyError:
            raise ApiError(404, "job_not_found", "未找到该分析任务")
        except Exception:
            if (self.repository.root / job_id).exists():
                raise ApiError(409, "job_state_invalid", "任务状态已损坏，无法继续")
            raise ApiError(404, "job_not_found", "未找到该分析任务")

    @staticmethod
    def _public_artifact(record):
        return {name: record[name] for name in ("id", "template_key", "new_id", "filename",
                                                "sha256", "size", "attempt_id") if name in record}

    def template_preview(self, job_id, key):
        job = self._job(job_id)
        if type(key) is not str or not TEMPLATE_RE.fullmatch(key):
            raise ApiError(404, "template_not_found", "未找到可预览的模板")
        with job.lock:
            state = job.snapshot()
            row = state['templates'].get(key)
            if row is None:
                raise ApiError(404, "template_not_found", "未找到可预览的模板")
            try:
                source = job.read_source(key)
                artifacts = row.get('output_artifacts') or []
                output = job.read_artifact(artifacts[0]) if len(artifacts)==1 else None
                offset = 0
                origins = None
                translation = {'dx': 0, 'dy': 0}
                plan=None
                original_transform=(0,0,0)
                if output is not None:
                    from .storage import _unpack
                    plan = _unpack(state['artifacts'][artifacts[0]]['plan'])
                    from .manual_edit import source_indices, initial_plan,source_transform
                    origins = source_indices(source,plan,state['artifacts'][artifacts[0]]['new_id'])
                    original_transform=source_transform(plan)
                    if type(plan) is TranslationPlan:
                        translation.update(dx=plan.dx, dy=plan.dy)
                        plan = plan.base_plan
                    baseline = rewrite_template(source, plan, state['artifacts'][artifacts[0]]['new_id'])
                    translation.update(translation_bounds(decode_template(baseline)))
                    engine_plan=initial_plan(plan)
                    if type(engine_plan) is OrdinaryAddPlan:
                        offset = engine_plan.original_z_offset
                    elif type(engine_plan) is RewritePlan:
                        offset = getattr(engine_plan.placement, 'original_z_offset', 0)
                data = comparison(source, output, original_z_offset=offset,
                                  original_xy_offset=original_transform[1:],original_quarter_turns=original_transform[0],output_origins=origins)
                _add_geometry_warnings(data,source,output,plan)
            except (ValueError, OSError, KeyError, TypeError):
                raise ApiError(409, "preview_unavailable", "模板未能通过预览校验，请重新生成或导入")
            data.update(job_id=job_id, template_key=key, revision=state['revision'])
            data.update(translation=translation, transform=dict(quarter_turns=original_transform[0],dx=original_transform[1],dy=original_transform[2]),
                        artifact_id=artifacts[0] if len(artifacts) == 1 else None)
            data['can_undo'] = len(artifacts) == 1 and job.can_undo(artifacts[0])
            return data

    def undo_template(self, job_id, key, payload):
        """Reissue one independently verified predecessor, consuming one undo step."""
        from .storage import _unpack
        _strict_object(payload, ('artifact_id',), ('artifact_id',))
        if type(key) is not str or not TEMPLATE_RE.fullmatch(key):
            raise ApiError(404, 'template_not_found', '未找到该模板')
        job = self._job(job_id)
        with self._worker_lock, job.lock:
            if job_id in self._workers:
                raise ApiError(409, 'job_busy', '任务正在运行，暂不能撤回')
            before = job.snapshot(); row = before['templates'].get(key)
            if row is None:
                raise ApiError(404, 'template_not_found', '未找到该模板')
            if row.get('output_artifacts') != [payload['artifact_id']]:
                raise ApiError(409, 'stale_artifact', '产物已变化，请刷新后重新撤回')
            try:
                previous = job.undo_predecessor(payload['artifact_id'])
                if previous is None:
                    raise ApiError(409, 'undo_unavailable', '没有可撤回的已保存微调')
                record, _ = previous
                current = before['artifacts'][payload['artifact_id']]
                source = job.read_source(key); plan = _unpack(record['plan'])
                output = rewrite_template(source, plan, current['new_id'])
            except (ValueError, KeyError, TypeError, OSError):
                raise ApiError(409, 'undo_unavailable', '历史产物未通过校验，当前产物保持不变')
            artifact = job.register_output(key, output, plan, current['new_id'],
                                           undo_previous=record.get('undo_previous'))
            if artifact is None:
                job._save(before)
                raise ApiError(409, 'undo_save_failed', '撤回未完成，当前产物保持不变')
            summary = deepcopy(record.get('plan_summary') or {})
            summary.update(new_id=current['new_id'], output_sha256=artifact['sha256'])
            job.update_template(key, plan_summary=summary)
            preview = self.template_preview(job_id, key)
            preview['artifact'] = self._public_artifact(artifact)
            return preview

    def center_template(self,job_id,key,payload):
        """Save one server-computed uniform centering revision, or skip at optimum."""
        _strict_object(payload,('artifact_id',),('artifact_id',))
        if type(key) is not str or not TEMPLATE_RE.fullmatch(key):
            raise ApiError(404,'template_not_found','未找到该模板')
        job=self._job(job_id)
        with self._worker_lock,job.lock:
            if job_id in self._workers:raise ApiError(409,'job_busy','任务正在运行，暂不能居中')
            state=job.snapshot();row=state['templates'].get(key)
            if row is None:raise ApiError(404,'template_not_found','未找到该模板')
            if row.get('output_artifacts')!=[payload['artifact_id']]:
                raise ApiError(409,'stale_artifact','产物已变化，请刷新后重新居中')
            from .storage import _unpack
            from .translation import center_translation
            try:
                output=job.read_artifact(payload['artifact_id']);record=state['artifacts'][payload['artifact_id']]
                plan=_unpack(record['plan']);current=(0,0)
                if type(plan) is TranslationPlan:
                    current=(plan.dx,plan.dy);plan=plan.base_plan
                source=job.read_source(key)
                baseline=rewrite_template(source,plan,record['new_id'])
                facts=center_translation(decode_template(output),baseline=decode_template(baseline),current=current)
            except (ValueError,TypeError,KeyError,OSError) as exc:
                raise ApiError(409,'centering_unavailable','当前产物无法安全居中')
            changed=bool(facts['dx'] or facts['dy'])
            if changed:
                result=self.translate_template(job_id,key,{'artifact_id':payload['artifact_id'],
                    'dx':facts['absolute_dx'],'dy':facts['absolute_dy']})
            else:
                result=self.template_preview(job_id,key)
                result['artifact']=self._public_artifact(record)
            result.update(centered=changed,skipped=not changed,centering=facts)
            return result

    def translate_template(self, job_id, key, payload):
        _strict_object(payload, ('artifact_id', 'dx', 'dy'), ('artifact_id', 'dx', 'dy'))
        if any(type(payload[name]) is not int or not -32 <= payload[name] <= 32 for name in ('dx', 'dy')):
            raise ApiError(400, 'invalid_translation', '整体平移量需为-32到32之间的整数')
        if type(key) is not str or not TEMPLATE_RE.fullmatch(key):
            raise ApiError(404, 'template_not_found', '未找到该模板')
        job = self._job(job_id)
        with self._worker_lock, job.lock:
            if job_id in self._workers:
                raise ApiError(409, 'job_busy', '该任务正在运行')
            state = job.snapshot()
            row = state['templates'].get(key)
            if row is None:
                raise ApiError(404, 'template_not_found', '未找到该模板')
            if row.get('output_artifacts') != [payload['artifact_id']]:
                raise ApiError(409, 'stale_artifact', '产物已更新，请刷新对比后重新调整')
            from .storage import _unpack
            try:
                job.read_artifact(payload['artifact_id'])
                record = state['artifacts'][payload['artifact_id']]
                plan = _unpack(record['plan'])
                base_plan = plan.base_plan if type(plan) is TranslationPlan else plan
                source = job.read_source(key)
                baseline = rewrite_template(source, base_plan, record['new_id'])
                validate_translation(decode_template(baseline), payload['dx'], payload['dy'])
                translated = TranslationPlan(base_plan, digest(source), payload['dx'], payload['dy'])
                output = rewrite_template(source, translated, record['new_id'])
            except (ValueError, KeyError, OSError, TypeError) as exc:
                if str(exc) == 'translation_out_of_bounds':
                    raise ApiError(409, 'translation_out_of_bounds', '整体平移超出安全区域或塔展开边界')
                raise ApiError(409, 'translation_unavailable', '当前产物无法安全平移，请重新生成')
            before = job.snapshot()
            artifact = job.register_output(key, output, translated, record['new_id'],
                                           undo_previous=record['id'])
            if artifact is None:
                # A rejected revision must not revoke the verified previous output.
                job._save(before)
                raise ApiError(409, 'translation_verification_failed', '平移结果未通过独立验收')
            summary = deepcopy(row.get('plan_summary') or {})
            summary.update(adjusted=True,translation={'dx': translated.dx, 'dy': translated.dy},
                           output_sha256=artifact['sha256'], new_id=record['new_id'])
            job.update_template(key, plan_summary=summary)
            preview = self.template_preview(job_id, key)
            preview['artifact'] = self._public_artifact(artifact)
            return preview

    def rotate_template(self,job_id,key,payload):
        """Validate and save a uniform quarter turn against the current artifact."""
        _strict_object(payload,('artifact_id','direction'),('artifact_id','direction'))
        if type(payload['direction']) is not str or payload['direction'] not in ('cw','ccw'):
            raise ApiError(400,'invalid_rotation','旋转方向需为cw或ccw')
        return self.edit_template(job_id,key,dict(artifact_id=payload['artifact_id'],deleted_indices=[],additions=[],
            transform=dict(quarter_turns=1 if payload['direction']=='cw' else 3,dx=0,dy=0)))

    def manual_recovery(self, job_id, key, payload):
        """Preview a source-bound removal seed, or commit its verified manual draft."""
        from .manual_edit import TowerRemovalPlan, revise_manual, source_indices
        from .verification import verify_output
        _strict_object(payload,('recovery_token','deleted_indices','additions','translation','transform'))
        committing=bool(payload)
        if committing:
            _strict_object(payload,('recovery_token','deleted_indices','additions','translation','transform'),
                           ('recovery_token','deleted_indices','additions'))
            if type(payload['recovery_token']) is not str:
                raise ApiError(400,'invalid_edit','删塔预览凭据不合法')
            deleted,additions=payload['deleted_indices'],payload['additions']
            if (type(deleted) is not list or len(deleted)>10000
                    or any(type(i) is not int or i<0 for i in deleted) or len(set(deleted))!=len(deleted)
                    or type(additions) is not list or len(additions)>1000):
                raise ApiError(400,'invalid_edit','增删牌数据不合法')
            for item in additions:
                _strict_object(item,('x','y','z','tile_id'),('x','y','z','tile_id'))
            translation,quarter_turns=_draft_transform(payload)
        if type(key) is not str or not TEMPLATE_RE.fullmatch(key):
            raise ApiError(404,'template_not_found','未找到该模板')
        job=self._job(job_id)
        with self._worker_lock,job.lock:
            before=job.snapshot(); row=before['templates'].get(key)
            if job_id in self._workers or before['status'] not in ('failed','partial_failed','succeeded','interrupted','no_op'):
                raise ApiError(409,'job_busy','任务正在运行，暂不能删塔补牌')
            if row is None: raise ApiError(404,'template_not_found','未找到该模板')
            options=dict(DEFAULT_OPTIONS);options.update(row.get('parameters') or {})
            mode=(row.get('options') or {}).get('mode',options.get('mode','reduce'))
            if (row.get('status') not in ('failed','interrupted') or row.get('output_artifacts')
                    or mode!='reduce'):
                raise ApiError(409,'recovery_unavailable','仅支持没有当前产物的失败减塔模板')
            try:
                source=job.read_source(key);document=decode_template(source)
                if not document.stacks: raise ValueError('源模板没有可删除的塔')
                naming=dict(options)
                if (row.get('naming') or {}).get('new_id'):
                    naming['new_id']=row['naming']['new_id']
                new_id=_allocated_template_name(job,key,naming,document.template_id,reserve=False)
                seed=TowerRemovalPlan(digest(source)); output=rewrite_template(source,seed,new_id)
                evidence=[job_id,key,digest(source),row.get('attempt_id',0),new_id,row.get('parameters'),row.get('options')]
                token=digest(json.dumps(evidence,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode())
                if not committing:
                    data=comparison(source,output,output_origins=source_indices(source,seed,new_id))
                    _add_geometry_warnings(data,source,output,seed)
                    data.update(job_id=job_id,template_key=key,revision=before['revision'],manual_recovery=True,
                                recovery_token=token,artifact_id=None,
                                translation={'dx':0,'dy':0,**translation_bounds(decode_template(output))})
                    return data
                if not hmac.compare_digest(payload['recovery_token'],token):
                    raise ApiError(409,'stale_recovery','源模板或处理批次已变化，请重新打开删塔补牌')
                plan=revise_manual(source,seed,new_id,deleted,additions,(translation['dx'],translation['dy']),quarter_turns)
                output=rewrite_template(source,plan,new_id)
                report=verify_output(source,output,plan,new_id)
            except (ValueError,TypeError,KeyError,OSError) as exc:
                raise ApiError(409,'invalid_edit',str(exc))
            if not report.ok:
                raise ApiError(409,'edit_verification_failed','；'.join(dict.fromkeys(i.message for i in report.errors))[:800])
            artifact=job.register_output(key,output,plan,new_id)
            if artifact is None:
                job._save(before)
                raise ApiError(409,'edit_save_failed','保存未完成，失败模板状态保持不变')
            result=decode_template(output);inspection=inspect_template(result)
            summary=_plan_summary(inspect_template(document))
            seed_origins=source_indices(source,seed,new_id)
            survivors={origin for i,origin in enumerate(seed_origins) if i not in plan.deleted_indices}
            removed_counts={kind:sum(t.tile_id==tile_id and t.index not in survivors for t in document.tiles)
                            for kind,tile_id in (('A',-10),('B',-11))}
            added_counts={kind:sum(t.tile_id==tile_id for t in plan.additions)
                          for kind,tile_id in (('A',-10),('B',-11))}
            original_layers={t.z for t in document.tiles if t.index in seed_origins}
            added_layers=len({t.z for t in result.tiles}-original_layers)
            removed_tower_tiles=len(document.tiles)-len(seed_origins)
            summary.update(status='verified',operation='reduce',manual_recovery=True,adjusted=True,
                manual_edit={'added':len(plan.additions),'deleted':removed_tower_tiles+len(plan.deleted_indices),
                             'removed_tower_tiles':removed_tower_tiles,'deleted_ordinary_tiles':len(plan.deleted_indices)},
                removed_tile_counts=removed_counts,added_tile_counts=added_counts,
                move_count=0,moved_count=0,move_type=None,destinations=[],added_layers=added_layers,
                retained_counts=[0]*len(document.stacks),original_z_offset=0,
                original_total=len(document.tiles),final_total=len(result.tiles),
                result_tile_counts={'A':inspection.a_count,'B':inspection.b_count},
                tile_count_changes={'A':inspection.a_count-sum(t.tile_id==-10 for t in document.tiles),
                                    'B':inspection.b_count-sum(t.tile_id==-11 for t in document.tiles)},
                final_counts=[inspection.a_count,inspection.b_count],final_ratio=0,
                result_tower_tile_count=0,final_tower_tiles=0,result_tower_ratio=0,
                source_sha256=digest(source),output_sha256=artifact['sha256'],new_id=new_id)
            summary['audit']['tower_count_after']=0
            job.update_template(key,plan_summary=summary)
            self._refresh_job_progress(job)
            data=self.template_preview(job_id,key);data['artifact']=self._public_artifact(artifact)
            return data

    def edit_template(self, job_id, key, payload):
        """Commit a draft atomically against the exact currently shown artifact."""
        from .manual_edit import revise_manual
        from .verification import verify_output
        from .storage import _unpack
        _strict_object(payload,('artifact_id','deleted_indices','additions','translation','transform'),('artifact_id','deleted_indices','additions'))
        translation,quarter_turns=_draft_transform(payload)
        deleted, additions=payload['deleted_indices'],payload['additions']
        if (type(deleted) is not list or len(deleted)>10000 or len(set(map(str,deleted)))!=len(deleted)
                or any(type(i) is not int or i<0 for i in deleted)
                or type(additions) is not list or len(additions)>1000):
            raise ApiError(400,'invalid_edit','增删牌数据不合法')
        for item in additions:
            _strict_object(item,('x','y','z','tile_id'),('x','y','z','tile_id'))
        job=self._job(job_id)
        with self._worker_lock,job.lock:
            if job_id in self._workers:raise ApiError(409,'job_busy','任务正在运行，暂不能微调')
            before=job.snapshot();row=before['templates'].get(key)
            if row is None:raise ApiError(404,'template_not_found','未找到该模板')
            if row.get('output_artifacts')!=[payload['artifact_id']]:
                raise ApiError(409,'stale_artifact','产物已变化，请保留草稿并重新核对当前模板')
            try:
                job.read_artifact(payload['artifact_id'])
                record=before['artifacts'][payload['artifact_id']]
                source=job.read_source(key)
                plan=revise_manual(source,_unpack(record['plan']),record['new_id'],deleted,additions,
                                   (translation['dx'],translation['dy']),quarter_turns)
                output=rewrite_template(source,plan,record['new_id'])
                report=verify_output(source,output,plan,record['new_id'])
            except (ValueError,TypeError,KeyError,OSError) as exc:
                raise ApiError(409,'invalid_edit',str(exc))
            if not report.ok:
                raise ApiError(409,'edit_verification_failed','；'.join(dict.fromkeys(issue.message for issue in report.errors))[:800])
            artifact=job.register_output(key,output,plan,record['new_id'],undo_previous=record['id'])
            if artifact is None:
                job._save(before)
                raise ApiError(409,'edit_save_failed','保存未完成，原验收产物保持不变')
            original=decode_template(source);result=decode_template(output);inspection=inspect_template(result)
            summary=deepcopy(row.get('plan_summary') or {})
            summary.update(adjusted=True,manual_edit={'added':len(plan.additions),'deleted':len(plan.deleted_indices)},
                original_total=len(original.tiles),final_total=len(result.tiles),
                result_tile_counts={'A':inspection.a_count,'B':inspection.b_count},
                tile_count_changes={'A':inspection.a_count-sum(t.tile_id==-10 for t in original.tiles),
                                    'B':inspection.b_count-sum(t.tile_id==-11 for t in original.tiles)},
                final_counts=[inspection.a_count,inspection.b_count],
                final_ratio=inspection.tower_tile_count/len(result.tiles) if result.tiles else 0,
                result_tower_tile_count=inspection.tower_tile_count,final_tower_tiles=inspection.tower_tile_count,
                result_tower_ratio=inspection.tower_tile_count/len(result.tiles) if result.tiles else 0,
                output_sha256=artifact['sha256'],new_id=record['new_id'])
            from .manual_edit import initial_plan,TowerRemovalPlan,source_indices
            if type(initial_plan(plan)) is TowerRemovalPlan:
                origins=source_indices(source,plan,record['new_id'])
                survivors={i for i in origins if i is not None}
                removed_tower_tiles=len(original.tiles)-len(source_indices(source,initial_plan(plan),record['new_id']))
                summary.update(
                    removed_tile_counts={kind:sum(t.tile_id==tile_id and t.index not in survivors for t in original.tiles)
                                         for kind,tile_id in (('A',-10),('B',-11))},
                    added_tile_counts={kind:sum(t.tile_id==tile_id and origins[t.index] is None for t in result.tiles)
                                       for kind,tile_id in (('A',-10),('B',-11))},
                    manual_edit={'added':sum(i is None for i in origins),'deleted':len(original.tiles)-len(survivors),
                                 'removed_tower_tiles':removed_tower_tiles,
                                 'deleted_ordinary_tiles':len(original.tiles)-len(survivors)-removed_tower_tiles},
                    added_layers=len({t.z for t in result.tiles}-{t.z for t in original.tiles if t.index in survivors}))
            job.update_template(key,plan_summary=summary)
            preview=self.template_preview(job_id,key)
            preview['artifact']=self._public_artifact(artifact)
            return preview

    def job_payload(self, job_id):
        job = self._job(job_id)
        try:
            state = job.snapshot()
            artifacts = [self._public_artifact(row) for row in job.artifact_records()]
        except Exception:
            raise ApiError(409, "job_state_invalid", "任务状态已损坏，无法继续")
        artifact_by_id = {row["id"]: row for row in artifacts}
        templates = {}
        for key, row in state["templates"].items():
            public = {name: row.get(name) for name in (
                "key", "source_template_id", "original_names", "status", "stage", "parameters",
                "options", "naming", "progress", "plan_summary", "interrupted_stage",
                "attempt_id") if name in row}
            public["error"] = _public_failure(row.get("error"), row.get('plan_summary'))
            public["current_attempt_id"] = row.get("attempt_id", 0)
            public["verification"] = _verification_summary(row.get("verification"))
            from .verification import source_geometry_warnings
            try:
                public['source_geometry_warnings']=source_geometry_warnings(job.read_source(key))
            except (ValueError,TypeError,OSError):
                public['source_geometry_warnings']=[]
            public['output_geometry_warnings']=public['verification']['facts'].get('source_geometry_warnings',[])
            structural = next((issue for issue in public['verification']['issues']
                               if issue['severity']=='error' and issue['code'] in ('unsupported','full_cover','negative_z')), None)
            if public['error'] and structural:
                public['error']['message'] = structural['message']
                public['error']['recommendations'] = ['本次结构未通过硬性验收，不能下载、导出或同步。请按明细中的牌序号、层号和坐标检查；若为源模板原牌，请先在编辑器补齐承接或错开完全重合的位置后重新导入。']
            public["artifacts"] = [artifact_by_id[item] for item in row.get("output_artifacts", ())
                                   if item in artifact_by_id]
            summary = row.get("plan_summary") if type(row.get("plan_summary")) is dict else {}
            if summary.get('operation') == 'ordinary_add' and 'original_total' not in summary:
                try:
                    original=inspect_template(decode_template(job.read_source(key)))
                    summary={**summary,'original_total':original.total_tiles,
                        'original_counts':[original.a_count,original.b_count],
                        'original_tower_tiles':original.tower_tile_count}
                    public['plan_summary']=summary
                except (ValueError,OSError,KeyError,TypeError):
                    pass  # Corrupt snapshots must not yield guessed counts.
            if summary.get('operation') == 'increase':
                # Normalize old public guidance without changing persisted jobs.
                summary = {**summary, 'recommendations': []}
                public['plan_summary'] = summary
            counts = (summary.get("non_tower_a_count"), summary.get("non_tower_b_count"))
            if not all(type(value) is int and value >= 0 for value in counts):
                # Historical jobs did not store the non-tower split. Read their
                # immutable source, never infer it from global A/B populations.
                source_sha = row.get("source_sha256")
                with self._resource_lock:
                    counts = self._source_non_tower_counts.get(source_sha)
                if counts is None:
                    try:
                        source_inspection = inspect_template(decode_template(job.read_source(key)))
                        counts = (source_inspection.non_tower_a_count, source_inspection.non_tower_b_count)
                        with self._resource_lock:
                            if len(self._source_non_tower_counts) >= 256:
                                self._source_non_tower_counts.pop(next(iter(self._source_non_tower_counts)), None)
                            self._source_non_tower_counts[source_sha] = counts
                    except (OSError, ValueError, KeyError, TypeError):
                        counts = (None, None)
            public["non_tower_a_count"], public["non_tower_b_count"] = counts
            failure = summary.get('placement_failure')
            if public['error'] and public['error']['code'] == 'capacity_exhausted' and type(failure) is dict:
                requested, placed, unplaced = (failure.get(name) for name in ('requested', 'placed', 'unplaced'))
                if (all(type(value) is int for value in (requested, placed, unplaced))
                        and 0 <= placed <= requested <= 10000 and unplaced == requested - placed):
                    public['error']['message'] = '补牌未完成：需要摆放{}张，本次排下{}张，还有{}张未能按规则摆放。'.format(requested, placed, unplaced)
            public["original_ratio"] = summary.get("source_tower_ratio")
            public["new_ratio"] = summary.get("result_tower_ratio")
            public["retained_counts"] = list(summary.get("retained_counts") or ())
            if summary.get('operation') == 'increase':
                public['placement_summary'] = '新增{}张塔牌 · {}堆塔 · 总量{}张'.format(
                    summary.get('added_tiles', 0), len(summary.get('towers') or ()),
                    summary.get('final_total', summary.get('original_total', '未记录')))
            elif summary.get('operation') == 'ordinary_add':
                public['placement_summary'] = '补入{}张普通{}牌 · 新增{}层 · 总量{}张'.format(
                    summary.get('added_tiles', 0), summary.get('tile_type', 'B'),
                    summary.get('added_layers', 0), summary.get('final_total', '未记录'))
            elif summary:
                public["placement_summary"] = "{} · 搬移 {} 张 · 新增 {} 层".format(
                    summary.get("strategy") or "无坐标搬移",
                    summary.get("move_count") if summary.get("move_count") is not None else 0,
                    summary.get("added_layers") if summary.get("added_layers") is not None else 0,
                )
            recommendations = summary.get("recommendations") or ()
            if recommendations:
                public["recommendation"] = str(recommendations[0])[:500]
            attempts = []
            for attempt in row.get("attempts", ()):
                item = {name: attempt.get(name) for name in ("status", "stage", "parameters", "naming",
                                                             "attempt_id")}
                item["error"] = _public_failure(attempt.get("error"))
                item["artifact_ids"] = list(attempt.get("output_artifacts", ()))
                attempts.append(item)
            public["attempts"] = attempts
            templates[key] = public
        progress = state.get("progress") or {
            "completed_templates": sum(row["status"] not in ("queued", "running", "generating")
                                       for row in state["templates"].values()),
            "total_templates": len(state["templates"]),
        }
        return {"protocol": PROTOCOL_VERSION, "job_id": job_id, "status": state["status"],
                "revision": state["revision"], "progress": progress,
                "logs": list(state.get("logs", ()))[-200:], "templates": templates,
                "artifacts": artifacts}

    def rerun(self, job_id, key, payload):
        _strict_object(payload, ("defaults",), ("defaults",))
        options = _validate_options(payload["defaults"], partial=False)
        if options['mode'] == 'increase' and 'naming_rule' not in payload['defaults']:
            options['naming_rule'] = load_settings(self.settings_path).increase_naming_rule
        if type(key) is not str or not TEMPLATE_RE.fullmatch(key):
            raise ApiError(400, "invalid_request", "模板键不合法")
        job = self._job(job_id)
        try:
            with self._worker_lock:
                if key in self._worker_keys.get(job_id, ()):
                    raise ApiError(409, "template_busy", "该模板正在运行或等待运行")
                if key not in job.snapshot()["templates"]:
                    raise ApiError(404, "template_not_found", "未找到该模板")
                source_doc = decode_template(job.read_source(key))
                _validate_reduce_source_ratio(options, source_doc, inspect_template(source_doc))
                if options['mode'] in ('increase', 'ordinary_add') or inspect_template(source_doc).needs_optimization:
                    _validated_template_name(options, source_doc.template_id)

                def prepare():
                    # Other rows may still be computing. Hold the repository
                    # lock across rollback so their manifest writes cannot be
                    # lost if preparing this attempt fails midway.
                    with job.lock:
                        before = job.snapshot()
                        try:
                            if options['mode'] in ('increase', 'ordinary_add') or inspect_template(source_doc).needs_optimization:
                                options['new_id'] = _allocated_template_name(job, key, options, source_doc.template_id)
                            job.start_attempt(key, parameters=options, naming={'new_id':options['new_id']} if 'new_id' in options else {})
                            job.set_status('queued')
                            self._refresh_job_progress(job)
                        except Exception:
                            # Only capabilities move; artifact bytes remain
                            # immutable and the snapshot restores all grants.
                            job._save(before)
                            raise

                # RLock keeps check, capacity reservation, attempt creation and
                # worker publication indivisible to competing rerun requests.
                if job_id in self._workers:
                    prepare()
                    self._worker_queues[job_id].append((key,))
                    self._worker_keys[job_id].add(key)
                else:
                    self._reserve_job_capacity_locked()
                    self._start_worker(job, (key,), prepare=prepare)
                attempt_id = job.snapshot()['templates'][key]['attempt_id']
        except ApiError:
            raise
        except Exception:
            raise ApiError(409, "job_state_invalid", "任务状态无法重试")
        return {"job_id": job_id, "template_key": key, "status": "queued",
                "attempt_id": attempt_id}

    # ----- settings -----

    @staticmethod
    def _settings_dict(settings):
        return {"tile_root": settings.tile_root, "template_root": settings.template_root,
                "tile_confirmed": settings.tile_confirmed,
                "template_confirmed": settings.template_confirmed,
                "export_root": settings.export_root, "naming_rule": settings.naming_rule,
                "increase_naming_rule": settings.increase_naming_rule,
                "default_sync_targets": settings.default_sync_targets,
                "config_root": settings.config_root,
                "sync_ready": settings.sync_ready}

    def get_settings(self):
        settings = load_settings(self.settings_path)
        if not settings.config_root:
            for base in (self.discovery_root, *(self.discovery_root / name for name in ("Projects", "Documents", "Desktop"))):
                candidate = base / "ulinkconfig" / "csv"
                if (candidate / "config_level.csv").is_file():
                    settings.config_root = str(candidate)
                    break
        discovery = discover_template_roots(self.discovery_root, 8, time.monotonic() + 1.0)
        return {"settings": self._settings_dict(settings),
                "load_error": bool(settings.load_error),
                "candidates": {"tile": [str(path) for path in discovery.tile_roots],
                               "template": [str(path) for path in discovery.template_roots]},
                "discovery": {"timed_out": discovery.timed_out,
                              "limit_reached": discovery.limit_reached}}

    def update_settings(self, payload):
        allowed = {"tile_root", "template_root", "tile_confirmed", "template_confirmed",
                   "export_root", "naming_rule", "increase_naming_rule", "default_sync_targets", "config_root", "recover"}
        _strict_object(payload, allowed)
        current = load_settings(self.settings_path)
        recover = payload.get("recover", False)
        if type(recover) is not bool:
            raise ApiError(400, "invalid_request", "恢复开关需为布尔值")
        if current.load_error and not recover:
            raise ApiError(409, "settings_recovery_required", "设置文件损坏，需明确选择恢复")
        settings = LocalSettings() if recover else current
        if 'default_sync_targets' in payload:
            targets = payload['default_sync_targets']
            if (type(targets) is not list or any(type(t) is not str or t not in ('tile', 'template') for t in targets)
                    or len(set(targets)) != len(targets)):
                raise ApiError(400, 'invalid_request', '默认同步目标只能选择模板仓、Tile 仓或两者，不能重复')
            settings.default_sync_targets = list(targets)
        for name in ("tile_root", "template_root", "export_root", "naming_rule", "increase_naming_rule", "config_root"):
            if name in payload:
                value = payload[name]
                if type(value) is not str:
                    raise ApiError(400, "invalid_request", "设置字段类型不合法")
                if name.endswith("_root") and value and not Path(value).is_absolute():
                    raise ApiError(400, "invalid_request", "本机目录必须是绝对路径")
                if name in ("tile_root", "template_root") and value != getattr(settings, name):
                    setattr(settings, name.replace("_root", "_confirmed"), False)
                setattr(settings, name, value)
        for name in ("tile_confirmed", "template_confirmed"):
            if name in payload:
                if type(payload[name]) is not bool:
                    raise ApiError(400, "invalid_request", "确认字段需为布尔值")
                setattr(settings, name, payload[name])
        checks = (("tile", TILE_SUFFIX), ("template", TEMPLATE_SUFFIX))
        for prefix, suffix in checks:
            if getattr(settings, prefix + "_confirmed"):
                result = validate_binding(getattr(settings, prefix + "_root"), suffix)
                if not result.valid:
                    raise ApiError(400, "binding_invalid", "目录不是指定的关卡模板库")
        _validate_naming_rule(settings.naming_rule, require_placeholder=True)
        _validate_naming_rule(settings.increase_naming_rule, require_placeholder=True)
        try:
            save_settings(settings, self.settings_path)
        except Exception:
            raise ApiError(400, "invalid_settings", "无法保存本机设置")
        return self.get_settings()

    # ----- artifacts and ZIP -----

    def download_artifact(self, job_id, artifact_id, template_keys=None, naming_rule=None):
        job = self._job(job_id)
        from .export_service import selected_template_keys
        try:
            selected=selected_template_keys(job,template_keys)
        except ValueError as exc:
            raise ApiError(400,'invalid_selection',str(exc))
        if naming_rule is not None:
            naming_rule=_validate_naming_rule(naming_rule)
            from .export_service import delivery_names
            try:
                delivery_names(job,template_keys,naming_rule)
            except ValueError as exc:
                raise ApiError(400,'invalid_naming_rule',str(exc))
        if artifact_id == "bundle":
            records = list(job.artifact_records())
            if selected is not None:
                records=[row for row in records if row['template_key'] in selected]
                if not records:
                    raise ApiError(409,'export_unavailable','选中的模板没有已验收产物')
            if sum(int(row.get("size", 0)) for row in records) > self.limits.zip_uncompressed_bytes:
                raise ApiError(413, "zip_too_large", "压缩包未压缩总大小超过上限")
            if not self._zips.acquire(blocking=False):
                raise ApiError(429, "zip_busy", "已有压缩包在生成，请稍后重试")
            with self._resource_lock:
                self._active_zips += 1
            try:
                raw = build_zip(job,template_keys=template_keys,naming_rule=naming_rule)
            except Exception:
                raise ApiError(409, "artifact_invalid", "已验证产物无法读取")
            finally:
                with self._resource_lock:
                    self._active_zips = max(0, self._active_zips - 1)
                self._zips.release()
            if len(raw) > self.limits.download_bytes:
                raise ApiError(413, "download_too_large", "下载大小超过上限")
            return raw, "tower-templates.zip", "application/zip"
        if type(artifact_id) is not str or not re.fullmatch(r"art-[0-9a-f]{32}", artifact_id):
            raise ApiError(404, "artifact_not_found", "未找到可下载的已验证产物")
        try:
            record = next(row for row in job.artifact_records() if row["id"] == artifact_id)
            if int(record["size"]) > self.limits.download_bytes:
                raise ApiError(413, "download_too_large", "下载大小超过上限")
            raw = job.read_artifact(artifact_id)
        except ApiError:
            raise
        except Exception:
            raise ApiError(404, "artifact_not_found", "未找到可下载的已验证产物")
        return raw, record["filename"], "application/octet-stream"

    # ----- two-phase export -----

    def export(self, job_id, payload):
        _strict_object(payload, ("action", "export_root", "plan_id", "confirmation_token", "conflict_decisions", "template_keys", "naming_rule", "task_id", "request_id"),
                       ("action",))
        action = payload["action"]
        if action == 'status':
            if set(payload) != {'action', 'task_id'} or type(payload['task_id']) is not str:
                raise ApiError(400, 'invalid_request', '导出进度请求不合法')
            return self._export_task_status(job_id, payload['task_id'])
        if action in ("preview", "preview_start"):
            allowed = {"action", "export_root", "template_keys", "naming_rule"}
            if action == 'preview_start':
                allowed.add('request_id')
            if set(payload) - allowed:
                raise ApiError(400, "invalid_request", "预览请求不应包含提交字段")
            if action == 'preview_start':
                return self._start_export_preview(job_id, payload)
            return self._preview_export(job_id, payload.get("export_root"),payload.get('template_keys'),payload.get('naming_rule'))
        if action not in ("commit", "start") or set(payload) != {"action", "plan_id", "confirmation_token", "conflict_decisions"}:
            raise ApiError(400, "invalid_request", "导出请求不合法")
        if type(payload["conflict_decisions"]) is not dict:
            raise ApiError(400, "invalid_request", "冲突决策不合法")
        return self._start_export(job_id, payload) if action == 'start' else self._commit_export(job_id, payload)

    def _export_task_status(self, job_id, task_id):
        # Never obtain the job lock: the writer holds it across durable IO.
        with self._export_task_lock:
            task = self._export_tasks.get(task_id)
            if task is None or task['job_id'] != job_id:
                raise ApiError(404, 'export_task_not_found', '未找到该导出任务，请重新预览')
            return deepcopy(task)

    def _start_export_preview(self, job_id, payload):
        request_id = payload.get('request_id')
        if type(request_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id):
            raise ApiError(400, 'invalid_request', '导出预览请求编号不合法')
        # Repository.get shares the writer's lock. Resolve even job existence
        # in the worker so start/recovery requests cannot stall behind its IO.
        if type(job_id) is not str or not JOB_RE.fullmatch(job_id):
            raise ApiError(400, 'invalid_request', '任务编号不合法')
        signature = sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).digest()
        with self._export_task_lock:
            self._cleanup_export_tasks_locked()
            for task_id, task in self._export_tasks.items():
                if task.get('kind') == 'preview' and task['job_id'] == job_id and task['request_id'] == request_id:
                    if not hmac.compare_digest(self._export_task_signatures[task_id], signature):
                        raise ApiError(409, 'preview_request_changed', '预览范围已变化，请重新预览')
                    return deepcopy(task)
            if len(self._export_tasks) >= self.limits.confirmation_plans:
                raise ApiError(429, 'export_busy', '导出任务已达到上限，请稍后重试')
            task_id = 'export-task-' + secrets.token_hex(16)
            self._export_task_signatures[task_id] = signature
            self._export_tasks[task_id] = {
                'task_id': task_id, 'job_id': job_id, 'plan_id': None, 'kind': 'preview',
                'request_id': request_id, 'status': 'running', 'phase': 'preparing',
                'completed': 0, 'total': 0, 'verified': 0, 'verification_total': None,
                'current_filename': None, 'result': None, 'error': None,
                'expires_at': float(self.clock()) + PLAN_TTL_SECONDS,
            }

        def progress(**update):
            with self._export_task_lock:
                self._export_tasks[task_id].update(update)

        def run():
            try:
                job = self._job(job_id)
                with job.lock:
                    result = self._preview_export(job_id, payload.get('export_root'),
                                                  payload.get('template_keys'), payload.get('naming_rule'),
                                                  progress=progress)
                progress(status='succeeded', phase='completed', result=result, plan_id=result['plan_id'],
                         current_filename=None, expires_at=float(self.clock()) + PLAN_TTL_SECONDS)
            except Exception as exc:
                error = {'code': exc.code, 'message': exc.message, 'status': exc.status} if isinstance(exc, ApiError) else {
                    'code': 'export_unavailable', 'message': '无法预览导出，请重试', 'status': 409}
                progress(status='failed', phase='failed', error=error,
                         expires_at=float(self.clock()) + PLAN_TTL_SECONDS)

        try:
            threading.Thread(target=run, name=task_id, daemon=True).start()
        except Exception:
            with self._export_task_lock:
                self._export_tasks.pop(task_id, None)
                self._export_task_signatures.pop(task_id, None)
            raise ApiError(503, 'export_busy', '无法启动导出预览，请重试')
        return self._export_task_status(job_id, task_id)

    def _cleanup_export_tasks_locked(self):
        now = float(self.clock())
        for task_id, task in list(self._export_tasks.items()):
            if task['status'] != 'running' and now > task['expires_at']:
                self._export_tasks.pop(task_id)
                self._export_task_signatures.pop(task_id, None)

    def _start_export(self, job_id, payload):
        if type(payload['plan_id']) is not str or type(payload['confirmation_token']) is not str:
            raise ApiError(409, 'plan_expired', '确认计划不存在或已失效')
        signature = sha256(json.dumps({
            'job_id': job_id, 'plan_id': payload['plan_id'],
            'confirmation_token': payload['confirmation_token'],
            'conflict_decisions': payload['conflict_decisions'],
        }, sort_keys=True, separators=(',', ':')).encode()).digest()
        with self._export_task_lock:
            now = float(self.clock())
            self._cleanup_export_tasks_locked()
            for task_id, task in self._export_tasks.items():
                if (task.get('kind') != 'preview' and task['job_id'] == job_id and task['plan_id'] == payload['plan_id']
                        and hmac.compare_digest(self._export_task_signatures[task_id], signature)):
                    return deepcopy(task)
            if len(self._export_tasks) >= self.limits.confirmation_plans:
                raise ApiError(429, 'export_busy', '导出任务已达到上限，请稍后重试')
            # Reserve before starting the worker, so simultaneous clicks cannot
            # launch two writers for the same immutable confirmation.
            plan = self._reserve_plan(self._export_plans, payload['plan_id'],
                                      payload['confirmation_token'], job_id)
            task_id = 'export-task-' + secrets.token_hex(16)
            self._export_task_signatures[task_id] = signature
            self._export_tasks[task_id] = {
                'task_id': task_id, 'job_id': job_id, 'plan_id': payload['plan_id'], 'kind': 'write',
                'status': 'running', 'phase': 'preparing', 'completed': 0,
                'verified': 0, 'verification_total': len(plan['rows']),
                'total': len(plan['rows']), 'current_filename': None,
                'result': None, 'error': None, 'expires_at': now + PLAN_TTL_SECONDS,
            }

        def progress(**update):
            with self._export_task_lock:
                self._export_tasks[task_id].update(update)

        def run():
            try:
                result = self._commit_export(job_id, deepcopy(payload), reserved_plan=plan, progress=progress)
                error = None if result['ok'] else {
                    'code': 'export_failed', 'message': '部分文件导出失败，请查看结果后重新预览', 'status': 409}
                progress(status='succeeded' if result['ok'] else 'failed',
                         phase='completed' if result['ok'] else 'failed', result=result, error=error,
                         expires_at=float(self.clock()) + PLAN_TTL_SECONDS)
            except Exception as exc:
                error = {'code': exc.code, 'message': exc.message, 'status': exc.status} if isinstance(exc, ApiError) else {
                    'code': 'export_failed', 'message': '导出提交失败，请重新预览后重试', 'status': 409}
                progress(status='failed', phase='failed', error=error,
                         expires_at=float(self.clock()) + PLAN_TTL_SECONDS)

        try:
            threading.Thread(target=run, name=task_id, daemon=True).start()
        except Exception:
            self._finish_plan(self._export_plans, payload['plan_id'], plan, reusable=True)
            with self._export_task_lock:
                self._export_tasks.pop(task_id, None)
                self._export_task_signatures.pop(task_id, None)
            raise ApiError(503, 'export_busy', '无法启动导出任务，请重试')
        return self._export_task_status(job_id, task_id)

    def _preview_export(self, job_id, export_root=None, template_keys=None, naming_rule=None, *, progress=None):
        job = self._job(job_id)
        from .export_service import selected_template_keys
        try:
            selected_template_keys(job,template_keys)
        except ValueError as exc:
            raise ApiError(400,'invalid_selection',str(exc))
        if naming_rule is not None:
            naming_rule=_validate_naming_rule(naming_rule)
            from .export_service import delivery_names
            try:
                delivery_names(job,template_keys,naming_rule)
            except ValueError as exc:
                raise ApiError(400,'invalid_naming_rule',str(exc))
        settings = load_settings(self.settings_path)
        if settings.load_error:
            raise ApiError(409, "settings_recovery_required", "设置文件损坏，需先恢复")
        target = settings.export_root if export_root is None else export_root
        if (type(target) is not str or not target or not Path(target).is_absolute()
                or ".." in Path(target).parts):
            raise ApiError(400, "invalid_export_root", "临时导出目录必须是无遍历的本机绝对路径")
        try:
            report = plan_exports(job, target, auto_version=True,template_keys=template_keys,naming_rule=naming_rule,
                                  **({'progress': progress} if progress is not None else {}))
        except Exception:
            raise ApiError(409, "export_unavailable", "无法预览导出")
        if report.rows and all(row.get("classification") == "blocked" for row in report.rows):
            raise ApiError(400, "invalid_export_root", "临时导出目录未通过安全校验")
        plan_id = "export-" + secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        items = []
        internal = {}
        for row in report.rows:
            item = {"template_key": row.get("template_key"),
                    "filename": row.get("relative_name"),
                    "original_filename": row.get("original_name"), "renamed": bool(row.get("renamed")),
                    "classification": row.get("classification")}
            if row.get("classification") == "conflict":
                conflict_id = "conflict-" + secrets.token_hex(16)
                item["conflict_id"] = conflict_id
                internal[conflict_id] = row.get("template_key")
            items.append(item)
        expires = float(self.clock()) + PLAN_TTL_SECONDS
        self._store_plan(self._export_plans, plan_id, {
            "job_id": job_id, "token_hash": sha256(token.encode()).digest(),
            "expires": expires, "target": report.target_dir,
            "rows": [dict(row) for row in report.rows],
            "conflicts": internal, "revision": job.snapshot()["revision"],
            "template_keys": tuple(template_keys) if template_keys is not None else None,
            "naming_rule": naming_rule,
        })
        return {"plan_id": plan_id, "confirmation_token": token,
                "expires_at": expires, "export_root": report.target_dir, "items": items}

    def _commit_export(self, job_id, payload, *, reserved_plan=None, progress=None):
        plan_id = payload["plan_id"]
        plan = reserved_plan if reserved_plan is not None else self._reserve_plan(
            self._export_plans, plan_id, payload["confirmation_token"], job_id)
        reusable = False
        try:
            job = self._job(job_id)
            if job.snapshot()["revision"] != plan["revision"]:
                raise ApiError(409, "plan_stale", "任务在预览后已变化，请重新预览")
            # export_outputs performs the full preflight under the job lock and
            # compares the immutable rows before writing. Avoid verifying the
            # whole batch a second time here.
            expected = plan["rows"]
            conflicts = {row.get("template_key"): row for row in expected
                         if row.get("classification") == "conflict"}
            decisions = payload["conflict_decisions"]
            if set(decisions) - set(plan["conflicts"]):
                reusable = True
                raise ApiError(400, "invalid_request", "冲突编号不属于当前计划")
            service_decisions = {}
            for conflict_id, key in plan["conflicts"].items():
                decision = decisions.get(conflict_id)
                if decision not in ("skip", "backup_and_replace"):
                    reusable = True
                    raise ApiError(409, "conflict_confirmation_required",
                                   "存在同名不同内容，需选择跳过或备份后替换")
                row = conflicts[key]
                service_decisions[key] = {
                    "job_id": job_id, "relative_name": row["relative_name"],
                    "expected_old_sha": row["expected_old_sha"], "action": decision,
                }
            try:
                report = export_outputs(job, plan["target"], service_decisions, auto_version=True,
                                        expected_rows=expected, expected_revision=plan['revision'],template_keys=plan.get('template_keys'),naming_rule=plan.get('naming_rule'),
                                        **({'progress': progress} if progress is not None else {}))
            except TargetChanged:
                raise ApiError(409, "target_changed", "目标文件或产物已变化，请重新预览导出")
            except Exception:
                raise ApiError(409, "export_failed", "导出提交失败，请重新预览后重试")
            rows = list(report.rows)
            if any((row.get("error") or {}).get("code") == "target_changed" for row in rows):
                raise ApiError(409, "target_changed", "目标文件在提交时发生变化")
            result = {"job_id": job_id, "plan_id": plan_id,
                    "items": [{"template_key": row.get("template_key"),
                               "filename": row.get("relative_name"), "status": row.get("status"),
                               "original_filename": row.get("original_name"), "renamed": bool(row.get("renamed")),
                               "backup": row.get("backup")} for row in rows],
                    "ok": all(row.get("status") in ("exported", "skipped") for row in rows)}
            result['directories'] = self.directory_actions.issue([('输出目录', plan['target'])]) if (
                result['ok'] and any(row.get('status') == 'exported' for row in rows)) else []
            return result
        finally:
            self._finish_plan(self._export_plans, plan_id, plan, reusable=reusable)

    # ----- Task-8 synchronization adapter -----

    def sync_plan(self, payload):
        _strict_object(payload, ("job_id", "bindings"), ("job_id", "bindings"))
        self._ensure_sync_mutation_ready()
        bindings = payload["bindings"]
        if (type(bindings) is not list or not bindings or len(bindings) != len(set(bindings))
                or any(item not in ("tile", "template") for item in bindings)):
            raise ApiError(400, "invalid_request", "同步目标只能使用 tile/template 逻辑绑定")
        job = self._job(payload["job_id"])
        settings = load_settings(self.settings_path)
        if settings.load_error:
            raise ApiError(409, "settings_recovery_required", "设置文件损坏，需先恢复")
        for binding in bindings:
            if not getattr(settings, binding + "_confirmed"):
                raise ApiError(409, "binding_confirmation_required", "同步目录尚未在设置中确认")
        try:
            raw = _result_dict(self.sync_service.create_sync_plan(job, tuple(bindings)))
        except Exception:
            raise ApiError(409, "sync_plan_failed", "无法创建同步预览")
        plan_id = raw.get("plan_id")
        if type(plan_id) is not str:
            raise ApiError(409, "sync_plan_failed", "同步服务未返回有效计划")
        token = secrets.token_urlsafe(32)
        expires = float(self.clock()) + PLAN_TTL_SECONDS
        self._store_plan(self._sync_plans, plan_id, {
            "token_hash": sha256(token.encode()).digest(), "expires": expires,
            "directories": [(('模板仓' if target.get('binding_key') == 'template' else 'Tile 仓'),
                              target['binding_path'])
                             for row in raw.get('rows', []) if type(row) is dict
                             for target in row.get('targets', []) if type(target) is dict
                             and target.get('binding_key') in bindings and type(target.get('binding_path')) is str],
        })
        result = dict(raw)
        result.update(confirmation_token=token, expires_at=expires)
        return result

    def sync_commit(self, payload):
        _strict_object(payload, ("plan_id", "confirmation_token", "conflict_confirmations"),
                       ("plan_id", "confirmation_token", "conflict_confirmations"))
        self._ensure_sync_mutation_ready()
        if type(payload["conflict_confirmations"]) is not dict:
            raise ApiError(400, "invalid_request", "同步冲突确认不合法")
        plan_id = payload["plan_id"]
        plan = self._reserve_plan(self._sync_plans, plan_id, payload["confirmation_token"])
        reusable = False
        try:
            result = _result_dict(self.sync_service.commit_sync(
                plan_id, payload["conflict_confirmations"]))
        except Exception:
            self._lock_sync_mutation()
            raise ApiError(409, "sync_commit_failed", "同步提交失败，未接受浏览器路径")
        finally:
            # Task 8 explicitly reports a confirmation-only preflight.  That
            # result has not crossed the mutation barrier and may reuse the
            # same SHA-bound token with corrected conflict selections.
            if "result" in locals() and result.get("status") == "confirmation_required":
                reusable = True
            self._finish_plan(self._sync_plans, plan_id, plan, reusable=reusable)
        result_rows = result.get("rows") if type(result.get("rows")) is list else ()
        if (result.get("status") in ("recovery_required", "audit_failed")
                or any(type(row) is dict and row.get("status") == "recovery_required"
                       for row in result_rows)):
            self._lock_sync_mutation()
        result['directories'] = self.directory_actions.issue(plan.get('directories', [])) if (
            result.get('status') == 'committed' and result_rows
            and all(type(row) is dict and row.get('status') in ('synced', 'skipped_same_sha')
                    for row in result_rows)) else []
        return result

    def open_directory(self, payload):
        _strict_object(payload, ('key',), ('key',))
        try:
            return self.directory_actions.open(payload['key'])
        except DirectoryActionError as error:
            raise ApiError(409, error.code, str(error))
