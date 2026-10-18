"""Allowlisted, deterministic tools and their bounded execution registry."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class ToolError(Exception):
    """Base class for safe tool-selection and execution errors."""


class UnknownToolError(ToolError):
    """Raised when a plan names a tool absent from the allowlist."""


class ToolInputError(ToolError, ValueError):
    """Raised before execution when a tool input does not match its contract."""


@dataclass(frozen=True)
class ToolResult:
    success: bool
    output: Mapping[str, Any]
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise ToolInputError("tool result success must be boolean")
        if not isinstance(self.output, Mapping):
            raise ToolInputError("tool result output must be an object")
        if self.error is not None and not isinstance(self.error, str):
            raise ToolInputError("tool result error must be a string or null")


@dataclass(frozen=True)
class ToolContext:
    """Execution context available to a tool without granting system access."""

    cancellation_event: threading.Event
    stop_event: threading.Event
    deadline: float

    @property
    def cancelled(self) -> bool:
        return self.cancellation_event.is_set() or self.stop_event.is_set()

    @property
    def timed_out(self) -> bool:
        return self.stop_event.is_set() and not self.cancellation_event.is_set()


class Tool(Protocol):
    name: str
    default_timeout_seconds: float
    is_verifier: bool

    def validate_input(self, value: Mapping[str, Any]) -> None:
        """Raise ToolInputError if value is invalid."""

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        """Perform a bounded, allowlisted operation."""


@dataclass(frozen=True)
class ToolExecution:
    result: ToolResult | None = None
    error: str | None = None
    timed_out: bool = False
    cancelled: bool = False


def _object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ToolInputError(f"{name} input must be an object")
    return dict(value)


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{field_name} must be a non-empty string")
    return value


class FixtureSuccessTool:
    name = "fixture.success"
    default_timeout_seconds = 1.0
    is_verifier = False

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        _string(data.get("message"), "message")
        if "verified" in data and not isinstance(data["verified"], bool):
            raise ToolInputError("verified must be boolean")
        unknown = set(data) - {"message", "verified"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.cancelled:
            return ToolResult(False, {}, "cancelled")
        return ToolResult(
            True,
            {
                "fixture": "success",
                "message": value["message"],
                "verified": value.get("verified", True),
            },
        )


class ApprovalRequiredTool(FixtureSuccessTool):
    name = "fixture.approval"

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        result = super().execute(value, context)
        if result.success:
            return ToolResult(True, {**result.output, "fixture": "approval"})
        return result


class FailOnceTool:
    name = "fixture.fail_once"
    default_timeout_seconds = 1.0
    is_verifier = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failed_keys: set[str] = set()

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        _string(data.get("key"), "key")
        unknown = set(data) - {"key"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.cancelled:
            return ToolResult(False, {}, "cancelled")
        key = value["key"]
        with self._lock:
            if key not in self._failed_keys:
                self._failed_keys.add(key)
                return ToolResult(False, {"fixture": "fail_once", "attempt": 1}, "injected failure")
        return ToolResult(True, {"fixture": "fail_once", "key": key, "verified": True})


class VerifyTool:
    name = "fixture.verify"
    default_timeout_seconds = 1.0
    is_verifier = True

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        action_output = data.get("action_output")
        if not isinstance(action_output, Mapping):
            raise ToolInputError("action_output must be an object")
        unknown = set(data) - {"action_output"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.cancelled:
            return ToolResult(False, {}, "cancelled")
        output = value["action_output"]
        verified = output.get("verified") is True
        if not verified:
            return ToolResult(False, {"verified": False}, "action output was not verified")
        return ToolResult(True, {"verified": True, "observed_fixture": output.get("fixture", "unknown")})


class DelayTool:
    """A safe deterministic delay fixture used to prove timeout boundaries."""

    name = "fixture.delay"
    default_timeout_seconds = 1.0
    is_verifier = False

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        seconds = data.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 5:
            raise ToolInputError("seconds must be a number between zero and five")
        unknown = set(data) - {"seconds"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        deadline = time.monotonic() + float(value["seconds"])
        while time.monotonic() < deadline:
            if context.cancelled:
                return ToolResult(False, {}, "cancelled")
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        return ToolResult(True, {"fixture": "delay", "verified": True, "seconds": value["seconds"]})


class ToolRegistry:
    """An explicit allowlist that validates and bounds every tool invocation."""

    def __init__(self, tools: list[Tool] | None = None, max_timeout_seconds: float = 10.0) -> None:
        if max_timeout_seconds <= 0:
            raise ValueError("max_timeout_seconds must be positive")
        self._tools: dict[str, Tool] = {}
        self.max_timeout_seconds = float(max_timeout_seconds)
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool must define a non-empty name")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        if not callable(getattr(tool, "validate_input", None)) or not callable(getattr(tool, "execute", None)):
            raise ValueError(f"tool {name!r} does not implement the tool protocol")
        self._tools[name] = tool

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(f"unknown tool: {name}")
        return tool

    def validate(self, name: str, value: Mapping[str, Any]) -> None:
        tool = self.get(name)
        try:
            tool.validate_input(value)
        except ToolInputError:
            raise
        except Exception as exc:
            raise ToolInputError(str(exc)) from exc

    def run(
        self,
        name: str,
        value: Mapping[str, Any],
        cancellation_event: threading.Event,
        timeout_seconds: float | None = None,
    ) -> ToolExecution:
        """Validate, execute, and return without waiting past the deadline.

        The worker is a daemon thread because a third-party test double may
        ignore its cancellation signal.  The registry still returns a bounded
        result to the orchestrator in that case.
        """

        try:
            tool = self.get(name)
            self.validate(name, value)
        except UnknownToolError:
            raise
        except ToolInputError:
            raise

        configured = timeout_seconds if timeout_seconds is not None else getattr(tool, "default_timeout_seconds", 1.0)
        if isinstance(configured, bool) or not isinstance(configured, (int, float)) or configured <= 0:
            raise ToolInputError("timeout_seconds must be a positive number")
        timeout = min(float(configured), self.max_timeout_seconds)
        if cancellation_event.is_set():
            return ToolExecution(error="cancelled", cancelled=True)

        stop_event = threading.Event()
        deadline = time.monotonic() + timeout
        context = ToolContext(cancellation_event, stop_event, deadline)
        result_holder: list[ToolResult] = []
        error_holder: list[str] = []

        def invoke() -> None:
            try:
                result = tool.execute(value, context)
                if not isinstance(result, ToolResult):
                    raise ToolError("tool returned an invalid result")
                result_holder.append(result)
            except Exception as exc:  # Tool failures become safe observations.
                error_holder.append(str(exc) or exc.__class__.__name__)

        worker = threading.Thread(target=invoke, name=f"arrax-tool-{name}", daemon=True)
        worker.start()
        while worker.is_alive():
            if cancellation_event.is_set():
                stop_event.set()
                return ToolExecution(error="cancelled", cancelled=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_event.set()
                return ToolExecution(error=f"tool timed out after {timeout:g}s", timed_out=True)
            worker.join(min(0.01, remaining))

        if cancellation_event.is_set():
            return ToolExecution(error="cancelled", cancelled=True)
        if error_holder:
            return ToolExecution(error=error_holder[0])
        if not result_holder:
            return ToolExecution(error="tool returned no result")
        return ToolExecution(result=result_holder[0])


def default_registry() -> ToolRegistry:
    """Create a fresh registry so fixture state is isolated per agent core."""

    return ToolRegistry(
        [
            FixtureSuccessTool(),
            ApprovalRequiredTool(),
            FailOnceTool(),
            VerifyTool(),
            DelayTool(),
        ]
    )
