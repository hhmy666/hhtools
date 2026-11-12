from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.experience import (  # noqa: E402
    COMPOSITE_CLUSTER_CANONICALIZATION_VERSION,
    COMPOSITE_CLUSTER_GRAPH_VERSION,
    EXPERIENCE_SIGNATURE_BIN_BOUNDARIES,
    EXPERIENCE_SIGNATURE_BINNING_VERSION,
    coupling_target_distance,
    evaluate_static_experience,
)
from puzzle_core.style_profiles import (  # noqa: E402
    MIN_ARCHETYPE_STRATUM_SAMPLE,
    ONLINE_STYLE_PROFILE_VERSION,
    ONLINE_STYLE_REFERENCE_COHORT,
    SoftBand,
    profile_manifest,
    select_style_profile,
    soft_band_distance,
)
from puzzle_core.special_presets import (  # noqa: E402
    SPECIAL_GRAMMAR_VERSION,
    SUSPENDED_EXPOSURE_BOUND_VERSION,
)
from puzzle_core.search import _experience_dynamic_inputs  # noqa: E402
from puzzle_core.template_reader import LevelTemplate, Tile  # noqa: E402


def _level(
    coords: list[tuple[int, int, int, int]],
    *,
    template_id: str = "experience-fixture",
) -> LevelTemplate:
    tiles = tuple(
        Tile(
            index=index,
            top_field_index=index,
            x=x,
            y=y,
            z=z,
            tile_id=tile_id,
            debuff_data_index=None,
            fields=(),
            raw=b"",
        )
        for index, (x, y, z, tile_id) in enumerate(coords)
    )
    return LevelTemplate(
        data=(template_id + repr(coords)).encode("utf-8"),
        fields=(),
        template_id=template_id,
        template_id_field_index=0,
        tiles=tiles,
        towers=(),
        level_width=40,
        level_height=20,
    )


def _intrinsic_fixture(*, dx: int = 0, dy: int = 0) -> tuple[LevelTemplate, tuple[int, ...], tuple[str, ...]]:
    coords = [
        (dx, dy, z, -10 if z % 2 == 0 else -11) for z in range(9)
    ]
    # Board-extents anchors keep visual normalization deterministic without
    # interacting with the structure column.
    coords.extend(((dx - 8, dy - 4, 0, -10), (dx + 8, dy + 4, 0, -11)))
    return (
        _level(coords),
        tuple(range(9)),
        ("C", "C", "C", "D", "D", "D", "E", "E", "E"),
    )


def _board_reveal_fixture() -> tuple[LevelTemplate, tuple[int, ...], tuple[str, ...]]:
    coords: list[tuple[int, int, int, int]] = []
    chosen = []
    for offset in range(9):
        x = offset * 4
        chosen.append(len(coords))
        coords.append((x, 0, 1, -10))  # becomes C--E
        coords.append((x, 0, 2, -11))  # A/B reveals C--E
        coords.append((x, 0, 0, -10))  # C--E releases A/B
    return (
        _level(coords),
        tuple(chosen),
        ("C", "C", "C", "D", "D", "D", "E", "E", "E"),
    )


def _hybrid_fixture() -> tuple[LevelTemplate, tuple[int, ...], tuple[str, ...]]:
    coords = [(0, 0, z + 1, -10) for z in range(9)]
    coords.extend(((0, 0, 10, -11), (0, 0, 0, -10)))
    return (
        _level(coords),
        tuple(range(9)),
        ("C", "C", "C", "D", "D", "D", "E", "E", "E"),
    )


class StyleProfileTests(unittest.TestCase):
    def test_profile_manifest_freezes_exact_cohort_denominators(self) -> None:
        manifest = profile_manifest()

        self.assertEqual(manifest["version"], ONLINE_STYLE_PROFILE_VERSION)
        self.assertEqual(
            manifest["reference_cohort"], ONLINE_STYLE_REFERENCE_COHORT
        )
        self.assertEqual(manifest["cohort"]["sample_size"], 44)
        self.assertEqual(manifest["cohort"]["mainstream_9_or_12"], "41/44")
        self.assertEqual(manifest["layer_span_definition"], "max_z_minus_min_z")
        self.assertEqual(manifest["size_profiles"]["9"]["sample_size"], 18)
        self.assertEqual(manifest["size_profiles"]["12"]["sample_size"], 23)
        self.assertEqual(
            manifest["size_profiles"]["9"]["quadrants_occupied"]["evidence"],
            "IQR",
        )
        self.assertEqual(
            manifest["size_profiles"]["12"]["left_right_difference_ratio"][
                "sample_size"
            ],
            23,
        )
        self.assertEqual(
            manifest["minimum_archetype_stratum_sample"],
            MIN_ARCHETYPE_STRATUM_SAMPLE,
        )
        self.assertEqual(
            manifest["size_archetype_profiles"]["9/hybrid"]["sample_size"],
            17,
        )

    def test_size_archetype_reference_shrinks_or_falls_back_explicitly(self) -> None:
        exact = select_style_profile(9, "hybrid")
        sparse = select_style_profile(9, "board_reveal")
        missing = select_style_profile(9, "intrinsic_gate")

        self.assertEqual(exact.status, "exact_size_archetype_reference")
        self.assertEqual(exact.stratum_sample_size, 17)
        self.assertEqual(exact.confidence, "usable")
        self.assertEqual(sparse.status, "shrunk_size_archetype_reference")
        self.assertEqual(sparse.stratum_sample_size, 1)
        self.assertLess(sparse.shrinkage_weight, 1)
        self.assertEqual(sparse.confidence, "low")
        self.assertEqual(missing.status, "size_reference_fallback")
        self.assertEqual(missing.stratum_sample_size, 0)
        self.assertEqual(
            missing.fallback_reason, "archetype_stratum_unavailable"
        )

    def test_coupling_distance_is_u_shaped_not_monotonic(self) -> None:
        middle = {
            "internal_dependency_relation_count": 7,
            "direct_ab_cover_ratio": 8 / 9,
            "transitive_ab_cover_ratio": 1,
            "released_distinct_ab_count": 6,
            "released_ab_per_structure": 0.67,
        }
        low = {
            **middle,
            "internal_dependency_relation_count": 0,
            "direct_ab_cover_ratio": 0,
            "transitive_ab_cover_ratio": 0,
            "released_distinct_ab_count": 0,
            "released_ab_per_structure": 0,
        }
        high = {
            **middle,
            "internal_dependency_relation_count": 20,
            "released_distinct_ab_count": 18,
            "released_ab_per_structure": 2,
        }

        middle_distance = coupling_target_distance(middle, 9)["distance"]
        self.assertEqual(middle_distance, 0)
        self.assertGreater(coupling_target_distance(low, 9)["distance"], 0)
        self.assertGreater(coupling_target_distance(high, 9)["distance"], 0)

    def test_ratio_band_distance_is_not_diluted_by_count_scale(self) -> None:
        exact_one = SoftBand(1.0, 1.0, 44, "fixture")

        self.assertEqual(soft_band_distance(1.0, exact_one), 0)
        self.assertAlmostEqual(soft_band_distance(0.9, exact_one), 1.0)
        self.assertAlmostEqual(soft_band_distance(1.1, exact_one), 1.0)

    def test_count_band_keeps_one_count_minimum_scale(self) -> None:
        exact_six = SoftBand(6.0, 6.0, 44, "fixture")

        self.assertEqual(soft_band_distance(6.0, exact_six), 0)
        self.assertEqual(soft_band_distance(5.0, exact_six), 1.0)
        self.assertEqual(soft_band_distance(7.0, exact_six), 1.0)


class StaticExperienceTests(unittest.TestCase):
    def test_composite_cluster_graph_adds_proximity_and_dependency_semantics(self) -> None:
        proximity_level = _level(
            [(0, 0, 0, -10), (2, 0, 0, -10), (20, 0, 0, -10)]
        )
        proximity = evaluate_static_experience(
            proximity_level, (0, 1, 2), ("C", "C", "C")
        )["composite_cluster_graph"]

        self.assertEqual(proximity["version"], COMPOSITE_CLUSTER_GRAPH_VERSION)
        self.assertEqual(proximity["cluster_count"], 2)
        edge_kinds = {
            kind
            for edge in proximity["membership_edges"]
            for kind in edge["kinds"]
        }
        self.assertIn("projection_neighbor", edge_kinds)
        self.assertIn("spatial_neighbor", edge_kinds)
        self.assertEqual(proximity["pair_relation_counts"]["parallel"], 1)

        # C(upper) -> A/B bridge -> C(lower) proves a serial inter-cluster
        # dependency without a direct C-to-C cover relation.
        serial_level = _level(
            [(-1, 0, 2, -10), (0, 0, 1, -10), (1, 0, 0, -10)]
        )
        serial = evaluate_static_experience(
            serial_level, (0, 2), ("C", "C")
        )["composite_cluster_graph"]

        self.assertEqual(serial["cluster_count"], 2)
        self.assertEqual(
            serial["dependency_dag"],
            [{"from_cluster": 1, "to_cluster": 2}],
        )
        self.assertEqual(serial["pair_relation_counts"]["serial"], 1)

    def test_low_confidence_archetype_remains_unclassified(self) -> None:
        level = _level([(offset * 5, 0, 0, -10) for offset in range(9)])
        result = evaluate_static_experience(
            level,
            tuple(range(9)),
            ("C", "C", "C", "D", "D", "D", "E", "E", "E"),
            requested_archetype="board_reveal",
        )
        archetype = result["archetype"]

        self.assertEqual(archetype["classification_status"], "uncertain")
        self.assertIsNone(archetype["primary"])
        self.assertEqual(len(archetype["top2"]), 2)
        self.assertFalse(archetype["requested_matches_automatic"])
        self.assertEqual(archetype["match_score"], 0.0)
        self.assertGreater(archetype["requested_match_score"], 0.0)
        style = result["online_style_distance"]
        self.assertEqual(style["reference_cohort"], ONLINE_STYLE_REFERENCE_COHORT)
        self.assertEqual(style["status"], "size_reference_fallback")
        self.assertEqual(style["sample_size"], 0)
        self.assertEqual(style["effective_sample_size"], 18)
        self.assertEqual(style["reference_confidence"], "low")
        self.assertEqual(
            style["fallback_reason"], "archetype_classification_uncertain"
        )

    def test_static_board_metrics_capture_bidirectional_coupling(self) -> None:
        level, chosen, types = _board_reveal_fixture()

        result = evaluate_static_experience(level, chosen, types)
        metrics = result["metrics"]

        self.assertEqual(metrics["initial_clickable_count"], 0)
        self.assertEqual(metrics["clickable_after_clearing_ab_count"], 9)
        self.assertEqual(metrics["direct_ab_cover_ratio"], 1)
        self.assertEqual(metrics["transitive_ab_cover_ratio"], 1)
        self.assertEqual(metrics["released_distinct_ab_count"], 9)
        self.assertEqual(metrics["bidirectionally_coupled_structure_ratio"], 1)
        self.assertEqual(result["observed_risk"]["status"], "unavailable")
        self.assertEqual(result["simulation_pressure"]["level"], "inconclusive")

    def test_non_mirror_layout_can_still_be_visually_balanced(self) -> None:
        # Counts are balanced around the board center, but positions do not
        # form exact mirror pairs.
        level = _level(
            [
                (0, 0, 0, -10),
                (2, 3, 0, -10),
                (7, 1, 0, -11),
                (10, 4, 0, -11),
                (-1, -1, 0, -10),
                (11, 5, 0, -11),
            ]
        )
        result = evaluate_static_experience(
            level, (0, 1, 2, 3), ("C", "C", "C", "D")
        )
        balance = result["visual_balance"]

        self.assertEqual(balance["left_right_count_difference_ratio"], 0)
        self.assertLess(balance["normalized_centroid_offset"], 0.2)
        self.assertNotIn("exact_mirror", balance)

    def test_translation_does_not_change_experience_signature(self) -> None:
        first_level, chosen, types = _intrinsic_fixture()
        second_level, second_chosen, second_types = _intrinsic_fixture(
            dx=100, dy=-70
        )

        first = evaluate_static_experience(first_level, chosen, types)
        second = evaluate_static_experience(
            second_level, second_chosen, second_types
        )

        self.assertEqual(first["experience_signature"], second["experience_signature"])
        signature_text = repr(first["experience_signature_evidence"])
        self.assertNotIn("100", signature_text)
        self.assertNotIn("-70", signature_text)

    def test_dependency_change_changes_experience_signature(self) -> None:
        first = _level(
            [(0, 0, 0, -10), (0, 0, 1, -10), (5, 0, 2, -10)]
        )
        second = _level(
            [(0, 0, 0, -10), (5, 0, 1, -10), (5, 0, 2, -10)]
        )
        first_result = evaluate_static_experience(
            first, (0, 1, 2), ("C", "C", "C")
        )
        second_result = evaluate_static_experience(
            second, (0, 1, 2), ("C", "C", "C")
        )

        self.assertNotEqual(
            first_result["experience_signature"],
            second_result["experience_signature"],
        )

    def test_cluster_and_source_index_permutation_preserves_signature(self) -> None:
        # The same upper-cluster -> A/B bridge -> lower-cluster mechanism is
        # serialized in opposite source order.  The public diagnostic graph
        # may retain those source-local ids, but the experience signature may
        # not.
        first = _level(
            [(-1, 0, 2, -10), (0, 0, 1, -10), (1, 0, 0, -10)]
        )
        permuted = _level(
            [(1, 0, 0, -10), (0, 0, 1, -10), (-1, 0, 2, -10)]
        )

        first_result = evaluate_static_experience(
            first, (0, 2), ("C", "D")
        )
        permuted_result = evaluate_static_experience(
            permuted, (0, 2), ("D", "C")
        )

        self.assertNotEqual(
            first_result["composite_cluster_graph"]["dependency_dag"],
            permuted_result["composite_cluster_graph"]["dependency_dag"],
        )
        self.assertEqual(
            first_result["experience_signature"],
            permuted_result["experience_signature"],
        )
        canonical = first_result["experience_signature_evidence"][
            "composite_cluster_graph"
        ]
        self.assertEqual(
            canonical["canonicalization_version"],
            COMPOSITE_CLUSTER_CANONICALIZATION_VERSION,
        )
        self.assertNotIn("cluster_id", repr(canonical))
        self.assertNotIn("from_cluster", repr(canonical))
        self.assertNotIn("to_cluster", repr(canonical))

    def test_type_label_permutation_does_not_fake_experience_diversity(self) -> None:
        level, chosen, types = _intrinsic_fixture()
        renamed = tuple(
            {"C": "J", "D": "K", "E": "L"}[label] for label in types
        )

        first = evaluate_static_experience(level, chosen, types)
        second = evaluate_static_experience(level, chosen, renamed)

        self.assertEqual(first["experience_signature"], second["experience_signature"])

    def test_experience_signature_uses_frozen_at_most_five_interval_bins(self) -> None:
        self.assertEqual(
            EXPERIENCE_SIGNATURE_BINNING_VERSION,
            "level-200-300-five-interval-bins-v1",
        )
        self.assertTrue(EXPERIENCE_SIGNATURE_BIN_BOUNDARIES)
        for name, boundaries in EXPERIENCE_SIGNATURE_BIN_BOUNDARIES.items():
            with self.subTest(name=name):
                self.assertLessEqual(len(boundaries), 4)
                self.assertEqual(tuple(sorted(set(boundaries))), boundaries)

        level, chosen, types = _intrinsic_fixture()
        result = evaluate_static_experience(level, chosen, types)
        signature_evidence = result["experience_signature_evidence"]
        self.assertEqual(
            signature_evidence["binning_version"],
            EXPERIENCE_SIGNATURE_BINNING_VERSION,
        )
        self.assertEqual(
            set(signature_evidence["binning_manifest"]),
            set(EXPERIENCE_SIGNATURE_BIN_BOUNDARIES),
        )

    def test_full_reveal_curve_changes_experience_signature(self) -> None:
        level, chosen, types = _intrinsic_fixture()

        def reveals(exposed: int) -> dict[str, object]:
            distribution = {
                "minimum": exposed,
                "q25": exposed,
                "median": exposed,
                "q75": exposed,
                "maximum": exposed,
            }
            curve_row = {
                "current_clickable_structure_count": distribution,
                "cumulative_exposed_structure_count": distribution,
                "complete_structure_group_type_count": distribution,
                "dangling_one_type_count": distribution,
                "dangling_two_type_count": distribution,
            }
            return {
                "first_match_progress": 0.5,
                "dangling_area": 2,
                "path_spread": 0.25,
                "per_duplicate": [
                    {
                        "curve": [curve_row] * 5,
                        "first_three_structure_matches": {},
                        "released_ab_per_structure_click": distribution,
                        "released_ab_on_match_completion_click": distribution,
                        "released_ab_total_per_path": distribution,
                    }
                ],
            }

        first = evaluate_static_experience(
            level, chosen, types, reveals=reveals(1)
        )
        second = evaluate_static_experience(
            level, chosen, types, reveals=reveals(6)
        )

        self.assertNotEqual(first["experience_signature"], second["experience_signature"])

    def test_equal_weight_strategy_curves_change_experience_signature(self) -> None:
        level, chosen, types = _intrinsic_fixture()

        def reveals(exposed: int) -> dict[str, object]:
            distribution = {
                "minimum": exposed,
                "q25": exposed,
                "median": exposed,
                "q75": exposed,
                "maximum": exposed,
            }
            curve = {
                "curve": [
                    {
                        "current_clickable_structure_count": distribution,
                        "cumulative_exposed_structure_count": distribution,
                        "complete_structure_group_type_count": distribution,
                        "dangling_one_type_count": distribution,
                        "dangling_two_type_count": distribution,
                    }
                ] * 5,
                "first_three_structure_matches": {},
                "released_ab_per_structure_click": distribution,
                "released_ab_on_match_completion_click": distribution,
                "released_ab_total_per_path": distribution,
            }
            return {
                "reveal_strength": 0.5,
                "median_reveal_strength": 0.5,
                "worst_reveal_strength": 0.5,
                "reveal_path_spread": 0.0,
                "per_duplicate_strategy_reveal": [
                    {
                        "duplicate_index": 1,
                        "strategies": {
                            strategy: {
                                "reveal_strength": 0.5,
                                "reveal_curve": curve,
                            }
                            for strategy in (
                                "random",
                                "greedy",
                                "local_optimal",
                                "unlock",
                                "adversarial",
                            )
                        },
                        "median_reveal_strength": 0.5,
                        "worst_reveal_strength": 0.5,
                        "reveal_strength_spread": 0.0,
                    }
                ],
            }

        first = evaluate_static_experience(
            level, chosen, types, reveals=reveals(1)
        )
        second = evaluate_static_experience(
            level, chosen, types, reveals=reveals(6)
        )

        self.assertNotEqual(
            first["experience_signature"], second["experience_signature"]
        )
        signature = first["experience_signature_evidence"]["reveal_buckets"]
        self.assertIn(
            "strategy_curve_and_release",
            signature["full_curve_and_release"][0],
        )

    def test_three_archetypes_are_softly_distinguished(self) -> None:
        intrinsic_level, intrinsic_chosen, intrinsic_types = _intrinsic_fixture()
        reveal_level, reveal_chosen, reveal_types = _board_reveal_fixture()
        hybrid_level, hybrid_chosen, hybrid_types = _hybrid_fixture()

        intrinsic = evaluate_static_experience(
            intrinsic_level, intrinsic_chosen, intrinsic_types
        )
        reveal = evaluate_static_experience(
            reveal_level, reveal_chosen, reveal_types
        )
        hybrid = evaluate_static_experience(
            hybrid_level, hybrid_chosen, hybrid_types
        )

        self.assertEqual(intrinsic["archetype"]["primary"], "intrinsic_gate")
        self.assertEqual(reveal["archetype"]["primary"], "board_reveal")
        self.assertEqual(hybrid["archetype"]["primary"], "hybrid")
        explicitly_requested = evaluate_static_experience(
            intrinsic_level,
            intrinsic_chosen,
            intrinsic_types,
            requested_archetype="board_reveal",
        )
        self.assertEqual(
            explicitly_requested["archetype"]["primary"], "intrinsic_gate"
        )
        self.assertEqual(
            explicitly_requested["archetype"]["automatic_primary"],
            "intrinsic_gate",
        )
        self.assertFalse(
            explicitly_requested["archetype"]["requested_matches_automatic"]
        )
        self.assertEqual(
            explicitly_requested["archetype"]["requested_match_score"],
            explicitly_requested["archetype"]["scores"]["board_reveal"],
        )
        self.assertTrue(
            explicitly_requested["archetype"]["explicitly_requested"]
        )

    def test_dynamic_reveal_evidence_can_change_automatic_archetype(self) -> None:
        level, chosen, types = _intrinsic_fixture()
        distribution = lambda value: {"median": value}  # noqa: E731
        reveal_curve = {
            "curve": [
                {
                    "cumulative_exposed_structure_count": distribution(0),
                },
                {
                    "cumulative_exposed_structure_count": distribution(9),
                },
            ],
            "first_three_structure_matches": {
                "1": {"progress": distribution(0.9)}
            },
            "released_ab_total_per_path": distribution(9),
            "dangling_one_area": distribution(5),
            "dangling_two_area": distribution(2),
        }
        reveals, difficulty = _experience_dynamic_inputs(
            SimpleNamespace(units=(), replacements=tuple(range(9))),
            {
                "duplicates": [
                    {
                        "strategies": {
                            strategy: {
                                "pass_probability": 1.0,
                                "trials": 1000 if strategy == "random" else 1,
                                "reveal_curve": reveal_curve,
                            }
                            for strategy in (
                                "random",
                                "greedy",
                                "local_optimal",
                                "unlock",
                                "adversarial",
                            )
                        }
                    }
                ]
            },
        )

        without_dynamic = evaluate_static_experience(level, chosen, types)
        with_dynamic = evaluate_static_experience(
            level,
            chosen,
            types,
            reveals=reveals,
            difficulty=difficulty,
        )

        self.assertEqual(
            without_dynamic["archetype"]["automatic_primary"],
            "intrinsic_gate",
        )
        self.assertNotEqual(
            with_dynamic["archetype"]["automatic_primary"],
            without_dynamic["archetype"]["automatic_primary"],
        )
        self.assertGreater(
            with_dynamic["archetype"]["dynamic_reveal_strength"], 0.8
        )

    def test_pressure_emits_machine_signals_and_source_baseline(self) -> None:
        level, chosen, types = _board_reveal_fixture()

        result = evaluate_static_experience(
            level,
            chosen,
            types,
            reveals={
                "reveal_strength": 0.7,
                "median_reveal_strength": 0.7,
                "worst_reveal_strength": 0.9,
                "reveal_path_spread": 0.12,
            },
            difficulty={
                "worst_failure_rate": 0.1,
                "path_spread": 0.2,
                "per_duplicate_path_spread": [
                    {"duplicate_index": 1, "spread": 0.2}
                ],
                "median_path_spread": 0.2,
                "archetype_baseline": "source_template",
            },
        )

        pressure = result["simulation_pressure"]
        self.assertEqual(
            pressure["signal_version"], "mechanism-pressure-signals-v1"
        )
        self.assertIn(
            "sampled_strategy_failures_observed", pressure["signals"]
        )
        self.assertIn(
            "strategy_path_dispersion_observed", pressure["signals"]
        )
        self.assertIn(
            "strategy_reveal_dispersion_observed", pressure["signals"]
        )
        self.assertEqual(
            result["archetype"]["baseline"], "source_template"
        )


class SpecialPresetTests(unittest.TestCase):
    def _all_types_fixture(self):
        coords = []
        labels = []
        for type_offset in range(12):
            label = chr(ord("C") + type_offset)
            for copy in range(3):
                index = len(coords)
                coords.append((index * 3, 0, (type_offset + copy) % 5, -10))
                labels.append(label)
        return _level(coords), tuple(range(36)), tuple(labels)

    def test_all_types_long_range_requires_dynamic_wave_proof(self) -> None:
        level, chosen, labels = self._all_types_fixture()

        uncertain = evaluate_static_experience(level, chosen, labels)
        self.assertEqual(
            uncertain["special_presets"]["all_types_long_range"]["status"],
            "inconclusive",
        )

        wave_only = evaluate_static_experience(
            level,
            chosen,
            labels,
            reveals={
                "all_types_long_range": {
                    "grammar_version": SPECIAL_GRAMMAR_VERSION,
                    "solver_status": "proved",
                    "wave_count": 4,
                    "dominance_chain": True,
                    "shortcut_count": 0,
                    "capacity_safe_exits": True,
                    "wave_assignment": {"1": {}, "2": {}, "3": {}, "4": {}},
                    "wave_dependency_dag": [
                        {"from_wave": 1, "to_wave": 2},
                        {"from_wave": 2, "to_wave": 3},
                        {"from_wave": 3, "to_wave": 4},
                    ],
                    "entry_event": {"id": "wave_first_triplet_completed"},
                    "completion_event": {
                        "id": "wave_all_structures_completed"
                    },
                }
            },
        )
        self.assertEqual(
            wave_only["special_presets"]["all_types_long_range"]["status"],
            "inconclusive",
        )
        self.assertEqual(
            wave_only["special_presets"]["all_types_long_range"]["reasons"],
            ["suspended_exposure_bound_not_proved"],
        )

        positive = evaluate_static_experience(
            level,
            chosen,
            labels,
            reveals={
                "all_types_long_range": {
                    **dict(
                        wave_only["special_presets"]["all_types_long_range"]
                    ),
                    "grammar_version": SPECIAL_GRAMMAR_VERSION,
                    "solver_status": "proved",
                    "suspended_exposure_bound": {
                        "status": "proved",
                        "metric_version": SUSPENDED_EXPOSURE_BOUND_VERSION,
                        "maximum_progress_span": 8,
                        "limit_progress_span": 10,
                        "proof_scope": "all_whole_board_topological_paths",
                        "limit_id": "long-range-v1-max-10",
                    },
                }
            },
        )
        self.assertEqual(
            positive["special_presets"]["all_types_long_range"]["status"],
            "matched",
        )

        malformed_claim = evaluate_static_experience(
            level,
            chosen,
            labels,
            reveals={
                "all_types_long_range": {
                    **dict(
                        wave_only["special_presets"]["all_types_long_range"]
                    ),
                    "grammar_version": SPECIAL_GRAMMAR_VERSION,
                    "solver_status": "proved",
                    "suspended_exposure_bound": {
                        "status": "proved",
                        "maximum_progress_span": 8,
                        "limit_progress_span": 10,
                    },
                }
            },
        )
        self.assertEqual(
            malformed_claim["special_presets"]["all_types_long_range"][
                "status"
            ],
            "inconclusive",
        )

        violated = evaluate_static_experience(
            level,
            chosen,
            labels,
            reveals={
                "all_types_long_range": {
                    **dict(
                        wave_only["special_presets"]["all_types_long_range"]
                    ),
                    "grammar_version": SPECIAL_GRAMMAR_VERSION,
                    "solver_status": "proved",
                    "suspended_exposure_bound": {
                        "status": "proved",
                        "metric_version": SUSPENDED_EXPOSURE_BOUND_VERSION,
                        "maximum_progress_span": 11,
                        "limit_progress_span": 10,
                        "proof_scope": "all_whole_board_topological_paths",
                        "limit_id": "long-range-v1-max-10",
                    },
                }
            },
        )
        self.assertEqual(
            violated["special_presets"]["all_types_long_range"]["status"],
            "rejected",
        )

        negative = evaluate_static_experience(
            level,
            chosen,
            labels,
            reveals={
                "all_types_long_range": {
                    "grammar_version": SPECIAL_GRAMMAR_VERSION,
                    "solver_status": "proved",
                    "wave_count": 5,
                    "dominance_chain": True,
                    "shortcut_count": 0,
                    "capacity_safe_exits": True,
                    "wave_assignment": {},
                    "wave_dependency_dag": [],
                    "entry_event": {"id": "wave_first_triplet_completed"},
                    "completion_event": {
                        "id": "wave_all_structures_completed"
                    },
                }
            },
        )
        self.assertEqual(
            negative["special_presets"]["all_types_long_range"]["status"],
            "rejected",
        )

    def test_serial_mechanism_is_conservative_about_shortcuts(self) -> None:
        level, chosen, labels = _board_reveal_fixture()

        uncertain = evaluate_static_experience(level, chosen, labels)
        self.assertEqual(
            uncertain["special_presets"]["serial_mechanism"]["status"],
            "inconclusive",
        )
        positive_evidence = {
            "serial_mechanism": {
                "grammar_version": SPECIAL_GRAMMAR_VERSION,
                "solver_status": "proved",
                "cluster_order": ["first", "second", "third"],
                "dominance_chain": True,
                "shortcut_count": 0,
                "capacity_safe_segments": True,
                "reverse_dependency": False,
                "cluster_assignment": {
                    "first": [0, 1, 2],
                    "second": [3, 4, 5],
                    "third": [6, 7, 8],
                },
                "cluster_dependency_dag": [
                    {"from_cluster": "first", "to_cluster": "second"},
                    {"from_cluster": "second", "to_cluster": "third"},
                ],
                "entry_event": {"id": "cluster_first_triplet_completed"},
                "completion_event": {
                    "id": "cluster_all_structures_completed"
                },
            }
        }
        positive = evaluate_static_experience(
            level, chosen, labels, reveals=positive_evidence
        )
        self.assertEqual(
            positive["special_presets"]["serial_mechanism"]["status"],
            "matched",
        )
        positive_evidence["serial_mechanism"]["shortcut_count"] = 1
        negative = evaluate_static_experience(
            level, chosen, labels, reveals=positive_evidence
        )
        self.assertEqual(
            negative["special_presets"]["serial_mechanism"]["status"],
            "rejected",
        )

    def test_nonmatching_special_preset_can_be_explicitly_rejected(self) -> None:
        level, chosen, labels = _intrinsic_fixture()

        result = evaluate_static_experience(
            level,
            chosen,
            labels,
            requested_special_preset="all_types_long_range",
        )
        self.assertEqual(
            result["special_presets"]["all_types_long_range"]["status"],
            "rejected",
        )


if __name__ == "__main__":
    unittest.main()
