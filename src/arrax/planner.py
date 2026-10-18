"""Deterministic planning from structured goals and demo goal names."""

from __future__ import annotations

from typing import Any, Mapping

from .domain import DomainValidationError, PlanStep, RiskLevel


class PlanningError(DomainValidationError):
    """Raised when a goal cannot be turned into a valid plan."""


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlanningError(f"goal field {key!r} must be a non-empty string")
    return value


class DeterministicPlanner:
    """Build plans without an AI provider or implicit tool selection."""

    def plan(self, goal: Mapping[str, Any]) -> tuple[PlanStep, ...]:
        if not isinstance(goal, Mapping):
            raise PlanningError("goal must be an object")
        data = dict(goal)
        goal_type = data.get("type", data.get("kind"))
        if not isinstance(goal_type, str) or not goal_type.strip():
            raise PlanningError("goal requires a non-empty type")
        goal_type = goal_type.strip().lower()

        if goal_type in {"success", "fixture_success"}:
            return (self._success_step(data),)
        if goal_type in {"approval", "approval_required", "sensitive"}:
            return (self._approval_step(data),)
        if goal_type in {"failure_recovery", "fail_once"}:
            return (
                PlanStep(
                    step_id="recoverable-action",
                    name="Run the recoverable fixture action",
                    tool_name="fixture.fail_once",
                    input={"key": data.get("key", "default")},
                    risk=RiskLevel.REVERSIBLE,
                    max_attempts=2,
                ),
            )
        if goal_type in {"verification_failure", "unverified"}:
            return (
                PlanStep(
                    step_id="unverified-action",
                    name="Run an action with failed verification",
                    tool_name="fixture.success",
                    input={"message": data.get("message", "unverified fixture"), "verified": False},
                    risk=RiskLevel.READ_ONLY,
                ),
            )
        if goal_type in {"timeout", "delay"}:
            seconds = data.get("seconds", 0.2)
            return (
                PlanStep(
                    step_id="delayed-action",
                    name="Run the bounded delay fixture",
                    tool_name="fixture.delay",
                    input={"seconds": seconds},
                    risk=RiskLevel.READ_ONLY,
                    timeout_seconds=float(data.get("timeout_seconds", 0.05)),
                ),
            )
        if goal_type in {"tool", "structured_action"}:
            return (self._structured_step(data, "structured-action"),)
        if goal_type in {"plan", "structured_plan"}:
            raw_steps = data.get("steps")
            if not isinstance(raw_steps, list) or not raw_steps:
                raise PlanningError("structured plan requires a non-empty steps list")
            steps = tuple(
                self._structured_step(step, f"step-{index + 1}")
                for index, step in enumerate(raw_steps)
                if isinstance(step, Mapping)
            )
            if len(steps) != len(raw_steps):
                raise PlanningError("every structured plan step must be an object")
            return steps
        raise PlanningError(f"unrecognized goal type: {goal_type}")

    def _success_step(self, data: Mapping[str, Any]) -> PlanStep:
        return PlanStep(
            step_id="success-action",
            name="Run the successful fixture action",
            tool_name="fixture.success",
            input={"message": data.get("message", "success fixture")},
            risk=RiskLevel.READ_ONLY,
        )

    def _approval_step(self, data: Mapping[str, Any]) -> PlanStep:
        return PlanStep(
            step_id="approval-action",
            name="Run the approval-gated fixture action",
            tool_name="fixture.approval",
            input={"message": data.get("message", "approved fixture")},
            risk=RiskLevel.SENSITIVE,
            approval_required=True,
        )

    def _structured_step(self, data: Mapping[str, Any], default_id: str) -> PlanStep:
        if not isinstance(data, Mapping):
            raise PlanningError("structured action must be an object")
        tool_name = data.get("tool_name", data.get("tool"))
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise PlanningError("structured action requires tool_name")
        action_input = data.get("input", {})
        if not isinstance(action_input, Mapping):
            raise PlanningError("structured action input must be an object")
        risk_value = data.get("risk", RiskLevel.READ_ONLY.value)
        try:
            risk = risk_value if isinstance(risk_value, RiskLevel) else RiskLevel(risk_value)
        except (TypeError, ValueError) as exc:
            raise PlanningError(f"invalid risk level: {risk_value!r}") from exc
        dependencies = data.get("dependencies", ())
        if not isinstance(dependencies, (list, tuple)):
            raise PlanningError("dependencies must be a list")
        approval_required = data.get("approval_required", False)
        if not isinstance(approval_required, bool):
            raise PlanningError("approval_required must be boolean")
        return PlanStep(
            step_id=data.get("step_id", data.get("id", default_id)),
            name=data.get("name", f"Run {tool_name}"),
            tool_name=tool_name,
            input=dict(action_input),
            dependencies=tuple(dependencies),
            risk=risk,
            approval_required=approval_required,
            max_attempts=data.get("max_attempts", 1),
            timeout_seconds=data.get("timeout_seconds", 1.0),
            verifier_tool=data.get("verifier_tool", "fixture.verify"),
            verification_input=data.get("verification_input"),
        )
