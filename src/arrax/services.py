"""Validation and application services for workflows and user memory.

The services keep HTTP concerns out of the SQLite repositories.  They also
make the safety boundary explicit: workflows are checked against the
deterministic planner and current tool registry before they are persisted, and
memory values are checked before any write is attempted.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any

from .orchestrator import AgentCore
from .storage import MemoryRecord, SQLiteStore, Workflow


WORKFLOW_VERSION = 1
MAX_WORKFLOW_NAME_LENGTH = 100
MAX_WORKFLOW_DESCRIPTION_LENGTH = 2_000
MAX_WORKFLOW_GOAL_BYTES = 16 * 1024
MAX_WORKFLOW_GOAL_DEPTH = 12
MAX_MEMORY_KEY_LENGTH = 128
MAX_MEMORY_CATEGORY_LENGTH = 64
MAX_MEMORY_VALUE_BYTES = 16 * 1024
MAX_MEMORY_VALUE_DEPTH = 8

_SENSITIVE_TERMS = (
    "apikey",
    "password",
    "token",
    "secret",
    "privatekey",
    "authorization",
    "bearer",
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:api[\s_-]*key|password|token|secret|private[\s_-]*key|authorization|bearer)(?![a-z0-9])",
    re.IGNORECASE,
)
_MACHINE_PATH_PATTERN = re.compile(r"^(?:[a-zA-Z]:[\\/]|[\\/]{1,2}|~[\\/])")


class ServiceValidationError(ValueError):
    """A safe, structured validation failure from an application service."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WorkflowValidationError(ServiceValidationError):
    """A workflow payload or goal was rejected before persistence."""


class WorkflowConflictError(ValueError):
    """A workflow name is already in use."""


class WorkflowNotFoundError(KeyError):
    """A workflow identifier did not resolve to a stored workflow."""


class MemoryValidationError(ServiceValidationError):
    """A memory record was rejected before persistence."""


class MemorySecretError(MemoryValidationError):
    """A memory key or value looked like a credential or secret."""

    def __init__(self) -> None:
        super().__init__("memory_secret_rejected", "memory records cannot contain credential-like data")


class MemoryNotFoundError(KeyError):
    """A memory identifier did not resolve to a stored record."""


def _validate_json_value(value: Any, *, max_depth: int, max_bytes: int, label: str) -> None:
    """Validate strict JSON values and their bounded serialized representation."""

    def visit(current: Any, depth: int) -> None:
        if depth > max_depth:
            raise ServiceValidationError("value_too_deep", f"{label} is nested too deeply")
        if current is None or isinstance(current, (str, bool, int)):
            return
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ServiceValidationError("invalid_json_value", f"{label} must contain JSON values")
            return
        if isinstance(current, Mapping):
            if any(not isinstance(key, str) for key in current):
                raise ServiceValidationError("invalid_json_value", f"{label} keys must be strings")
            for nested in current.values():
                visit(nested, depth + 1)
            return
        if isinstance(current, list):
            for nested in current:
                visit(nested, depth + 1)
            return
        raise ServiceValidationError("invalid_json_value", f"{label} must contain JSON values")

    try:
        visit(value, 0)
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ServiceValidationError("invalid_json_value", f"{label} must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ServiceValidationError("value_too_large", f"{label} exceeds its size limit")


def _validate_text(value: Any, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ServiceValidationError("invalid_text", f"{label} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ServiceValidationError("invalid_text", f"{label} must be a non-empty string")
    if len(normalized) > maximum or "\x00" in normalized:
        raise ServiceValidationError("value_too_large", f"{label} exceeds its size limit")
    return normalized


def _sensitive_key(value: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", value.casefold())
    return any(term in compact for term in _SENSITIVE_TERMS)


def _sensitive_value(value: str) -> bool:
    return bool(_SENSITIVE_VALUE_PATTERN.search(value))


def _validate_memory_safety(value: Any) -> None:
    def visit(current: Any) -> None:
        if isinstance(current, str):
            if _sensitive_value(current):
                raise MemorySecretError()
            if _MACHINE_PATH_PATTERN.match(current):
                raise MemoryValidationError(
                    "memory_path_rejected", "memory records cannot contain machine paths"
                )
            return
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if isinstance(key, str) and _sensitive_key(key):
                    raise MemorySecretError()
                visit(nested)
            return
        if isinstance(current, list):
            for nested in current:
                visit(nested)

    visit(value)


_GOAL_FIELDS: dict[str, set[str]] = {
    "success": {"type", "kind", "message"},
    "fixture_success": {"type", "kind", "message"},
    "approval": {"type", "kind", "message"},
    "approval_required": {"type", "kind", "message"},
    "sensitive": {"type", "kind", "message"},
    "failure_recovery": {"type", "kind", "key"},
    "fail_once": {"type", "kind", "key"},
    "verification_failure": {"type", "kind", "message"},
    "unverified": {"type", "kind", "message"},
    "timeout": {"type", "kind", "seconds", "timeout_seconds"},
    "delay": {"type", "kind", "seconds", "timeout_seconds"},
    "workspace_report": {"type", "kind", "path", "report_path", "overwrite"},
    "workspace_readiness_report": {"type", "kind", "path", "report_path", "overwrite"},
    "tool": {
        "type",
        "kind",
        "tool_name",
        "tool",
        "input",
        "risk",
        "approval_required",
        "step_id",
        "id",
        "name",
        "max_attempts",
        "timeout_seconds",
        "verifier_tool",
        "verification_input",
    },
    "structured_action": {
        "type",
        "kind",
        "tool_name",
        "tool",
        "input",
        "risk",
        "approval_required",
        "step_id",
        "id",
        "name",
        "max_attempts",
        "timeout_seconds",
        "verifier_tool",
        "verification_input",
    },
    "plan": {"type", "kind", "steps"},
    "structured_plan": {"type", "kind", "steps"},
}
_STRUCTURED_STEP_FIELDS = {
    "tool_name",
    "tool",
    "input",
    "risk",
    "dependencies",
    "approval_required",
    "step_id",
    "id",
    "name",
    "max_attempts",
    "timeout_seconds",
    "verifier_tool",
    "verification_input",
}


def _validate_goal_contract(goal: Mapping[str, Any]) -> None:
    goal_type = goal.get("type", goal.get("kind"))
    if not isinstance(goal_type, str) or not goal_type.strip():
        raise WorkflowValidationError("workflow_goal_invalid", "workflow goal was not accepted")
    normalized_type = goal_type.strip().casefold()
    allowed = _GOAL_FIELDS.get(normalized_type)
    if allowed is None or set(goal) - allowed:
        raise WorkflowValidationError("workflow_goal_invalid", "workflow goal was not accepted")
    if normalized_type in {"plan", "structured_plan"}:
        steps = goal.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, Mapping) or set(step) - _STRUCTURED_STEP_FIELDS:
                    raise WorkflowValidationError("workflow_goal_invalid", "workflow goal was not accepted")


class WorkflowService:
    """Validate and persist reusable workflows without executing them."""

    def __init__(self, store: SQLiteStore, core: AgentCore) -> None:
        self.store = store
        self.core = core

    def create(
        self,
        *,
        name: Any,
        description: Any,
        goal: Any,
        version: Any = WORKFLOW_VERSION,
    ) -> Workflow:
        normalized_name = _validate_text(
            name, label="workflow name", maximum=MAX_WORKFLOW_NAME_LENGTH
        )
        normalized_description = _validate_text(
            description, label="workflow description", maximum=MAX_WORKFLOW_DESCRIPTION_LENGTH
        )
        if isinstance(version, bool) or not isinstance(version, int) or version != WORKFLOW_VERSION:
            raise WorkflowValidationError("unsupported_workflow_version", "workflow version is not supported")
        if not isinstance(goal, Mapping) or not goal:
            raise WorkflowValidationError("workflow_goal_invalid", "workflow goal was not accepted")
        try:
            _validate_json_value(
                goal,
                max_depth=MAX_WORKFLOW_GOAL_DEPTH,
                max_bytes=MAX_WORKFLOW_GOAL_BYTES,
                label="workflow goal",
            )
            _validate_goal_contract(goal)
            self.core.validate_deterministic_goal(goal)
        except WorkflowValidationError:
            raise
        except ServiceValidationError as exc:
            raise WorkflowValidationError(exc.code, "workflow goal was not accepted") from exc
        except Exception as exc:
            # Planner/tool details can contain caller-controlled text.  Keep
            # those details inside the service boundary and return one safe
            # public error to the API.
            raise WorkflowValidationError("workflow_goal_invalid", "workflow goal was not accepted") from exc

        workflow = Workflow(
            id=uuid.uuid4().hex,
            name=normalized_name,
            description=normalized_description,
            goal=dict(goal),
            version=WORKFLOW_VERSION,
        )
        try:
            return self.store.workflows.create(workflow)
        except sqlite3.IntegrityError as exc:
            # sqlite3.IntegrityError is intentionally not exposed: a duplicate
            # name is the only expected conflict at this boundary.
            raise WorkflowConflictError("workflow name is already in use") from exc

    def get(self, workflow_id: str) -> Workflow:
        workflow = self.store.workflows.get(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(workflow_id)
        return workflow

    def list(self) -> list[Workflow]:
        return self.store.workflows.list()

    def delete(self, workflow_id: str) -> None:
        if not self.store.workflows.delete(workflow_id):
            raise WorkflowNotFoundError(workflow_id)


class MemoryService:
    """Validate, upsert, list, delete, and explicitly reset user memory."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def upsert(self, *, key: Any, value: Any, category: Any = "general") -> tuple[MemoryRecord, bool]:
        normalized_key = _validate_text(key, label="memory key", maximum=MAX_MEMORY_KEY_LENGTH)
        if _sensitive_key(normalized_key):
            raise MemorySecretError()
        normalized_category = _validate_text(
            category, label="memory category", maximum=MAX_MEMORY_CATEGORY_LENGTH
        )
        if _sensitive_value(normalized_category) or _sensitive_key(normalized_category):
            raise MemorySecretError()
        try:
            _validate_json_value(
                value,
                max_depth=MAX_MEMORY_VALUE_DEPTH,
                max_bytes=MAX_MEMORY_VALUE_BYTES,
                label="memory value",
            )
            _validate_memory_safety(value)
        except MemoryValidationError:
            raise
        except ServiceValidationError as exc:
            raise MemoryValidationError(exc.code, exc.message) from exc
        record = MemoryRecord(
            id=uuid.uuid4().hex,
            key=normalized_key,
            value=value,
            category=normalized_category,
        )
        return self.store.memory.upsert(record)

    def list(self) -> list[MemoryRecord]:
        return self.store.memory.list()

    def delete(self, memory_id: str) -> None:
        if not self.store.memory.delete(memory_id):
            raise MemoryNotFoundError(memory_id)

    def reset(self) -> int:
        return self.store.memory.reset()


__all__ = [
    "MAX_MEMORY_CATEGORY_LENGTH",
    "MAX_MEMORY_KEY_LENGTH",
    "MAX_MEMORY_VALUE_BYTES",
    "MAX_MEMORY_VALUE_DEPTH",
    "MAX_WORKFLOW_DESCRIPTION_LENGTH",
    "MAX_WORKFLOW_GOAL_BYTES",
    "MAX_WORKFLOW_GOAL_DEPTH",
    "MAX_WORKFLOW_NAME_LENGTH",
    "MemoryNotFoundError",
    "MemorySecretError",
    "MemoryService",
    "MemoryValidationError",
    "ServiceValidationError",
    "WORKFLOW_VERSION",
    "WorkflowConflictError",
    "WorkflowNotFoundError",
    "WorkflowService",
    "WorkflowValidationError",
]
