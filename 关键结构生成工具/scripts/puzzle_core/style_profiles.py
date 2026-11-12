"""Versioned soft references for key-structure experience analysis.

The values in this module describe the common *style* of the audited online
200--300 cohort.  They are deliberately not safety thresholds and must never
be presented as calibrated player-outcome risk.  Distances are zero anywhere
inside the reference band and increase on either side, so coupling is not
treated as something that is always better when smaller.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Mapping


ONLINE_STYLE_REFERENCE_COHORT = "level_200_300_v1"
ONLINE_STYLE_PROFILE_VERSION = "level_200_300_size_archetype_v2"
LAYER_SPAN_DEFINITION = "max_z_minus_min_z"
MIN_ARCHETYPE_STRATUM_SAMPLE = 8


@dataclass(frozen=True)
class SoftBand:
    """One non-causal, cohort-derived preferred interval."""

    lower: float
    upper: float
    sample_size: int
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SizeStyleProfile:
    """Soft reference for one mainstream key-structure size."""

    structure_count: int
    sample_size: int
    type_count: int
    copies_per_type: int
    dependency_components: SoftBand
    internal_dependency_relations: SoftBand
    initial_clickable: SoftBand
    clickable_after_clearing_ab: SoftBand
    direct_ab_cover_ratio: SoftBand
    transitive_ab_cover_ratio: SoftBand
    released_distinct_ab: SoftBand
    left_right_difference_ratio: SoftBand
    top_bottom_difference_ratio: SoftBand
    quadrants_occupied: SoftBand
    maximum_quadrant_share: SoftBand
    normalized_centroid_offset: SoftBand
    maximum_layer_share: SoftBand
    layer_share_stddev: SoftBand

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["layer_span_definition"] = LAYER_SPAN_DEFINITION
        return payload


@dataclass(frozen=True)
class StyleProfileSelection:
    """One auditable ``(structure_count, archetype)`` reference decision.

    A small archetype stratum is never treated as a stable standalone truth.
    It is shrunk toward the corresponding size cohort; an absent stratum falls
    back to the size cohort explicitly.  This prevents a one- or two-template
    special case from silently becoming the default online style.
    """

    profile: SizeStyleProfile | None
    status: str
    requested_group: str
    effective_group: str
    stratum_sample_size: int
    effective_sample_size: int
    confidence: str
    shrinkage_weight: float
    fallback_reason: str | None = None

    def evidence(self) -> dict[str, object]:
        return {
            "status": self.status,
            "requested_group": self.requested_group,
            "effective_group": self.effective_group,
            "sample_size": self.stratum_sample_size,
            "effective_sample_size": self.effective_sample_size,
            "confidence": self.confidence,
            "shrinkage_weight": self.shrinkage_weight,
            "fallback_reason": self.fallback_reason,
        }


# Exact descriptive values from the 44-template audit.  The 9/12-block groups
# contain 18 and 23 samples respectively; the remaining 3/18/21-block examples
# are intentionally not used to stretch the mainstream reference bands.
_SIZE_PROFILES = {
    9: SizeStyleProfile(
        structure_count=9,
        sample_size=18,
        type_count=3,
        copies_per_type=3,
        dependency_components=SoftBand(2.0, 3.0, 18, "IQR"),
        internal_dependency_relations=SoftBand(6.0, 8.0, 18, "IQR"),
        initial_clickable=SoftBand(0.0, 1.0, 18, "17/18 <= 1"),
        clickable_after_clearing_ab=SoftBand(3.0, 4.0, 18, "IQR"),
        direct_ab_cover_ratio=SoftBand(8.0 / 9.0, 1.0, 18, "17/18 >= 8/9"),
        transitive_ab_cover_ratio=SoftBand(1.0, 1.0, 18, "16/18 = 9/9"),
        released_distinct_ab=SoftBand(2.25, 10.0, 18, "IQR"),
        left_right_difference_ratio=SoftBand(0.0, 0.777778, 18, "zero-to-q75"),
        top_bottom_difference_ratio=SoftBand(0.0, 1.0, 18, "zero-to-q75"),
        quadrants_occupied=SoftBand(2.0, 3.0, 18, "IQR"),
        maximum_quadrant_share=SoftBand(0.333333, 0.777778, 18, "observed-min-to-q75"),
        normalized_centroid_offset=SoftBand(0.0, 0.653483, 18, "zero-to-q75"),
        maximum_layer_share=SoftBand(0.222222, 0.555556, 18, "IQR"),
        layer_share_stddev=SoftBand(0.0, 0.166667, 18, "zero-to-q75"),
    ),
    12: SizeStyleProfile(
        structure_count=12,
        sample_size=23,
        type_count=4,
        copies_per_type=3,
        dependency_components=SoftBand(2.0, 3.5, 23, "IQR"),
        internal_dependency_relations=SoftBand(10.0, 13.0, 23, "IQR"),
        initial_clickable=SoftBand(0.0, 1.0, 23, "23/23 <= 1"),
        clickable_after_clearing_ab=SoftBand(3.0, 5.5, 23, "IQR"),
        direct_ab_cover_ratio=SoftBand(11.0 / 12.0, 1.0, 23, "20/23 >= 11/12"),
        transitive_ab_cover_ratio=SoftBand(1.0, 1.0, 23, "21/23 = 12/12"),
        released_distinct_ab=SoftBand(4.0, 9.5, 23, "IQR"),
        left_right_difference_ratio=SoftBand(0.0, 0.583333, 23, "zero-to-q75"),
        top_bottom_difference_ratio=SoftBand(0.0, 0.666667, 23, "zero-to-q75"),
        quadrants_occupied=SoftBand(3.0, 4.0, 23, "IQR"),
        maximum_quadrant_share=SoftBand(0.333333, 0.583333, 23, "observed-min-to-q75"),
        normalized_centroid_offset=SoftBand(0.0, 0.560129, 23, "zero-to-q75"),
        maximum_layer_share=SoftBand(0.333333, 0.416667, 23, "IQR"),
        layer_share_stddev=SoftBand(0.0, 0.117851, 23, "zero-to-q75"),
    ),
}

SIZE_STYLE_PROFILES: Mapping[int, SizeStyleProfile] = MappingProxyType(
    _SIZE_PROFILES
)


# Frozen descriptive strata from the same 44-template audit. Hybrid strata
# are large enough to stand on their own. Board-reveal strata are intentionally
# retained even though they
# contain only one/two templates: selection code shrinks them toward the size
# cohort and exposes that low confidence instead of over-fitting.  No audited
# intrinsic-gate size stratum is currently available, so it falls back.
_ARCHETYPE_STYLE_PROFILES = {
    (9, "hybrid"): SizeStyleProfile(
        structure_count=9,
        sample_size=17,
        type_count=3,
        copies_per_type=3,
        dependency_components=SoftBand(2.0, 3.0, 17, "IQR"),
        internal_dependency_relations=SoftBand(6.0, 8.0, 17, "IQR"),
        initial_clickable=SoftBand(0.0, 0.0, 17, "IQR"),
        clickable_after_clearing_ab=SoftBand(3.0, 4.0, 17, "IQR"),
        direct_ab_cover_ratio=SoftBand(8.0 / 9.0, 1.0, 17, "IQR"),
        transitive_ab_cover_ratio=SoftBand(1.0, 1.0, 17, "IQR"),
        released_distinct_ab=SoftBand(2.0, 10.0, 17, "IQR"),
        left_right_difference_ratio=SoftBand(0.111111, 0.777778, 17, "IQR"),
        top_bottom_difference_ratio=SoftBand(0.333333, 1.0, 17, "IQR"),
        quadrants_occupied=SoftBand(2.0, 3.0, 17, "IQR"),
        maximum_quadrant_share=SoftBand(0.555556, 0.777778, 17, "IQR"),
        normalized_centroid_offset=SoftBand(0.42963, 0.653483, 17, "IQR"),
        maximum_layer_share=SoftBand(0.222222, 0.444444, 17, "IQR"),
        layer_share_stddev=SoftBand(0.044444, 0.124226, 17, "IQR"),
    ),
    (12, "hybrid"): SizeStyleProfile(
        structure_count=12,
        sample_size=21,
        type_count=4,
        copies_per_type=3,
        dependency_components=SoftBand(2.0, 3.0, 21, "IQR"),
        internal_dependency_relations=SoftBand(11.0, 13.0, 21, "IQR"),
        initial_clickable=SoftBand(0.0, 0.0, 21, "IQR"),
        clickable_after_clearing_ab=SoftBand(3.0, 5.0, 21, "IQR"),
        direct_ab_cover_ratio=SoftBand(0.916667, 1.0, 21, "IQR"),
        transitive_ab_cover_ratio=SoftBand(1.0, 1.0, 21, "IQR"),
        released_distinct_ab=SoftBand(4.0, 10.0, 21, "IQR"),
        left_right_difference_ratio=SoftBand(0.0, 0.666667, 21, "IQR"),
        top_bottom_difference_ratio=SoftBand(0.083333, 0.75, 21, "IQR"),
        quadrants_occupied=SoftBand(3.0, 4.0, 21, "IQR"),
        maximum_quadrant_share=SoftBand(0.416667, 0.666667, 21, "IQR"),
        normalized_centroid_offset=SoftBand(0.133763, 0.601498, 21, "IQR"),
        maximum_layer_share=SoftBand(0.333333, 0.416667, 21, "IQR"),
        layer_share_stddev=SoftBand(0.068041, 0.117851, 21, "IQR"),
    ),
    (9, "board_reveal"): SizeStyleProfile(
        structure_count=9,
        sample_size=1,
        type_count=3,
        copies_per_type=3,
        dependency_components=SoftBand(7.0, 7.0, 1, "observed range"),
        internal_dependency_relations=SoftBand(2.0, 2.0, 1, "observed range"),
        initial_clickable=SoftBand(0.0, 0.0, 1, "observed range"),
        clickable_after_clearing_ab=SoftBand(7.0, 7.0, 1, "observed range"),
        direct_ab_cover_ratio=SoftBand(1.0, 1.0, 1, "observed range"),
        transitive_ab_cover_ratio=SoftBand(1.0, 1.0, 1, "observed range"),
        released_distinct_ab=SoftBand(45.0, 45.0, 1, "observed range"),
        left_right_difference_ratio=SoftBand(0.0, 0.0, 1, "observed range"),
        top_bottom_difference_ratio=SoftBand(0.333333, 0.333333, 1, "observed range"),
        quadrants_occupied=SoftBand(3.0, 3.0, 1, "observed range"),
        maximum_quadrant_share=SoftBand(0.444444, 0.444444, 1, "observed range"),
        normalized_centroid_offset=SoftBand(0.090909, 0.090909, 1, "observed range"),
        maximum_layer_share=SoftBand(0.777778, 0.777778, 1, "observed range"),
        layer_share_stddev=SoftBand(0.277778, 0.277778, 1, "observed range"),
    ),
    (12, "board_reveal"): SizeStyleProfile(
        structure_count=12,
        sample_size=2,
        type_count=4,
        copies_per_type=3,
        dependency_components=SoftBand(7.0, 10.0, 2, "observed range"),
        internal_dependency_relations=SoftBand(2.0, 5.0, 2, "observed range"),
        initial_clickable=SoftBand(0.0, 0.0, 2, "observed range"),
        clickable_after_clearing_ab=SoftBand(7.0, 10.0, 2, "observed range"),
        direct_ab_cover_ratio=SoftBand(1.0, 1.0, 2, "observed range"),
        transitive_ab_cover_ratio=SoftBand(1.0, 1.0, 2, "observed range"),
        released_distinct_ab=SoftBand(0.0, 8.0, 2, "observed range"),
        left_right_difference_ratio=SoftBand(0.0, 0.166667, 2, "observed range"),
        top_bottom_difference_ratio=SoftBand(0.166667, 0.5, 2, "observed range"),
        quadrants_occupied=SoftBand(4.0, 4.0, 2, "observed range"),
        maximum_quadrant_share=SoftBand(0.333333, 0.416667, 2, "observed range"),
        normalized_centroid_offset=SoftBand(0.111111, 0.424918, 2, "observed range"),
        maximum_layer_share=SoftBand(0.416667, 0.833333, 2, "observed range"),
        layer_share_stddev=SoftBand(0.068041, 0.333333, 2, "observed range"),
    ),
}

ARCHETYPE_STYLE_PROFILES: Mapping[tuple[int, str], SizeStyleProfile] = (
    MappingProxyType(_ARCHETYPE_STYLE_PROFILES)
)

SIZE_COHORT_FACTS: Mapping[int, Mapping[str, object]] = MappingProxyType(
    {
        9: MappingProxyType(
            {
                "sample_size": 18,
                "composition": "3_types_times_3",
                "initial_clickable_zero": "16/18",
                "initial_clickable_at_most_one": "17/18",
                "direct_cover_at_least_8_of_9": "17/18",
                "transitive_cover_9_of_9": "16/18",
                "dependency_components_median": 3,
                "internal_dependencies_median": 7,
                "clickable_after_clearing_ab_median": 3,
                "released_distinct_ab_median": 6,
            }
        ),
        12: MappingProxyType(
            {
                "sample_size": 23,
                "composition": "4_types_times_3",
                "initial_clickable_zero": "21/23",
                "initial_clickable_at_most_one": "23/23",
                "direct_cover_at_least_11_of_12": "20/23",
                "transitive_cover_12_of_12": "21/23",
                "dependency_components_median": 3,
                "internal_dependencies_median": 12,
                "clickable_after_clearing_ab_median": 4,
                "released_distinct_ab_median": 6,
            }
        ),
    }
)

# Cross-size descriptive references from the same 44-template cohort.
GLOBAL_SOFT_BANDS: Mapping[str, SoftBand] = MappingProxyType(
    {
        "layer_span": SoftBand(2.0, 5.0, 44, "35/44 in band"),
        "direct_ab_cover_ratio": SoftBand(0.889, 1.0, 44, "q10-q90"),
        "transitive_ab_cover_ratio": SoftBand(0.967, 1.0, 44, "q10-q90"),
        "released_ab_per_structure": SoftBand(0.31, 0.94, 44, "IQR"),
        "normalized_centroid_offset_preferred": SoftBand(
            0.0, 0.5, 44, "preferred; cohort median 0.369, q75 0.514"
        ),
        "normalized_centroid_offset_transition": SoftBand(
            0.5, 0.65, 44, "gradual style distance; cohort q90 0.615"
        ),
    }
)

COHORT_FACTS: Mapping[str, object] = MappingProxyType(
    {
        "sample_size": 44,
        "excluded_template": "newv1hhmy046moretile",
        "mainstream_9_or_12": "41/44",
        "three_copies_per_type": "43/44",
        "touches_layer_1": "40/44",
        "layer_span_median": 4,
        "layer_span_2_through_5": "35/44",
        "multiple_dependency_components": "38/44",
        "dependency_components_median": 3,
        "dependency_components_2_or_3": "28/44",
        "initial_clickable_zero": "39/44",
        "initial_clickable_at_most_one": "43/44",
        "all_directly_covered_by_ab": "32/44",
        "all_transitively_covered_by_ab": "39/44",
        "releases_at_least_one_ab": "41/44",
        "exact_mirror": "2/44",
        "layer_span_definition": LAYER_SPAN_DEFINITION,
    }
)


def soft_band_distance(value: float, band: SoftBand) -> float:
    """Return a symmetric outside-band distance in band-width units.

    A value anywhere in ``[lower, upper]`` has zero distance.  Both too little
    and too much coupling are therefore penalized.  Ratio-like bands use a
    ten-percentage-point minimum scale, while count-like bands use one count.
    This keeps exact-one ratio targets meaningful without turning harmless
    floating-point noise into an infinite score.
    """

    numeric = float(value)
    lower = float(band.lower)
    upper = float(band.upper)
    raw_width = upper - lower
    ratio_like = 0.0 <= lower <= upper <= 1.0
    minimum_scale = 0.1 if ratio_like else 1.0
    width = max(raw_width, minimum_scale)
    if numeric < lower:
        return (lower - numeric) / width
    if numeric > upper:
        return (numeric - upper) / width
    return 0.0


def profile_for_structure_count(count: int) -> SizeStyleProfile | None:
    """Return an exact mainstream reference, never a silently nearest one."""

    return SIZE_STYLE_PROFILES.get(count)


_PROFILE_BAND_FIELDS = (
    "dependency_components",
    "internal_dependency_relations",
    "initial_clickable",
    "clickable_after_clearing_ab",
    "direct_ab_cover_ratio",
    "transitive_ab_cover_ratio",
    "released_distinct_ab",
    "left_right_difference_ratio",
    "top_bottom_difference_ratio",
    "quadrants_occupied",
    "maximum_quadrant_share",
    "normalized_centroid_offset",
    "maximum_layer_share",
    "layer_share_stddev",
)


def _shrunk_profile(
    size_profile: SizeStyleProfile,
    stratum_profile: SizeStyleProfile,
    weight: float,
) -> SizeStyleProfile:
    values: dict[str, object] = {
        "structure_count": size_profile.structure_count,
        "sample_size": stratum_profile.sample_size,
        "type_count": size_profile.type_count,
        "copies_per_type": size_profile.copies_per_type,
    }
    for field_name in _PROFILE_BAND_FIELDS:
        parent = getattr(size_profile, field_name)
        child = getattr(stratum_profile, field_name)
        values[field_name] = SoftBand(
            lower=(1.0 - weight) * parent.lower + weight * child.lower,
            upper=(1.0 - weight) * parent.upper + weight * child.upper,
            sample_size=stratum_profile.sample_size,
            evidence=(
                f"shrunk_to_size_cohort; raw={child.evidence}; "
                f"weight={weight:.3f}"
            ),
        )
    return SizeStyleProfile(**values)


def select_style_profile(
    count: int, archetype: str | None
) -> StyleProfileSelection:
    """Select a versioned style baseline without hiding sparse evidence."""

    requested_archetype = archetype if archetype else "unclassified"
    requested_group = f"{count}_blocks/{requested_archetype}"
    size_profile = SIZE_STYLE_PROFILES.get(count)
    if size_profile is None:
        return StyleProfileSelection(
            profile=None,
            status="global_reference_only",
            requested_group=requested_group,
            effective_group="global",
            stratum_sample_size=0,
            effective_sample_size=int(COHORT_FACTS["sample_size"]),
            confidence="low",
            shrinkage_weight=0.0,
            fallback_reason="no_exact_size_reference",
        )
    if archetype is None:
        return StyleProfileSelection(
            profile=size_profile,
            status="size_reference_fallback",
            requested_group=requested_group,
            effective_group=f"{count}_blocks/all_archetypes",
            stratum_sample_size=0,
            effective_sample_size=size_profile.sample_size,
            confidence="low",
            shrinkage_weight=0.0,
            fallback_reason="archetype_classification_uncertain",
        )
    stratum = ARCHETYPE_STYLE_PROFILES.get((count, archetype))
    if stratum is None:
        return StyleProfileSelection(
            profile=size_profile,
            status="size_reference_fallback",
            requested_group=requested_group,
            effective_group=f"{count}_blocks/all_archetypes",
            stratum_sample_size=0,
            effective_sample_size=size_profile.sample_size,
            confidence="low",
            shrinkage_weight=0.0,
            fallback_reason="archetype_stratum_unavailable",
        )
    if stratum.sample_size < MIN_ARCHETYPE_STRATUM_SAMPLE:
        weight = stratum.sample_size / MIN_ARCHETYPE_STRATUM_SAMPLE
        return StyleProfileSelection(
            profile=_shrunk_profile(size_profile, stratum, weight),
            status="shrunk_size_archetype_reference",
            requested_group=requested_group,
            effective_group=(
                f"{requested_group}+{count}_blocks/all_archetypes"
            ),
            stratum_sample_size=stratum.sample_size,
            effective_sample_size=size_profile.sample_size,
            confidence="low",
            shrinkage_weight=weight,
            fallback_reason="archetype_stratum_below_minimum_sample",
        )
    return StyleProfileSelection(
        profile=stratum,
        status="exact_size_archetype_reference",
        requested_group=requested_group,
        effective_group=requested_group,
        stratum_sample_size=stratum.sample_size,
        effective_sample_size=stratum.sample_size,
        confidence="usable",
        shrinkage_weight=1.0,
    )


def profile_manifest() -> dict[str, object]:
    """Return a JSON-friendly, auditable snapshot of the frozen reference."""

    return {
        "version": ONLINE_STYLE_PROFILE_VERSION,
        "reference_cohort": ONLINE_STYLE_REFERENCE_COHORT,
        "layer_span_definition": LAYER_SPAN_DEFINITION,
        "cohort": dict(COHORT_FACTS),
        "size_profiles": {
            str(size): profile.as_dict()
            for size, profile in SIZE_STYLE_PROFILES.items()
        },
        "size_archetype_profiles": {
            f"{size}/{archetype}": profile.as_dict()
            for (size, archetype), profile in ARCHETYPE_STYLE_PROFILES.items()
        },
        "minimum_archetype_stratum_sample": MIN_ARCHETYPE_STRATUM_SAMPLE,
        "size_cohort_facts": {
            str(size): dict(facts) for size, facts in SIZE_COHORT_FACTS.items()
        },
        "global_soft_bands": {
            name: band.as_dict() for name, band in GLOBAL_SOFT_BANDS.items()
        },
        "causal_or_risk_calibration": "unavailable",
    }


__all__ = [
    "ARCHETYPE_STYLE_PROFILES",
    "COHORT_FACTS",
    "GLOBAL_SOFT_BANDS",
    "LAYER_SPAN_DEFINITION",
    "ONLINE_STYLE_PROFILE_VERSION",
    "ONLINE_STYLE_REFERENCE_COHORT",
    "MIN_ARCHETYPE_STRATUM_SAMPLE",
    "SIZE_STYLE_PROFILES",
    "SIZE_COHORT_FACTS",
    "SizeStyleProfile",
    "StyleProfileSelection",
    "SoftBand",
    "profile_for_structure_count",
    "profile_manifest",
    "select_style_profile",
    "soft_band_distance",
]
