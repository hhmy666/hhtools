"""Bounded, independently verified alternatives; never mutate the requested plan."""
from hashlib import sha256
from time import monotonic

from .ordinary_add import build_ordinary_add, normalize_ordinary_add_options, OrdinaryAddPlan
from .ordinary_add_verification import verify_ordinary_add


def recommend_ordinary_add(raw, options, new_id):
    current = normalize_ordinary_add_options(options)
    advice = dict(operation='ordinary_add', source_sha256=sha256(raw).hexdigest(),
                  current_options=current, status='limited', schemes=[])
    # First preserve quantity; then preserve placement. Every proposal remains
    # opt-in, including increases to layer range or changes to explicit splits.
    candidates = []
    def add(**changes):
        try:
            p = normalize_ordinary_add_options({**current, **changes})
        except ValueError:
            return
        if p != current and p not in candidates:
            candidates.append(p)
    add(layer_mode='range', min_added_layers=0, max_added_layers=max(6,current['max_added_layers']))
    add(placement_strategy='both', split_mode='auto')
    if current['target_mode']=='count':
        for delta in (3,6,9):
            add(added_tiles=current['added_tiles']+delta, split_mode='auto')
    else:
        add(target_mode='count', added_tiles=18, split_mode='auto')
    add(placement_strategy='top_first' if current['placement_strategy']=='bottom_first' else 'bottom_first', split_mode='auto')
    add(placement_strategy='both', split_mode='auto', layer_mode='range', min_added_layers=0,
        max_added_layers=max(6,current['max_added_layers']))
    deadline = monotonic()+6
    for p in candidates[:8]:
        if monotonic() >= deadline or len(advice['schemes']) >= 2:
            break
        result = build_ordinary_add(raw, p, new_id, deadline=deadline)
        if (not result.get('ok') or type(result.get('plan')) is not OrdinaryAddPlan
                or dict(result['plan'].options) != p
                or not verify_ordinary_add(raw,result['output'],result['plan'],new_id).ok):
            continue
        advice['schemes'].append(dict(verified=True,
            source_sha256=advice['source_sha256'], output_sha256=sha256(result['output']).hexdigest(),
            parameters={'mode':'ordinary_add','ordinary_add':p},
            changes=[dict(field=k, **{'from':current[k],'to':v}) for k,v in p.items() if current[k]!=v],
            facts=dict(result['summary'])))
    if advice['schemes']:
        advice['status']='verified'
    return advice
