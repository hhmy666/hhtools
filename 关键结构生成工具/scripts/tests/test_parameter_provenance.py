from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puzzle_core.config_io import parse_batch_request, resolve_batch_config, _parameter_snapshot_hash
from services.job_service import GenerationItemResult
from services.job_service import generate_batch
from services.profile_service import Profile
from services.profile_service import ProfileService
from app import AppState, JobRecord
from tests.test_ui_service import _chain_source, _generated


def configuration(*, local=False, changed=False, full=False, different_plan_id=False):
    plan = {"plan_id": "main", "units": [{"step": 4}]}
    request = {
        "schema_version": 1,
        "template_keys": ["ui001"],
        "batch_seed": 7,
        "generation_defaults": {
            "unit_defaults": {
                "type_count": {"kind": "range", "minimum": 3, "maximum": 5},
                "uniform_groups_per_type": 0,
            },
        },
        "batch_plans": [plan],
    }
    if local:
        local_plan = copy.deepcopy(plan)
        local_plan["units"][0]["unit_id"] = "U1"
        local_plan["layout"] = "single"
        if different_plan_id:
            local_plan["plan_id"] = "template-main"
        if changed:
            local_plan["generation"] = {"total_percent_limit": 40}
        request["template_rules"] = [
            {"template_key": "ui001", "mode": "replace", "plans": [local_plan]}
        ]
    config = resolve_batch_config(parse_batch_request(request))
    return config if full else config.work_items[0]


class ParameterProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _chain_source()
        cls.generated = _generated(cls.source)

    def payloads(self, works, *, outcome_hashes=None):
        candidates = tuple(
            replace(
                self.generated,
                candidate=replace(
                    self.generated.candidate,
                    generation_config_hash=(outcome_hashes or ["same"] * len(works))[index],
                ),
                parameter_source=work.parameter_source,
                parameter_config_hash=work.parameter_config_hash,
                uses_independent_parameters=work.uses_independent_parameters,
                configured_parameters=work.configured_parameters,
                uniform_parameters=work.uniform_parameters,
            )
            for index, work in enumerate(works)
        )
        row = GenerationItemResult(
            template_key="ui001", plan_id="main", plan_signature="s4",
            parameter_source=works[-1].parameter_source, generation_seed=7,
            source=self.source, inspection=None, candidates=candidates,
            attempts_completed=1, rejection_counts={}, target_candidate_count=2,
            status="success",
        )
        return row.as_dict()["candidates"]

    def test_identical_template_override_matches_batch_configuration(self):
        batch = configuration()
        local = configuration(local=True)
        self.assertFalse(getattr(local, "uses_independent_parameters", True))
        self.assertTrue(getattr(batch, "parameter_config_hash", ""))
        self.assertEqual(batch.parameter_config_hash, local.parameter_config_hash)

    def test_legacy_same_type_span_zero_retains_parameter_identity_on_continuation(self):
        current = configuration()
        historical_snapshot = copy.deepcopy(current.configured_parameters)
        historical_snapshot["generation"].pop("min_same_type_layer_span")
        historical_hash = _parameter_snapshot_hash(historical_snapshot)
        historical = replace(current, parameter_config_hash=historical_hash, configured_parameters=historical_snapshot)

        self.assertEqual(current.parameter_config_hash, historical_hash)
        self.assertEqual([payload["parameter_provenance"]["non_uniform"] for payload in self.payloads([historical, current])], [False, False])
        self.assertEqual(current.configured_parameters["generation"]["min_same_type_layer_span"], 0)

        changed_snapshot = copy.deepcopy(current.configured_parameters)
        changed_snapshot["generation"]["min_same_type_layer_span"] = 2
        self.assertNotEqual(_parameter_snapshot_hash(changed_snapshot), historical_hash)

    def test_zero_percent_and_unlimited_share_identity_without_losing_ui_mode(self):
        raw = {
            "schema_version": 1, "template_keys": ["ui001"], "batch_seed": 7,
            "batch_plans": [{"plan_id": "main", "units": [{"step": 4}]}],
        }
        batch = resolve_batch_config(parse_batch_request(raw)).work_items[0]
        for generation in ({"total_count_minimum": 0}, {"total_percent_minimum": 0}):
            raw["template_rules"] = [{"template_key": "ui001", "mode": "replace", "plans": [
                {"plan_id": "main", "units": [{"step": 4}], "generation": generation},
            ]}]
            local = resolve_batch_config(parse_batch_request(raw)).work_items[0]
            self.assertEqual(local.parameter_config_hash, batch.parameter_config_hash)
            self.assertFalse(local.uses_independent_parameters)
            self.assertEqual([p["parameter_provenance"]["non_uniform"] for p in self.payloads([batch, local])], [False, False])
            if "total_percent_minimum" in generation:
                self.assertEqual(local.configured_parameters["generation"]["total_percent_minimum"], 0)

    def test_unchanged_continuation_does_not_mark_local_source(self):
        payloads = self.payloads([configuration(), configuration(local=True)])
        self.assertEqual([p["parameter_provenance"]["non_uniform"] for p in payloads], [False, False])

    def test_random_resolved_outcomes_do_not_mark_one_configuration(self):
        work = configuration()
        payloads = self.payloads([work, work], outcome_hashes=["types-3-groups-1-2-1", "types-4-groups-1-1-1-1"])
        self.assertEqual([p["parameter_provenance"]["non_uniform"] for p in payloads], [False, False])

    def test_actual_parameter_change_marks_retained_and_new_candidates(self):
        payloads = self.payloads([configuration(), configuration(local=True, changed=True)])
        self.assertEqual([p["parameter_provenance"]["non_uniform"] for p in payloads], [True, True])

    def test_info_keeps_declared_ranges_and_uniform_comparison_snapshot(self):
        work = configuration(local=True, changed=True)
        self.assertEqual((getattr(work, "configured_parameters", None) or {}).get("generation", {}).get("total_percent_limit"), 40)
        payload = self.payloads([work])[0]
        current = payload.get("configured_parameters") or {}
        uniform = payload.get("uniform_parameters") or {}
        self.assertEqual(current.get("generation", {}).get("total_percent_limit"), 40)
        self.assertIsNone(uniform.get("generation", {}).get("total_percent_limit"))
        self.assertEqual(current.get("unit_settings", [{}])[0].get("type_count"),
                         {"kind": "range", "value": None, "minimum": 3, "maximum": 5})
        self.assertEqual(current["unit_settings"][0]["uniform_groups_per_type"], 0)

    def test_unique_uniform_reference_survives_changed_template_tower_scope(self):
        config = resolve_batch_config(parse_batch_request({
            "schema_version": 1, "template_keys": ["ui001"],
            "batch_plans": [{"plan_id": "batch", "units": [{"step": 4}]}],
            "template_rules": [{"template_key": "ui001", "mode": "replace", "plans": [
                {"plan_id": "local", "tower_mode": "allow", "units": [{"step": 5}]},
            ]}],
        }))
        work = config.work_items[0]
        self.assertEqual((work.uniform_parameters or {}).get("tower_mode"), "disallow")
        self.assertEqual(work.configured_parameters["tower_mode"], "allow")

    def test_initial_template_parameters_different_from_batch_are_marked(self):
        payloads = self.payloads([configuration(local=True, changed=True)])
        self.assertTrue(payloads[0]["parameter_provenance"]["non_uniform"])
        self.assertEqual(payloads[0]["parameter_provenance"]["reason"], "template_specific")

    def test_local_plan_name_does_not_change_parameter_values(self):
        work = configuration(local=True, different_plan_id=True)
        self.assertFalse(work.uses_independent_parameters)

    def test_real_generation_serializes_configured_identity(self):
        request = {
            "schema_version": 1, "template_keys": ["ui001"], "batch_seed": 7,
            "generation_defaults": {
                "candidate_count": 1, "attempt_budget": 30,
                "assignment_attempts": 500, "replacement_sources": ["A", "B"],
            },
            "batch_plans": [{"plan_id": "main", "units": [{"step": 4}]}],
        }
        config = resolve_batch_config(parse_batch_request(request))
        result = generate_batch(
            config, Profile("qa", "qa", None, None, "/private/tmp"),
            loaded_inputs={"ui001": self.source},
        )
        payload = result.as_dict()["items"][0]["candidates"][0]
        self.assertTrue(payload["parameter_config_hash"])
        self.assertFalse(payload["parameter_provenance"]["non_uniform"])

    def test_real_unchanged_continuation_keeps_candidate_without_marking(self):
        request = {
            "schema_version": 1, "template_keys": ["ui001"], "batch_seed": 7,
            "generation_defaults": {
                "candidate_count": 2, "attempt_budget": 30,
                "assignment_attempts": 500, "replacement_sources": ["A", "B"],
            },
            "batch_plans": [{"plan_id": "main", "units": [{"step": 4}]}],
        }
        config = resolve_batch_config(parse_batch_request(request))
        profile = Profile("qa", "qa", None, None, "/private/tmp")
        parent_result = generate_batch(config, profile, loaded_inputs={"ui001": self.source})
        self.assertEqual(len(parent_result.items[0].candidates), 2)
        retained = parent_result.items[0].candidates[0]
        parent_result = replace(parent_result, items=(replace(
            parent_result.items[0], candidates=(retained,), status="cancelled",
            slot_attempts_completed={1: parent_result.items[0].slot_attempts_completed.get(1, 0)},
        ),), run_status="cancelled")
        child_request = copy.deepcopy(request)
        child_request["template_rules"] = [{
            "template_key": "ui001", "mode": "replace",
            "plans": copy.deepcopy(request["batch_plans"]),
        }]
        child_config = resolve_batch_config(parse_batch_request(child_request))
        with tempfile.TemporaryDirectory() as temporary:
            service = ProfileService(Path(temporary) / "settings.json")
            service.initialize(Path(temporary) / "tool")
            state = AppState(service)
            parent = JobRecord(
                job_id="parent", created_at="2026-10-13T12:00:00+08:00",
                import_id="qa", config_payload=request, config=config,
                sources={"ui001": self.source}, result=parent_result,
            )
            state.jobs[parent.job_id] = parent
            child = JobRecord(
                job_id="child", created_at="2026-10-13T12:01:00+08:00",
                import_id="qa", config_payload=child_request, config=child_config,
                sources={"ui001": self.source}, parent_job_id=parent.job_id,
                retry_scope={"template_key": "ui001", "plan_id": "main"},
            )
            carried = state._compatible_parent_result(child, parent_result)
            work = child_config.work_items[0]
            result = generate_batch(
                child_config, profile, loaded_inputs={"ui001": self.source},
                previous_result=carried,
                retry_work_item_keys=((work.template_key, work.plan.plan_id, work.plan.signature),),
            )
            presented = state._present_result(child, result)
        self.assertEqual(len(result.items[0].candidates), 2)
        self.assertEqual(result.items[0].candidates[0].candidate.candidate_id, retained.candidate.candidate_id)
        self.assertEqual(
            [p["parameter_provenance"]["non_uniform"] for p in presented["items"][0]["candidates"]],
            [False, False],
        )
