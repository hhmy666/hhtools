"""Bounded, read-only trial generation; publish only independently valid plans.

Advice is tied to the exact source bytes and exact parameter set. Trials never
register artifacts, write templates, export or synchronize either repository.
"""
from dataclasses import asdict, replace
from hashlib import sha256
from time import monotonic

from .distribution import DistributionOptions, plan_tower_distribution
from .inspection import inspect_template
from .placement import PlacementOptions, SearchBudget, SearchLimit, plan_joint_layout
from .rewrite import RewritePlan, rewrite_template
from .template_reader import decode_template
from . import verification


STRATEGIES = ('top_first', 'bottom_first', 'balanced', 'seeded_random')
STRATEGY_NAMES = {'top_first': '顶层优先', 'bottom_first': '底层优先',
                  'balanced': '五五开', 'seeded_random': '随机'}


class AdviceBudget(SearchBudget):
    def __init__(self, states, seconds):
        super().__init__(states)
        self.deadline = monotonic() + seconds

    def use(self):
        if monotonic() >= self.deadline:
            raise SearchLimit('advice time budget exhausted')
        super().use()


def _layout_candidates(options, needed, strategy):
    """Keep size limits first; then single changes and small joint changes."""
    base = replace(options, strategy=strategy)
    yield base
    for layers in range(options.max_added_layers + 1, max(options.max_added_layers, needed) + 1):
        yield replace(base, max_added_layers=layers)
    for count in range(options.max_tiles_per_layer + 1, options.max_tiles_per_layer + needed + 1):
        yield replace(base, max_tiles_per_layer=count)
    # Joint increases are search candidates, not limits on user parameters.
    for delta in range(2, min(needed * 2, 12) + 1):
        for layers in range(1, delta):
            count = delta - layers
            if layers <= needed and count <= needed:
                yield replace(base, max_added_layers=options.max_added_layers + layers,
                              max_tiles_per_layer=options.max_tiles_per_layer + count)


def verified_parameter_advice(raw, distribution_options, placement_options, new_id,
                             *, max_states=120000, max_seconds=8):
    document = decode_template(raw)
    inspection = inspect_template(document)
    original = {**asdict(distribution_options), **asdict(placement_options)}
    original['retained_counts'] = (None if distribution_options.retained_counts is None
                                   else list(distribution_options.retained_counts))
    baseline = plan_tower_distribution(document, inspection, distribution_options)
    ratio_ceiling = baseline.final_ratio if baseline.ok else distribution_options.ratio
    distribution_candidates = [distribution_options]
    # A manually entered height can violate symmetry or final A/B divisibility.
    # Correct the exact heights, then test geometry and rewritten bytes before
    # suggesting them. Merely adding layers cannot fix a count/type mismatch.
    if not baseline.ok and distribution_options.retained_counts is not None:
        automatic = plan_tower_distribution(document, inspection,
                                             replace(distribution_options, retained_counts=None))
        if automatic.ok:
            distribution_candidates = [replace(distribution_options, retained_counts=automatic.retained_counts)]
            baseline = automatic
    seen, schemes = set(), []
    checks = 0
    limited = False

    def trial(dopt, popt, budget):
        nonlocal checks, limited
        parameters = {**asdict(dopt), **asdict(popt)}
        parameters['retained_counts'] = (None if dopt.retained_counts is None else list(dopt.retained_counts))
        fingerprint = repr(parameters)
        if fingerprint in seen or parameters == original:
            return None
        seen.add(fingerprint)
        budget.use()
        checks += 1
        if (dopt.ratio > distribution_options.ratio or dopt.min_ratio != distribution_options.min_ratio
                or dopt.replenish_type != distribution_options.replenish_type):
            return None
        distribution = plan_tower_distribution(document, inspection, dopt)
        if not distribution.ok or distribution.final_ratio > ratio_ceiling + 1e-12:
            return None
        joint = plan_joint_layout(document, inspection, dopt, popt, search_budget=budget)
        distribution, placement = joint.distribution, joint.placement
        if not placement.ok:
            if placement.code == 'search_limit':
                limited=True
                if budget.remaining<=0 or monotonic()>=budget.deadline:
                    raise SearchLimit('advice search exhausted')
            # A bounded packing branch being inconclusive does not consume
            # the whole advice allowance. Continue to the next independently
            # tested parameter set instead of suppressing all alternatives.
            return None
        if distribution.final_ratio > ratio_ceiling + 1e-12:
            return None
        plan = RewritePlan(distribution, placement, dopt, popt)
        output = rewrite_template(raw, plan, new_id)
        report = verification.verify_output(raw, output, plan, new_id)
        if not report.ok:
            return None
        return {'verified': True, 'parameters': parameters,
                'changes': [{'field': key, 'from': original[key], 'to': value}
                            for key, value in parameters.items() if original[key] != value],
                'source_sha256': sha256(raw).hexdigest(),
                'output_sha256': sha256(output).hexdigest(),
                'facts': {'tower_ratio': report.facts['tower_ratio'],
                          'move_count': distribution.move_count,
                          'added_layers': placement.added_layers,
                          'retained_counts': list(distribution.retained_counts)},
                'title': ('保留' if popt.strategy == placement_options.strategy else '改用')
                         + STRATEGY_NAMES[popt.strategy]}

    # Separate budgets reserve room for an alternative even when the current
    # strategy is difficult. Incomplete search is never a no-solution proof.
    for group in ('current', 'alternative'):
        budget = AdviceBudget(max_states // 2, max_seconds / 2)
        strategies = ((placement_options.strategy,) if group == 'current'
                      else tuple(s for s in STRATEGIES if s != placement_options.strategy))
        found = None
        try:
            for strategy in strategies:
                needed = baseline.move_count if baseline.ok else inspection.tower_tile_count
                for dopt in distribution_candidates:
                    for popt in _layout_candidates(placement_options, needed, strategy):
                        candidate = trial(dopt, popt, budget)
                        if candidate:
                            found = candidate
                            break
                    if found:
                        break
                if found:
                    break
            # Lower the ratio only when a fixed-ratio solution was not found.
            # Try every distinct automatic distribution at UI's 0.1% step;
            # do not silently change advanced per-tower settings.
            if not found and distribution_options.retained_counts is None:
                retained_seen = set()
                for step in range(int(round(distribution_options.ratio * 1000)) - 1, -1, -1):
                    if (step / 1000 < distribution_options.min_ratio
                            or (distribution_options.min_ratio > 0
                                and step / 1000 == distribution_options.min_ratio)):
                        break
                    dopt = replace(distribution_options, ratio=step / 1000)
                    distribution = plan_tower_distribution(document, inspection, dopt)
                    if not distribution.ok or distribution.retained_counts in retained_seen:
                        continue
                    retained_seen.add(distribution.retained_counts)
                    for strategy in strategies:
                        for popt in (replace(placement_options, strategy=strategy),
                                     replace(placement_options, strategy=strategy,
                                             max_added_layers=placement_options.max_added_layers + 1)):
                            candidate = trial(dopt, popt, budget)
                            if candidate:
                                found = candidate
                                break
                        if found:
                            break
                    if found:
                        break
        except SearchLimit:
            limited = True
        if found:
            schemes.append(found)
    return {'schemes': schemes, 'status': 'limited' if limited else ('ready' if schemes else 'not_found'),
            'checked_trials': checks, 'source_sha256': sha256(raw).hexdigest()}
