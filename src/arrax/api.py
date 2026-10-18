"""Flask HTTP boundary for the deterministic ARRAX agent core.

This module deliberately contains no provider, network, desktop, or shell
integration.  It validates JSON requests, delegates policy decisions to
``AgentCore``, and schedules synchronous core runs on a bounded local worker
pool.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
import math
import threading
from collections.abc import Mapping
from typing import Any

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from .domain import DomainValidationError, PlanStep, Task, TaskStatus
from .orchestrator import AgentCore, TaskNotFoundError
from .planner import PlanningError
from .storage import Event


DEFAULT_MAX_CONTENT_LENGTH = 64 * 1024
DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_QUEUE = 32
MAX_GOAL_DEPTH = 32


class APIError(Exception):
    """An expected API error with a safe public response."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class WorkerCapacityError(RuntimeError):
    """Raised when the bounded local execution queue has no free slot."""


class WorkerShutdownError(RuntimeError):
    """Raised when work is submitted after the worker manager was shut down."""


class TaskWorkerManager:
    """Run core tasks asynchronously with bounded capacity and clean shutdown.

    A task ID can have at most one active worker.  Approval can arrive while a
    worker is returning from an approval boundary, so approval requests mark a
    follow-up run when necessary rather than submitting a concurrent run.
    """

    def __init__(self, core: AgentCore, *, max_workers: int, max_queue: int) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if isinstance(max_queue, bool) or not isinstance(max_queue, int) or max_queue < 0:
            raise ValueError("max_queue must be a non-negative integer")

        self.core = core
        self.max_workers = max_workers
        self.max_queue = max_queue
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="arrax-task")
        self._slots = threading.BoundedSemaphore(max_workers + max_queue)
        self._lock = threading.RLock()
        self._active: dict[str, Future[None]] = {}
        self._follow_up: set[str] = set()
        self._shutdown = False

    def start(self, task_id: str, *, follow_up: bool = False) -> bool:
        """Queue a task unless it is terminal or already active.

        Returns ``True`` only when a new worker was submitted.  ``follow_up``
        is used after approval to safely resume a worker that is just leaving
        an approval boundary.
        """

        task = self.core.get_task(task_id)
        if task.status.terminal:
            return False

        with self._lock:
            if self._shutdown:
                raise WorkerShutdownError("worker manager is shut down")
            if task_id in self._active:
                if follow_up:
                    self._follow_up.add(task_id)
                return False
            if not self._slots.acquire(blocking=False):
                raise WorkerCapacityError("execution queue is full")
            try:
                future = self._executor.submit(self._run, task_id)
            except Exception:
                self._slots.release()
                raise
            self._active[task_id] = future
            return True

    def is_active(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._active

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def _run(self, task_id: str) -> None:
        try:
            # AgentCore owns the task lifecycle and all safety decisions.
            self.core.run_task(task_id)
        except Exception:
            # The core normally converts operational failures into task state.
            # If an unexpected exception escapes, persist a generic terminal
            # failure so the task cannot remain apparently active forever.
            try:
                self.core.fail_task(task_id)
            except Exception:
                # The worker must not terminate the executor because a second
                # persistence failure occurred while recording the guard state.
                pass
        finally:
            with self._lock:
                self._active.pop(task_id, None)
                follow_up = task_id in self._follow_up
                self._follow_up.discard(task_id)
                shutting_down = self._shutdown
            self._slots.release()
            if follow_up and not shutting_down:
                try:
                    task = self.core.get_task(task_id)
                    if not task.status.terminal:
                        self.start(task_id)
                except (TaskNotFoundError, WorkerCapacityError, WorkerShutdownError):
                    pass

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting work and optionally wait for all workers to finish.

        The application factory exposes this method at
        ``app.extensions["arrax_worker_manager"]`` and as
        ``app.extensions["arrax"]["shutdown"]`` so tests and embedding code
        can shut down without leaking executor threads.
        """

        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._follow_up.clear()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        if wait:
            with self._lock:
                self._active.clear()


def serialize_step(step: PlanStep) -> dict[str, Any]:
    """Return only JSON-facing plan-step values."""

    return {
        "step_id": step.step_id,
        "name": step.name,
        "tool_name": step.tool_name,
        "input": dict(step.input),
        "dependencies": list(step.dependencies),
        "risk": step.risk.value,
        "approval_required": step.approval_required,
        "approved": step.approved,
        "status": step.status.value,
        "attempts": step.attempts,
        "max_attempts": step.max_attempts,
        "timeout_seconds": step.timeout_seconds,
        "verifier_tool": step.verifier_tool,
        "verification_input": dict(step.verification_input) if step.verification_input is not None else None,
        "result": dict(step.result) if step.result is not None else None,
        "verification_evidence": (
            dict(step.verification_evidence) if step.verification_evidence is not None else None
        ),
    }


def serialize_task(task: Task) -> dict[str, Any]:
    """Return a stable task document without exposing dataclass instances."""

    return {
        "task_id": task.task_id,
        "goal": dict(task.goal),
        "status": task.status.value,
        "plan": [serialize_step(step) for step in task.plan],
        "result": dict(task.result) if task.result is not None else None,
        "error": task.error,
        "cancellation_requested": task.cancellation_requested,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def serialize_event(event: Event) -> dict[str, Any]:
    """Return a stable persisted event document."""

    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "step_id": event.step_id,
        "event_type": event.event_type,
        "payload": dict(event.payload),
        "created_at": event.created_at,
    }


def _error_response(code: str, message: str, status_code: int):
    return jsonify({"error": {"code": code, "message": message}}), status_code


def _safe_task(core: AgentCore, task_id: str) -> Task:
    try:
        return core.get_task(task_id)
    except TaskNotFoundError as exc:
        raise APIError("task_not_found", "task was not found", 404) from exc


def _validate_json_value(value: Any, *, path: str, depth: int = 0) -> None:
    """Reject non-JSON values and pathological nesting before core entry."""

    if depth > MAX_GOAL_DEPTH:
        raise APIError("invalid_goal", f"{path} is nested too deeply", 400)
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise APIError("invalid_goal", f"{path} cannot contain NaN or infinity", 400)
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise APIError("invalid_goal", f"{path} keys must be strings", 400)
        for key, nested in value.items():
            _validate_json_value(nested, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_json_value(nested, path=f"{path}[{index}]", depth=depth + 1)
        return
    raise APIError("invalid_goal", f"{path} contains an unsupported value", 400)


def _json_object(*, allow_empty: bool = False) -> dict[str, Any]:
    """Read a JSON object and convert parser failures to the API error shape."""

    try:
        raw = request.get_data(cache=True)
    except RequestEntityTooLarge:
        raise
    if not raw:
        if allow_empty:
            return {}
        raise APIError("invalid_json", "request body must contain a JSON object", 400)
    if not request.is_json:
        raise APIError("invalid_json", "request body must use application/json", 400)
    try:
        value = request.get_json(silent=False)
    except HTTPException as exc:
        raise APIError("invalid_json", "request body must be valid JSON", 400) from exc
    except (TypeError, ValueError) as exc:
        raise APIError("invalid_json", "request body must be valid JSON", 400) from exc
    if not isinstance(value, dict):
        raise APIError("invalid_request", "request body must be a JSON object", 400)
    return value


def _require_task_create_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    unknown = set(payload) - {"goal", "start"}
    if unknown:
        raise APIError("invalid_request", "request contains unsupported fields", 400)
    if "goal" not in payload:
        raise APIError("missing_goal", "goal is required", 400)
    goal = payload["goal"]
    if not isinstance(goal, dict) or not goal:
        raise APIError("invalid_goal", "goal must be a non-empty object", 400)
    _validate_json_value(goal, path="goal")
    try:
        json.dumps(goal, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise APIError("invalid_goal", "goal must contain JSON values", 400) from exc
    start = payload.get("start", True)
    if not isinstance(start, bool):
        raise APIError("invalid_start", "start must be a boolean", 400)
    return goal, start


def _task_response(task: Task, status_code: int = 200):
    return jsonify(serialize_task(task)), status_code


def create_app(
    core: AgentCore | None = None,
    *,
    db_path: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> Flask:
    """Create the local deterministic Flask application.

    ``core`` is injectable for isolated tests.  Without one, the factory
    creates an ``AgentCore`` backed by ``ARRAX_DB_PATH`` (or ``db_path`` and
    ultimately an in-memory SQLite database).  No server is started here.
    Call ``app.extensions["arrax"]["shutdown"]()`` or
    ``app.extensions["arrax_worker_manager"].shutdown()`` when the app is no
    longer needed.
    """

    app = Flask(__name__)
    app.config.from_mapping(
        MAX_CONTENT_LENGTH=DEFAULT_MAX_CONTENT_LENGTH,
        ARRAX_DB_PATH=db_path or ":memory:",
        ARRAX_MAX_WORKERS=DEFAULT_MAX_WORKERS,
        ARRAX_MAX_QUEUE=DEFAULT_MAX_QUEUE,
    )
    if config:
        app.config.update(config)
    if app.config.get("MAX_CONTENT_LENGTH") is None or app.config["MAX_CONTENT_LENGTH"] <= 0:
        raise ValueError("MAX_CONTENT_LENGTH must be a positive integer")

    owns_core = core is None
    if core is None:
        core = AgentCore(db_path=str(app.config["ARRAX_DB_PATH"]))
    manager = TaskWorkerManager(
        core,
        max_workers=app.config["ARRAX_MAX_WORKERS"],
        max_queue=app.config["ARRAX_MAX_QUEUE"],
    )
    shutdown_lock = threading.Lock()
    shutdown_state = {"closed": False}

    def shutdown() -> None:
        """Wait for local workers and close a core owned by this app."""

        with shutdown_lock:
            if shutdown_state["closed"]:
                return
            shutdown_state["closed"] = True
        manager.shutdown(wait=True)
        if owns_core:
            core.close()

    app.extensions["arrax_core"] = core
    app.extensions["arrax_worker_manager"] = manager
    app.extensions["arrax"] = {
        "core": core,
        "worker_manager": manager,
        "shutdown": shutdown,
        "owns_core": owns_core,
    }

    @app.errorhandler(APIError)
    def handle_api_error(error: APIError):
        return _error_response(error.code, error.message, error.status_code)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_too_large(_error: RequestEntityTooLarge):
        return _error_response("request_too_large", "request body is too large", 413)

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if error.code == 413:
            return _error_response("request_too_large", "request body is too large", 413)
        if error.code == 404:
            return _error_response("not_found", "resource was not found", 404)
        return _error_response("invalid_request", "request could not be processed", error.code or 400)

    @app.errorhandler(Exception)
    def handle_unexpected_error(_error: Exception):
        return _error_response("internal_error", "internal server error", 500)

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "service": "arrax-api",
                "status": "ready",
                "ready": True,
                "execution_mode": "deterministic_local",
                "provider_mode": "none",
            }
        )

    @app.get("/api/capabilities")
    def capabilities():
        return jsonify(
            {
                "tools": list(core.registry.names()),
                "execution_mode": "deterministic_local",
                "provider_mode": "none",
            }
        )

    @app.post("/api/tasks")
    def create_task():
        payload = _json_object()
        goal, start = _require_task_create_payload(payload)
        try:
            task = core.create_task(goal)
        except (DomainValidationError, PlanningError) as exc:
            raise APIError("invalid_goal", "goal could not be accepted", 400) from exc
        except Exception as exc:
            raise APIError("internal_error", "task could not be created", 500) from exc

        if start and not task.status.terminal:
            try:
                manager.start(task.task_id)
            except WorkerCapacityError as exc:
                raise APIError("worker_capacity", "execution queue is full", 409) from exc
            except WorkerShutdownError as exc:
                raise APIError("worker_shutdown", "execution workers are unavailable", 409) from exc
            return _task_response(task, 202)
        return _task_response(task, 201)

    @app.post("/api/tasks/<task_id>/run")
    def run_task(task_id: str):
        task = _safe_task(core, task_id)
        if not task.status.terminal:
            try:
                manager.start(task_id)
            except WorkerCapacityError as exc:
                raise APIError("worker_capacity", "execution queue is full", 409) from exc
            except WorkerShutdownError as exc:
                raise APIError("worker_shutdown", "execution workers are unavailable", 409) from exc
            return _task_response(core.get_task(task_id), 202)
        return _task_response(task)

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str):
        return _task_response(_safe_task(core, task_id))

    @app.get("/api/tasks/<task_id>/events")
    def get_events(task_id: str):
        _safe_task(core, task_id)
        try:
            events = core.get_events(task_id)
        except TaskNotFoundError as exc:
            raise APIError("task_not_found", "task was not found", 404) from exc
        return jsonify({"task_id": task_id, "events": [serialize_event(event) for event in events]})

    @app.post("/api/tasks/<task_id>/approve")
    def approve_task(task_id: str):
        payload = _json_object(allow_empty=True)
        unknown = set(payload) - {"step_id"}
        if unknown:
            raise APIError("invalid_request", "request contains unsupported fields", 400)
        step_id = payload.get("step_id")
        if "step_id" in payload and (not isinstance(step_id, str) or not step_id.strip()):
            raise APIError("invalid_step", "step_id must be a non-empty string", 400)

        task = _safe_task(core, task_id)
        if step_id is not None and step_id not in {step.step_id for step in task.plan}:
            raise APIError("invalid_step", "step_id was not found for this task", 400)
        if task.status in {TaskStatus.RUNNING, TaskStatus.PLANNING, TaskStatus.RECOVERING, TaskStatus.VERIFYING}:
            raise APIError("task_running", "task is currently executing", 409)
        try:
            task = core.approve_task(task_id, step_id=step_id)
        except TaskNotFoundError as exc:
            raise APIError("task_not_found", "task was not found", 404) from exc
        except KeyError as exc:
            raise APIError("invalid_step", "step_id was not found for this task", 400) from exc
        if not task.status.terminal:
            try:
                manager.start(task_id, follow_up=True)
            except WorkerCapacityError as exc:
                raise APIError("worker_capacity", "execution queue is full", 409) from exc
            except WorkerShutdownError as exc:
                raise APIError("worker_shutdown", "execution workers are unavailable", 409) from exc
            return _task_response(core.get_task(task_id), 202)
        return _task_response(task)

    @app.post("/api/tasks/<task_id>/cancel")
    def cancel_task(task_id: str):
        payload = _json_object(allow_empty=True)
        if payload:
            raise APIError("invalid_request", "cancellation does not accept fields", 400)
        try:
            task = core.cancel_task(task_id)
        except TaskNotFoundError as exc:
            raise APIError("task_not_found", "task was not found", 404) from exc
        return _task_response(task, 202 if not task.status.terminal else 200)

    return app


__all__ = [
    "APIError",
    "TaskWorkerManager",
    "create_app",
    "serialize_event",
    "serialize_step",
    "serialize_task",
]
