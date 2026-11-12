from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.combination import CombinationUnitInput, analyze_combination
from puzzle_core.errors import InputValidationError
from puzzle_core.models import Layout, UnitRole
from puzzle_core.solver import SolverLimits, analyze_puzzle


def chain_masks(length: int) -> tuple[int, ...]:
    return tuple(0 if index == 0 else 1 << (index - 1) for index in range(length))


def unit(
    unit_id: str,
    role: UnitRole,
    target: int,
    sequence: tuple[str, ...],
    gate: str,
    recycles: int = 1,
) -> CombinationUnitInput:
    return CombinationUnitInput(
        unit_id=unit_id,
        role=role,
        target_step=target,
        blocker_masks=chain_masks(len(sequence)),
        tile_types=sequence,
        threshold_type=gate,
        required_pre_gate_recycles=recycles,
        first_recycle_lead_tiles=2,
    )


class CombinationProofTests(unittest.TestCase):
    def test_independently_recomputed_unit_proof_is_reused_and_bound_to_inputs(self):
        source = CombinationUnitInput('one', UnitRole.SINGLE, 3, (0,) * 6, tuple('CCCDDD'), 'C')
        expected = analyze_combination((source,), layout=Layout.SINGLE)
        proof = analyze_puzzle(source.blocker_masks, source.tile_types, threshold_type='C')
        with patch('puzzle_core.combination.analyze_puzzle', side_effect=AssertionError('duplicate proof')):
            actual = analyze_combination((source,), layout=Layout.SINGLE, _unit_proofs=(proof,))
        self.assertEqual(actual, expected)
        for changed in (replace(proof, threshold_type='D'), replace(proof, blocker_masks=(0,)*5+(1,)),
                        replace(proof, tile_types=tuple('DDDCCC')), replace(proof, required_pre_gate_recycles=1)):
            with self.assertRaises(InputValidationError):
                analyze_combination((source,), layout=Layout.SINGLE, _unit_proofs=(changed,))

    def test_large_single_unit_reuses_exact_capacity_proof(self):
        source = CombinationUnitInput('one', UnitRole.SINGLE, 3, (0,) * 27, tuple('CDE' * 9), 'C')
        proof = analyze_combination((source,), layout=Layout.SINGLE,
                                    limits=SolverLimits(max_states=10000, max_path_count=17))
        self.assertTrue(proof.all_units_qualified)
        for ordered, isolated in zip(proof.ordered_capacity_proofs, proof.unit_proofs[0].capacity_proofs):
            self.assertEqual(ordered.complete_path_count, isolated.complete_path_count)
            self.assertEqual(ordered.classification, 'all_safe')
            self.assertEqual(tuple(i for _, i in ordered.recommended_order), isolated.recommended_order)

    def test_parallel_proves_both_whole_unit_orders_and_reports_interleaving(self) -> None:
        left = unit(
            "left",
            UnitRole.LEFT,
            4,
            ("C", "C", "E", "E", "E", "D", "C", "D", "D"),
            "C",
        )
        right = unit(
            "right",
            UnitRole.RIGHT,
            5,
            ("F", "F", "H", "H", "H", "I", "G", "F", "I", "I", "G", "G"),
            "F",
        )

        proof = analyze_combination((left, right), layout=Layout.X)

        self.assertTrue(proof.all_units_qualified)
        self.assertEqual(proof.signature, "s4s5")
        self.assertEqual(proof.naming_order, ("left", "right"))
        self.assertEqual(proof.interleaving_coverage, "exhaustive")
        self.assertEqual(
            proof.interleaving_result_at_capacity_7,
            "safe_and_failure_paths",
        )
        self.assertTrue(proof.safe_interleaving_exists_at_capacity_7)
        self.assertIsNotNone(proof.shortest_interleaving_failure_at_capacity_7)
        self.assertEqual(
            {item.capacity for item in proof.interleaving_capacity_proofs},
            {7, 8},
        )
        self.assertEqual(proof.singleton_rule_scope, "per_unit_only")
        self.assertEqual(
            {(item.unit_order, item.capacity) for item in proof.ordered_capacity_proofs},
            {
                (("left", "right"), 7),
                (("left", "right"), 8),
                (("right", "left"), 7),
                (("right", "left"), 8),
            },
        )
        self.assertTrue(
            all(item.classification == "all_safe" for item in proof.ordered_capacity_proofs)
        )

    def test_serial_uses_only_high_to_low_and_preserves_capacity_pressure(self) -> None:
        high = unit(
            "high",
            UnitRole.HIGH,
            8,
            (
                "C",
                "C",
                "E",
                "E",
                "E",
                "F",
                "F",
                "G",
                "G",
                "D",
                "C",
                "F",
                "G",
                "D",
                "D",
            ),
            "C",
        )
        low = unit(
            "low",
            UnitRole.LOW,
            4,
            ("H", "H", "J", "J", "J", "I", "H", "I", "I"),
            "H",
        )

        proof = analyze_combination((high, low), layout="z")

        self.assertTrue(proof.all_units_qualified)
        self.assertEqual(proof.signature, "s8s4")
        self.assertEqual(len(proof.ordered_capacity_proofs), 2)
        by_capacity = {item.capacity: item for item in proof.ordered_capacity_proofs}
        self.assertEqual(by_capacity[7].unit_order, ("high", "low"))
        self.assertEqual(by_capacity[7].classification, "all_fail")
        self.assertFalse(by_capacity[7].complete_exists)
        self.assertEqual(by_capacity[8].classification, "all_safe")
        self.assertTrue(by_capacity[8].complete_exists)
        self.assertEqual(proof.interleaving_coverage, "not_applicable")
        self.assertEqual(proof.interleaving_result_at_capacity_7, "not_applicable")
        self.assertEqual(proof.interleaving_capacity_proofs, ())

    def test_parallel_interleaving_budget_is_reported_without_false_proof(self) -> None:
        left = unit(
            "left",
            UnitRole.LEFT,
            4,
            ("C", "C", "E", "E", "E", "D", "C", "D", "D"),
            "C",
        )
        right = unit(
            "right",
            UnitRole.RIGHT,
            4,
            ("F", "F", "H", "H", "H", "G", "F", "G", "G"),
            "F",
        )

        proof = analyze_combination(
            (left, right),
            layout=Layout.X,
            limits=SolverLimits(max_states=50, max_path_count=10**6),
        )

        self.assertEqual(proof.interleaving_coverage, "bounded")
        self.assertTrue(
            all(item.coverage == "bounded" for item in proof.interleaving_capacity_proofs)
        )
        self.assertNotEqual(proof.interleaving_result_at_capacity_7, "all_safe")

    def test_rejects_shared_structure_types(self) -> None:
        left = unit(
            "left",
            UnitRole.LEFT,
            4,
            ("C", "C", "E", "E", "E", "D", "C", "D", "D"),
            "C",
        )
        right = unit(
            "right",
            UnitRole.RIGHT,
            4,
            ("C", "C", "G", "G", "G", "F", "C", "F", "F"),
            "C",
        )
        with self.assertRaises(InputValidationError) as caught:
            analyze_combination((left, right), layout=Layout.X)
        self.assertEqual(caught.exception.detail.evidence["shared_types"], ["C"])

    def test_rejects_wrong_role_order(self) -> None:
        top = unit(
            "top",
            UnitRole.TOP,
            4,
            ("C", "C", "E", "E", "E", "D", "C", "D", "D"),
            "C",
        )
        bottom = unit(
            "bottom",
            UnitRole.BOTTOM,
            4,
            ("F", "F", "H", "H", "H", "G", "F", "G", "G"),
            "F",
        )
        with self.assertRaises(InputValidationError):
            analyze_combination((bottom, top), layout=Layout.Y)

    def test_as_dict_is_json_ready_and_keeps_gate_metrics(self) -> None:
        single = unit(
            "single",
            UnitRole.SINGLE,
            4,
            ("C", "C", "E", "E", "E", "D", "C", "D", "D"),
            "C",
        )
        proof = analyze_combination((single,), layout=Layout.SINGLE)
        payload = proof.as_dict()
        unit_payload = payload["unit_proofs"][0]
        self.assertEqual(unit_payload["gate_steps"], (4,))
        self.assertEqual(unit_payload["minimum_gate_step"], 4)


if __name__ == "__main__":
    unittest.main()
