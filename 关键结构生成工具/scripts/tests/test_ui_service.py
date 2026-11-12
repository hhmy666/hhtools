from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.geometry import build_geometry  # noqa: E402
from puzzle_core.models import RawOutputPlan, RawPuzzleUnit, StepSpec, TowerMode  # noqa: E402
from puzzle_core.planning import normalize_output_plan  # noqa: E402
from puzzle_core.protobuf_wire import encode_varint  # noqa: E402
from puzzle_core.search import (  # noqa: E402
    GenerationOptions,
    UnitGenerationOptions,
    search_candidates,
)
from puzzle_core.template_reader import parse_level_bytes  # noqa: E402
from puzzle_core.validator import validate_candidate  # noqa: E402
from services.import_service import LoadedTemplate  # noqa: E402
from services.job_service import GeneratedCandidate  # noqa: E402
from services.ui_service import (  # noqa: E402
    build_candidate_board,
    build_candidate_comparison,
    build_solver_traces,
    build_source_board,
)


def _varint_field(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def _bytes_field(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def _chain_source(tile_count: int = 12) -> LoadedTemplate:
    payload = [
        _bytes_field(2, b"ui001"),
        _varint_field(4, tile_count + 3),
        _varint_field(5, 4),
    ]
    for index in range(tile_count):
        tile = b"".join(
            (
                _varint_field(1, index),
                _varint_field(2, 1),
                _varint_field(3, index),
                _varint_field(4, -10 if index % 2 == 0 else -11),
            )
        )
        payload.append(_bytes_field(6, tile))
    level = parse_level_bytes(
        b"".join(payload), source_name="ui001_m.bytes"
    )
    return LoadedTemplate(
        level=level,
        input_mode="file",
        source_locator="ui001_m.bytes",
        source_filename="ui001_m.bytes",
    )


def _generated(source: LoadedTemplate, seed: int = 2026) -> GeneratedCandidate:
    plan = normalize_output_plan(
        RawOutputPlan(
            plan_id="s4",
            units=(RawPuzzleUnit(StepSpec.exact(4), "U1"),),
            tower_mode=TowerMode.DISALLOW,
        ),
        batch_seed=7,
        template_key=source.level.template_id,
    )[0]
    options = GenerationOptions(
        unit_options=(UnitGenerationOptions(type_count=3),),
        replacement_sources=("A", "B"),
        attempt_budget=30,
        assignment_attempts=500,
    )
    candidate = search_candidates(
        source.level, plan, options, candidate_seed=seed
    ).candidates[0]
    # UI serialization only needs structurally valid evidence; formal-export
    # experience proof is covered with exact DupPara1-10 integration fixtures.
    validation = validate_candidate(
        source.level,
        plan,
        options,
        candidate,
        require_exportable_experience=False,
    )
    return GeneratedCandidate(
        candidate=candidate,
        validation=validation,
        generation_options=options,
    )


class UiServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = _chain_source()
        cls.generated = _generated(cls.source)

    def test_source_board_is_complete_versioned_and_json_ready(self) -> None:
        board = build_source_board(self.source)
        complete_graph = build_geometry(self.source.level)

        self.assertEqual(board["schema_name"], "tile-board")
        self.assertEqual(board["schema_version"], 1)
        self.assertEqual(board["kind"], "source")
        self.assertEqual(len(board["tiles"]), len(self.source.level.tiles))
        self.assertEqual(board["bounds"]["footprint"]["min_x"], 0)
        self.assertEqual(board["bounds"]["footprint"]["max_x_exclusive"], 13)
        self.assertEqual(board["bounds"]["layer"], {"min": 1, "max": 12})
        self.assertTrue(board["static_relation_scope"]["tower_members_excluded"])
        self.assertGreater(len(board["static_relations"]), 0)
        self.assertTrue(all(not tile["is_replacement"] for tile in board["tiles"]))
        self.assertEqual(
            [tile["is_initially_visible"] for tile in board["tiles"]],
            [
                not complete_graph.direct_blockers[tile.index]
                for tile in self.source.level.tiles
            ],
        )
        json.dumps(board, ensure_ascii=False)

    def test_candidate_board_preserves_all_tiles_and_marks_effective_units(self) -> None:
        board = build_candidate_board(self.source, self.generated)
        replacements = {
            item.tile_index: item for item in self.generated.candidate.replacements
        }

        self.assertEqual(board["kind"], "candidate")
        self.assertEqual(len(board["tiles"]), len(self.source.level.tiles))
        self.assertEqual(
            sum(tile["is_replacement"] for tile in board["tiles"]),
            len(replacements),
        )
        for tile in board["tiles"]:
            replacement = replacements.get(tile["tile_index"])
            if replacement is None:
                self.assertEqual(tile["effective_tile_id"], tile["source_tile_id"])
                self.assertIsNone(tile["replacement"])
            else:
                self.assertEqual(tile["effective_type"], replacement.structure_type)
                self.assertEqual(tile["replacement"]["group_id"], "U1")
                self.assertEqual(tile["replacement"]["unit_id"], "U1")
        self.assertTrue(board["validation_summary"]["passed"])
        self.assertIn("solver_traces", board)
        json.dumps(board, ensure_ascii=False)

    def test_solver_trace_maps_local_proof_order_to_global_tiles_and_bags(self) -> None:
        payload = build_solver_traces(self.source, self.generated)
        unit = self.generated.candidate.units[0]
        trace = payload["units"][0]["threshold_trace"]

        self.assertTrue(payload["available"])
        self.assertEqual(
            payload["scope"]["code"],
            "structure_intrinsic_external_ab_cleared",
        )
        self.assertIn("A/B Tile 已被清除", payload["scope"]["assumption"])
        self.assertIsNone(trace["capacity"])
        self.assertEqual(trace["proof_source"], "PuzzleProof.recommended_order")
        self.assertTrue(trace["complete"])
        self.assertEqual(
            trace["order_global"],
            [unit.selected_indices[index] for index in unit.proof.recommended_order],
        )
        self.assertEqual(len(trace["steps"]), len(unit.selected_indices))
        first = trace["steps"][0]
        source_tile = self.source.level.tiles[first["tile_index"]]
        self.assertEqual(first["local_tile_index"], unit.proof.recommended_order[0])
        self.assertEqual(
            first["coordinate"],
            {
                "x": source_tile.x,
                "y": source_tile.y,
                "z": source_tile.z,
                "layer": source_tile.layer,
                "tower_index": source_tile.tower_index,
            },
        )
        self.assertIn("bag_before", first)
        self.assertIn("bag_after_insert", first)
        self.assertIn("bag_after_settlement", first)

        capacities = {
            item["capacity"]: item for item in payload["units"][0]["capacity_traces"]
        }
        self.assertEqual(set(capacities), {7, 8})
        for capacity in (7, 8):
            self.assertTrue(capacities[capacity]["proof_available"])
            capacity_trace = capacities[capacity]["trace"]
            self.assertEqual(capacity_trace["capacity"], capacity)
            self.assertEqual(
                capacity_trace["proof_source"],
                "CapacityProof.recommended_order",
            )
            self.assertTrue(capacity_trace["available"])
            self.assertTrue(capacity_trace["complete"])

    def test_capacity_without_safe_order_is_explicitly_unavailable(self) -> None:
        unit = self.generated.candidate.units[0]
        proof_7 = next(
            proof for proof in unit.proof.capacity_proofs if proof.capacity == 7
        )
        no_order = replace(
            proof_7,
            complete_exists=False,
            classification="all_fail",
            recommended_order=None,
        )
        altered_proof = replace(
            unit.proof,
            capacity_proofs=(
                no_order,
                *(
                    proof
                    for proof in unit.proof.capacity_proofs
                    if proof.capacity != 7
                ),
            ),
        )
        altered_unit = replace(unit, proof=altered_proof)
        altered_candidate = replace(
            self.generated.candidate,
            units=(altered_unit,),
        )
        altered = replace(self.generated, candidate=altered_candidate)

        payload = build_solver_traces(self.source, altered)
        by_capacity = {
            item["capacity"]: item for item in payload["units"][0]["capacity_traces"]
        }
        trace = by_capacity[7]["trace"]
        self.assertFalse(trace["available"])
        self.assertEqual(trace["classification"], "all_fail")
        self.assertEqual(trace["unavailable_reason"], "no_safe_complete_order")
        self.assertEqual(trace["steps"], [])

    def test_comparison_contains_scan_summary_and_pairwise_delta(self) -> None:
        second_candidate = replace(
            self.generated.candidate,
            candidate_id=self.generated.candidate.candidate_id + "_variant",
            quality_score=self.generated.candidate.quality_score + 0.25,
        )
        second = replace(self.generated, candidate=second_candidate)

        comparison = build_candidate_comparison(
            self.source, (self.generated, second)
        )

        self.assertEqual(comparison["schema_version"], 1)
        self.assertEqual(comparison["candidate_count"], 2)
        self.assertEqual(len(comparison["candidates"]), 2)
        self.assertEqual(len(comparison["pairwise"]), 1)
        difference = comparison["pairwise"][0]
        self.assertTrue(difference["same_replacement_positions"])
        self.assertTrue(difference["identical_layout"])
        self.assertAlmostEqual(difference["quality_score_delta"], 0.25)
        self.assertIn("替换位置相同", difference["brief"])
        json.dumps(comparison, ensure_ascii=False)

    def test_candidate_must_match_source_snapshot(self) -> None:
        mismatched = replace(
            self.generated.candidate,
            source_sha256="0" * 64,
        )
        with self.assertRaisesRegex(ValueError, "source_sha256"):
            build_candidate_board(
                self.source,
                replace(self.generated, candidate=mismatched),
            )


if __name__ == "__main__":
    unittest.main()
