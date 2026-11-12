from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core import dynamic_reference  # noqa: E402
from puzzle_core.dynamic_reference import (  # noqa: E402
    DYNAMIC_REFERENCE_FILE,
    DYNAMIC_REFERENCE_VERSION,
    _select_reference,
    compare_dynamic_reference,
)
from puzzle_core.style_profiles import (  # noqa: E402
    ARCHETYPE_STYLE_PROFILES,
    SIZE_STYLE_PROFILES,
)
from tools.derive_online_dynamic_reference import (  # noqa: E402
    COHORT,
    EXCLUDED_SPECIAL,
)


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


class DynamicReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            DYNAMIC_REFERENCE_FILE.read_text(encoding="utf-8")
        )

    def test_frozen_manifest_has_exact_audited_cohort_and_exclusion(self) -> None:
        manifest = self.manifest
        members = manifest["members"]
        self.assertEqual(manifest["version"], DYNAMIC_REFERENCE_VERSION)
        self.assertEqual(len(COHORT), 44)
        self.assertEqual(len(members), 44)
        self.assertEqual(
            {
                (
                    member["level_index"],
                    member["online_level_id"],
                    member["archetype_stratum"],
                )
                for member in members
            },
            set(COHORT),
        )
        self.assertEqual(
            manifest["excluded_special"]["level_index"],
            EXCLUDED_SPECIAL[0],
        )
        self.assertEqual(
            manifest["excluded_special"]["online_level_id"],
            EXCLUDED_SPECIAL[1],
        )
        self.assertNotIn(
            EXCLUDED_SPECIAL[1],
            {member["online_level_id"] for member in members},
        )
        self.assertEqual(
            {
                key: group["sample_size"]
                for key, group in manifest["groups"].items()
            },
            {
                "9/all": 18,
                "9/hybrid": 17,
                "9/board_reveal": 1,
                "12/all": 23,
                "12/hybrid": 21,
                "12/board_reveal": 2,
            },
        )

    def test_manifest_provenance_is_complete_and_runtime_portable(self) -> None:
        manifest = self.manifest
        self.assertEqual(
            DYNAMIC_REFERENCE_FILE.resolve().parent,
            Path(dynamic_reference.__file__).resolve().parent,
        )
        self.assertEqual(DYNAMIC_REFERENCE_FILE.name, "online_dynamic_reference.json")
        self.assertEqual(
            manifest["derivation"]["template_vote"],
            "one template per feature",
        )
        self.assertEqual(
            manifest["derivation"]["strategies"],
            ["random", "greedy", "local_optimal", "unlock", "adversarial"],
        )
        self.assertEqual(manifest["derivation"]["duplicate_count"], 10)
        self.assertEqual(manifest["derivation"]["progress_bins"], 5)
        self.assertEqual(
            len(manifest["derivation"]["config_file_sha256"]), 64
        )
        for member in manifest["members"]:
            self.assertEqual(len(member["template_sha256"]), 64)
            self.assertEqual(len(member["config_row_sha256"]), 64)
            static = member["static_reference"]
            self.assertEqual(static["geometry_scope"], "tower_external_static")
            self.assertEqual(
                static["metrics"]["structure_count"],
                member["structure_count"],
            )
            self.assertIn("normalized_centroid_offset", static["visual_balance"])

        # The committed runtime artifact must be relocatable.  External paths
        # are CLI inputs to the offline derivation script, never runtime data.
        def walk(value: object):
            if isinstance(value, dict):
                for nested in value.values():
                    yield from walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from walk(nested)
            elif isinstance(value, str):
                yield value

        strings = list(walk(manifest))
        self.assertFalse(any(value.startswith("/") for value in strings))
        self.assertFalse(any("huanghemingyang" in value for value in strings))
        self.assertFalse(any("tileleveltemplate" in value for value in strings))

    def test_static_snapshot_reproduces_style_profile_inputs(self) -> None:
        members = self.manifest["members"]
        fields = {
            "dependency_components": "dependency_component_count",
            "internal_dependency_relations": "internal_dependency_relation_count",
            "clickable_after_clearing_ab": "clickable_after_clearing_ab_count",
            "released_distinct_ab": "released_distinct_ab_count",
        }
        for structure_count, profile in SIZE_STYLE_PROFILES.items():
            rows = [
                member["static_reference"]["metrics"]
                for member in members
                if member["structure_count"] == structure_count
            ]
            self.assertEqual(len(rows), profile.sample_size)
            for profile_name, metric_name in fields.items():
                band = getattr(profile, profile_name)
                values = [float(row[metric_name]) for row in rows]
                self.assertAlmostEqual(band.lower, _quantile(values, 0.25), 6)
                self.assertAlmostEqual(band.upper, _quantile(values, 0.75), 6)

        # Large hybrid strata use their own IQR; tiny board-reveal strata use
        # the observed range before runtime shrinkage toward the size parent.
        for key, profile in ARCHETYPE_STYLE_PROFILES.items():
            structure_count, archetype = key
            rows = [
                member["static_reference"]["metrics"]
                for member in members
                if member["structure_count"] == structure_count
                and member["archetype_stratum"] == archetype
            ]
            self.assertEqual(len(rows), profile.sample_size)
            values = [float(row["internal_dependency_relation_count"]) for row in rows]
            if len(rows) >= 8:
                lower, upper = _quantile(values, 0.25), _quantile(values, 0.75)
            else:
                lower, upper = min(values), max(values)
            self.assertAlmostEqual(profile.internal_dependency_relations.lower, lower, 6)
            self.assertAlmostEqual(profile.internal_dependency_relations.upper, upper, 6)

    def test_auto_exact_shrunk_and_fallback_reference_selection(self) -> None:
        auto_reference, auto = _select_reference(9, "auto")
        self.assertIsNotNone(auto_reference)
        self.assertEqual(auto["status"], "size_reference")
        self.assertEqual(auto["effective_group"], "9/all")
        self.assertEqual(auto["reference_member_count"], 44)

        exact_reference, exact = _select_reference(9, "hybrid")
        self.assertIsNotNone(exact_reference)
        self.assertEqual(exact["status"], "exact_size_archetype_reference")
        self.assertEqual(exact["effective_group"], "9/hybrid")
        self.assertEqual(exact_reference["sample_size"], 17)

        shrunk_reference, shrunk = _select_reference(12, "board_reveal")
        self.assertIsNotNone(shrunk_reference)
        self.assertEqual(
            shrunk["status"], "shrunk_size_archetype_reference"
        )
        self.assertEqual(shrunk["effective_group"], "12/board_reveal+12/all")
        self.assertEqual(shrunk["shrinkage_weight"], 0.25)
        self.assertEqual(shrunk_reference["sample_size"], 2)

        fallback_reference, fallback = _select_reference(9, "intrinsic_gate")
        self.assertIsNotNone(fallback_reference)
        self.assertEqual(fallback["status"], "size_reference_fallback")
        self.assertEqual(fallback["effective_group"], "9/all")
        self.assertEqual(
            fallback["reason"], "archetype_dynamic_reference_unavailable"
        )

    def test_missing_structure_size_is_inconclusive_not_zero_distance(self) -> None:
        report = compare_dynamic_reference(
            (), structure_count=18, board_size=100, requested_archetype="auto"
        )
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["reference_selection_status"], "inconclusive")
        self.assertEqual(report["reason"], "no_exact_size_dynamic_reference")
        self.assertIsNone(report["effective_group"])
        self.assertIsNone(report["primary_distance"])
        self.assertIsNone(report["stable_tiebreak"])
        self.assertEqual(report["coverage"], 0.0)
        self.assertEqual(report["reference_member_count"], 44)

    def test_comparison_separates_reference_selection_from_evidence_status(self) -> None:
        manifest = {
            "version": DYNAMIC_REFERENCE_VERSION,
            "reference_cohort": "fixture_cohort",
            "excluded_special": {
                "level_index": 203,
                "online_level_id": "fixture_special",
            },
            "members": [{} for _ in range(44)],
            "groups": {
                "9/all": {
                    "sample_size": 18,
                    "features": {
                        "dup01.random.present.reveal": {
                            "lower": 1.0,
                            "upper": 1.0,
                            "median": 1.0,
                            "robust_scale": 0.05,
                            "sample_size": 18,
                            "support": 1.0,
                        }
                    },
                }
            },
        }
        rows = (
            {
                "duplicate_index": 1,
                "strategies": {"random": {"reveal_curve": {"curve": []}}},
            },
        )
        with patch.object(dynamic_reference, "_load_manifest", return_value=manifest):
            report = compare_dynamic_reference(
                rows,
                structure_count=9,
                board_size=100,
                requested_archetype="auto",
            )

        self.assertEqual(report["status"], "compared")
        self.assertEqual(report["reference_selection_status"], "size_reference")
        self.assertEqual(report["requested_group"], "9/auto")
        self.assertEqual(report["effective_group"], "9/all")
        self.assertEqual(report["reference_cohort"], "fixture_cohort")
        self.assertEqual(report["reference_member_count"], 44)
        self.assertEqual(report["coverage"], 1.0)
        self.assertEqual(report["primary_distance"], 0.0)
        self.assertTrue(math.isfinite(report["stable_tiebreak"]))
        self.assertEqual(
            report["claim"], "descriptive_reference_not_outcome_calibration"
        )


if __name__ == "__main__":
    unittest.main()
