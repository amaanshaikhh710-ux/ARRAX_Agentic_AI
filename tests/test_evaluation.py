from __future__ import annotations

import json

import pytest

from arrax import AgentCore, create_app
from arrax.evaluation import UnknownScenarioError, run_evaluation, scenario_catalog
from arrax.provider import GeminiProvider, ProviderConfig, ProviderMode


SCENARIO_NAMES = [entry["name"] for entry in scenario_catalog()]


@pytest.mark.parametrize("scenario_name", SCENARIO_NAMES)
def test_each_catalog_scenario_meets_its_declared_outcome(scenario_name):
    report = run_evaluation(scenario_name)

    assert report["evaluation_type"] == "deterministic_regression"
    assert report["mode"] == "offline_deterministic"
    assert len(report["scenario_results"]) == 1
    result = report["scenario_results"][0]
    assert result["name"] == scenario_name
    assert result["passed"] is True
    assert all(result["expected_checks"].values())
    assert result["elapsed_seconds"] >= 0
    assert isinstance(result["recovery_event_count"], int)
    assert isinstance(result["approval_behavior"], dict)


def test_all_scenarios_summary_counts_and_rates_match_measured_results():
    report = run_evaluation()
    results = report["scenario_results"]
    summary = report["summary"]

    assert [result["name"] for result in results] == SCENARIO_NAMES
    assert summary["scenario_count"] == len(results) == len(SCENARIO_NAMES)
    assert summary["passed_count"] == sum(result["passed"] for result in results)
    assert summary["failed_count"] == summary["scenario_count"] - summary["passed_count"]
    assert summary["completed_count"] == sum(result["final_status"] == "completed" for result in results)
    assert summary["recovered_count"] == sum(result["recovery_event_count"] > 0 for result in results)
    assert summary["verified_count"] == sum(bool(result["verification_evidence"]) for result in results)
    assert summary["pass_rate"] == pytest.approx(summary["passed_count"] / summary["scenario_count"])
    assert summary["completion_rate"] == pytest.approx(summary["completed_count"] / summary["scenario_count"])
    assert summary["recovery_rate"] == pytest.approx(summary["recovered_count"] / summary["scenario_count"])
    assert summary["verification_rate"] == pytest.approx(summary["verified_count"] / summary["scenario_count"])
    json.dumps(report, allow_nan=False)


def test_evaluation_recovery_approval_guard_and_timeout_are_measured():
    recovery = run_evaluation("self_healing_recovery")["scenario_results"][0]
    assert recovery["recovery_event_count"] == 1
    assert recovery["final_status"] == "completed"
    assert recovery["verification_evidence"]

    approval = run_evaluation("approval_gate")["scenario_results"][0]
    assert approval["approval_behavior"] == {
        "required": True,
        "paused": True,
        "approved": True,
        "resumed": True,
        "approval_event_count": 1,
        "approval_grant_count": 1,
    }

    guard = run_evaluation("verification_guard")["scenario_results"][0]
    assert guard["final_status"] == "failed"
    assert guard["error_category"] == "verification_failed"
    assert guard["verification_evidence"] == []
    assert guard["completion_event_count"] == 0

    timeout = run_evaluation("timeout_boundary")["scenario_results"][0]
    assert timeout["final_status"] == "failed"
    assert timeout["error_category"] == "timeout"
    assert timeout["completion_event_count"] == 0


def test_unknown_evaluation_scenario_is_rejected():
    with pytest.raises(UnknownScenarioError):
        run_evaluation("not-a-real-scenario")


class CountingTransport:
    def __init__(self):
        self.calls = 0

    def request(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("the evaluation lab must not call Gemini")


def test_evaluation_api_never_uses_configured_gemini_transport_and_leaks_no_paths_or_secrets():
    transport = CountingTransport()
    provider = GeminiProvider(
        ProviderConfig(mode=ProviderMode.AUTO, api_key="test-secret-that-must-not-escape"),
        transport=transport,
    )
    core = AgentCore(provider=provider)
    application = create_app(core=core, config={"TESTING": True})
    try:
        response = application.test_client().post("/api/evaluation/run", json={})
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert transport.calls == 0
        assert "test-secret-that-must-not-escape" not in body
        assert "C:\\Users\\" not in body
        assert "/Users/" not in body
        json.dumps(response.get_json(), allow_nan=False)
    finally:
        application.extensions["arrax"]["shutdown"]()
        core.close()
