"""Pure, deterministic placement planning with indivisible symmetry orbits.

Inspection's directional offset signatures are deliberately converted here to
sorted overlap areas. Placement profiles describe each individual overlap, not
their total. Source geometry and the decoded document are never mutated.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Dict, Optional, Tuple
from time import monotonic

from .geometry import footprint, overlap_area, overlap_profile, symmetry_orbit, within_bounds
from .increase_geometry import SHIFTS, rectangle, separated


@dataclass(frozen=True)
class PlacementOptions:
    strategy: str = 'top_first'
    max_tiles_per_layer: int = 20
    max_added_layers: int = 2
    seed: int = 0


@dataclass(frozen=True)
class Placement:
    x: int
    y: int
    z: int
    profile: Tuple[int, ...]
    orbit: Tuple[Tuple[int, int], ...]
    tile_index: Optional[int] = None
    overlaps: Tuple[Tuple[Tuple[int, int], int], ...] = ()


@dataclass
class PlacementState:
    board: Tuple[int, int]
    layers: Dict[int, list]
    towers: Dict[int, list]
    axes: Tuple[int, int] = (0, 0)
    symmetry: Tuple[str, ...] = ()
    layer_profiles: dict = field(default_factory=dict)
    single_profile: Optional[Tuple[int, ...]] = None
    stacking_unit: Optional[int] = None
    rejected: dict = field(default_factory=dict)
    added_points: dict = field(default_factory=dict)
    flexible_profiles: bool = False
    tower_envelopes: tuple = ()
    relaxed_profiles: bool = False
    proximity_anchors: tuple = ()
    proximity_count: int = 0
    inherited_full_cover: tuple = ()


def _surface_symmetry(layers, axes):
    """Recognize visible top and projected outline independently of hidden Z.

    These are preferences only. A source can have a regular visible formation
    while a buried layer intentionally lacks the matching tile.
    """
    top = set(layers[max(layers)])
    outline = set().union(*(set(points) for points in layers.values()))
    names = ('horizontal', 'vertical', 'rotation')
    detected = tuple(name for name in names if
        (any(len(symmetry_orbit(p, axes, (name,))) > 1 for p in top) and
         set().union(*(set(symmetry_orbit(p, axes, (name,))) for p in top)) == top)
        or set().union(*(set(symmetry_orbit(p, axes, (name,))) for p in outline)) == outline)
    # A single row's half-turn and left/right mirror induce the same visible
    # permutation. Combining them invents an unrelated vertical constraint.
    chosen, seen = [], set()
    for name in detected:
        permutation=tuple(tuple(p for p in symmetry_orbit(point,axes,(name,)) if p != point)
                          for point in sorted(top))
        if permutation not in seen:
            chosen.append(name)
            seen.add(permutation)
    return tuple(chosen)


def _near_proximity(point, anchors):
    return any(sum((a-b)**2 for a,b in zip(point, anchor)) <= 8 for anchor in anchors)


@dataclass(frozen=True)
class LayerCapacity:
    layer: int
    existing: int
    remaining_limit: int
    candidate_orbits: int
    max_feasible: int
    profiles: tuple
    side: str
    rejected: tuple


@dataclass(frozen=True)
class PlacementPlan:
    placements: Tuple[Placement, ...]
    strategy: str
    seed: int
    layers: tuple = ()
    added_layers: int = 0
    strategy_version: str = 'orbit-v2-added-support-sha256'
    ok: bool = True
    original_z_offset: int = 0


@dataclass(frozen=True)
class PlacementFailure:
    code: str
    message: str
    requested: int
    planned: int
    shortage: int
    layers: tuple
    rejected: tuple
    recommendations: tuple
    strategy: str
    seed: int
    evidence: tuple = ()
    ok: bool = False


class SearchLimit(Exception):
    """The bounded search is inconclusive, not proof of geometric failure."""


@dataclass
class SearchBudget:
    remaining: int = 100000
    deadline: Optional[float] = None

    def use(self):
        if self.deadline is not None and monotonic() >= self.deadline:
            raise SearchLimit('placement search time budget exhausted')
        self.remaining -= 1
        if self.remaining < 0:
            raise SearchLimit('placement search state budget exhausted')


def _allowed(state, z, signature):
    if state.relaxed_profiles:
        return (bool(signature) or z<=min(0,min(state.layers))) and not (set(signature)-{1,2})
    if state.flexible_profiles:
        units=set(signature)
        reference=({n for n in state.single_profile} if state.single_profile is not None
                   else {n for p in state.layer_profiles.get(z,()) for n in p})
        return bool(signature) and not (units-{1,2}) and (units.issubset(reference) if reference else len(units)==1)
    if state.single_profile is not None:
        return signature == state.single_profile
    return signature in state.layer_profiles.get(z, ())


def _inherited_units(state, z):
    """Overlap unit of the immediate lower layer, including newly placed Tiles.

    Quarter corners remain the same style regardless of how many supports are
    contacted. A lowest layer without its own supports uses the source unit,
    if one was established; absent evidence imposes no invented style.
    """
    lower = state.layers.get(z-1, ())
    supports = state.layers.get(z-2, ())
    units = {area for point in lower for area in overlap_profile(point, supports) if area in (1,2)}
    if not units and state.stacking_unit in (1,2):
        units = {state.stacking_unit}
    return units


def _upper_valid(state, target, added, partial=False):
    """Only affected original upper Tiles are revalidated; no history rewrite."""
    lower = state.layers.get(target, []) + list(added)
    for point in state.layers.get(target + 1, []):
        if any(overlap_area(point, new) for new in added):
            signature = overlap_profile(point, lower)
            allowed = _allowed(state, target+1, signature)
            inherited=(point[0],point[1],target+1) in state.inherited_full_cover
            if inherited:
                allowed=bool(signature) and not (set(signature)-{1,2,4})
            if partial and not allowed:
                profiles = (state.single_profile,) if state.single_profile else state.layer_profiles.get(target+1, ())
                # More disjoint lower Tiles can add overlaps, never change an
                # already present overlap area. Permit only completable prefixes.
                allowed = any(not (Counter(signature)-Counter(p)) for p in profiles)
            if not signature or (4 in signature and not inherited) or not allowed:
                return False
    return True


def enumerate_legal_positions(state, target_layer, profile=None, *, provisional=False):
    """One representative Placement per complete orbit; diagnostics on state.

    ``profile`` is one exact overlap signature, or None to enumerate all legal
    signatures. The representative's orbit contains every destination point.
    """
    rejected = Counter()
    candidates, seen = [], set()
    below = target_layer < min(state.layers)
    if target_layer > 0 and not state.layers.get(target_layer-1) and not below and not provisional:
        state.rejected = {'missing_immediate_lower': 1}
        return []
    bottom = target_layer == min(0, min(state.layers)) or below
    adjacent = state.layers.get(target_layer + (1 if bottom else -1), [])
    occupied = state.layers.get(target_layer, [])
    inherited = (_inherited_units(state, target_layer) if not bottom and target_layer > max(state.layers)
                 else {state.stacking_unit} if below and state.stacking_unit in (1,2) else set())
    for y in range(2, 17):
        for x in range(2, 15):
            orbit = symmetry_orbit((x, y), state.axes, state.symmetry)
            if orbit in seen:
                continue
            seen.add(orbit)
            reason = None
            signatures = []
            for point in orbit:
                if not (2 <= point[0] <= 14 and 2 <= point[1] <= 16):
                    reason = 'safe_bounds'
                elif not within_bounds(point, state.board):
                    reason = 'board_bounds'
                elif any(overlap_area(point, other) for other in state.towers.get(target_layer, [])):
                    reason = 'tower_collision'
                elif any(not separated(rectangle(*point,shift=shift),rectangle(*tower,shift=shift))
                         for tower in state.tower_envelopes for shift in (0,)+SHIFTS):
                    reason = 'tower_unfolded_collision'
                elif any(overlap_area(point, other) for other in occupied):
                    reason = 'same_layer_collision'
                if reason:
                    break
                signature = overlap_profile(point, adjacent)
                if not signature and not (state.relaxed_profiles and (bottom or provisional)):
                    reason = 'unsupported'
                elif 4 in signature or any(overlap_area(point, other) == 4
                                          for z in (target_layer-1, target_layer+1)
                                          for other in state.towers.get(z, [])):
                    reason = 'full_cover'
                elif profile is not None and signature != profile:
                    reason = 'profile_mismatch'
                elif state.single_profile is not None and not (provisional and not signature) and not _allowed(state, target_layer, signature):
                    reason = 'profile_mismatch'
                elif not bottom and target_layer in state.layer_profiles and not (provisional and not signature) and not _allowed(state, target_layer, signature):
                    reason = 'profile_mismatch'
                elif inherited and not state.relaxed_profiles and not set(signature).issubset(inherited):
                    reason = 'inherited_style_mismatch'
                signatures.append(signature)
                if reason:
                    break
            if not reason and any(overlap_area(a, b) for i, a in enumerate(orbit) for b in orbit[:i]):
                reason = 'orbit_collision'
            if not reason and not state.relaxed_profiles and len(set(signatures)) != 1:
                reason = 'orbit_profile_mismatch'
            if not reason and (bottom or state.flexible_profiles) and not _upper_valid(state, target_layer, orbit, partial=True):
                reason = 'upper_profile_mismatch'
            if not reason and state.flexible_profiles and any(overlap_area(point, other)==4
                    for point in orbit for other in state.layers.get(target_layer+1,())):
                reason = 'full_cover'
            if reason:
                rejected[reason] += 1
                continue
            px, py = orbit[0]
            overlaps = tuple((other, overlap_area((px, py), other)) for other in adjacent
                             if overlap_area((px, py), other))
            candidates.append(Placement(px, py, target_layer, signatures[0], orbit, overlaps=overlaps))
    state.rejected = dict(rejected)
    return sorted(candidates, key=lambda p: (len(p.orbit), p.profile, p.orbit))


def _choose_orbits(candidates, count, budget, accept=None):
    """Stable first exact packing, with subset-sum DP and conflict-mask caching.

    Packing is NP-hard in general. Each visited state consumes a finite shared
    budget; exhaustion is exposed as search_limit, never capacity=0. The fixed
    safety region provides at most 195 candidate anchors regardless of options.
    """
    if count == 0:
        return []
    if count < 0 or not candidates:
        return None
    cells = [frozenset().union(*(footprint(p) for p in c.orbit)) for c in candidates]
    sizes = [len(c.orbit) for c in candidates]
    conflicts = [sum(1 << j for j, other in enumerate(cells) if cell & other) for cell in cells]
    failed = set()

    def search(mask, remaining, chosen):
        budget.use()
        if remaining == 0:
            return chosen if accept is None or accept(chosen) else None
        key = (mask, remaining)
        if not mask or (accept is None and key in failed):
            return None
        reachable = 1
        union = set()
        for i in range(len(candidates)):
            if mask >> i & 1:
                reachable |= reachable << sizes[i]
                union.update(cells[i])
        if not reachable >> remaining & 1 or len(union)//4 < remaining:
            return None
        while mask:
            bit = mask & -mask
            i = bit.bit_length()-1
            mask ^= bit
            if sizes[i] > remaining:
                continue
            result = search(mask & ~conflicts[i], remaining-sizes[i], chosen+[candidates[i]])
            if result is not None:
                return result
        if accept is None:
            failed.add(key)
        return None

    return search((1 << len(candidates))-1, count, [])


def _random_key(seed, value):
    # Hash ordering makes the PRNG definition explicit and Python-version stable.
    return sha256((str(seed)+':'+repr(value)).encode('ascii')).digest()


def _layer_choice(state, z, maximum, options, budget, *, new=False, bottom=False):
    candidates = enumerate_legal_positions(state, z)
    # A new upper layer should use this run's lower additions as supports when
    # legal, rather than always choosing lexicographically earlier old supports.
    # This is an ordering preference only: no candidate/style rule is relaxed.
    supports = state.added_points.get(z-1, ()) if new else ()
    def added_support_rank(candidate):
        return sum(any(overlap_area(point, lower) for lower in supports) for point in candidate.orbit)
    if supports and options.strategy != 'seeded_random':
        candidates.sort(key=lambda p: (-added_support_rank(p), len(p.orbit), p.profile, p.orbit))
    if options.strategy == 'seeded_random':
        candidates.sort(key=lambda p: _random_key(options.seed, (z, p.profile, p.orbit)))
    rejected = dict(state.rejected)
    frequencies = Counter(p.profile for p in candidates)
    # Prefer the source overlap unit for accepted prototype compatibility, but
    # this is a tie-break, not an extra ban on legal multi-profile layouts.
    def unit_rank(p):
        return bool(state.stacking_unit is not None and any(n != state.stacking_unit for n in p))
    support_scores = {profile: max((added_support_rank(p) for p in candidates if p.profile == profile), default=0)
                      for profile in frequencies}
    ranked = sorted(frequencies, key=lambda p: (unit_rank(p), -support_scores[p], -frequencies[p], len(p), sum(p), p))
    accept = (lambda chosen: _upper_valid(state, z, [point for p in chosen for point in p.orbit])) if bottom else None
    choice = []
    for count in range(min(maximum, 56), -1, -1):
        profiles = ranked if count == min(maximum, 56) else sorted(frequencies, key=lambda p: (unit_rank(p), -support_scores[p], len(p), sum(p), p))
        if options.strategy == 'seeded_random':
            profiles = sorted(profiles, key=lambda p: _random_key(options.seed, ('profile', z, p)))
        groups = [[p for p in candidates if p.profile == profile] for profile in profiles] if new else [candidates]
        for group in groups:
            result = _choose_orbits(group, count, budget, accept)
            if result is not None:
                choice = result
                break
        else:
            if count:
                rejected['orbit_count_or_conflict'] = rejected.get('orbit_count_or_conflict', 0)+1
                continue
        break
    placed = sum(len(p.orbit) for p in choice)
    report = LayerCapacity(z, len(state.layers.get(z, [])), max(0, options.max_tiles_per_layer-len(state.layers.get(z, []))),
                           len(candidates), placed, tuple(sorted(set(p.profile for p in choice))),
                           'bottom' if bottom else 'top', tuple(sorted(rejected.items())))
    return choice, report


def _append(state, z, choice, destinations):
    for candidate in choice:
        for x, y in candidate.orbit:
            adjacent = state.layers.get(z-1, []) if z-1 in state.layers else state.layers.get(z+1, [])
            overlaps = tuple((other, overlap_area((x,y), other)) for other in adjacent if overlap_area((x,y), other))
            destinations.append(replace(candidate, x=x, y=y, overlaps=overlaps))
            state.layers.setdefault(z, []).append((x,y))
            state.added_points.setdefault(z, []).append((x,y))


def _run(state, needed, bottom_target, options, budget):
    budget.use()  # Bound empty/unsupported trials as well as packing states.
    state = replace(state, layers={z: list(points) for z, points in state.layers.items()}, rejected={},
                    added_points={z: list(points) for z, points in state.added_points.items()})
    destinations, reports = [], []
    low, top = min(state.layers), max(state.layers)
    if bottom_target:
        maximum = min(bottom_target, max(0, options.max_tiles_per_layer-len(state.layers[low])))
        choice, report = _layer_choice(state, low, maximum, options, budget, bottom=True)
        _append(state, low, choice, destinations)
        reports.append(report)
    # A level can only be created by placing at least one moved Tile. This caps
    # large max_added_layers values at needed, without imposing a product limit.
    added = 0
    for offset in range(min(options.max_added_layers, needed)+1):
        remaining = needed-len(destinations)
        if not remaining:
            break
        z = top+offset
        maximum = min(remaining, max(0, options.max_tiles_per_layer-len(state.layers.get(z, []))))
        choice, report = _layer_choice(state, z, maximum, options, budget, new=offset>0, bottom=z==low)
        reports.append(report)
        if offset and not choice:
            break
        _append(state, z, choice, destinations)
        added += bool(offset and choice)
    return destinations, tuple(reports), added


def _strategy_targets(state, needed, options):
    if options.strategy == 'bottom_first':
        return [needed]
    if options.strategy not in ('balanced', 'seeded_random'):
        return [0]
    # Recompute this bound for every candidate parameter value: increasing the
    # layer limit can introduce bottom targets absent from the original run.
    bound = min(needed, 56, max(0, options.max_tiles_per_layer-len(state.layers[min(state.layers)])))
    targets = list(range(bound+1))
    targets.sort(key=lambda bottom: (abs(needed-2*bottom), bottom > needed//2, bottom))
    if options.strategy == 'seeded_random':
        targets.sort(key=lambda bottom: _random_key(options.seed, ('split',bottom)))
    return targets


def _search_strategy(state, needed, options, budget):
    """The same complete split search serves primary plans and advice trials."""
    return _search_directional(state,needed,options,budget)[0]


def _search_all_layers(state, needed, options, budget, *, levels=None, bottom_levels=(), bottom_target=None,
                       flexible_profiles=True):
    """Backtrack complete symmetry orbits across every original and new layer.

    The old staged result remains the incumbent. Contact count may differ
    within a uniform quarter/half overlap unit; it is not a reason to discard
    an otherwise supported centre Tile or skip original intermediate layers.
    """
    state=replace(state,flexible_profiles=flexible_profiles,layers={z:list(ps) for z,ps in state.layers.items()},
                  added_points={})
    original=set(state.layers)
    top=max(original)
    levels=list(levels) if levels is not None else sorted(original)+list(range(top+1,top+min(options.max_added_layers,needed)+1))
    best=([],(),0)
    limited=False
    if bottom_target is not None and state.relaxed_profiles:
        # Optimistic subset sums ignore collisions and future support, so they
        # can prove impossible orbit parity without rejecting a feasible split.
        # This avoids spending an entire branch budget on an odd half after a
        # central repair has already consumed the only singleton orbit.
        reachable={True:1,False:1}
        for level in levels:
            side=level in bottom_levels
            for candidate in enumerate_legal_positions(state,level,provisional=True):
                reachable[side] |= reachable[side] << len(candidate.orbit)
        if not (reachable[True] >> bottom_target & 1 and
                reachable[False] >> (needed-bottom_target) & 1):
            return best

    def finish(current, destinations, reports):
        if not _supported_layout(current, ()):
            return None
        if current.proximity_count and sum(_near_proximity((p.x,p.y,p.z), current.proximity_anchors)
                for p in destinations) < current.proximity_count:
            return None
        final=[]
        for p in destinations:
            adjacent=current.layers.get(p.z-1 if p.z>min(0,min(current.layers)) else p.z+1,())
            profile=overlap_profile((p.x,p.y),adjacent)
            if not _allowed(current,p.z,profile):
                return None
            final.append(replace(p,profile=profile,overlaps=tuple((q,overlap_area((p.x,p.y),q)) for q in adjacent if overlap_area((p.x,p.y),q))))
        return final,tuple(reports),len(set(current.layers)-original)

    def visit(index,current,destinations,reports,remaining):
        nonlocal best,limited
        budget.use()
        bottom_count=sum(p.z in bottom_levels for p in destinations)
        if bottom_target is not None and (bottom_count>bottom_target or
                bottom_count+remaining<bottom_target):
            return None
        if bottom_target is not None:
            bottom_capacity=sum(max(0,options.max_tiles_per_layer-len(current.layers.get(level,())))
                                for level in levels[index:] if level in bottom_levels)
            top_capacity=sum(max(0,options.max_tiles_per_layer-len(current.layers.get(level,())))
                             for level in levels[index:] if level not in bottom_levels)
            if bottom_target-bottom_count>bottom_capacity or remaining-(bottom_target-bottom_count)>top_capacity:
                return None
        if not remaining:
            if bottom_target is not None and bottom_count!=bottom_target:
                return None
            return finish(current,destinations,reports)
        if len(destinations)>len(best[0]) and _supported_layout(current, ()):
            best=(destinations,tuple(reports),len(set(current.layers)-original))
        if index==len(levels):
            return None
        if remaining>sum(max(0,options.max_tiles_per_layer-len(current.layers.get(z,()))) for z in levels[index:]):
            return None
        z=levels[index]
        # Empty new levels terminate construction; never jump a missing support.
        if z>top and z-1 not in current.layers:
            return visit(index+1,current,destinations,reports,remaining)
        maximum=min(remaining,max(0,options.max_tiles_per_layer-len(current.layers.get(z,()))))
        if bottom_target is not None:
            maximum=min(maximum,bottom_target-bottom_count if z in bottom_levels else remaining-(bottom_target-bottom_count))
        candidates=enumerate_legal_positions(current,z)
        rejected=tuple(sorted(current.rejected.items()))
        if options.strategy=='seeded_random':
            candidates.sort(key=lambda p:_random_key(options.seed,('all_layers',z,p.profile,p.orbit)))
        if current.relaxed_profiles:
            gaps=[point for level,points in current.layers.items() if level>z
                  for point in points if not overlap_profile(point,current.layers.get(level-1,()))]
            if gaps:
                # Lower support chains must be grown towards source gaps,
                # rather than filling unrelated edges first and discovering
                # several closed layers later that a centre cannot be reached.
                candidates.sort(key=lambda p:min(abs(x-u)+abs(y-v)
                    for x,y in p.orbit for u,v in gaps))
        unsupported_upper=[point for point in current.layers.get(z+1,())
                           if not overlap_profile(point,current.layers.get(z,()))]
        if unsupported_upper:
            # Repair existing consumers before spending slots on decoration.
            # Closing a layer with an unsupported upper Tile cannot succeed;
            # prune that branch before enumerating its packing combinations.
            if any(not any(overlap_area(point,q) for p in candidates for q in p.orbit)
                   for point in unsupported_upper):
                return None
            candidates.sort(key=lambda p:-sum(any(overlap_area(point,q) for q in p.orbit)
                                              for point in unsupported_upper))
        groups={}
        for p in candidates:
            groups.setdefault(() if state.relaxed_profiles else tuple(sorted(set(p.profile))),[]).append(p)
        future=sum(max(0,options.max_tiles_per_layer-len(current.layers.get(level,()))) for level in levels[index+1:])
        minimum=max(0,remaining-future)
        def supports_upper(updated):
            # Once this layer is closed, later layers cannot repair an upper
            # source Tile that still has no immediate lower support.
            return all((profile:=overlap_profile(p,updated.layers.get(z,()))) and
                       (4 not in profile or (*p,z+1) in updated.inherited_full_cover)
                       for p in updated.layers.get(z+1,()))
        counts=list(range(maximum,minimum-1,-1))
        if z in original and len(levels)>1:
            # Convex occupancy cost is an ordering preference, never a ban on
            # the denser packing required by an exact feasible solution.
            peers=[level for level in levels[index:] if level in original and
                   (bottom_target is None or (level in bottom_levels)==(z in bottom_levels))]
            target=(sum(len(current.layers.get(level,())) for level in peers)+remaining)/max(1,len(peers))
            counts.sort(key=lambda n:(abs(len(current.layers.get(z,()))+n-target),
                _random_key(options.seed,('count',z,n)) if options.strategy=='seeded_random' else -n))
        for count in counts:
            if not count:
                answer=visit(index+1,current,destinations,reports,remaining) if supports_upper(current) else None
                if answer is not None:
                    return answer
                continue
            for group in groups.values():
                answer=None
                def accept(chosen):
                    nonlocal answer
                    points=[point for p in chosen for point in p.orbit]
                    if not _upper_valid(current,z,points):
                        return False
                    updated=replace(current,layers={level:list(ps) for level,ps in current.layers.items()},
                                    added_points={level:list(ps) for level,ps in current.added_points.items()})
                    placed=list(destinations)
                    _append(updated,z,chosen,placed)
                    if not supports_upper(updated):
                        return False
                    report=LayerCapacity(z,len(current.layers.get(z,())),max(0,options.max_tiles_per_layer-len(current.layers.get(z,()))),len(candidates),count,
                        tuple(sorted({p.profile for p in chosen})),'bottom' if z in bottom_levels or z<0 else 'top',rejected)
                    answer=visit(index+1,updated,placed,reports+[report],remaining-count)
                    return answer is not None
                local=SearchBudget(min(4000,budget.remaining),budget.deadline)
                available=local.remaining
                try:
                    _choose_orbits(group,count,local,accept)
                    if answer is not None:
                        return answer
                except SearchLimit:
                    limited=True
                finally:
                    budget.remaining-=available-local.remaining
                budget.use()
        return None
    try:
        result=visit(0,state,[],[],needed)
        if result is not None:
            return result
        if limited:
            raise SearchLimit('some all-layer packing branches remain unvisited')
        return best
    except SearchLimit as exc:
        exc.best=best
        raise


def _supported_layout(state, destinations):
    layers={z:list(ps) for z,ps in state.layers.items()}
    for p in destinations:
        layers.setdefault(p.z,[]).append((p.x,p.y))
    ground=min(0,min(layers))
    return all(z==ground or (bool(profile) and
               (4 not in profile or (*point,z) in state.inherited_full_cover))
               for z,points in layers.items() for point in points
               for profile in (overlap_profile(point,layers.get(z-1,())),))


def _end_bands(state):
    """Sparse contiguous ends stop at a dense core; a plateau stays one band.

    The occupancy peak is evidence of this template's core, not a fixed two
    layer assumption. An end already at that peak consists of its outer layer.
    """
    levels=sorted(state.layers)
    peak=max(len(state.layers[z]) for z in levels)
    def band(ordered):
        result=[]
        for z in ordered:
            if result and len(state.layers[z])>=peak:
                break
            result.append(z)
        return tuple(result)
    bottom,top=band(levels),band(list(reversed(levels)))
    # With no central plateau the two sparse runs can meet. Give each original
    # layer a single side, keeping both ends available on two-layer templates.
    middle=(levels[0]+levels[-1])/2
    overlap=set(bottom)&set(top)
    if len(levels)>1 and overlap:
        bottom=tuple(z for z in bottom if z not in overlap or z<=middle)
        top=tuple(z for z in top if z not in overlap or z>middle)
    return bottom,top


def _repair_support_chains(state, needed, options, budget):
    """Grow supports down to ground before the layer-closing packing search.

    Provisional candidates are enumerated again after their lower supports are
    built. Backtrack across source gaps: the first support for one gap can block
    another. Every final source and added Tile is checked before packing begins.
    """
    start=replace(state,layers={z:list(ps) for z,ps in state.layers.items()},added_points={})
    ground=min(0,min(start.layers))

    def support(current, destinations, z, point):
        budget.use()
        profile=overlap_profile(point,current.layers.get(z-1,()))
        if 4 in profile:
            if (*point,z) in current.inherited_full_cover:
                yield current,destinations
            return
        if z==ground or profile:
            yield current,destinations
            return
        target=z-1
        if target not in state.layers or len(current.layers.get(target,()))>=options.max_tiles_per_layer:
            return
        candidates=enumerate_legal_positions(current,target,provisional=True)
        candidates=[p for p in candidates if any(overlap_area(point,q) for q in p.orbit)]
        candidates=[p for p in candidates if not any(overlap_area(q,upper)==4
            for q in p.orbit for upper in current.layers.get(target+1,()))]
        # A narrow axis-only repair can create a conspicuous one-Tile stem.
        # Prefer complete mirrored groups; singleton candidates remain in the
        # search for exact odd counts or genuinely tight geometry.
        candidates.sort(key=lambda p:(len(p.orbit)==1,len(p.orbit),
            sum(abs(x-point[0])+abs(y-point[1]) for x,y in p.orbit),
            _random_key(options.seed,('repair',target,p.orbit))))
        for candidate in candidates:
            if len(destinations)+len(candidate.orbit)>needed:
                continue
            def support_orbit(index,branch,placed):
                if index==len(candidate.orbit):
                    yield branch,placed
                    return
                for updated,added in support(branch,placed,target,candidate.orbit[index]):
                    yield from support_orbit(index+1,updated,added)
            for branch,placed in support_orbit(0,current,destinations):
                if (len(placed)+len(candidate.orbit)>needed or
                        len(branch.layers.get(target,()))+len(candidate.orbit)>options.max_tiles_per_layer):
                    continue
                legal=next((p for p in enumerate_legal_positions(branch,target)
                            if p.orbit==candidate.orbit),None)
                if legal is not None:
                    branch=replace(branch,layers={level:list(ps) for level,ps in branch.layers.items()},
                        added_points={level:list(ps) for level,ps in branch.added_points.items()})
                    placed=list(placed)
                    _append(branch,target,[legal],placed)
                    yield branch,placed

    source_points=[(z,point) for z,points in sorted(state.layers.items()) for point in points]
    def repair(index,current,placed):
        if index==len(source_points):
            return (current,placed) if _supported_layout(current,()) else None
        z,point=source_points[index]
        for branch,added in support(current,placed,z,point):
            answer=repair(index+1,branch,added)
            if answer is not None:
                return answer
        return None
    return repair(0,start,[])


def _search_directional(state, needed, options, budget, *, bands=None):
    """Repair source support gaps, then search the original selected ends.

    Logical negative levels are temporary. The caller shifts ordinary Z once,
    leaving retained towers fixed and recording the offset in the saved plan.
    """
    bottom,top=_end_bands(state) if bands is None else bands
    low,high=min(state.layers),max(state.layers)
    if state.relaxed_profiles and not _supported_layout(state,()):
        repair_budget=SearchBudget(min(20000,budget.remaining),budget.deadline)
        available=repair_budget.remaining
        try:
            repaired=_repair_support_chains(state,needed,options,repair_budget)
            if repaired is not None and _supported_layout(repaired[0],()):
                repaired_state,prefix=repaired
                repaired_state=replace(repaired_state, proximity_count=max(0,state.proximity_count-
                    sum(_near_proximity((p.x,p.y,p.z),state.proximity_anchors) for p in prefix)))
                result,offset=_search_directional(repaired_state,needed-len(prefix),options,repair_budget,
                    bands=(bottom,top))
                if len(result[0])+len(prefix)==needed:
                    combined=prefix+list(result[0])
                    final_layers={z:list(ps) for z,ps in state.layers.items()}
                    for p in combined:
                        final_layers.setdefault(p.z,[]).append((p.x,p.y))
                    final=[]
                    for p in combined:
                        adjacent=final_layers.get(p.z-1 if p.z>min(0,min(final_layers)) else p.z+1,())
                        final.append(replace(p,profile=overlap_profile((p.x,p.y),adjacent),
                            overlaps=tuple((q,overlap_area((p.x,p.y),q)) for q in adjacent if overlap_area((p.x,p.y),q))))
                    return (final,result[1],result[2]),offset
        except SearchLimit:
            pass
        finally:
            budget.remaining-=available-repair_budget.remaining
    best=([],(),0)
    best_offset=0
    splits=list(range(needed+1)) if options.strategy=='balanced' else [None]
    if options.strategy=='balanced':
        splits.sort(key=lambda n:(abs(needed-2*n),n>needed//2,n))
    allocations=[(0,0)]
    for total in range(1,min(needed,options.max_added_layers)+1):
        if options.strategy=='top_first':
            allocations.append((0,total))
        elif options.strategy=='bottom_first':
            allocations.append((total,0))
        else:
            choices=[(below,total-below) for below in range(total+1)]
            choices.sort(key=lambda pair:(_random_key(options.seed,('extension',pair)) if options.strategy=='seeded_random' else (abs(pair[0]-pair[1]),pair[0])))
            allocations.extend(choices)
    limited=False
    trials=[(below,above,reverse) for below,above in allocations
            for reverse in ((False,True) if options.strategy=='seeded_random' else (False,))]
    for split in splits:
        for below,above,reverse in trials:
            offset=max(0,below-low)
            current=replace(state,towers={z-offset:list(ps) for z,ps in state.towers.items()})
            lower=list(range(low-1,low-below-1,-1))
            upper=list(range(high+1,high+above+1))
            if options.strategy=='top_first':
                levels=list(top)+upper
            elif options.strategy=='bottom_first':
                levels=list(bottom)+lower
            elif options.strategy=='balanced':
                levels=list(top)+list(bottom)+upper+lower
            else:
                # Construct supports before consumers. Seeded orbit ordering
                # randomizes choices across all original layers while this
                # topological order avoids starving a repairable middle layer.
                levels=sorted(state.layers,reverse=reverse)+upper+lower
            levels=list(dict.fromkeys(levels))
            bottom_levels=set(bottom)|set(lower)
            # Reserve a finite branch budget, so an unlucky packing cannot
            # monopolize all alternative allocations under the same limits.
            local=SearchBudget(min(6000 if state.relaxed_profiles else 1200,budget.remaining),budget.deadline)
            available=local.remaining
            try:
                result=_search_all_layers(current,needed,options,local,levels=levels,
                    bottom_levels=bottom_levels,bottom_target=split,
                    flexible_profiles=state.single_profile is None)
                if len(result[0])<needed and state.single_profile is not None:
                    result=_search_all_layers(current,needed,options,local,levels=levels,
                        bottom_levels=bottom_levels,bottom_target=split)
            except SearchLimit as exc:
                result=exc.best
                limited=True
            finally:
                budget.remaining-=available-local.remaining
            if len(result[0])>len(best[0]):
                best,best_offset=result,offset
            if len(result[0])==needed:
                # Count only nonempty newly constructed layers and use the
                # smallest shift actually required by their final minimum.
                actual_offset=max(0,-min([low]+[p.z for p in result[0]]))
                if actual_offset!=offset:
                    continue
                return result,offset
            try:
                budget.use()
            except SearchLimit as exc:
                exc.best=best
                raise
    if limited:
        exc=SearchLimit('some directional packing branches remain unvisited')
        exc.best=best
        raise exc
    if not best[1]:
        reports=[]
        eligible=top if options.strategy=='top_first' else bottom if options.strategy=='bottom_first' else tuple(state.layers)
        for z in eligible:
            candidates=enumerate_legal_positions(state,z)
            reports.append(LayerCapacity(z,len(state.layers[z]),max(0,options.max_tiles_per_layer-len(state.layers[z])),
                len(candidates),0,(),'bottom' if z in bottom else 'top',tuple(sorted(state.rejected.items()))))
        best=(best[0],tuple(reports),best[2])
    return best,best_offset


def _parameter_advice(state, needed, options, budget):
    """Prove recommendations with full strategy runs, or report incompleteness.

    Each new nonempty layer consumes at least one moved Tile, so ``needed``
    bounds reachable added-layer counts without an arbitrary +N window.
    """
    # 56 bounds newly placed footprints inside the safety region, not preserved
    # source Tiles elsewhere on the board. The option limits the layer's total.
    target_layers = {max(state.layers)}
    if options.strategy != 'top_first':
        target_layers.add(min(state.layers))
    total_bounds = [len(state.layers[z])+56 for z in target_layers]
    if options.max_added_layers:
        total_bounds.append(56)  # A genuinely new layer starts with zero Tiles.
    ranges = (('max_tiles_per_layer', options.max_tiles_per_layer+1, max(total_bounds)),
              ('max_added_layers', options.max_added_layers+1, needed))
    recommendations, checks, checked_ranges = [], [], []
    interrupted = None
    complete = True
    for name, start, stop in ranges:
        last_checked = None
        try:
            for value in range(start, stop+1):
                trial = _search_strategy(state, needed, replace(options, **{name:value}), budget)
                checks.append((name, value, len(trial[0])))
                last_checked = value
                if len(trial[0]) == needed:
                    recommendations.append('已验证 {} >= {} 可放置全部 {} 张；其余参数保持当前值'.format(name,value,needed))
                    break
        except SearchLimit:
            complete = False
            interrupted = (name, value)
        if last_checked is not None:
            checked_ranges.append((name, start, last_checked))
        if not complete:
            break
    status = {'complete':complete, 'requested_ranges':ranges,
              'checked_ranges':tuple(checked_ranges), 'interrupted_trial':interrupted}
    if not complete:
        recommendations.append('参数建议搜索未完成（达到独立状态预算）；已检查范围 {}，中断参数 {}；未检查部分不代表无解'.format(
            tuple(checked_ranges), interrupted))
    elif not recommendations:
        recommendations.append('已检查范围 {} 内未找到完整的确定性分层方案；不是全局无解证明'.format(tuple(checked_ranges)))
    return recommendations, tuple(checks), status


def assign_replenishment_positions(document, distribution, placements):
    """Assign minority corrections within sqrt(8) XYZ units of source ordinary Tiles.

    No original ordinary Tile of the correction type means the requested
    proximity cannot be satisfied. A present but distant type also fails.
    The radius preserves the former same-layer (2,2) maximum distance while
    measuring all three coordinates equally, including across layers.
    """
    ids = getattr(distribution, 'replenishment_ids', None)
    if ids is None or distribution.move_type not in ('majority_A', 'majority_B'):
        return tuple(placements)
    other = -11 if distribution.move_type == 'majority_A' else -10
    anchors = {(s.x, s.y) for s in document.stacks}
    originals = [t for t in document.tiles if t.tile_id == other and (t.x,t.y) not in anchors]
    if not originals:
        return None if other in ids else tuple(placements)
    def near(p):
        return any((p.x-t.x)**2 + (p.y-t.y)**2 + (p.z-t.z)**2 <= 8 for t in originals)
    minor_indices = [i for i, tile_id in zip(sorted(distribution.moved_tile_indices), ids) if tile_id == other]
    near_places = [p for p in placements if near(p)]
    if len(near_places) < len(minor_indices):
        return None
    selected = near_places[:len(minor_indices)]
    others = [p for p in placements if p not in selected]
    remaining = [i for i in sorted(distribution.moved_tile_indices) if i not in minor_indices]
    return tuple(replace(p, tile_index=i) for i,p in zip(minor_indices + remaining, selected + others))


def _search_minority_prefix(state, needed, options, budget):
    """Only required minority corrections may precede the selected end band."""
    bands=_end_bands(state)
    original_levels=tuple(sorted(state.layers))
    def visit(current,prefix,remaining):
        budget.use()
        if not remaining:
            result,offset=_search_directional(replace(current,proximity_count=0),
                needed-len(prefix),options,budget,bands=bands)
            if len(result[0])+len(prefix)==needed:
                placements=prefix+list(result[0])
                final_layers={z:list(ps) for z,ps in state.layers.items()}
                for p in placements:
                    final_layers.setdefault(p.z,[]).append((p.x,p.y))
                final=[]
                for p in placements:
                    adjacent=final_layers.get(p.z-1 if p.z>min(0,min(final_layers)) else p.z+1,())
                    final.append(replace(p,profile=overlap_profile((p.x,p.y),adjacent),
                        overlaps=tuple((q,overlap_area((p.x,p.y),q)) for q in adjacent if overlap_area((p.x,p.y),q))))
                return (final,result[1],result[2]),offset
            return None
        candidates=[p for z in original_levels
            if len(current.layers[z])<options.max_tiles_per_layer
            for p in enumerate_legal_positions(current,z)
            if len(p.orbit)<=remaining and len(current.layers[z])+len(p.orbit)<=options.max_tiles_per_layer
            and all(_near_proximity((x,y,z),state.proximity_anchors) for x,y in p.orbit)]
        candidates.sort(key=lambda p:(-len(p.orbit),min(sum((a-b)**2 for a,b in zip((p.x,p.y,p.z),anchor))
            for anchor in state.proximity_anchors),p.z,p.orbit))
        for candidate in candidates:
            branch=replace(current,layers={z:list(ps) for z,ps in current.layers.items()},
                added_points={z:list(ps) for z,ps in current.added_points.items()})
            placed=list(prefix)
            _append(branch,candidate.z,[candidate],placed)
            answer=visit(branch,placed,remaining-len(candidate.orbit))
            if answer is not None:
                return answer
        return None
    try:
        answer=visit(replace(state,relaxed_profiles=True),[],state.proximity_count)
    except SearchLimit as exc:
        # A partial prefix is not a complete main-direction plan. Keep the
        # bounded search inconclusive and expose no incomplete prefix as output.
        exc.best=([],(),0)
        raise
    return answer if answer is not None else (([],(),0),0)


def plan_placements(document, inspection, distribution, options=PlacementOptions(), *, include_advice=True, search_budget=None):
    """Pair sorted moved Tile indices to destinations; return auditable failure."""
    needed = getattr(distribution, 'move_count', 0)
    def failure(code, message, planned=0, layers=(), recommendations=(), evidence=()):
        rejected = Counter()
        for layer in layers:
            rejected.update(dict(layer.rejected))
        requested = needed if type(needed) is int and needed >= 0 else 0
        return PlacementFailure(code, message, requested, planned, max(0, requested-planned), layers,
                                tuple(sorted(rejected.items())), recommendations, options.strategy, options.seed, evidence)
    if (options.strategy not in ('top_first','bottom_first','balanced','seeded_random')
            or type(options.max_tiles_per_layer) is not int or options.max_tiles_per_layer < 1
            or type(options.max_added_layers) is not int or options.max_added_layers < 0
            or type(options.seed) is not int):
        return failure('invalid_options', '策略、单层上限、新增层数或seed非法',
                       recommendations=('单层上限使用正整数；新增层数使用非负整数；seed使用整数',))
    if not getattr(distribution, 'ok', False):
        return failure('invalid_distribution', '塔分配失败，不能规划补牌')
    moved = distribution.moved_tile_indices
    if (type(needed) is not int or needed < 0 or len(document.tiles) != inspection.total_tiles
            or len(moved) != needed or any(type(i) is not int or not 0 <= i < len(document.tiles) for i in moved)
            or len(set(moved)) != needed or len(distribution.retained_counts) != len(inspection.stacks)
            or any(type(n) is not int or not 0 <= n <= stack.count
                   for stack, n in zip(inspection.stacks, distribution.retained_counts))):
        return failure('invalid_distribution', '搬移索引或源模板与塔分配不一致')
    expected = tuple(index for stack, retained in zip(inspection.stacks, distribution.retained_counts)
                     for index in stack.tile_indices[retained:])
    if sorted(moved) != sorted(expected):
        return failure('invalid_distribution', '搬移索引不是塔顶连续后缀')
    if not needed:
        return PlacementPlan((), options.strategy, options.seed)
    if any(i.severity == 'error' and i.code != 'ab_count_not_multiple_of_three' for i in inspection.issues):
        return failure('precheck_failed', '缺少安全搬移所需的信息', evidence=inspection.issues)
    if not inspection.non_tower_layers:
        return failure('no_surface', '没有非塔承接面')
    ids=getattr(distribution,'replenishment_ids',None)
    if ids is not None and distribution.move_type in ('majority_A','majority_B'):
        other=-11 if distribution.move_type=='majority_A' else -10
        available=inspection.non_tower_b_count if other==-11 else inspection.non_tower_a_count
        if other in ids and not available:
            return failure('minority_source_type_absent','原非塔区域没有少数补位的同类牌，无法满足邻近要求')
    layers, towers = defaultdict(list), defaultdict(list)
    tower_indices = {i for stack in inspection.stacks for i in stack.tile_indices}
    for tile in document.tiles:
        if tile.index not in tower_indices:
            layers[tile.z].append((tile.x, tile.y))
        elif tile.index not in moved:
            towers[tile.z].append((tile.x, tile.y))
    ordinary=[tile for tile in document.tiles if tile.index not in tower_indices]
    source_full_cover=tuple((lower.index,upper.index,lower.x,lower.y,lower.z,upper.z)
        for lower in ordinary for upper in ordinary
        if upper.z==lower.z+1 and (upper.x,upper.y)==(lower.x,lower.y))
    points = [p for layer in layers.values() for p in layer]
    axes = (min(x for x,y in points)+max(x for x,y in points), min(y for x,y in points)+max(y for x,y in points))
    profiles = {z: tuple(sorted({overlap_profile(p, layers.get(z-1, [])) for p in points}))
                for z, points in layers.items() if z-1 in layers}
    all_profiles = {p for ps in profiles.values() for p in ps if p and 4 not in p}
    units = {n for p in all_profiles for n in p}
    state = PlacementState((document.width, document.height), dict(layers), dict(towers), axes, inspection.symmetry,
                           profiles, next(iter(all_profiles)) if len(all_profiles)==1 else None,
                           next(iter(units)) if len(units)==1 else None)
    state.tower_envelopes=tuple((stack.x,stack.y,count,stack.direction)
        for stack,count in zip(document.stacks,distribution.retained_counts) if count)
    state.inherited_full_cover=tuple((x,y,upper_z) for _,_,x,y,_,upper_z in source_full_cover)
    if ids is not None and distribution.move_type in ('majority_A','majority_B'):
        other=-11 if distribution.move_type=='majority_A' else -10
        state.proximity_anchors=tuple((t.x,t.y,t.z) for t in document.tiles
            if t.index not in tower_indices and t.tile_id==other)
        state.proximity_count=ids.count(other)
    needs_minor_prefix=False
    if state.proximity_count and options.strategy in ('top_first','bottom_first'):
        bottom,top=_end_bands(state)
        extension=min(options.max_added_layers,needed)
        levels=(list(top)+list(range(max(layers)+1,max(layers)+extension+1))
            if options.strategy=='top_first' else
            list(bottom)+list(range(min(layers)-extension,min(layers))))
        # A vertical separation alone can prove incompatibility. Horizontal
        # packing cannot repair it, and adding supports is unnecessary here.
        if all((z-anchor[2])**2>8 for z in levels for anchor in state.proximity_anchors):
            needs_minor_prefix=True
    budget = search_budget if search_budget is not None else SearchBudget()
    available_states = budget.remaining
    offset = 0
    best = ([], (), 0)
    relaxed = False
    incomplete = False
    # Preserve visible regularity first, without treating buried asymmetry as
    # a reason to randomize the surface. The original geometry remains a
    # complete fallback when orbit parity or support prevents this preference.
    top_points=state.layers[max(state.layers)]
    preferred_axes=(state.axes if state.symmetry else
        (min(x for x,y in top_points)+max(x for x,y in top_points),
         min(y for x,y in top_points)+max(y for x,y in top_points)))
    preferred=tuple(dict.fromkeys(state.symmetry+_surface_symmetry(state.layers,preferred_axes)))
    if not needs_minor_prefix and not state.symmetry and preferred:
        preferred_budget=SearchBudget(min(16000,budget.remaining),
            min(monotonic()+1.5,budget.deadline) if budget.deadline is not None else monotonic()+1.5)
        preferred_available=preferred_budget.remaining
        try:
            for partial_style in ((False,True) if len(units)>1 else (False,)):
                try:
                    result,preferred_offset=_search_directional(replace(state,axes=preferred_axes,
                        symmetry=preferred,relaxed_profiles=partial_style),needed,options,preferred_budget)
                    if len(result[0])==needed:
                        best,offset,relaxed=result,preferred_offset,partial_style
                        break
                except SearchLimit:
                    if preferred_budget.remaining <= 0:
                        break
        except SearchLimit:
            pass
        finally:
            budget.remaining-=preferred_available-preferred_budget.remaining
    # Reserve work for the general partial-overlap search. Style-preserving
    # trials must not consume the entire shared deadline before safety-valid
    # alternative supports are considered.
    style_budget=SearchBudget(min(10000,budget.remaining),
        min(monotonic()+1,budget.deadline) if budget.deadline is not None else monotonic()+1)
    style_available=style_budget.remaining
    try:
        if not needs_minor_prefix and len(best[0]) < needed:
            best,offset = _search_directional(state, needed, options, style_budget)
    except SearchLimit as exc:
        best = max((best,exc.best),key=lambda result:len(result[0]))
        incomplete = True
    finally:
        budget.remaining-=style_available-style_budget.remaining
    if len(best[0]) < needed and budget.remaining > 0:
        # Uniform overlap style is a preference, not a third safety invariant.
        # In particular, repairing a source support gap may require a different
        # partial overlap from the layer's existing quarter-corner pattern.
        relaxed = True
        state = replace(state, relaxed_profiles=True)
        try:
            best,offset = (_search_minority_prefix(state,needed,options,budget) if needs_minor_prefix
                else _search_directional(state, needed, options, budget))
        except SearchLimit as exc:
            best = max((best,exc.best),key=lambda result:len(result[0]))
            incomplete = True
    if len(best[0]) < needed and incomplete:
        return failure('search_limit', '本次搜索尚未完成；不能断言无解', len(best[0]), best[1],
                       ('本次搜索未完成；请试算其他补牌位置或层数，塔占比不提高，原模板不修改',), (('available_states_at_start',available_states),))
    if len(best[0]) == needed:
        if offset:
            best=([replace(p,z=p.z+offset) for p in best[0]],
                  tuple(replace(layer,layer=layer.layer+offset) for layer in best[1]),best[2])
            from dataclasses import replace as replace_record
            document=replace_record(document,tiles=[replace_record(t,z=t.z+offset)
                if t.index not in tower_indices else t for t in document.tiles])
        assigned = assign_replenishment_positions(document, distribution,
                    tuple(replace(p,tile_index=i) for i,p in zip(sorted(moved),best[0])))
        if assigned is None:
            return failure('minority_proximity_unavailable', '当前合法布局没有足够邻近原同类普通牌的位置用于少数补位',
                           evidence=(('proximity','squared Euclidean XYZ distance <= 8'),),
                           recommendations=('调整补牌位置或层数后重新试算；不放宽邻近条件',))
        return PlacementPlan(assigned,
                             options.strategy, options.seed, best[1], best[2],
                             strategy_version=('orbit-v8-inherited-source-minority'
                                if relaxed or source_full_cover or needs_minor_prefix else 'orbit-v4-directional-bands'),
                             original_z_offset=offset)
    recommendations, checks, advice_status = (_parameter_advice(state, needed, options, SearchBudget(30000))
                                             if include_advice else ([], (), {'complete':False}))
    return failure('capacity_exhausted', '当前分层方案的合法容量不足', len(best[0]), best[1], tuple(recommendations),
                   (('max_added_layers',options.max_added_layers), ('attempted_added_layers',best[2]),
                    ('recommendation_checks',tuple(checks)),
                    ('recommendation_search',advice_status),
                    ('capacity_scope','deterministic staged layout; not global infeasibility proof')))


@dataclass(frozen=True)
class JointLayout:
    distribution: object
    placement: object
    checked_distributions: int
    complete: bool

    @property
    def ok(self):
        return self.distribution.ok and self.placement.ok


def plan_joint_layout(document, inspection, distribution_options, placement_options=PlacementOptions(), *, search_budget=None):
    """Retry populations without changing any original user option.

    Automatic heights are solver output, not user overrides. First-pass
    successes retain the incumbent output. Search limits do not prove no-solution.
    """
    from .distribution import plan_tower_distribution, iter_tower_distributions
    budget = search_budget if search_budget is not None else SearchBudget(200000, monotonic() + 8)
    initial = plan_tower_distribution(document, inspection, distribution_options)
    first = plan_placements(document, inspection, initial, placement_options,
                            include_advice=False, search_budget=budget)
    if not initial.ok or first.ok or distribution_options.retained_counts is not None:
        return JointLayout(initial, first, 1, getattr(first, 'code', None) != 'search_limit')
    checked = 1
    try:
        if first.code == 'search_limit':
            raise SearchLimit('initial layout exhausted the shared budget')
        for candidate in iter_tower_distributions(document, inspection, distribution_options, budget):
            if candidate.retained_counts == initial.retained_counts:
                continue
            checked += 1
            layout = plan_placements(document, inspection, candidate, placement_options,
                                     include_advice=False, search_budget=budget)
            if layout.ok:
                return JointLayout(candidate, layout, checked, True)
            if layout.code == 'search_limit':
                raise SearchLimit('joint layout exhausted the shared budget')
    except SearchLimit:
        incomplete = replace(first, code='search_limit', message='本次搜索未完成，尚未找到符合当前参数的完整补牌方案')
        return JointLayout(initial, incomplete, checked, False)
    return JointLayout(initial, first, checked, True)
