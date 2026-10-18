"""Small, honest, offline regression evaluations for the ARRAX core.

The evaluation lab deliberately uses the deterministic planner and a fresh
in-memory :class:`~arrax.orchestrator.AgentCore` for every scenario.  It does
not inspect or reuse the application's configured provider, workspace, or
database.  Durations in the returned report measure this local regression run;
they are not production performance benchmarks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import time
from typing import Any, Mapping

from .domain import Task, TaskStatus
from .orchestrator import AgentCore
from .planner import DeterministicPlanner
from .provider import ProviderConfig, ProviderMode


MAX_SCENARIOS = 5
MAX_SCENARIO_NAME_LENGTH = 64
MAX_REPORT_BYTES = 256 * 1024


class EvaluationError(ValueError):
    """Base class for invalid or unsupported evaluation requests."""


class UnknownScenarioError(EvaluationError):
    """Raised when a caller selects a scenario outside the catalog."""


@dataclass(frozen=True)
class EvaluationScenario:
    """A catalog entry and the deterministic goal used to exercise it."""

    name: str
    description: str
    goal: Mapping[str, Any]
    expected_status: TaskStatus
    requires_verification: bool
    requires_recovery: bool = False
    requires_approval: bool = False
    expected_error_category: str | None = None
    completion_expected: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > MAX_SCENARIO_NAME_LENGTH:
            raise EvaluationError("evaluation scenario name is invalid")
        if not isinstance(self.description, str) or not self.description.strip() or len(self.description) > 240:
            raise EvaluationError("evaluation scenario description is invalid")
        if not isinstance(self.goal, Mapping) or not self.goal:
            raise EvaluationError("evaluation scenario goal is invalid")
        if not isinstance(self.expected_status, TaskStatus):
            raise EvaluationError("evaluation scenario status is invalid")
        for flag in (self.requires_verification, self.requires_recovery, self.requires_approval, self.completion_expected):
            if not isinstance(flag, bool):
                raise EvaluationError("evaluation scenario expectation flags are invalid")
        if self.expected_error_category is not None and (
            not isinstance(self.expected_error_category, str) or not self.expected_error_category.strip()
        ):
            raise EvaluationError("evaluation scenario error category is invalid")

    def to_dict(self) -> dict[str, Any]:
        """Return only the public catalog contract, never the internal goal."""

        return {
            "name": self.name,
            "description": self.description,
            "expected_outcome": {
                "final_status": self.expected_status.value,
                "requires_verification": self.requires_verification,
                "requires_recovery_event": self.requires_recovery,
                "requires_approval_pause": self.requires_approval,
                "error_category": self.expected_error_category,
                "completion_expected": self.completion_expected,
            },
        }


_SCENARIOS: tuple[EvaluationScenario, ...] = (
    EvaluationScenario(
        name="verified_success",
        description="A successful fixture action must complete with verifier evidence.",
        goal={"type": "success", "message": "evaluation verified success"},
        expected_status=TaskStatus.COMPLETED,
        requires_verification=True,
    ),
    EvaluationScenario(
        name="self_healing_recovery",
        description="A fail-once action must recover within its bounded retry plan and verify.",
        goal={"type": "failure_recovery", "key": "evaluation-recovery"},
        expected_status=TaskStatus.COMPLETED,
        requires_verification=True,
        requires_recovery=True,
    ),
    EvaluationScenario(
        name="approval_gate",
        description="A sensitive action must pause for approval before it can complete.",
        goal={"type": "approval", "message": "evaluation approval gate"},
        expected_status=TaskStatus.COMPLETED,
        requires_verification=True,
        requires_approval=True,
    ),
    EvaluationScenario(
        name="verification_guard",
        description="An unverified action output must fail and never emit task completion.",
        goal={"type": "verification_failure", "message": "evaluation unverified output"},
        expected_status=TaskStatus.FAILED,
        requires_verification=False,
        expected_error_category="verification_failed",
        completion_expected=False,
    ),
    EvaluationScenario(
        name="timeout_boundary",
        description="A bounded delay must fail with the timeout error classification.",
        goal={"type": "timeout", "seconds": 0.2, "timeout_seconds": 0.05},
        expected_status=TaskStatus.FAILED,
        requires_verification=False,
        expected_error_category="timeout",
        completion_expected=False,
    ),
)


def scenario_catalog() -> list[dict[str, Any]]:
    """Return the actual bounded catalog in stable order."""

    return [scenario.to_dict() for scenario in _SCENARIOS]


def _scenario_by_name(name: str) -> EvaluationScenario:
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_SCENARIO_NAME_LENGTH:
        raise EvaluationError("scenario must be a valid catalog name")
    for scenario in _SCENARIOS:
        if scenario.name == name:
            return scenario
    raise UnknownScenarioError("scenario is not in the evaluation catalog")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_rate(count: int, total: int) -> float:
    return round(count / total, 6) if total else 0.0


def _error_category(task: Task) -> str | None:
    for step in task.plan:
        if isinstance(step.result, Mapping) and isinstance(step.result.get("error_type"), str):
            return step.result["error_type"]
    if isinstance(task.result, Mapping) and isinstance(task.result.get("error_type"), str):
        return task.result["error_type"]
    return None


def _verification_evidence(task: Task) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for step in task.plan:
        if step.verification_evidence:
            evidence.append({"step_id": step.step_id, "evidence": dict(step.verification_evidence)})
    return evidence[:MAX_SCENARIOS * 2]


def _run_single_scenario(scenario: EvaluationScenario) -> dict[str, Any]:
    started = time.monotonic()
    core: AgentCore | None = None
    try:
        # Supplying both boundaries makes the offline contract explicit.  With
        # a planner injected, AgentCore does not instantiate or contact Gemini.
        core = AgentCore(
            db_path=":memory:",
            planner=DeterministicPlanner(),
            provider_config=ProviderConfig(mode=ProviderMode.DETERMINISTIC),
            max_timeout_seconds=1.0,
        )
        task = core.create_task(scenario.goal)
        first_run = core.run_task(task.task_id)
        approval_behavior: dict[str, Any] = {
            "required": scenario.requires_approval,
            "paused": first_run.status is TaskStatus.AWAITING_APPROVAL,
            "approved": False,
            "resumed": False,
            "approval_event_count": 0,
        }
        if scenario.requires_approval and first_run.status is TaskStatus.AWAITING_APPROVAL:
            approved = core.approve_task(task.task_id)
            approval_behavior["approved"] = any(step.approved for step in approved.plan)
            approval_behavior["resumed"] = approved.status is TaskStatus.QUEUED
            final_task = core.run_task(task.task_id)
        else:
            final_task = first_run

        events = core.get_events(task.task_id)
        recovery_event_count = sum(event.event_type == "step.recovering" for event in events)
        completion_event_count = sum(event.event_type == "task.completed" for event in events)
        approval_event_count = sum(event.event_type == "approval.required" for event in events)
        approval_grant_count = sum(event.event_type == "approval.granted" for event in events)
        approval_behavior["approval_event_count"] = approval_event_count
        approval_behavior["approval_grant_count"] = approval_grant_count
        evidence = _verification_evidence(final_task)
        actual_error_category = _error_category(final_task)
        checks = {
            "final_status_matches": final_task.status is scenario.expected_status,
            "completion_event_matches": (completion_event_count > 0) is scenario.completion_expected,
            "verification_evidence_matches": bool(evidence) is scenario.requires_verification,
            "recovery_event_matches": (recovery_event_count > 0) is scenario.requires_recovery,
            "approval_behavior_matches": (
                approval_behavior["required"] is scenario.requires_approval
                and (not scenario.requires_approval or (
                    approval_behavior["paused"]
                    and approval_behavior["approved"]
                    and approval_behavior["resumed"]
                    and approval_event_count == 1
                    and approval_grant_count == 1
                ))
            ),
            "error_category_matches": actual_error_category == scenario.expected_error_category,
        }
        result = {
            "name": scenario.name,
            "description": scenario.description,
            "passed": all(checks.values()),
            "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
            "final_status": final_task.status.value,
            "expected_status": scenario.expected_status.value,
            "expected_checks": checks,
            "verification_evidence": evidence,
            "recovery_event_count": recovery_event_count,
            "approval_behavior": approval_behavior,
            "error_category": actual_error_category,
            "completion_event_count": completion_event_count,
        }
    except Exception:
        # The catalog itself is fixed and local.  If an unexpected regression
        # escapes the core, report it as a failed measured scenario without
        # exposing implementation details or machine data.
        result = {
            "name": scenario.name,
            "description": scenario.description,
            "passed": False,
            "elapsed_seconds": round(max(0.0, time.monotonic() - started), 6),
            "final_status": "evaluation_error",
            "expected_status": scenario.expected_status.value,
            "expected_checks": {
                "final_status_matches": False,
                "completion_event_matches": False,
                "verification_evidence_matches": False,
                "recovery_event_matches": False,
                "approval_behavior_matches": False,
                "error_category_matches": False,
            },
            "verification_evidence": [],
            "recovery_event_count": 0,
                "approval_behavior": {
                    "required": scenario.requires_approval,
                    "paused": False,
                    "approved": False,
                    "resumed": False,
                    "approval_event_count": 0,
                    "approval_grant_count": 0,
                },
            "error_category": "evaluation_error",
            "completion_event_count": 0,
        }
    finally:
        if core is not None:
            core.close()
    return result


def _validate_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the public result JSON-safe, finite, and bounded."""

    if not isinstance(report, Mapping):
        raise EvaluationError("evaluation report must be an object")
    results = report.get("scenario_results")
    if not isinstance(results, list) or not 1 <= len(results) <= MAX_SCENARIOS:
        raise EvaluationError("evaluation results exceeded their scenario bound")
    seen_names: set[str] = set()
    for result in results:
        if not isinstance(result, Mapping):
            raise EvaluationError("evaluation scenario result must be an object")
        name = result.get("name")
        if not isinstance(name, str) or name in seen_names:
            raise EvaluationError("evaluation scenario results must have unique names")
        _scenario_by_name(name)
        seen_names.add(name)
        if not isinstance(result.get("description"), str) or len(result["description"]) > 240:
            raise EvaluationError("evaluation scenario result description was invalid")
        if not isinstance(result.get("passed"), bool):
            raise EvaluationError("evaluation scenario result status was invalid")
        for count_name in ("recovery_event_count", "completion_event_count"):
            count = result.get(count_name)
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 32:
                raise EvaluationError("evaluation event count was invalid")
        evidence = result.get("verification_evidence")
        if not isinstance(evidence, list) or len(evidence) > MAX_SCENARIOS * 2:
            raise EvaluationError("evaluation evidence exceeded its bound")
        checks = result.get("expected_checks")
        if not isinstance(checks, Mapping) or any(not isinstance(value, bool) for value in checks.values()):
            raise EvaluationError("evaluation checks were invalid")
        approval = result.get("approval_behavior")
        if not isinstance(approval, Mapping):
            raise EvaluationError("evaluation approval behavior was invalid")

    try:
        encoded = json.dumps(report, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise EvaluationError("evaluation report was not serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise EvaluationError("evaluation report exceeded its size bound")
    for result in report.get("scenario_results", []):
        elapsed = result.get("elapsed_seconds")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed):
            raise EvaluationError("evaluation duration was invalid")
    return dict(report)


def run_evaluation(scenario: str | None = None) -> dict[str, Any]:
    """Run one catalog scenario or all scenarios with fresh offline cores.

    The call is intentionally synchronous.  There are five small, bounded
    fixture scenarios and no provider, network, workspace, or application
    database is consulted.
    """

    if scenario is None:
        selected = list(_SCENARIOS)
    else:
        selected = [_scenario_by_name(scenario)]
    if len(selected) > MAX_SCENARIOS:
        raise EvaluationError("evaluation scenario selection exceeded its bound")

    run_started_at = _utc_now()
    results = [_run_single_scenario(item) for item in selected]
    run_finished_at = _utc_now()
    total = len(results)
    passed = sum(result["passed"] is True for result in results)
    completed = sum(result["final_status"] == TaskStatus.COMPLETED.value for result in results)
    recovered = sum(result["recovery_event_count"] > 0 for result in results)
    verified = sum(bool(result["verification_evidence"]) for result in results)
    summary = {
        "scenario_count": total,
        "passed_count": passed,
        "failed_count": total - passed,
        "completed_count": completed,
        "recovered_count": recovered,
        "verified_count": verified,
        "pass_rate": _safe_rate(passed, total),
        "completion_rate": _safe_rate(completed, total),
        "recovery_rate": _safe_rate(recovered, total),
        "verification_rate": _safe_rate(verified, total),
    }
    return _validate_report(
        {
            "evaluation_type": "deterministic_regression",
            "mode": "offline_deterministic",
            "run_timestamp": run_started_at,
            "run_started_at": run_started_at,
            "run_finished_at": run_finished_at,
            "scenario_results": results,
            "summary": summary,
            "notes": "Measured local scenario durations are not production performance benchmarks.",
        }
    )


__all__ = [
    "EvaluationError",
    "EvaluationScenario",
    "MAX_SCENARIOS",
    "UnknownScenarioError",
    "run_evaluation",
    "scenario_catalog",
]
