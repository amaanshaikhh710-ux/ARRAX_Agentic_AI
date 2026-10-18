"""Flask HTTP boundary for the deterministic ARRAX agent core.

This module validates JSON requests, delegates policy decisions to
``AgentCore``, exposes safe provider status, and schedules synchronous core
runs on a bounded local worker pool. Provider verification remains explicit and
is never triggered by health polling.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import threading
from collections.abc import Mapping
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from .domain import DomainValidationError, PlanStep, Task, TaskStatus
from .evaluation import EvaluationError, UnknownScenarioError, run_evaluation, scenario_catalog
from .orchestrator import AgentCore, TaskNotFoundError
from .planner import PlanningError
from .provider import ProviderConfig, ProviderConfigError
from .services import (
    MemoryNotFoundError,
    MemoryService,
    MemoryValidationError,
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowService,
    WorkflowValidationError,
)
from .storage import Event, MemoryRecord, TaskOverview, TaskSummary, Workflow
from .system import SystemSnapshotService
from .workspace import normalize_workspace_root


DEFAULT_MAX_CONTENT_LENGTH = 64 * 1024
DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_QUEUE = 32
DEFAULT_TASK_HISTORY_LIMIT = 20
MAX_TASK_HISTORY_LIMIT = 50
MAX_TASK_HISTORY_OFFSET = 10_000
MAX_GOAL_DEPTH = 32
FRONTEND_DIRECTORY = Path(__file__).resolve().parents[2] / "frontend"


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


def serialize_task_summary(summary: TaskSummary) -> dict[str, Any]:
    """Return bounded-list state while leaving full detail to task routes."""

    return {
        "task_id": summary.task_id,
        "goal": dict(summary.goal),
        "status": summary.status.value,
        "result": dict(summary.result) if summary.result is not None else None,
        "error": summary.error,
        "cancellation_requested": summary.cancellation_requested,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "step_count": summary.step_count,
        "event_count": summary.event_count,
    }


def serialize_task_overview(overview: TaskOverview) -> dict[str, Any]:
    """Return measured aggregate task state for the command center."""

    return {
        "total": overview.total,
        "active": overview.active,
        "completed": overview.completed,
        "failed": overview.failed,
        "partially_completed": overview.partially_completed,
        "awaiting_approval": overview.awaiting_approval,
        "recovery_event_count": overview.recovery_event_count,
        "last_updated_at": overview.last_updated_at,
        "counts": dict(overview.counts),
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


def serialize_workflow(workflow: Workflow) -> dict[str, Any]:
    """Return the public workflow document without storage internals."""

    return {
        "id": workflow.id,
        "name": workflow.name,
        "description": workflow.description,
        "goal": dict(workflow.goal),
        "version": workflow.version,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
    }


def serialize_memory(record: MemoryRecord) -> dict[str, Any]:
    """Return a JSON-safe public memory record."""

    return {
        "id": record.id,
        "key": record.key,
        "value": record.value,
        "category": record.category,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
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


def _history_query_integer(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise APIError("invalid_pagination", f"{name} must be an integer", 400) from exc
    if not minimum <= value <= maximum:
        raise APIError(
            "invalid_pagination",
            f"{name} must be between {minimum} and {maximum}",
            400,
        )
    return value


def create_app(
    core: AgentCore | None = None,
    *,
    db_path: str | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    planner: Any | None = None,
    provider: Any | None = None,
    config: Mapping[str, Any] | None = None,
    system_service: Any | None = None,
) -> Flask:
    """Create the local Flask application without verifying a provider.

    ``core`` is injectable for isolated tests.  Without one, the factory
    creates an ``AgentCore`` backed by ``ARRAX_DB_PATH`` (or ``db_path`` and
    ultimately an in-memory SQLite database).  An optional workspace root is
    passed to that core; without it, only fixture tools are registered. No
    server is started here.
    Call ``app.extensions["arrax"]["shutdown"]()`` or
    ``app.extensions["arrax_worker_manager"].shutdown()`` when the app is no
    longer needed.
    """

    app = Flask(__name__)
    app.config.from_mapping(
        MAX_CONTENT_LENGTH=DEFAULT_MAX_CONTENT_LENGTH,
        ARRAX_DB_PATH=db_path or ":memory:",
        ARRAX_WORKSPACE_ROOT=None,
        ARRAX_AI_MODE=os.getenv("ARRAX_AI_MODE", "auto"),
        GEMINI_API_KEY=os.getenv("GEMINI_API_KEY"),
        GEMINI_MODEL=os.getenv("GEMINI_MODEL"),
        ARRAX_MAX_WORKERS=DEFAULT_MAX_WORKERS,
        ARRAX_MAX_QUEUE=DEFAULT_MAX_QUEUE,
        ARRAX_SYSTEM_DISK_PATH=None,
        ARRAX_SYSTEM_PROCESS_LIMIT=5,
        ARRAX_SYSTEM_PROCESS_SCAN_LIMIT=50,
    )
    if config:
        app.config.update(config)
    if app.config.get("MAX_CONTENT_LENGTH") is None or app.config["MAX_CONTENT_LENGTH"] <= 0:
        raise ValueError("MAX_CONTENT_LENGTH must be a positive integer")

    configured_workspace_root = workspace_root
    if configured_workspace_root is None:
        configured_workspace_root = app.config.get("ARRAX_WORKSPACE_ROOT")
    if configured_workspace_root is None:
        configured_workspace_root = os.getenv("ARRAX_WORKSPACE_ROOT")
    configured_workspace_root = normalize_workspace_root(configured_workspace_root)

    owns_core = core is None
    if core is None:
        try:
            provider_config = ProviderConfig.from_values(
                mode=app.config.get("ARRAX_AI_MODE"),
                api_key=app.config.get("GEMINI_API_KEY"),
                model=app.config.get("GEMINI_MODEL"),
            )
        except ProviderConfigError as exc:
            raise ValueError(str(exc)) from exc
        core = AgentCore(
            db_path=str(app.config["ARRAX_DB_PATH"]),
            workspace_root=configured_workspace_root,
            planner=planner,
            provider=provider,
            provider_config=provider_config,
        )
    elif configured_workspace_root is not None and getattr(core, "workspace_root", None) is None:
        raise ValueError("workspace_root must be configured on an injected AgentCore")
    if system_service is None:
        system_service = app.config.get("ARRAX_SYSTEM_SERVICE")
    if system_service is None:
        system_disk_path = app.config.get("ARRAX_SYSTEM_DISK_PATH")
        if system_disk_path is None:
            system_disk_path = os.getenv("ARRAX_SYSTEM_DISK_PATH")
        system_service = SystemSnapshotService(
            disk_path=system_disk_path,
            process_limit=app.config["ARRAX_SYSTEM_PROCESS_LIMIT"],
            process_scan_limit=app.config["ARRAX_SYSTEM_PROCESS_SCAN_LIMIT"],
        )
    workflow_service = WorkflowService(core.store, core)
    memory_service = MemoryService(core.store)
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
        "provider_status": lambda: core.provider_status,
        "system_service": system_service,
        "workflow_service": workflow_service,
        "memory_service": memory_service,
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

    frontend_directory = FRONTEND_DIRECTORY

    @app.get("/")
    def dashboard():
        """Serve the dashboard entry point without making the factory a server."""

        index_path = frontend_directory / "index.html"
        if not index_path.is_file():
            return _error_response("not_found", "dashboard is not available", 404)
        return send_from_directory(str(frontend_directory), "index.html")

    @app.get("/assets/<path:filename>")
    def frontend_asset(filename: str):
        """Serve only files below the repository's frontend directory.

        ``send_from_directory`` rejects traversal outside the directory.  The
        explicit file and extension checks keep this route scoped to the
        dashboard's browser assets rather than turning it into a general file
        server.
        """

        requested = Path(filename)
        allowed_extensions = {".css", ".js"}
        if (
            requested.is_absolute()
            or ".." in requested.parts
            or requested.suffix.lower() not in allowed_extensions
            or not (frontend_directory / requested).is_file()
        ):
            return _error_response("not_found", "asset was not found", 404)
        return send_from_directory(str(frontend_directory), filename)

    @app.get("/api/health")
    def health():
        provider_status = core.provider_status
        return jsonify(
            {
                "service": "arrax-api",
                "status": "ready",
                "ready": True,
                "execution_mode": "deterministic_local",
                "provider_mode": provider_status["mode"],
                "provider_status": provider_status,
                "configured_model": provider_status["model"],
            }
        )

    @app.get("/api/capabilities")
    def capabilities():
        provider_status = core.provider_status
        payload = {
            "tools": list(core.registry.names()),
            "execution_mode": "deterministic_local",
            "provider_mode": provider_status["mode"],
            "provider_status": provider_status,
            "configured_model": provider_status["model"],
        }
        if getattr(core, "workspace_root", None) is not None:
            payload["workspace_enabled"] = True
        return jsonify(payload)

    @app.get("/api/system/snapshot")
    def system_snapshot():
        """Return one bounded, capability-aware local system observation."""

        return jsonify(system_service.snapshot())

    @app.get("/api/system/capabilities")
    def system_capabilities():
        """Return system metric support without collecting machine details."""

        return jsonify(system_service.capabilities())

    @app.get("/api/workflows")
    def list_workflows():
        """List persisted reusable workflows without running them."""

        return jsonify({"workflows": [serialize_workflow(item) for item in workflow_service.list()]})

    @app.post("/api/workflows")
    def create_workflow():
        payload = _json_object()
        unknown = set(payload) - {"name", "description", "goal", "version"}
        if unknown:
            raise APIError("invalid_request", "request contains unsupported fields", 400)
        missing = [field for field in ("name", "description", "goal") if field not in payload]
        if missing:
            raise APIError("missing_workflow_field", "workflow name, description, and goal are required", 400)
        try:
            workflow = workflow_service.create(
                name=payload["name"],
                description=payload["description"],
                goal=payload["goal"],
                version=payload.get("version", 1),
            )
        except WorkflowConflictError as exc:
            raise APIError("workflow_name_conflict", str(exc), 409) from exc
        except WorkflowValidationError as exc:
            raise APIError(exc.code, exc.message, 400) from exc
        except Exception as exc:
            raise APIError("internal_error", "workflow could not be saved", 500) from exc
        return jsonify(serialize_workflow(workflow)), 201

    @app.get("/api/workflows/<workflow_id>")
    def get_workflow(workflow_id: str):
        try:
            workflow = workflow_service.get(workflow_id)
        except WorkflowNotFoundError as exc:
            raise APIError("workflow_not_found", "workflow was not found", 404) from exc
        return jsonify(serialize_workflow(workflow))

    @app.delete("/api/workflows/<workflow_id>")
    def delete_workflow(workflow_id: str):
        try:
            workflow_service.delete(workflow_id)
        except WorkflowNotFoundError as exc:
            raise APIError("workflow_not_found", "workflow was not found", 404) from exc
        return jsonify({"deleted": True, "id": workflow_id})

    @app.post("/api/workflows/<workflow_id>/run")
    def run_workflow(workflow_id: str):
        payload = _json_object(allow_empty=True)
        if payload:
            raise APIError("invalid_request", "workflow run does not accept fields", 400)
        try:
            workflow = workflow_service.get(workflow_id)
        except WorkflowNotFoundError as exc:
            raise APIError("workflow_not_found", "workflow was not found", 404) from exc
        try:
            # A run creates a normal persisted AgentCore task. Saving a
            # workflow only validates its plan and never reaches execution.
            task = core.create_task(workflow.goal)
        except (DomainValidationError, PlanningError) as exc:
            raise APIError("workflow_run_failed", "workflow could not be converted into a task", 500) from exc
        except Exception as exc:
            raise APIError("internal_error", "workflow could not be run", 500) from exc
        if task.status.terminal:
            return _task_response(task, 201)
        try:
            started = manager.start(task.task_id)
        except WorkerCapacityError as exc:
            raise APIError("worker_capacity", "execution queue is full", 409) from exc
        except WorkerShutdownError as exc:
            raise APIError("worker_shutdown", "execution workers are unavailable", 409) from exc
        return _task_response(core.get_task(task.task_id), 202 if started or not task.status.terminal else 200)

    @app.get("/api/memory")
    def list_memory():
        """List only validated, JSON-safe user memory records."""

        return jsonify({"memory": [serialize_memory(item) for item in memory_service.list()]})

    @app.post("/api/memory")
    def save_memory():
        payload = _json_object()
        unknown = set(payload) - {"key", "value", "category"}
        if unknown:
            raise APIError("invalid_request", "request contains unsupported fields", 400)
        if "key" not in payload or "value" not in payload:
            raise APIError("missing_memory_field", "memory key and value are required", 400)
        try:
            record, created = memory_service.upsert(
                key=payload["key"],
                value=payload["value"],
                category=payload.get("category", "general"),
            )
        except MemoryValidationError as exc:
            raise APIError(exc.code, exc.message, 400) from exc
        except Exception as exc:
            raise APIError("internal_error", "memory could not be saved", 500) from exc
        return jsonify(serialize_memory(record)), 201 if created else 200

    @app.delete("/api/memory/<memory_id>")
    def delete_memory(memory_id: str):
        try:
            memory_service.delete(memory_id)
        except MemoryNotFoundError as exc:
            raise APIError("memory_not_found", "memory record was not found", 404) from exc
        return jsonify({"deleted": True, "id": memory_id})

    @app.post("/api/memory/reset")
    def reset_memory():
        payload = _json_object(allow_empty=True)
        if payload:
            raise APIError("invalid_request", "memory reset does not accept fields", 400)
        return jsonify({"deleted": memory_service.reset()})

    @app.get("/api/provider/status")
    def provider_status():
        """Return safe provider state; verification is opt-in per request."""

        raw_verify = request.args.get("verify")
        verify = False
        if raw_verify is not None:
            if raw_verify in {"1", "true", "yes"}:
                verify = True
            elif raw_verify in {"0", "false", "no"}:
                verify = False
            else:
                raise APIError("invalid_verify", "verify must be 0 or 1", 400)
        status = core.verify_provider() if verify else core.provider_status
        return jsonify({**status, "verification_requested": verify})

    @app.get("/api/evaluation/scenarios")
    def evaluation_scenarios():
        """Return the fixed catalog for the synchronous offline evaluation lab."""

        return jsonify(
            {
                "evaluation_type": "deterministic_regression",
                "mode": "offline_deterministic",
                "deterministic": True,
                "offline": True,
                "provider": "disabled",
                "workspace": "disabled",
                "scenarios": scenario_catalog(),
            }
        )

    @app.post("/api/evaluation/run")
    def evaluation_run():
        """Run a small local evaluation synchronously; no worker or provider is used."""

        payload = _json_object(allow_empty=True)
        unknown = set(payload) - {"scenario"}
        if unknown:
            raise APIError("invalid_request", "request contains unsupported fields", 400)
        selected = payload.get("scenario")
        if "scenario" in payload and (not isinstance(selected, str) or not selected.strip()):
            raise APIError("invalid_scenario", "scenario must be a non-empty catalog name", 400)
        try:
            report = run_evaluation(selected)
        except UnknownScenarioError as exc:
            raise APIError("unknown_scenario", "scenario is not in the evaluation catalog", 400) from exc
        except EvaluationError as exc:
            raise APIError("invalid_evaluation", "evaluation request could not be accepted", 400) from exc
        except Exception as exc:
            raise APIError("evaluation_failed", "evaluation could not be completed", 500) from exc
        return jsonify(report)

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

    @app.get("/api/tasks/history")
    def task_history():
        """List persisted task summaries without loading full task detail."""

        unknown = set(request.args) - {"limit", "offset"}
        if unknown:
            raise APIError("invalid_request", "request contains unsupported fields", 400)
        limit = _history_query_integer(
            "limit",
            default=DEFAULT_TASK_HISTORY_LIMIT,
            minimum=1,
            maximum=MAX_TASK_HISTORY_LIMIT,
        )
        offset = _history_query_integer(
            "offset",
            default=0,
            minimum=0,
            maximum=MAX_TASK_HISTORY_OFFSET,
        )
        summaries, total = core.list_task_summaries(limit=limit, offset=offset)
        return jsonify(
            {
                "tasks": [serialize_task_summary(summary) for summary in summaries],
                "limit": limit,
                "offset": offset,
                "total": total,
                "has_more": offset + len(summaries) < total,
                "overview": serialize_task_overview(core.store.tasks.overview()),
            }
        )

    @app.get("/api/tasks/overview")
    def task_overview():
        """Return measured aggregate task and recovery state."""

        return jsonify(serialize_task_overview(core.store.tasks.overview()))

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
    "serialize_memory",
    "serialize_step",
    "serialize_task",
    "serialize_task_overview",
    "serialize_task_summary",
    "serialize_workflow",
]
