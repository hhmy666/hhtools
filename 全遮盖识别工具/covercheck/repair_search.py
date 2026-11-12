"""Bounded local repair search. Every accepted layout passes the source audit.

Search is deliberately finite: ordinary hidden locations, at most three hidden
source helpers, small count changes, and finally existing tower members. Failure
does not claim mathematical infeasibility and never changes the source bytes.
"""

from collections import Counter, defaultdict
from itertools import combinations, combinations_with_replacement
import time

from .repair import (_geometry, _overlaps, _covered, _unsupported, _new_unsupported, _tower_plans,
                     _removal_variants, rewrite_tiles)


PHASE_SECONDS = 1.5
PHASE_NODES = 24000
MAX_REMOVAL_PLANS = 48
MAX_HELPER_PLANS = 48
MAX_SLOTS = 128
PREFERRED_PLAN_COUNT = 2
PREFERRED_PLACEMENT_NODES = 2000


class _Limit(Exception):
    pass


class _ProbeLimit(Exception):
    pass


class _Budget:
    def __init__(self):
        from . import repair
        self.nodes = 0
        self.truncated = False
        self.rejected_candidates = 0
        self.rejection_examples = []
        self.node_limit = max(0, min(PHASE_NODES, repair.SEARCH_NODE_LIMIT))
        self.deadline = time.monotonic() + max(0, min(PHASE_SECONDS, repair.SEARCH_SECONDS))

    def step(self):
        self.nodes += 1
        if self.nodes > self.node_limit or time.monotonic() >= self.deadline:
            raise _Limit()


def _issue_free(tiles, towers):
    """Engine's exact pair/blocker rule without encoding protobuf per node."""
    geometry = _geometry({"tiles": tiles, "towers": towers})
    groups = defaultdict(list)
    for tile in tiles:
        if tile["tower_index"] is None:
            groups[tile["x"], tile["y"]].append(tile)
    for group in groups.values():
        group.sort(key=lambda tile: tile["z"])
        for lower, upper in zip(group, group[1:]):
            if upper["z"] == lower["z"] + 1:
                return False
            item = (lower, lower["x"] * 85, lower["y"] * 85)
            if not any(lower["z"] < other[0]["z"] < upper["z"] and _overlaps(item, other)
                       for other in geometry):
                return False
    return True


def _removals(original, budget):
    """Upper-first vertex covers, plus keep-one choices for longer chains."""
    edges = [(issue["lower_index"], issue["upper_index"]) for issue in original["issues"]]
    seen, result = set(), []

    def add(indices):
        key = frozenset(indices)
        if key not in seen:
            seen.add(key)
            result.append(key)

    add(upper for _, upper in edges)
    add(lower for lower, _ in edges)
    if len(edges) > 24:
        budget.truncated = True
        return result

    def visit(chosen):
        budget.step()
        if len(result) >= MAX_REMOVAL_PLANS:
            return
        remaining = next(((lower, upper) for lower, upper in edges
                          if lower not in chosen and upper not in chosen), None)
        if remaining is None:
            add(chosen)
            return
        lower, upper = remaining
        visit(chosen | {upper})
        visit(chosen | {lower})

    visit(set())
    return result


def _base_variants(original, removed, budget=None):
    kept = [dict(tile) for tile in original["tiles"] if tile["index"] not in removed]
    ordinary_name = "upper" if removed == {issue["upper_index"] for issue in original["issues"]} else "alternative"
    # A lower/middle deletion can leave a problem upper without its support.
    # Only problem members shift; no whole-board layer compression is allowed.
    problem = {issue[key] for issue in original["issues"] for key in ("lower_index", "upper_index")}
    removed_by_xy = defaultdict(list)
    for tile in original["tiles"]:
        if tile["index"] in removed:
            removed_by_xy[tile["x"], tile["y"]].append(tile["z"])
    compacted = [dict(tile, z=tile["z"] - sum(z < tile["z"] for z in
                       removed_by_xy[tile["x"], tile["y"]]))
                 if tile["index"] in problem else tile for tile in kept]
    compact_first = (compacted != kept and
                     len(_unsupported(_geometry({"tiles": compacted, "towers": original["towers"]}))) <
                     len(_unsupported(_geometry({"tiles": kept, "towers": original["towers"]}))))
    if compact_first:
        yield compacted, "lower_middle"
    yield kept, ordinary_name
    if compacted != kept and not compact_first:
        yield compacted, "lower_middle"
    # Earlier whole-problem-layer deletion remains a narrow allowed operation.
    # Compress only genuinely occupied layers completely removed by this plan;
    # the final audit enforces exposed-face exceptions and every change budget.
    seen = {tuple((tile["index"], tile["z"]) for tile in variant) for variant in (kept, compacted)}
    for candidate, layers in _removal_variants(original, removed, budget or _Budget()):
        key = tuple((tile["index"], tile["z"]) for tile in candidate)
        if layers and key not in seen:
            seen.add(key)
            yield candidate, "whole_problem_layer"


def _slots(tiles, towers, source_layers, budget):
    geometry = _geometry({"tiles": tiles, "towers": towers})
    by_z = defaultdict(list)
    for item in geometry:
        by_z[item[0]["z"]].append(item)
    tower_xy = {(tower["x"], tower["y"]) for tower in towers}
    unsupported = _unsupported(geometry)
    slots = []
    for z in sorted(source_layers):
        if not 0 <= z < 255:
            continue
        higher = [item for item in geometry if item[0]["z"] > z]
        if not higher:
            continue
        for y in range(2, 17):
            for x in range(2, 15):
                budget.step()
                if (x, y) in tower_xy:
                    continue
                tile = {"index": None, "x": x, "y": y, "z": z,
                        "tile_id": 0, "tower_index": None}
                item = (tile, x * 85, y * 85)
                if any(_overlaps(item, other) for other in by_z[z]) or not _covered(item, higher):
                    continue
                # Same-XY adjacent pairs cannot acquire an intermediate blocker.
                if any(other["x"] == x and other["y"] == y and abs(other["z"] - z) <= 1
                       for other in tiles if other["tower_index"] is None):
                    continue
                priority = -sum(other["z"] == z + 1 and abs(other["x"] - x) < 2 and
                                abs(other["y"] - y) < 2 for other in unsupported)
                supported = z == 0 or any(_overlaps(item, other) for other in by_z[z - 1])
                slots.append(((priority, not supported, z, y, x), tile))
    if len(slots) > MAX_SLOTS:
        budget.truncated = True
    return [tile for _, tile in sorted(slots)[:MAX_SLOTS]]


def _grouped(tiles):
    counts = Counter(tile["tile_id"] for tile in tiles)
    return len(tiles) % 3 == 0 and counts[-10] % 3 == 0 and counts[-11] % 3 == 0


def _placements(kept, mobile, fixed, towers, slots, original, budget, probe_nodes=None):
    """Assign source identities to local slots; validate support atomically."""
    from .lowimpact import audit_layout
    probe_start = budget.nodes

    def step():
        budget.step()
        if probe_nodes is not None and budget.nodes - probe_start > probe_nodes:
            budget.truncated = True
            raise _ProbeLimit()

    chosen = list(fixed)
    base_geometry = _geometry({"tiles": kept + list(fixed), "towers": towers})
    pending = _new_unsupported(base_geometry, original)
    coverage = [{index for index, unsupported in enumerate(pending)
                 if unsupported["z"] == slot["z"] + 1 and abs(unsupported["x"] - slot["x"]) < 2
                 and abs(unsupported["y"] - slot["y"]) < 2} for slot in slots]
    required = set(range(len(pending)))
    if required and (not coverage or set().union(*coverage) != required):
        return None
    # Types do not affect geometry. Identical-type mobile permutations are
    # redundant, but identities are retained on their actual moved messages.
    mobile = sorted(mobile, key=lambda tile: (tile["tile_id"], tile.get("index") is None,
                                              tile.get("index") or 0))

    def visit(position, previous_slot=-1, unsupported=None):
        step()
        unsupported = required if unsupported is None else unsupported
        if unsupported:
            most_supported = max((len(covered & unsupported) for covered in coverage), default=0)
            if most_supported == 0 or (len(unsupported) + most_supported - 1) // most_supported > len(mobile) - position:
                return None
        if position == len(mobile):
            result = kept + chosen
            geometry = _geometry({"tiles": result, "towers": towers})
            if _new_unsupported(geometry, original) or not _issue_free(result, towers):
                return None
            audit = audit_layout(original, result)
            if not audit["valid"]:
                budget.rejected_candidates += 1
                for error in audit["errors"]:
                    if error not in budget.rejection_examples and len(budget.rejection_examples) < 3:
                        budget.rejection_examples.append(error)
            return (result, audit) if audit["valid"] else None
        source = mobile[position]
        same_type = position and mobile[position - 1]["tile_id"] == source["tile_id"]
        ordered = list(enumerate(slots))
        ordered.sort(key=lambda pair: (-len(coverage[pair[0]] & unsupported),
                     abs(pair[1]["x"] - source["x"]) + abs(pair[1]["y"] - source["y"]) +
                     abs(pair[1]["z"] - source["z"]), pair[0]))
        for slot_index, slot in ordered:
            step()
            if same_type and slot_index <= previous_slot:
                continue
            candidate = dict(slot, index=source.get("index"), tile_id=source["tile_id"])
            item = _geometry({"tiles": [candidate], "towers": towers})[0]
            if any(other["z"] == candidate["z"] and _overlaps(item, other_item)
                   for other, other_item in zip(chosen, _geometry({"tiles": chosen, "towers": towers}))):
                continue
            if any(other["tower_index"] is None and other["x"] == candidate["x"] and
                   other["y"] == candidate["y"] and abs(other["z"] - candidate["z"]) <= 1 for other in chosen):
                continue
            chosen.append(candidate)
            found = visit(position + 1, slot_index, unsupported - coverage[slot_index])
            if found:
                return found
            chosen.pop()
        return None

    try:
        return visit(0)
    except _ProbeLimit:
        return None


def _count_plans(mobile, kept, slack, budget):
    """Prefer zero type/count delta; drop <=3 or add <=3 of existing types."""
    if not slack:
        if _grouped(kept + mobile):
            yield mobile
        return
    for amount in range(1, 4):
        seen = set()
        for indices in combinations(range(len(mobile)), amount):
            budget.step()
            removed = frozenset(indices)
            signature = tuple(sorted(Counter(mobile[index]["tile_id"] for index in indices).items()))
            if signature in seen:
                continue
            seen.add(signature)
            candidate = [tile for index, tile in enumerate(mobile) if index not in removed]
            if _grouped(kept + candidate):
                yield candidate
        types = sorted({tile["tile_id"] for tile in kept + mobile})
        for type_group in combinations_with_replacement(types, amount):
            budget.step()
            additions = [dict(mobile[0] if mobile else kept[0], index=None, tile_id=tile_id)
                         for tile_id in type_group]
            candidate = mobile + additions
            if _grouped(kept + candidate):
                yield candidate


def search_repair(raw, original_analysis):
    original = original_analysis
    if not original["issues"]:
        from .lowimpact import audit_layout
        checked = audit_layout(original, original["tiles"])
        if not checked["valid"]:
            return {"status": "unrepairable", "raw": raw, "reason": "原模板未通过最终安全核验：" + "；".join(checked["errors"]),
                    "removed_count": 0, "added_count": 0, "search_complete": True,
                    "strategy": "source_invalid", "metrics": checked["metrics"]}
        return {"status": "unchanged", "raw": raw, "origin_indices": list(range(len(original["tiles"]))),
                "reason": "未发现全遮盖，无需修复", "removed_count": 0, "added_count": 0,
                "search_complete": True, "strategy": "unchanged", "metrics": checked["metrics"]}
    towers = original["towers"]
    problem = {issue[key] for issue in original["issues"] for key in ("lower_index", "upper_index")}
    geometry = _geometry(original)
    baseline_floats = {tile['index'] for tile in _unsupported(geometry)}
    hidden = [tile for tile, px, py in geometry if tile["index"] not in problem and tile["tower_index"] is None
              and tile['index'] not in baseline_floats
              and _covered((tile, px, py), [other for other in geometry if other[0]["z"] > tile["z"]])]
    hidden_tower = [tile for tile, px, py in geometry if tile["tower_index"] is not None
                    and tile['index'] not in baseline_floats
                    and _covered((tile, px, py), [other for other in geometry if other[0]["z"] > tile["z"]])]
    source_layers = {z for tile in original["tiles"] for z in (tile["z"] - 1, tile["z"], tile["z"] + 1)
                     if 0 <= z <= 255}
    incomplete, nodes, rejected, rejection_examples = False, 0, 0, []
    removal_budget = _Budget()
    try:
        plans = _removals(original, removal_budget)
        incomplete = removal_budget.truncated
    except _Limit:
        plans = [frozenset(issue["upper_index"] for issue in original["issues"])]
        incomplete = True
    if len(plans) >= MAX_REMOVAL_PLANS:
        incomplete = True
    # Every mobile ordinary slot is fully hidden. An exposed removed face
    # therefore needs a retained, existing same-XY/type lower replacement;
    # deleting a different-type visible upper cannot be rescued by any hidden
    # refill or a remote tower. Prune that proven-impossible branch before it
    # can spend the budget intended for lower/middle alternatives.
    visible = [tile for tile, px, py in geometry if tile["index"] in problem and
               not _covered((tile, px, py), [other for other in geometry if other[0]["z"] > tile["z"]])]
    replacements = {tile["index"]: {other["index"] for other in original["tiles"]
                    if other["z"] < tile["z"] and all(other[key] == tile[key] for key in ("x", "y", "tile_id"))}
                    for tile in visible}
    plans = [removed for removed in plans if all(index not in removed or bool(choices - removed)
                                               for index, choices in replacements.items())]
    # Probe a few smaller vertex covers first: one middle identity can resolve
    # several pairs. Then preserve the original fast upper-first plan before
    # spending the remaining budget on other sorted alternatives.
    baseline_plan = plans[0] if plans else frozenset()
    ranked_plans = sorted(plans, key=len)
    preferred_plans = set(plan for plan in ranked_plans if len(plan) < len(baseline_plan))
    preferred_order = [plan for plan in ranked_plans if plan in preferred_plans][:PREFERRED_PLAN_COUNT]
    preferred_plans = set(preferred_order)
    plans = preferred_order + ([baseline_plan] if plans else []) + [plan for plan in ranked_plans
             if plan not in preferred_plans and plan != baseline_plan]

    # A separate budget for each phase prevents an early branch from starving
    # every fallback. No tower membership is touched before ordinary phases.
    phases = [("ordinary", 0, False, False), ("hidden_rearrangement", 3, False, False),
              ("count_slack", 3, True, False), ("tower_fallback", 6, True, True)]
    for phase, helper_limit, slack, allow_tower in phases:
        budget = _Budget()
        phase_best, phase_score = None, None
        helper_pool = hidden_tower + hidden if allow_tower else hidden
        first_helper_count = 1 if phase == "hidden_rearrangement" else 0
        try:
            for helper_count in range(first_helper_count, min(helper_limit, len(helper_pool)) + 1):
                for removed in plans:
                    for base, variant in _base_variants(original, removed, budget):
                        problem_mobile = [dict(tile) for tile in original["tiles"] if tile["index"] in removed]
                        helper_combinations = combinations(helper_pool, helper_count)
                        for helper_number, helpers in enumerate(helper_combinations):
                            budget.step()
                            if helper_number >= MAX_HELPER_PLANS:
                                incomplete = True
                                break
                            if sum(tile["tower_index"] is None for tile in helpers) > 3:
                                continue
                            helper_ids = {tile["index"] for tile in helpers}
                            kept = [tile for tile in base if tile["index"] not in helper_ids]
                            mobile = problem_mobile + [dict(tile) for tile in helpers]
                            # Earlier stages have already searched zero delta.
                            count_plans = list(_count_plans(mobile, kept, slack, budget))
                            if allow_tower and _grouped(kept + mobile):
                                count_plans.insert(0, mobile)
                            ordinary_slots = None
                            for allocated in count_plans:
                                tower_options = [()] if not allow_tower else _tower_options(
                                    {"tiles": kept, "towers": towers}, allocated, budget)
                                for fixed_slots in tower_options:
                                    budget.step()
                                    if len(fixed_slots) > len(allocated):
                                        continue
                                    fixed = [dict(slot, index=allocated[index].get("index"),
                                                  tile_id=allocated[index]["tile_id"])
                                             for index, slot in enumerate(fixed_slots)]
                                    layout = kept + fixed
                                    if ordinary_slots is None or fixed:
                                        ordinary_slots = _slots(layout, towers, source_layers, budget)
                                    remaining = allocated[len(fixed):]
                                    if len(remaining) > len(ordinary_slots):
                                        continue
                                    probe_nodes = PREFERRED_PLACEMENT_NODES if phase == "ordinary" and removed in preferred_plans else None
                                    found = _placements(kept, remaining, fixed, towers, ordinary_slots, original, budget, probe_nodes)
                                    if found:
                                        tiles, audit = found
                                        final_raw = rewrite_tiles(raw, tiles)
                                        retained = {tile["index"] for tile in tiles if tile.get("index") is not None}
                                        moved = sum(tile.get("index") is not None and any(tile[key] != original["tiles"][tile["index"]][key]
                                                    for key in ("x", "y", "z")) for tile in tiles)
                                        removed_count = len(original["tiles"]) - len(retained) + moved
                                        added_count = sum(tile.get("index") is None for tile in tiles) + moved
                                        phase_name = {"ordinary": "普通隐藏补位", "hidden_rearrangement": "少量隐藏牌重排",
                                                      "count_slack": "小幅数量调整", "tower_fallback": "既有塔最后补位"}[phase]
                                        # Placements return their first valid assignment. Even
                                        # without a budget hit, other assignments/removal plans
                                        # have not been exhaustively compared.
                                        complete = False
                                        reason = "低扰动{}方案通过轮廓、表面、数量和最终安全核验；移动{}张，净变化{:+d}张".format(
                                            phase_name, moved, len(tiles) - len(original["tiles"]))
                                        if not complete:
                                            reason += "；有限搜索未穷尽全部摆法，当前方案不代表全局最少改动"
                                        result = {"status": "repaired", "raw": final_raw,
                                                "origin_indices": [tile.get("index") for tile in tiles],
                                                "reason": reason,
                                                "removed_count": removed_count, "added_count": added_count,
                                                "search_complete": complete, "strategy": phase + ":" + variant,
                                                "metrics": dict(audit["metrics"], search_nodes=nodes + budget.nodes,
                                                                search_phase=phase, search_selected_helpers=helper_count,
                                                                search_selected_tower_helpers=sum(tile["tower_index"] is not None for tile in helpers),
                                                                search_prior_phases_complete=not incomplete,
                                                                search_rejected_candidates=rejected + budget.rejected_candidates)}
                                        if not allow_tower:
                                            return result
                                        metrics = audit["metrics"]
                                        score = (metrics["tower_changed_count"], abs(metrics["tile_delta"]),
                                                 metrics["hidden_changed_count"], metrics["changed_source_count"],
                                                 metrics["displacement_total"])
                                        if phase_score is None or score < phase_score:
                                            phase_best, phase_score = result, score
        except _Limit:
            incomplete = True
        incomplete = incomplete or budget.truncated
        nodes += budget.nodes
        rejected += budget.rejected_candidates
        rejection_examples.extend(error for error in budget.rejection_examples if error not in rejection_examples)
        if phase_best is not None:
            phase_best["search_complete"] = False
            phase_best["metrics"]["search_nodes"] = nodes
            phase_best["metrics"]["search_rejected_candidates"] = rejected
            if incomplete and "全局最少" not in phase_best["reason"]:
                phase_best["reason"] += "；有限搜索存在预算或候选截断，当前方案不代表全局最少改动"
            return phase_best
    return {"status": "unrepairable", "raw": raw,
            "reason": "限定低扰动搜索未找到满足完整轮廓、原露出面、移动及数量预算和最终安全的方案；保留原模板，不代表数学无解" +
                      ("；搜索预算或候选上限已到" if incomplete else ""),
            "removed_count": 0, "added_count": 0, "search_complete": False,
            "strategy": "bounded_local", "metrics": {"search_nodes": nodes,
                "search_rejected_candidates": rejected, "search_rejection_examples": rejection_examples[:3]}}


def _tower_options(analysis, mobile, budget):
    """Fill holes or grow existing towers only; rank/outline use final audit."""
    # Group allocation reaches useful equal-height growth directly instead of
    # spending the whole budget on arbitrary invalid subsets of tower slots.
    yield from sorted(_tower_plans(analysis, min(6, len(mobile)), budget), key=len)
    by_tower = defaultdict(list)
    for tile in analysis["tiles"]:
        if tile["tower_index"] is not None:
            by_tower[tile["tower_index"]].append(tile)
    candidates = []
    for tower in analysis["towers"]:
        members = by_tower[tower["index"]]
        if not members:
            continue
        occupied = {tile["z"] for tile in members}
        for z in range(max(occupied)):
            budget.step()
            if z not in occupied:
                candidates.append({"index": None, "x": tower["x"], "y": tower["y"], "z": z,
                                   "tile_id": 0, "tower_index": tower["index"]})
    # Smallest membership changes first, within each count prefer filling low
    # gaps. Equal-height/rank relationships are checked on the complete plan.
    for count in range(1, min(6, len(mobile), len(candidates)) + 1):
        for selected in combinations(candidates, count):
            budget.step()
            geometry = _geometry({"tiles": analysis["tiles"] + list(selected), "towers": analysis["towers"]})
            if any(left[0]["z"] == right[0]["z"] and _overlaps(left, right)
                   for index, left in enumerate(geometry) for right in geometry[index + 1:]):
                continue
            yield selected
