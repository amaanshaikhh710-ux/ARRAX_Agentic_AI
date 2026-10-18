"""Core value objects for the deterministic ARRAX agent.

The domain module deliberately has no storage, provider, or operating-system
dependencies.  Values in this module are the contracts passed between the
planner, policy layer, tools, and persistence boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Mapping


class DomainValidationError(ValueError):
    """Raised when an externally supplied domain value is malformed."""


class TaskStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    RECOVERING = "recovering"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.COMPLETED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string")
    return value


def _mapping(value: Mapping[str, Any] | None, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DomainValidationError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise DomainValidationError(f"{field_name} keys must be strings")
    _validate_json_value(value, field_name)
    return dict(value)


def _validate_json_value(value: Any, field_name: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainValidationError(f"{field_name} cannot contain NaN or infinity")
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise DomainValidationError(f"{field_name} keys must be strings")
        for key, nested in value.items():
            _validate_json_value(nested, f"{field_name}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _validate_json_value(nested, f"{field_name}[{index}]")
        return
    raise DomainValidationError(f"{field_name} contains an unsupported value")


@dataclass(frozen=True)
class PlanStep:
    """One allowlisted action and its verification contract."""

    step_id: str
    name: str
    tool_name: str
    input: Mapping[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.READ_ONLY
    approval_required: bool = False
    max_attempts: int = 1
    timeout_seconds: float = 1.0
    verifier_tool: str = "fixture.verify"
    verification_input: Mapping[str, Any] | None = None
    attempts: int = 0
    result: Mapping[str, Any] | None = None
    verification_evidence: Mapping[str, Any] | None = None
    status: TaskStatus = TaskStatus.QUEUED
    approved: bool = False

    @property
    def id(self) -> str:
        """Short alias useful to callers rendering a plan."""

        return self.step_id

    def __post_init__(self) -> None:
        _require_non_empty(self.step_id, "step_id")
        _require_non_empty(self.name, "name")
        _require_non_empty(self.tool_name, "tool_name")
        _require_non_empty(self.verifier_tool, "verifier_tool")
        object.__setattr__(self, "input", _mapping(self.input, "input") or {})
        object.__setattr__(self, "verification_input", _mapping(self.verification_input, "verification_input"))
        object.__setattr__(self, "result", _mapping(self.result, "result"))
        object.__setattr__(self, "verification_evidence", _mapping(self.verification_evidence, "verification_evidence"))
        try:
            risk = self.risk if isinstance(self.risk, RiskLevel) else RiskLevel(self.risk)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(f"invalid risk level: {self.risk!r}") from exc
        object.__setattr__(self, "risk", risk)
        try:
            status = self.status if isinstance(self.status, TaskStatus) else TaskStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(f"invalid step status: {self.status!r}") from exc
        object.__setattr__(self, "status", status)
        if not isinstance(self.dependencies, (list, tuple)):
            raise DomainValidationError("dependencies must be a list or tuple")
        if not isinstance(self.dependencies, tuple):
            object.__setattr__(self, "dependencies", tuple(self.dependencies))
        if any(not isinstance(dep, str) or not dep.strip() for dep in self.dependencies):
            raise DomainValidationError("dependencies must contain non-empty step ids")
        if self.step_id in self.dependencies:
            raise DomainValidationError("a step cannot depend on itself")
        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise DomainValidationError("max_attempts must be a positive integer")
        if not isinstance(self.attempts, int) or not 0 <= self.attempts <= self.max_attempts:
            raise DomainValidationError("attempts must be between zero and max_attempts")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise DomainValidationError("timeout_seconds must be a positive number")
        if self.timeout_seconds <= 0:
            raise DomainValidationError("timeout_seconds must be a positive number")
        if not isinstance(self.approval_required, bool) or not isinstance(self.approved, bool):
            raise DomainValidationError("approval flags must be booleans")

    def with_updates(self, **changes: Any) -> "PlanStep":
        values = {
            "step_id": self.step_id,
            "name": self.name,
            "tool_name": self.tool_name,
            "input": self.input,
            "dependencies": self.dependencies,
            "risk": self.risk,
            "approval_required": self.approval_required,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "verifier_tool": self.verifier_tool,
            "verification_input": self.verification_input,
            "attempts": self.attempts,
            "result": self.result,
            "verification_evidence": self.verification_evidence,
            "status": self.status,
            "approved": self.approved,
        }
        values.update(changes)
        return PlanStep(**values)


@dataclass(frozen=True)
class Task:
    """Persisted task state and its deterministic plan."""

    task_id: str
    goal: Mapping[str, Any]
    status: TaskStatus = TaskStatus.QUEUED
    plan: tuple[PlanStep, ...] = ()
    result: Mapping[str, Any] | None = None
    error: str | None = None
    cancellation_requested: bool = False
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def id(self) -> str:
        """Short alias for the persisted task identifier."""

        return self.task_id

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "task_id")
        normalized_goal = _mapping(self.goal, "goal")
        if normalized_goal is None or not normalized_goal:
            raise DomainValidationError("goal must be a non-empty object")
        object.__setattr__(self, "goal", normalized_goal)
        try:
            status = self.status if isinstance(self.status, TaskStatus) else TaskStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise DomainValidationError(f"invalid task status: {self.status!r}") from exc
        object.__setattr__(self, "status", status)
        if not isinstance(self.plan, tuple):
            object.__setattr__(self, "plan", tuple(self.plan))
        ids = [step.step_id for step in self.plan]
        if len(ids) != len(set(ids)):
            raise DomainValidationError("plan step ids must be unique")
        if any(not isinstance(step, PlanStep) for step in self.plan):
            raise DomainValidationError("plan must contain PlanStep values")
        object.__setattr__(self, "result", _mapping(self.result, "result"))
        if self.error is not None and not isinstance(self.error, str):
            raise DomainValidationError("error must be a string or null")
        if not isinstance(self.cancellation_requested, bool):
            raise DomainValidationError("cancellation_requested must be a boolean")

    def with_updates(self, **changes: Any) -> "Task":
        values = {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "plan": self.plan,
            "result": self.result,
            "error": self.error,
            "cancellation_requested": self.cancellation_requested,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        values.update(changes)
        return Task(**values)
