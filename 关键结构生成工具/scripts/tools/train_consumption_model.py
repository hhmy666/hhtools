"""Reproducible, read-only-input calibration training with source-family holdouts.

Only --output-dir is written. Historical config is read via git show; no checkout,
fetch, configuration update or source-template mutation is performed.
NumPy is an offline training dependency, never a runtime service dependency.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from puzzle_core.template_reader import parse_level_bytes
from puzzle_core.search import STRUCTURE_ID_TO_TYPE
from services.consumption_calibration_service import (
    _empirical_static_structure_features, _paired_board_features,
    _replacement_objects, _structure_correction_features,
)
from services.parameter_recommendation_service import parse_dup_param

MODEL_ID = "consumption-structure-correction-20261027-v2"
OLD_MODEL = SCRIPTS / "puzzle_core/data/consumption_structure_correction_s5_200_300_v1.json"
GEOMETRY_FEATURES = ["source_tower_fraction", "anchor_tower_fraction", "anchor_structure_fraction",
                     "log_source_tiles", "log_tile_ratio", "source_max_layer", "layer_delta"]
PRESSURE_FEATURES = ["candidate_a", "candidate_b", "remaining_a_pressure", "remaining_b_pressure"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git_bytes(repo, revision, path):
    return subprocess.check_output(["git", "-C", str(repo), "show", f"{revision}:{path}"], stderr=subprocess.DEVNULL)


def group_folds(groups, count):
    unique = sorted(set(groups), key=lambda g: sha(g.encode()))
    mapping = {g: i % count for i, g in enumerate(unique)}
    return [mapping[g] for g in groups]


def history_key(row, template=None):
    return (row.get("user_info", ""), template or row["level_template_id"],
            int(row["level_index"]), int(row["level_dup_para_id"]))


def resolve_identity(row, historical_config):
    """A changed ID requires exact LevelId + tested DupPara agreement, not suffix guessing."""
    configured = historical_config.get(row["level_id"])
    reported = row["level_template_id"]
    if configured and configured["TemplateId"] != reported:
        if configured.get("DupPara" + row["level_dup_para_id"]) != row["level_dup"]:
            raise ValueError("historical_parameter_mismatch")
        return configured["TemplateId"], "historical_config_rename"
    return reported, "reported_template"


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("nonpositive_or_nonfinite_observation")
    return number


def collect(args):
    history = list(csv.DictReader(args.csv.open(encoding="utf-8-sig")))
    config_blob = git_bytes(args.config_repo, args.config_ref, "csv/config_level.csv")
    config = {r["LevelId"]: r for r in csv.DictReader(io.StringIO(config_blob.decode("utf-8-sig")))}
    manifests = collections.defaultdict(list)
    for root in args.manifest_root:
        for path in sorted(root.rglob("*manifest.json")):
            m = json.loads(path.read_text())
            manifests[m.get("output_sha256")].append((path, m))
    index = collections.defaultdict(list)
    for row in history:
        index[history_key(row)].append(row)
    cache = {}

    def level(name, expected_sha=None):
        key = (name, expected_sha)
        if key not in cache:
            try:
                data = git_bytes(args.template_repo, args.template_ref, f"Template/{name}_m.bytes")
            except subprocess.CalledProcessError:
                # Cleaned intermediate sources were not uploaded. Their export hash,
                # not their current filename, is the authority for this fallback.
                if not expected_sha or not args.source_template_dir:
                    raise
                data = (args.source_template_dir / (name + "_m.bytes")).read_bytes()
                if sha(data) != expected_sha:
                    raise ValueError("source_hash_mismatch")
            cache[key] = parse_level_bytes(data, source_name=name + "_m.bytes")
        return cache[key]

    records, audit, static_cache = [], [], {}
    for number, row in enumerate(history, 2):
        entry = {"csv_row": number, "reported_template": row["level_template_id"],
                 "level_id": row["level_id"], "position": row["level_index"],
                 "duplicate": row["level_dup_para_id"], "tested_parameter": row["level_dup"],
                 "actual": row["人均消耗"], "entrants": row["进入人数"]}
        try:
            resolved, identity_reason = resolve_identity(row, config)
            entry.update(resolved_template=resolved, identity_reason=identity_reason)
            generated = level(resolved)
            entry["template_sha256"] = generated.sha256
            if not any(t.tile_id in STRUCTURE_ID_TO_TYPE for t in generated.tiles):
                raise ValueError("no_key_structure")
            hits = manifests[generated.sha256]
            if not hits:
                raise ValueError("no_hash_verified_generation_record")
            # Duplicate copies of the same manifest are harmless; conflicting sources are not.
            if len({(m["source_template_id"], m["source_sha256"]) for _, m in hits}) != 1:
                raise ValueError("ambiguous_generation_source")
            path, manifest = hits[0]
            source_id = manifest["source_template_id"]
            source = level(source_id, manifest["source_sha256"])
            if source.sha256 != manifest["source_sha256"]:
                raise ValueError("source_hash_mismatch")
            entry.update(source_id=source_id, source_sha256=source.sha256,
                         manifest=path.name, manifest_sha256=sha(path.read_bytes()),
                         generated_at=manifest.get("created_at", ""))
            aliases = [source_id]
            if source_id.startswith("lessT"):
                aliases.append(source_id[5:])
            if source_id.endswith("clean"):
                aliases.append(source_id[:-5])
            anchors = [a for a in dict.fromkeys(aliases) if index.get(history_key(row, a))]
            if len(anchors) != 1:
                raise ValueError("missing_or_ambiguous_same_position_anchor")
            anchor_id = anchors[0]
            observed = index[history_key(row, anchor_id)]
            count = sum(positive(r["进入人数"]) for r in observed)
            target = sum(positive(r["人均消耗"]) * positive(r["进入人数"]) for r in observed) / count
            original = parse_dup_param(max(observed, key=lambda r: float(r["进入人数"]))["level_dup"])
            candidate = parse_dup_param(row["level_dup"])
            if candidate.luck != "100" or any(parse_dup_param(r["level_dup"]).luck != "100" for r in observed):
                raise ValueError("unsupported_lucky_value")
            key = (generated.sha256, source.sha256, anchor_id)
            if key not in static_cache:
                static = _empirical_static_structure_features(source, _replacement_objects(manifest))
                static.update(_paired_board_features(source, level(anchor_id)))
                static_cache[key] = static
            static = static_cache[key]
            features = _structure_correction_features(
                manifest, target=target, original=original, candidate=candidate,
                duplicate_index=int(row["level_dup_para_id"]), level_index=int(row["level_index"]),
                static_structure_features=static)
            actual, entrants = positive(row["人均消耗"]), positive(row["进入人数"])
            if manifest["signature"] != "s5":
                raise ValueError("unsupported_signature")
            entry.update(status="included", anchor_id=anchor_id, actual=actual, target=target,
                         entrants=entrants, anchor_entrants=count,
                         anchor_parameters=";".join(sorted({r["level_dup"] for r in observed})))
            records.append({**entry, "group": row.get("user_info", "") + ":" + anchor_id,
                            "features": features, "signature": manifest["signature"],
                            "structure_count": int(static["replacement_count"])})
        except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
            entry["status"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        audit.append(entry)
    provenance = {
        "csv_sha256": sha(args.csv.read_bytes()), "config_sha256": sha(config_blob),
        "config_commit": subprocess.check_output(["git", "-C", str(args.config_repo), "rev-parse", args.config_ref]).decode().strip(),
        "template_commit": subprocess.check_output(["git", "-C", str(args.template_repo), "rev-parse", args.template_ref]).decode().strip(),
        "strategy_dimension_available": False,
        "identity_rule": "historical LevelId + exact DupPara + template SHA256 + generation source SHA256",
        "pairing": "same product, position and duplicate; entrants-weighted original consumption",
    }
    return records, audit, provenance


def fit(x, y, weights, alpha, names):
    import numpy as np
    weights = weights / weights.mean()
    mean = np.average(x, axis=0, weights=weights)
    scale = np.sqrt(np.average((x - mean) ** 2, axis=0, weights=weights))
    scale[scale < 1e-8] = 1
    z = (x - mean) / scale
    intercept = np.average(y, weights=weights)
    gram = (z * weights[:, None]).T @ z + alpha * np.eye(x.shape[1])
    rhs = (z * weights[:, None]).T @ (y - intercept)
    coef = np.linalg.solve(gram, rhs)
    bounded = {names.index(n) for n in PRESSURE_FEATURES if n in names}
    for _ in range(2000):
        previous = coef.copy()
        for j in range(len(coef)):
            v = (rhs[j] - gram[j] @ coef + gram[j, j] * coef[j]) / gram[j, j]
            coef[j] = max(0, v) if j in bounded else v
        if np.max(abs(previous - coef)) < 1e-9:
            break
    return mean, scale, coef, float(intercept)


def predict(model, x):
    import numpy as np
    return np.clip((x - model[0]) / model[1] @ model[2] + model[3], -1.5, 1.5)


def metrics(actual, predictions):
    import numpy as np
    error = np.abs(actual - predictions)
    return {"rows": len(actual), "mae": float(error.mean()),
            "mape": float((error / actual).mean()), "within_25_percent": float((error / actual <= .25).mean())}


def train(records, provenance):
    import numpy as np
    old = json.loads(OLD_MODEL.read_text())
    names = [n for n in old["feature_order"] if n not in ("delta_a", "delta_b")]
    names += GEOMETRY_FEATURES + ["remaining_a_pressure", "remaining_b_pressure", "replacement_fraction", "replaced_b_fraction"]
    groups = [r["group"] for r in records]
    if len(set(groups)) < 15:
        raise ValueError("Need at least 15 independent source families")
    x = np.array([[r["features"][n] for n in names] for r in records])
    actual, target = (np.array([r[k] for r in records]) for k in ("actual", "target"))
    y = np.log(actual / target)
    counts = collections.Counter(groups)
    weights = np.array([math.sqrt(min(r["entrants"], 500)) / counts[r["group"]] for r in records])
    outer = np.array(group_folds(groups, 5))
    alphas = [3, 30, 100, 300, 1000]

    def choose(indices):
        inner = np.array(group_folds([groups[i] for i in indices], 3))
        scores = []
        for alpha in alphas:
            errors = np.zeros(len(indices))
            for fold in range(3):
                tr, te = indices[inner != fold], indices[inner == fold]
                model = fit(x[tr], y[tr], weights[tr], alpha, names)
                errors[inner == fold] = abs(predict(model, x[te]) - y[te])
            scores.append((float(np.average(errors, weights=weights[indices])), alpha))
        return min(scores)[1]

    predicted_log, selected = np.zeros(len(records)), []
    for fold in range(5):
        tr, te = np.where(outer != fold)[0], np.where(outer == fold)[0]
        assert not set(groups[i] for i in tr).intersection(groups[i] for i in te)
        alpha = choose(tr)
        selected.append(alpha)
        predicted_log[te] = predict(fit(x[tr], y[tr], weights[tr], alpha, names), x[te])
        print(f"Validated held-out source families: fold {fold + 1}/5", flush=True)
    predictions = target * np.exp(predicted_log)
    baseline = []
    for r in records:
        log_ratio = old["intercept"] + sum((r["features"][n] - old["means"][n]) / old["scales"][n] * old["coefficients"][n] for n in old["feature_order"])
        baseline.append(r["target"] * math.exp(max(-1, min(1, log_ratio))))
    alpha = choose(np.arange(len(records)))
    mean, scale, coef, intercept = fit(x, y, weights, alpha, names)
    residuals = y - predicted_log
    offsets = np.quantile(residuals, [.1, .9]).tolist()
    positions = [int(r["position"]) for r in records]
    scope = {"level_index_min": min(positions), "level_index_max": max(positions),
             "level_bands_observed": sorted({p // 100 for p in positions}),
             "signatures": ["s5"], "structure_count_min": min(r["structure_count"] for r in records),
             "structure_count_max": max(r["structure_count"] for r in records),
             "strategy_groups_observed": [], "lucky_values": ["100"]}
    report = {"model_id": MODEL_ID, "provenance": provenance, "scope": scope,
              "rows": len(records), "templates": len({r["resolved_template"] for r in records}),
              "source_families": len(set(groups)), "validation": "nested grouped CV: 5 outer / 3 inner; entire source family held out",
              "alpha_selected_per_fold": selected, "final_alpha": alpha,
              "old_fixed_model": metrics(actual, np.array(baseline)), "new_held_out_model": metrics(actual, predictions),
              "unchanged_source_baseline": metrics(actual, target),
              "by_band": {str(b): metrics(actual[np.array(positions) // 100 == b], predictions[np.array(positions) // 100 == b]) for b in scope["level_bands_observed"]},
              "limitations": ["Observational tests; strategy dimension unavailable, not causal effects",
                              "Seed selected by board simulation, not assigned a consumption coefficient",
                              "Historical parameter agreement does not prove client deployment; CSV contains no client build/time",
                              "Reference interval uses held-out residuals, not a guarantee of width <=300 or future coverage"]}
    artifact = {"model_id": MODEL_ID, "scope": scope, "feature_order": names,
                "means": dict(zip(names, mean.tolist())), "scales": dict(zip(names, scale.tolist())),
                "coefficients": dict(zip(names, coef.tolist())), "intercept": intercept,
                "log_ratio_clip": [-1.5, 1.5], "prediction_interval_log_offsets": offsets,
                "training": report, "monotone_features": PRESSURE_FEATURES}
    for i, record in enumerate(records):
        record.update(validation_fold=int(outer[i]), held_out_prediction=float(predictions[i]), old_prediction=baseline[i])
    return artifact, report


def write_csv(path, rows):
    keys = list(dict.fromkeys(k for row in rows for k in row if k != "features"))
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("csv", "config-repo", "template-repo", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--manifest-root", type=Path, action="append", required=True)
    parser.add_argument("--source-template-dir", type=Path)
    parser.add_argument("--config-ref", required=True)
    parser.add_argument("--template-ref", required=True)
    args = parser.parse_args()
    records, audit, provenance = collect(args)
    print(f"Verified {len(records)} paired observations", flush=True)
    artifact, report = train(records, provenance)
    report["audit_counts"] = dict(collections.Counter(r["status"] for r in audit))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (("model.json", artifact), ("validation.json", report)):
        (args.output_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    write_csv(args.output_dir / "sample_audit.csv", audit)
    write_csv(args.output_dir / "verified_training_pairs.csv", records)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
