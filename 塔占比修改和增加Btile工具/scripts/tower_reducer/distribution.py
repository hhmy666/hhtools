"""Deterministic tower heights and one-type replenishment of moved suffixes."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from math import isfinite
from heapq import heappop, heappush
from typing import Dict, Optional, Tuple, Union

from .inspection import TemplateInspection
from .models import TemplateDocument

REPLENISH_TYPES = ('auto', 'A', 'B', 'majority_A', 'majority_B', 'preserve')
MIXED_TYPES = ('majority_A', 'majority_B', 'preserve')


@dataclass(frozen=True)
class DistributionOptions:
    ratio: float = 0.25
    retained_counts: Optional[Tuple[int, ...]] = None
    min_ratio: float = 0.0
    replenish_type: str = 'auto'


@dataclass(frozen=True)
class DistributionFailure:
    code: str
    message: str
    evidence: Tuple[object, ...]
    recommendations: Tuple[str, ...]
    ok: bool = False


@dataclass(frozen=True)
class MoveTypeSelection:
    tile_type: Optional[str]
    moved_tile_indices: Tuple[int, ...]
    ok: bool = True
    replenishment_ids: Optional[Tuple[int, ...]] = None


@dataclass(frozen=True)
class DistributionPlan:
    retained_counts: Tuple[int, ...]
    moved_tile_indices: Tuple[int, ...]
    move_type: Optional[str]
    move_count: int
    cap: int
    final_ratio: float
    ok: bool = True
    # None/None identifies historical plans that preserved each moved TileId.
    # New plans record actual removals and the final (A, B) population.
    removed_counts: Optional[Tuple[int, int]] = None
    final_counts: Optional[Tuple[int, int]] = None
    # Ordered by sorted moved Tile index; None preserves old plan semantics.
    replenishment_ids: Optional[Tuple[int, ...]] = None


def strict_tile_cap(total_tiles: int, ratio: float) -> int:
    """Largest integer count strictly below a nonzero ratio of all Tiles."""
    if isinstance(total_tiles, bool) or not isinstance(total_tiles, int) or total_tiles < 0:
        raise ValueError("total_tiles must be a nonnegative integer")
    if isinstance(ratio, bool) or not isinstance(ratio, (float, int, Decimal)):
        raise ValueError("ratio must be between 0 and 1")
    if not isfinite(float(ratio)) or not 0 <= ratio <= 1:
        raise ValueError("ratio must be between 0 and 1")
    if ratio == 0 or total_tiles == 0:
        return 0
    product = Decimal(total_tiles) * Decimal(str(ratio))
    return max(0, int(product.to_integral_value(rounding=ROUND_CEILING)) - 1)


def ratio_tile_bounds(total_tiles: int, ratio: float, min_ratio: float = 0.0) -> Tuple[int, int]:
    """Inclusive integer minimum and the existing strict integer maximum."""
    cap = strict_tile_cap(total_tiles, ratio)
    strict_tile_cap(total_tiles, min_ratio)  # Apply the same finite numeric validation.
    if min_ratio > ratio or (min_ratio == ratio and ratio != 0):
        raise ValueError('塔占比下限应小于上限；仅0%至0%可用于清空塔')
    minimum = int((Decimal(total_tiles) * Decimal(str(min_ratio))).to_integral_value(rounding=ROUND_CEILING))
    return minimum, cap


def _failure(code: str, message: str, evidence: Tuple[object, ...],
             *recommendations: str) -> DistributionFailure:
    return DistributionFailure(code, message, evidence, tuple(recommendations))


def _tower_types(document: TemplateDocument, inspection: TemplateInspection) -> Tuple[Tuple[str, ...], ...]:
    return tuple(tuple("A" if document.tiles[index].tile_id == -10 else "B"
                       for index in stack.tile_indices) for stack in inspection.stacks)


def _equalities(inspection: TemplateInspection):
    """Return source mirror pairs, original equal-height pairs, and centered stacks."""
    stacks = inspection.stacks
    anchors = [stack.anchor for stack in stacks]
    anchor_index = {anchor: i for i, anchor in enumerate(anchors)}
    mirror_pairs = set()
    centered = set()
    if anchors:
        x_sum = min(x for x, _ in anchors) + max(x for x, _ in anchors)
        y_sum = min(y for _, y in anchors) + max(y for _, y in anchors)
        for name in inspection.symmetry:
            if name == "horizontal":
                transform = lambda x, y: (x_sum - x, y)
            elif name == "vertical":
                transform = lambda x, y: (x, y_sum - y)
            elif name == "rotation":
                transform = lambda x, y: (x_sum - x, y_sum - y)
            else:
                continue
            reflected = [anchor_index.get(transform(*anchor)) for anchor in anchors]
            # Only a complete source symmetry can be preserved by the tower plan.
            if any(j is None or stacks[i].count != stacks[j].count
                   for i, j in enumerate(reflected)):
                continue
            for i, j in enumerate(reflected):
                if i == j:
                    centered.add(i)
                else:
                    mirror_pairs.add(tuple(sorted((i, j))))
    equal_pairs = {(i, j) for i in range(len(stacks)) for j in range(i + 1, len(stacks))
                   if stacks[i].count == stacks[j].count}
    return tuple(sorted(mirror_pairs)), tuple(sorted(equal_pairs)), centered


def _groups(inspection: TemplateInspection):
    mirror_pairs, equal_pairs, centered = _equalities(inspection)
    parent = list(range(len(inspection.stacks)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for i, j in mirror_pairs:
        parent[root(j)] = root(i)
    grouped: Dict[int, list] = {}
    for i in range(len(parent)):
        grouped.setdefault(root(i), []).append(i)
    groups = tuple(sorted((tuple(indices) for indices in grouped.values()),
                          key=lambda members: min(inspection.stacks[i].anchor for i in members)))
    # A lone tower on a reflection axis keeps its odd/even centered height when
    # mirror companions exist. This also keeps the accepted five-tower shape.
    has_pairs = bool(mirror_pairs)
    parity_centers = {next(iter(group)) for group in groups
                      if len(group) == 1 and next(iter(group)) in centered and has_pairs}
    return groups, mirror_pairs, equal_pairs, parity_centers


def _moved_indices(document: TemplateDocument, inspection: TemplateInspection,
                   retained_counts: Tuple[int, ...]):
    moved = []
    for stack, retained in zip(inspection.stacks, retained_counts):
        moved.extend(stack.tile_indices[retained:])
    return tuple(moved)


def _preferred_types(inspection: TemplateInspection) -> Tuple[str, ...]:
    if inspection.a_count and not inspection.b_count:
        return ("A",)
    if inspection.b_count and not inspection.a_count:
        return ("B",)
    if inspection.a_count < inspection.b_count:
        return ("A",)
    if inspection.b_count < inspection.a_count:
        return ("B",)
    return ("A", "B")


def _replenish_types(inspection, replenish_type):
    return _preferred_types(inspection) if replenish_type == 'auto' else (replenish_type,)


def _type_counts(document, inspection, moved, kind):
    removed = tuple(sum(document.tiles[index].tile_id == tile_id for index in moved)
                    for tile_id in (-10, -11))
    final = (inspection.a_count - removed[0] + (len(moved) if kind == "A" else 0),
             inspection.b_count - removed[1] + (len(moved) if kind == "B" else 0))
    return removed, final


def _divisibility_failure(document, inspection, counts, moved, kind):
    removed, final = _type_counts(document, inspection, moved, kind)
    evidence = (("source_counts", (inspection.a_count, inspection.b_count)),
                ("removed_counts", removed), ("replenish_type", kind),
                ("replenish_count", len(moved)), ("final_counts", final),
                ("remainders", tuple(n % 3 for n in final)),
                ("retained_counts", tuple(counts)))
    message = ("原有 A={}、B={}；移出 A={}、B={}，补入 {} {} 张后，"
               "最终 A={}、B={}，无法各自组成3的倍数").format(
                   inspection.a_count, inspection.b_count, *removed,
                   kind or "无", len(moved), *final)
    return _failure("final_type_counts_not_multiple_of_three", message, evidence,
                    "保持当前塔占比上限，调整逐堆保留数量，使最终A/B数量各为3的倍数")


def select_move_type(document: TemplateDocument, inspection: TemplateInspection,
                     retained_counts: Tuple[int, ...], *, cap: Optional[int] = None,
                     ratio: Optional[float] = None,
                     replenish_type: str = 'auto') -> Union[MoveTypeSelection, DistributionFailure]:
    """Choose the replenishment type; the removed suffix may contain A and B."""
    if replenish_type not in REPLENISH_TYPES:
        return _failure('invalid_replenish_type', '非塔补牌类型只能选择自动、A或B',
                        (replenish_type,), '重新选择非塔补牌类型')
    if len(retained_counts) != len(inspection.stacks):
        return _failure("invalid_target_length", "逐堆数量与塔堆数不符",
                        (len(retained_counts), len(inspection.stacks)), "按全部塔堆重新填写保留数量")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > stack.count
           for value, stack in zip(retained_counts, inspection.stacks)):
        return _failure("manual_out_of_range", "逐堆保留数量超出原塔范围",
                        tuple((stack.anchor, value, stack.count)
                              for stack, value in zip(inspection.stacks, retained_counts)),
                        "将每堆数量设为0至原塔数量之间的整数")
    moved = _moved_indices(document, inspection, retained_counts)
    if replenish_type in MIXED_TYPES:
        removed, _ = _type_counts(document, inspection, moved, None)
        base = (inspection.a_count - removed[0], inspection.b_count - removed[1])
        if replenish_type == 'preserve':
            ids = tuple(document.tiles[i].tile_id for i in sorted(moved))
        else:
            target = -10 if replenish_type == 'majority_A' else -11
            other = -11 if target == -10 else -10
            other_base = base[1 if other == -11 else 0]
            minority_count = (-other_base) % 3
            if minority_count > len(moved):
                return _divisibility_failure(document, inspection, retained_counts, moved, replenish_type)
            ids = (other,) * minority_count + (target,) * (len(moved) - minority_count)
        final = tuple(base[n] + ids.count(tile_id) for n, tile_id in enumerate((-10, -11)))
        if any(n % 3 for n in final):
            return _divisibility_failure(document, inspection, retained_counts, moved, replenish_type)
        return MoveTypeSelection(replenish_type if moved else None, moved, replenishment_ids=ids)
    types = _replenish_types(inspection, replenish_type) if moved else (None,)
    # For equal populations preserve the historical choice for a homogeneous
    # suffix. Mixed suffixes use A first, then B if only B yields valid counts.
    if len(types) > 1 and {document.tiles[i].tile_id for i in moved} == {-11}:
        types = ("B", "A")
    for kind in types:
        _, final = _type_counts(document, inspection, moved, kind)
        if not any(n % 3 for n in final):
            return MoveTypeSelection(kind, moved)
    return _divisibility_failure(document, inspection, retained_counts, moved, types[0])


def _automatic_counts(document: TemplateDocument, inspection: TemplateInspection,
                      ratio: float, cap: int, allowed_type: str, minimum: int = 0):
    groups, _, _, parity_centers = _groups(inspection)
    tower_types = _tower_types(document, inspection)
    tower_total = inspection.tower_tile_count
    if not tower_total:
        return ()
    scale = Decimal(str(ratio)) * Decimal(inspection.total_tiles) / Decimal(tower_total)
    by_original: Dict[int, list] = {}
    for group_index, members in enumerate(groups):
        original = inspection.stacks[members[0]].count
        by_original.setdefault(original, []).append((group_index, members))

    # Within one original-height class, equal-height cost is determined by
    # only N, sum(height), and sum(height²):
    #   Σ_{i<j}(height_i-height_j)² = N*sum(height²)-sum(height)².
    # Thus a class DP can merge every prefix with the same nonempty/total/residue
    # after retaining the smallest weighted square sum and coordinate tie.
    targets_a = allowed_type in ('A', 'majority_A')
    other_type = "B" if targets_a else "A"
    other_total = inspection.b_count if targets_a else inspection.a_count
    other_non_tower = other_total - sum(types.count(other_type) for types in tower_types)
    class_options = []
    for original, indexed_groups in by_original.items():
        local = {(0, 0, 0): (0, ())}  # Include retained-other residue in every merge.
        class_size = sum(len(members) for _, members in indexed_groups)
        ideal = Decimal(original) * scale
        for _, members in indexed_groups:
            choices = []
            for height in range(original + 1):
                if (members[0] in parity_centers and height > 0
                        and height % 2 != original % 2):
                    continue
                choices.append(height)
            next_local = {}
            for (nonempty, total, residue), (squares, chosen) in local.items():
                for height in choices:
                    new_total = total + len(members) * height
                    if new_total > cap:
                        continue
                    added_other = sum(tower_types[i][:height].count(other_type) for i in members)
                    key = (nonempty + len(members) * (height > 0), new_total,
                           (residue + added_other) % 3)
                    candidate = (squares + len(members) * height * height,
                                 chosen + (height,))
                    if key not in next_local or candidate < next_local[key]:
                        next_local[key] = candidate
            local = next_local
        options = []
        for (nonempty, total, residue), (squares, chosen) in local.items():
            equal_cost = class_size * squares - total * total
            scale_cost = Decimal(squares) - 2 * ideal * total + class_size * ideal * ideal
            assignments = tuple((group_index, height)
                                for (group_index, _), height in zip(indexed_groups, chosen))
            options.append((nonempty, total, residue, equal_cost, scale_cost, assignments))
        class_options.append(options)

    states = {(0, 0, 0): (0, Decimal(0), (-1,) * len(groups))}
    for options in class_options:
        next_states = {}
        for (nonempty, total, residue), (equal_cost, scale_cost, chosen) in states.items():
            for add_nonempty, add_total, add_residue, add_equal, add_scale, assignments in options:
                new_total = total + add_total
                if new_total > cap:
                    continue
                key = (nonempty + add_nonempty, new_total, (residue + add_residue) % 3)
                new_chosen = list(chosen)
                for group_index, height in assignments:
                    new_chosen[group_index] = height
                candidate = (equal_cost + add_equal, scale_cost + add_scale, tuple(new_chosen))
                if key not in next_states or candidate < next_states[key]:
                    next_states[key] = candidate
        states = next_states
    states = {key: value for key, value in states.items()
              if ((allowed_type == 'preserve' and inspection.a_count % 3 == inspection.b_count % 3 == 0)
                  or (allowed_type in ('majority_A', 'majority_B')
                      and (-(other_non_tower + key[2])) % 3 <= tower_total - key[1])
                  or (allowed_type not in MIXED_TYPES and (other_non_tower + key[2]) % 3 == 0))
              and (inspection.a_count + inspection.b_count) % 3 == 0
              and key[1] >= minimum}
    if not states:
        return None
    best_key = min(states, key=lambda key: (-key[0], states[key][0], -key[1],
                                             states[key][1], states[key][2]))
    heights = states[best_key][2]
    counts = [0] * len(inspection.stacks)
    for group, height in zip(groups, heights):
        for i in group:
            counts[i] = height
    return tuple(counts)


def _plan(document, inspection, counts, selection, cap):
    moved = selection.moved_tile_indices
    removed, final = _type_counts(document, inspection, moved, selection.tile_type)
    if selection.replenishment_ids is not None:
        final = tuple(total - count + selection.replenishment_ids.count(tile_id)
                      for total, count, tile_id in zip((inspection.a_count, inspection.b_count), removed, (-10, -11)))
    return DistributionPlan(tuple(counts), moved, selection.tile_type, len(moved), cap,
                            sum(counts) / inspection.total_tiles if inspection.total_tiles else 0.0,
                            removed_counts=removed, final_counts=final,
                            replenishment_ids=selection.replenishment_ids)


def plan_tower_distribution(document: TemplateDocument, inspection: TemplateInspection,
                            options: DistributionOptions) -> Union[DistributionPlan, DistributionFailure]:
    """Plan retained tower heights without mutating the decoded source."""
    try:
        minimum, cap = ratio_tile_bounds(inspection.total_tiles, options.ratio, options.min_ratio)
    except ValueError as exc:
        return _failure("invalid_ratio", str(exc), (options.min_ratio, options.ratio),
                        '将塔占比范围设为0%至100%，下限小于上限；清空塔时两项均设0%')
    if options.replenish_type not in REPLENISH_TYPES:
        return _failure('invalid_replenish_type', '非塔补牌类型只能选择自动、A或B',
                        (options.replenish_type,), '重新选择非塔补牌类型')
    if len(document.tiles) != inspection.total_tiles or len(document.stacks) != len(inspection.stacks):
        return _failure("source_inspection_mismatch", "源文档与检查结果不一致",
                        (len(document.tiles), inspection.total_tiles,
                         len(document.stacks), len(inspection.stacks)),
                        "重新检查当前源文档")
    if minimum > cap:
        return _failure('tower_ratio_range_unattainable', '该塔占比范围内没有可保留的整数塔牌数量',
                        ('minimum', minimum, 'cap', cap, 'total', inspection.total_tiles),
                        '扩大塔占比范围，使范围内包含至少一个整数塔牌数量')
    if (minimum > inspection.tower_tile_count
            or (options.min_ratio > 0 and not inspection.total_tiles)):
        return _failure('tower_ratio_range_unattainable', '原模板的塔牌数量低于下限，减少塔牌无法达到该范围',
                        ('original', inspection.tower_tile_count, 'minimum', minimum),
                        '将塔占比下限设为不高于原模板塔占比')
    if inspection.stacks and options.ratio > inspection.tower_ratio:
        return _failure('reduce_ratio_above_source',
                        '模板 {} 的塔占比上限不能高于原模板塔占比（{:.4f}%）'.format(
                            document.template_id, inspection.tower_ratio * 100),
                        ('source_ratio', inspection.tower_ratio, 'ratio', options.ratio),
                        '降低当前模板的塔占比上限，或为该模板单独设置参数')
    if inspection.stacks and options.ratio == inspection.tower_ratio:
        # A rounded float source ratio can multiply to just above the source
        # integer count. Equality must still remove at least one tower Tile.
        cap = min(cap, inspection.tower_tile_count - 1)
        if minimum > cap:
            return _failure('tower_ratio_range_unattainable', '该塔占比范围内没有可保留的整数塔牌数量',
                            ('minimum', minimum, 'cap', cap, 'total', inspection.total_tiles),
                            '扩大塔占比范围，使范围内包含至少一个整数塔牌数量')
    if not inspection.stacks:
        selection = select_move_type(document, inspection, (), replenish_type=options.replenish_type)
        return selection if not selection.ok else _plan(document, inspection, (), selection, cap)
    blockers = tuple(issue for issue in inspection.issues if issue.severity == "error"
                     and issue.code != "ab_count_not_multiple_of_three")
    if blockers:
        return _failure("precheck_failed", "当前搬移无法安全执行：" + "；".join(dict.fromkeys(issue.message for issue in blockers)),
                        tuple((issue.code, issue.evidence) for issue in blockers),
                        "原模板已读取且不会被修改；需确认这些条件下的安全搬移方式")

    if options.retained_counts is not None:
        counts = options.retained_counts
        example = ()  # Candidate advice must also pass joint placement verification.
        if len(counts) != len(inspection.stacks):
            return _failure("invalid_target_length", "逐堆数量与塔堆数不符",
                            (len(counts), len(inspection.stacks)),
                            "按全部塔堆重新填写保留数量", *example)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > stack.count
               for value, stack in zip(counts, inspection.stacks)):
            return _failure("manual_out_of_range", "逐堆保留数量超出原塔范围",
                            tuple((stack.anchor, value, stack.count)
                                  for stack, value in zip(inspection.stacks, counts)),
                            "将每堆数量设为0至原塔数量之间的整数", *example)
        _, mirror_pairs, _, parity_centers = _groups(inspection)
        broken = tuple((i, j, counts[i], counts[j]) for i, j in mirror_pairs if counts[i] != counts[j])
        if broken:
            return _failure("manual_symmetry", "镜像塔的保留数量不同", broken,
                            "将镜像塔设为相同数量", *example)
        broken = tuple((i, inspection.stacks[i].count, counts[i]) for i in parity_centers
                       if counts[i] > 0 and counts[i] % 2 != inspection.stacks[i].count % 2)
        if broken:
            return _failure("manual_center_parity", "对称轴上的塔高度奇偶发生变化", broken,
                            "保持中心塔的原有奇偶高度", *example)
        if sum(counts) > cap:
            return _failure("tower_ratio_exceeded", "目标塔牌超过严格比例上限",
                            ("target", sum(counts), "cap", cap, "ratio", options.ratio),
                            "减少逐堆保留数量", *example)
        if sum(counts) < minimum:
            return _failure('tower_ratio_below_minimum', '目标塔牌数量低于塔占比下限',
                            ('target', sum(counts), 'minimum', minimum, 'min_ratio', options.min_ratio),
                            '增加逐堆保留数量，使最终塔占比不低于下限')
        selection = select_move_type(document, inspection, counts, cap=cap, ratio=options.ratio,
                                     replenish_type=options.replenish_type)
        if isinstance(selection, DistributionFailure):
            return selection
    else:
        types = _replenish_types(inspection, options.replenish_type)
        candidates = []
        for tile_type in types:
            candidate = _automatic_counts(document, inspection, options.ratio, cap, tile_type, minimum)
            if candidate is not None:
                selection = select_move_type(document, inspection, candidate,
                                             replenish_type=options.replenish_type)
                if not isinstance(selection, DistributionFailure):
                    candidates.append((candidate, selection))
        if not candidates:
            if minimum:
                return _failure('tower_ratio_range_unattainable',
                                '该塔占比范围内没有同时满足塔形与最终A/B各为3的倍数的保留数量',
                                ('minimum', minimum, 'cap', cap, 'replenish_type', options.replenish_type),
                                '调整塔占比范围或补牌类型后重新计算')
            counts = (0,) * len(inspection.stacks)
            return _divisibility_failure(document, inspection, counts,
                                         _moved_indices(document, inspection, counts), types[0])
        counts, selection = candidates[0]  # Equal A/B counts resolve to A when both work.
    return _plan(document, inspection, counts, selection, cap)


def iter_tower_distributions(document, inspection, options, budget):
    """Explore legal tower populations below the unchanged original cap.

    Keep the incumbent shape first. The caller owns the joint allocation and
    geometry budget. Explicit per-tower input is final and never reallocated.
    """
    primary = plan_tower_distribution(document, inspection, options)
    if not primary.ok:
        return
    budget.use()
    yield primary
    if options.retained_counts is not None or not inspection.stacks:
        return
    groups, _, equal_pairs, parity_centers = _groups(inspection)
    minimum, _ = ratio_tile_bounds(inspection.total_tiles, options.ratio, options.min_ratio)
    scale = Decimal(str(options.ratio)) * inspection.total_tiles / inspection.tower_tile_count
    heap, domains, seen = [], {}, set()
    yielded = {primary.retained_counts}

    def expand_counts(heights):
        counts = [0] * len(inspection.stacks)
        for members, height in zip(groups, heights):
            for index in members:
                counts[index] = height
        return tuple(counts)

    def push(kind, heights):
        key = (kind, heights)
        if key in seen:
            return
        budget.use()
        seen.add(key)
        counts = expand_counts(heights)
        if sum(counts) > primary.cap:
            return
        score = (-sum(count > 0 for count in counts),
                 sum((counts[i]-counts[j])**2 for i,j in equal_pairs), -sum(counts),
                 sum((Decimal(count)-Decimal(stack.count)*scale)**2
                     for count,stack in zip(counts,inspection.stacks)), counts)
        heappush(heap, (score, kind, heights))

    for kind in _replenish_types(inspection, options.replenish_type):
        choices = []
        for members in groups:
            original = inspection.stacks[members[0]].count
            choices.append(tuple(height for height in range(original + 1)
                if members[0] not in parity_centers or height == 0 or height % 2 == original % 2))
        start = _automatic_counts(document, inspection, options.ratio, primary.cap, kind, minimum)
        if start is not None:
            domains[kind] = tuple(choices)
            push(kind, tuple(start[members[0]] for members in groups))
    while heap:
        budget.use()
        _, kind, heights = heappop(heap)
        counts = expand_counts(heights)
        if counts not in yielded and sum(counts) >= minimum:
            selection = select_move_type(document, inspection, counts, cap=primary.cap, ratio=options.ratio,
                                         replenish_type=options.replenish_type)
            if selection.ok:
                yielded.add(counts)
                yield _plan(document, inspection, counts, selection, primary.cap)
        for index, choices in enumerate(domains[kind]):
            position = choices.index(heights[index])
            for neighbour in (position-1, position+1):
                if 0 <= neighbour < len(choices):
                    candidate = list(heights)
                    candidate[index] = choices[neighbour]
                    push(kind, tuple(candidate))
