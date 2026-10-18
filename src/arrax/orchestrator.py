"""Synchronous plan, policy, act, observe, verify, and recovery flow."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Mapping

from .domain import PlanStep, RiskLevel, Task, TaskStatus
from .planner import DeterministicPlanner, PlanningError
from .provider import GeminiProvider, HybridPlanner, ProviderConfig, ProviderMode, ProviderStatus, _coerce_status
from .storage import Event, SQLiteStore, utc_now
from .tools import ToolInputError, ToolRegistry, UnknownToolError, default_registry
from .workspace import normalize_workspace_root


class TaskNotFoundError(KeyError):
    """Raised when a public operation names an unknown task."""


class CancellationToken:
    def __init__(self) -> None:
        self.event = threading.Event()

    def cancel(self) -> None:
        self.event.set()


class AgentCore:
    """Small public service for deterministic offline task execution."""

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        store: SQLiteStore | None = None,
        registry: ToolRegistry | None = None,
        planner: Any | None = None,
        provider: Any | None = None,
        provider_config: ProviderConfig | None = None,
        max_timeout_seconds: float = 10.0,
        workspace_root: str | None = None,
    ) -> None:
        self.store = store or SQLiteStore(db_path)
        self.workspace_root = normalize_workspace_root(workspace_root)
        self.registry = registry or default_registry(self.workspace_root)
        if self.workspace_root is None and any(name.startswith("workspace.") for name in self.registry.names()):
            raise ValueError("workspace tools require an explicitly configured workspace_root")
        deterministic_planner = DeterministicPlanner(workspace_enabled=self.workspace_root is not None)
        if planner is None:
            configured_provider = provider if provider is not None else GeminiProvider(provider_config or ProviderConfig.from_env())
            self.provider = configured_provider
            self.planner = HybridPlanner(
                deterministic_planner,
                configured_provider,
                registry=self.registry,
            )
        else:
            self.planner = planner
            self.provider = provider
        self.max_timeout_seconds = max_timeout_seconds
        self._active_lock = threading.RLock()
        self._active_tokens: dict[str, CancellationToken] = {}

    @property
    def provider_status(self) -> dict[str, Any]:
        """Return safe provider state without triggering verification or I/O."""

        if isinstance(self.planner, HybridPlanner):
            return self.planner.status().to_dict()
        if self.provider is not None:
            status = self.provider.status() if callable(getattr(self.provider, "status", None)) else getattr(self.provider, "status", None)
            if isinstance(status, ProviderStatus):
                return status.to_dict()
            if isinstance(status, Mapping):
                provider_config = getattr(self.provider, "config", None)
                return _coerce_status(
                    status,
                    config_mode=getattr(provider_config, "mode", ProviderMode.DETERMINISTIC),
                    model=getattr(provider_config, "model", ProviderConfig().model),
                ).to_dict()
        return ProviderStatus(
            mode=ProviderMode.DETERMINISTIC,
            configured=False,
            verified=False,
            model=ProviderConfig().model,
            message="Deterministic planner selected; Gemini is disabled.",
            last_mode=ProviderMode.DETERMINISTIC,
            configured_mode=ProviderMode.DETERMINISTIC,
        ).to_dict()

    def verify_provider(self) -> dict[str, Any]:
        """Explicitly verify the configured provider, if one is available."""

        if isinstance(self.planner, HybridPlanner):
            return self.planner.verify_provider().to_dict()
        if self.provider is not None and callable(getattr(self.provider, "verify", None)):
            result = self.provider.verify()
            if isinstance(result, ProviderStatus):
                return result.to_dict()
            if isinstance(result, Mapping):
                provider_config = getattr(self.provider, "config", None)
                return _coerce_status(
                    result,
                    config_mode=getattr(provider_config, "mode", ProviderMode.DETERMINISTIC),
                    model=getattr(provider_config, "model", ProviderConfig().model),
                ).to_dict()
        return self.provider_status

    def create_task(self, goal: Mapping[str, Any]) -> Task:
        """Persist a queued task and its deterministic plan."""

        if not isinstance(goal, Mapping):
            raise PlanningError("goal must be an object")
        task_id = uuid.uuid4().hex
        task = Task(task_id=task_id, goal=dict(goal), status=TaskStatus.QUEUED)
        task = self.store.tasks.create(task)
        self._event(task_id, "task.created", {"status": task.status.value})
        try:
            task = task.with_updates(status=TaskStatus.PLANNING)
            task = self._save_task(task, "task.planning")
            goal_type = task.goal.get("type", task.goal.get("kind"))
            if isinstance(goal_type, str) and goal_type.strip().lower() in {
                "workspace_report",
                "workspace_readiness_report",
            } and self.workspace_root is None:
                raise PlanningError("workspace_report requires an explicitly configured ARRAX_WORKSPACE_ROOT")
            plan = self.planner.plan(task.goal)
            task = task.with_updates(plan=plan, status=TaskStatus.QUEUED, error=None)
            self.store.steps.replace_for_task(task.task_id, plan)
            plan_event = {"step_count": len(plan)}
            provider_status = self.provider_status
            if provider_status.get("last_mode"):
                plan_event["planner_mode"] = provider_status["last_mode"]
            task = self._save_task(task, "plan.created", plan_event)
            if isinstance(goal_type, str) and goal_type.strip().lower() in {
                "workspace_report",
                "workspace_readiness_report",
            }:
                required_workspace_tools = {
                    "workspace.list",
                    "workspace.create_report",
                    "workspace.verify_snapshot",
                    "workspace.verify_report",
                }
                missing_workspace_tools = required_workspace_tools - set(self.registry.names())
                if missing_workspace_tools:
                    raise PlanningError("workspace_report requires all workspace tools to be available")
            self._validate_plan_references(task)
            return task
        except Exception as exc:
            error = self._safe_error(exc)
            task = task.with_updates(status=TaskStatus.FAILED, error=error)
            self._save_task(task, "task.failed", {"phase": "planning", "error": error})
            return task

    def validate_deterministic_goal(self, goal: Mapping[str, Any]) -> tuple[PlanStep, ...]:
        """Validate a reusable goal without creating or running a task.

        Workflow persistence must not execute tools or create task history.  It
        still needs the same deterministic planner, registry allowlist, risk
        floors, and verifier contract that a task will use later, so this
        method performs those checks against an in-memory task value only.
        """

        if not isinstance(goal, Mapping):
            raise PlanningError("goal must be an object")
        planner = DeterministicPlanner(workspace_enabled=self.workspace_root is not None)
        plan = planner.plan(goal)
        validation_task = Task(task_id="workflow-validation", goal=dict(goal), plan=plan)
        self._validate_plan_references(validation_task)
        for step in plan:
            try:
                self.registry.validate(step.tool_name, step.input)
                verifier = self.registry.get(step.verifier_tool)
            except (UnknownToolError, ToolInputError) as exc:
                raise PlanningError("workflow goal references an unavailable tool") from exc
            if not getattr(verifier, "is_verifier", False):
                raise PlanningError("workflow goal references an invalid verifier")
            verification_input = dict(step.verification_input or {})
            verification_input["action_output"] = {}
            try:
                self.registry.validate(step.verifier_tool, verification_input)
            except (UnknownToolError, ToolInputError) as exc:
                raise PlanningError("workflow goal references an invalid verifier") from exc
        return plan

    def run_task(self, task_id: str) -> Task:
        """Run one task synchronously until terminal or approval boundary."""

        task = self._get_task(task_id)
        if task.status.terminal:
            return task
        if task.cancellation_requested:
            return self._cancel(task, "cancellation requested before execution")
        token = CancellationToken()
        with self._active_lock:
            self._active_tokens[task_id] = token
        watcher_stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch_cancellation,
            args=(task_id, token, watcher_stop),
            name=f"arrax-cancel-watch-{task_id}",
            daemon=True,
        )
        watcher.start()
        try:
            return self._run(task, token)
        finally:
            watcher_stop.set()
            # Do not let the short-lived watcher query SQLite after an
            # embedding test or application closes the store.
            watcher.join(timeout=0.2)
            with self._active_lock:
                self._active_tokens.pop(task_id, None)

    def approve_task(self, task_id: str, step_id: str | None = None) -> Task:
        """Grant explicit approval to one or all pending approval-gated steps."""

        task = self._get_task(task_id)
        if task.status.terminal:
            return task
        selected = set([step_id]) if step_id is not None else None
        updated_steps: list[PlanStep] = []
        granted = 0
        for step in task.plan:
            needs_approval = self._requires_approval(step)
            if needs_approval and not step.approved and (selected is None or step.step_id in selected):
                updated_steps.append(step.with_updates(approved=True, status=TaskStatus.QUEUED))
                granted += 1
            else:
                updated_steps.append(step)
        if selected is not None and granted == 0 and not any(step.step_id == step_id for step in task.plan):
            raise KeyError(f"unknown step: {step_id}")
        task = task.with_updates(plan=tuple(updated_steps), status=TaskStatus.QUEUED)
        self.store.steps.replace_for_task(task.task_id, task.plan)
        task = self._save_task(task, "approval.granted", {"step_id": step_id, "count": granted})
        return task

    def cancel_task(self, task_id: str) -> Task:
        """Request cancellation and wake a currently running tool if present."""

        task = self._get_task(task_id)
        if task.status.terminal:
            return task
        with self._active_lock:
            token = self._active_tokens.get(task_id)
        if token is not None:
            token.cancel()
        # Re-read after signalling the worker.  Saving the stale task loaded
        # above can otherwise race the worker's terminal cancellation update
        # and write RUNNING back over a completed cancellation.
        task = self._get_task(task_id)
        if task.status.terminal:
            return task
        task = task.with_updates(cancellation_requested=True)
        if token is None:
            task = self._save_task(task, "task.cancel_requested", {})
            return self._cancel(task, "cancelled by caller")
        self._save_task(task, "task.cancel_requested", {})
        latest = self._get_task(task_id)
        if latest.status.terminal:
            return latest
        return self._cancel(latest, "cancelled by caller")

    def get_task(self, task_id: str) -> Task:
        return self._get_task(task_id)

    def get_events(self, task_id: str) -> list[Event]:
        self._get_task(task_id)
        return self.store.events.list(task_id)

    def fail_task(self, task_id: str, reason: str = "task execution failed unexpectedly") -> Task:
        """Persist a safe terminal failure for an unexpected worker error.

        The asynchronous API boundary uses this guard when a worker catches an
        exception outside the normal tool/task error paths. A task must never
        remain apparently active forever merely because its worker crashed.
        Internal exception details are intentionally not persisted or exposed.
        """

        task = self._get_task(task_id)
        if task.status.terminal:
            return task
        steps = [
            step.with_updates(
                status=TaskStatus.FAILED,
                result={"error_type": "internal_execution_error"},
            )
            if step.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            else step
            for step in task.plan
        ]
        return self._finish(
            task.with_updates(plan=tuple(steps)),
            TaskStatus.FAILED,
            {"error_type": "internal_execution_error"},
            reason,
        )

    def close(self) -> None:
        self.store.close()

    def _run(self, task: Task, token: CancellationToken) -> Task:
        if not task.plan:
            return self._fail(task, "plan contains no steps")
        task = self._save_task(task.with_updates(status=TaskStatus.RUNNING), "task.running")
        steps = list(task.plan)
        completed_outputs: dict[str, Any] = {}

        while True:
            if token.event.is_set() or self.store.tasks.cancellation_requested(task.task_id):
                return self._cancel(task, "cancelled during execution", steps)
            pending = [step for step in steps if step.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}]
            if not pending:
                break

            progressed = False
            waiting_for_approval = False
            for index, step in enumerate(steps):
                if step.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    if step.status == TaskStatus.COMPLETED and step.result is not None:
                        completed_outputs[step.step_id] = dict(step.result)
                    continue
                if any(self._step_by_id(steps, dep).status != TaskStatus.COMPLETED for dep in step.dependencies):
                    dependency = next(
                        (self._step_by_id(steps, dep) for dep in step.dependencies if self._step_by_id(steps, dep).status != TaskStatus.COMPLETED),
                        None,
                    )
                    if dependency is not None and dependency.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                        steps[index] = step.with_updates(status=TaskStatus.FAILED, result={"error_type": "dependency_failed"})
                        self.store.steps.update(task.task_id, steps[index])
                        self._event(task.task_id, "step.failed", {"error_type": "dependency_failed"}, step.step_id)
                        progressed = True
                    continue
                if self._requires_approval(step) and not step.approved:
                    steps[index] = step.with_updates(status=TaskStatus.AWAITING_APPROVAL)
                    self.store.steps.update(task.task_id, steps[index])
                    self._event(task.task_id, "approval.required", {"risk": step.risk.value}, step.step_id)
                    waiting_for_approval = True
                    continue
                self._set_task_status(task.task_id, TaskStatus.RUNNING)
                steps[index] = step.with_updates(status=TaskStatus.RUNNING)
                self.store.steps.update(task.task_id, steps[index])
                self._event(task.task_id, "step.running", {"tool": step.tool_name}, step.step_id)
                outcome = self._execute_step(task, steps[index], token)
                steps[index] = outcome
                progressed = True
                if outcome.status == TaskStatus.CANCELLED:
                    return self._cancel(task, "cancelled during tool execution", steps)
                if outcome.status == TaskStatus.FAILED:
                    self.store.steps.update(task.task_id, outcome)
                    continue
                self.store.steps.update(task.task_id, outcome)
                completed_outputs[outcome.step_id] = dict(outcome.result or {})

            if waiting_for_approval and not progressed:
                task = task.with_updates(plan=tuple(steps), status=TaskStatus.AWAITING_APPROVAL)
                return self._save_task(task, "task.awaiting_approval", {})
            if not progressed:
                return self._fail(task.with_updates(plan=tuple(steps)), "plan dependencies cannot be resolved")

        task = task.with_updates(plan=tuple(steps))
        failed = [step for step in steps if step.status == TaskStatus.FAILED]
        cancelled = [step for step in steps if step.status == TaskStatus.CANCELLED]
        if cancelled:
            return self._cancel(task, "a plan step was cancelled", steps)
        if failed:
            if any(step.status == TaskStatus.COMPLETED for step in steps):
                return self._finish(task, TaskStatus.PARTIALLY_COMPLETED, {"steps": completed_outputs}, "some steps failed")
            return self._fail(task, self._step_error(failed[0]), steps=steps)
        if not all(step.verification_evidence for step in steps):
            return self._fail(task, "completion requires verification evidence", steps=steps)
        return self._finish(task, TaskStatus.COMPLETED, {"steps": completed_outputs})

    def _execute_step(self, task: Task, step: PlanStep, token: CancellationToken) -> PlanStep:
        current = step
        for attempt in range(step.attempts + 1, step.max_attempts + 1):
            if token.event.is_set():
                return current.with_updates(status=TaskStatus.CANCELLED, result={"error_type": "cancelled"})
            current = current.with_updates(attempts=attempt, status=TaskStatus.RUNNING)
            self.store.steps.update(task.task_id, current)
            self._event(task.task_id, "tool.started", {"tool": current.tool_name, "attempt": attempt}, current.step_id)
            try:
                action = self.registry.run(
                    current.tool_name,
                    current.input,
                    token.event,
                    timeout_seconds=min(current.timeout_seconds, self.max_timeout_seconds),
                    approved=current.approved,
                )
            except UnknownToolError as exc:
                self._event(task.task_id, "tool.rejected", {"error_type": "unknown_tool", "error": str(exc)}, current.step_id)
                return current.with_updates(
                    status=TaskStatus.FAILED,
                    result={"error_type": "unknown_tool", "error": str(exc)},
                )
            except ToolInputError as exc:
                self._event(task.task_id, "tool.rejected", {"error_type": "invalid_input", "error": str(exc)}, current.step_id)
                return current.with_updates(
                    status=TaskStatus.FAILED,
                    result={"error_type": "invalid_input", "error": str(exc)},
                )
            if action.cancelled:
                return current.with_updates(status=TaskStatus.CANCELLED, result={"error_type": "cancelled"})
            if action.timed_out:
                observation = {"error_type": "timeout", "error": action.error or "tool timed out"}
                self._event(task.task_id, "tool.observed", observation, current.step_id)
                if attempt < current.max_attempts:
                    current = current.with_updates(status=TaskStatus.RECOVERING, result=observation)
                    self.store.steps.update(task.task_id, current)
                    self._set_task_status(task.task_id, TaskStatus.RECOVERING)
                    self._event(task.task_id, "step.recovering", {"attempt": attempt}, current.step_id)
                    continue
                return current.with_updates(status=TaskStatus.FAILED, result=observation)
            if action.error is not None or action.result is None or not action.result.success:
                observation = {
                    "error_type": "tool_failure",
                    "error": action.error or (action.result.error if action.result else "tool failed"),
                    "output": dict(action.result.output) if action.result else {},
                }
                self._event(task.task_id, "tool.observed", observation, current.step_id)
                if attempt < current.max_attempts:
                    current = current.with_updates(status=TaskStatus.RECOVERING, result=observation)
                    self.store.steps.update(task.task_id, current)
                    self._set_task_status(task.task_id, TaskStatus.RECOVERING)
                    self._event(task.task_id, "step.recovering", {"attempt": attempt}, current.step_id)
                    continue
                return current.with_updates(status=TaskStatus.FAILED, result=observation)

            output = dict(action.result.output)
            current = current.with_updates(result=output, status=TaskStatus.VERIFYING)
            self.store.steps.update(task.task_id, current)
            self._set_task_status(task.task_id, TaskStatus.VERIFYING)
            self._event(task.task_id, "step.verifying", {"tool": current.verifier_tool}, current.step_id)
            try:
                verifier = self.registry.get(current.verifier_tool)
            except UnknownToolError as exc:
                self._event(task.task_id, "verification.rejected", {"error_type": "unknown_verifier", "error": str(exc)}, current.step_id)
                return current.with_updates(status=TaskStatus.FAILED, result={"error_type": "unknown_verifier", "error": str(exc)})
            if not getattr(verifier, "is_verifier", False):
                error = f"tool is not an approved verifier: {current.verifier_tool}"
                self._event(task.task_id, "verification.rejected", {"error_type": "invalid_verifier", "error": error}, current.step_id)
                return current.with_updates(status=TaskStatus.FAILED, result={"error_type": "invalid_verifier", "error": error})
            verification_input = dict(current.verification_input or {})
            # The verifier must inspect the actual action output. A plan cannot
            # replace that evidence with a caller-supplied claim.
            verification_input["action_output"] = output
            try:
                verification = self.registry.run(
                    current.verifier_tool,
                    verification_input,
                    token.event,
                    timeout_seconds=min(current.timeout_seconds, self.max_timeout_seconds),
                )
            except UnknownToolError as exc:
                self._event(task.task_id, "verification.rejected", {"error_type": "unknown_verifier", "error": str(exc)}, current.step_id)
                return current.with_updates(status=TaskStatus.FAILED, result={"error_type": "unknown_verifier", "error": str(exc)})
            except ToolInputError as exc:
                self._event(task.task_id, "verification.rejected", {"error_type": "invalid_verifier_input", "error": str(exc)}, current.step_id)
                return current.with_updates(status=TaskStatus.FAILED, result={"error_type": "invalid_verifier_input", "error": str(exc)})
            if verification.cancelled:
                return current.with_updates(status=TaskStatus.CANCELLED, result={"error_type": "cancelled"})
            if verification.timed_out:
                failure = {"error_type": "verification_timeout", "error": verification.error or "verifier timed out"}
            elif verification.error is not None or verification.result is None or not verification.result.success:
                failure = {
                    "error_type": "verification_failed",
                    "error": verification.error or (verification.result.error if verification.result else "verification failed"),
                }
            else:
                evidence = dict(verification.result.output)
                if not evidence:
                    failure = {"error_type": "verification_failed", "error": "verifier returned no evidence"}
                else:
                    self._event(task.task_id, "verification.completed", evidence, current.step_id)
                    return current.with_updates(status=TaskStatus.COMPLETED, verification_evidence=evidence)
            self._event(task.task_id, "verification.failed", failure, current.step_id)
            if attempt < current.max_attempts:
                current = current.with_updates(status=TaskStatus.RECOVERING, result=failure)
                self.store.steps.update(task.task_id, current)
                self._set_task_status(task.task_id, TaskStatus.RECOVERING)
                self._event(task.task_id, "step.recovering", {"attempt": attempt}, current.step_id)
                continue
            return current.with_updates(status=TaskStatus.FAILED, result=failure)
        return current.with_updates(status=TaskStatus.FAILED, result={"error_type": "retry_exhausted"})

    def _watch_cancellation(self, task_id: str, token: CancellationToken, stop: threading.Event) -> None:
        while not stop.is_set() and not token.event.is_set():
            try:
                if self.store.tasks.cancellation_requested(task_id):
                    token.cancel()
                    return
            except KeyError:
                return
            stop.wait(0.01)

    def _requires_approval(self, step: PlanStep) -> bool:
        required_risk = step.risk
        try:
            required_risk = self.registry.risk_for(step.tool_name)
        except UnknownToolError:
            pass
        return step.approval_required or required_risk in {RiskLevel.SENSITIVE, RiskLevel.DESTRUCTIVE}

    def _validate_plan_references(self, task: Task) -> None:
        ids = {step.step_id for step in task.plan}
        for step in task.plan:
            missing = set(step.dependencies) - ids
            if missing:
                raise PlanningError(f"step {step.step_id!r} has missing dependencies: {sorted(missing)}")
            if step.step_id in step.dependencies:
                raise PlanningError(f"step {step.step_id!r} depends on itself")
            try:
                required_risk = self.registry.risk_for(step.tool_name)
            except UnknownToolError:
                continue
            if self._risk_rank(step.risk) < self._risk_rank(required_risk):
                raise PlanningError(
                    f"step {step.step_id!r} risk {step.risk.value} is below the tool minimum {required_risk.value}"
                )

    @staticmethod
    def _risk_rank(risk: RiskLevel) -> int:
        return {
            RiskLevel.READ_ONLY: 0,
            RiskLevel.REVERSIBLE: 1,
            RiskLevel.SENSITIVE: 2,
            RiskLevel.DESTRUCTIVE: 3,
        }[risk]

    def _step_by_id(self, steps: list[PlanStep], step_id: str) -> PlanStep:
        for step in steps:
            if step.step_id == step_id:
                return step
        raise PlanningError(f"unknown dependency: {step_id}")

    def _get_task(self, task_id: str) -> Task:
        task = self.store.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def _save_task(self, task: Task, event_type: str, payload: dict[str, Any] | None = None) -> Task:
        saved = self.store.tasks.update(task)
        self._event(saved.task_id, event_type, payload or {"status": saved.status.value})
        return saved

    def _set_task_status(self, task_id: str, status: TaskStatus) -> None:
        current = self._get_task(task_id)
        if current.status.terminal:
            return
        self._save_task(current.with_updates(status=status), "task.status", {"status": status.value})

    def _event(self, task_id: str, event_type: str, payload: dict[str, Any], step_id: str | None = None) -> Event:
        return self.store.events.append(task_id, event_type, payload, step_id)

    def _cancel(self, task: Task, reason: str, steps: list[PlanStep] | None = None) -> Task:
        current_steps = steps if steps is not None else list(task.plan)
        updated_steps = [
            step.with_updates(status=TaskStatus.CANCELLED, result={"error_type": "cancelled"})
            if step.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}
            else step
            for step in current_steps
        ]
        for step in updated_steps:
            self.store.steps.update(task.task_id, step)
        task = task.with_updates(
            plan=tuple(updated_steps),
            status=TaskStatus.CANCELLED,
            error=reason,
            cancellation_requested=True,
        )
        saved = self._save_task(task, "task.cancelled", {"reason": reason})
        return saved

    def _fail(self, task: Task, error: str, steps: list[PlanStep] | None = None) -> Task:
        if steps is not None:
            task = task.with_updates(plan=tuple(steps))
        status = TaskStatus.PARTIALLY_COMPLETED if any(step.status == TaskStatus.COMPLETED for step in task.plan) else TaskStatus.FAILED
        return self._finish(task, status, {"error": error}, error)

    def _finish(self, task: Task, status: TaskStatus, result: dict[str, Any], error: str | None = None) -> Task:
        task = task.with_updates(status=status, result=result, error=error)
        event_type = {
            TaskStatus.COMPLETED: "task.completed",
            TaskStatus.PARTIALLY_COMPLETED: "task.partially_completed",
            TaskStatus.FAILED: "task.failed",
        }.get(status, "task.finished")
        return self._save_task(task, event_type, {"status": status.value, **result})

    def _step_error(self, step: PlanStep) -> str:
        if step.result and step.result.get("error"):
            return str(step.result["error"])
        return f"step {step.step_id} failed"

    def _safe_error(self, exc: Exception) -> str:
        if isinstance(exc, (PlanningError, ValueError, KeyError)):
            return str(exc)
        return exc.__class__.__name__


def create_default_core(
    db_path: str = ":memory:",
    *,
    workspace_root: str | None = None,
    planner: Any | None = None,
    provider: Any | None = None,
    provider_config: ProviderConfig | None = None,
) -> AgentCore:
    """Convenience factory for the public API."""

    return AgentCore(
        db_path=db_path,
        workspace_root=workspace_root,
        planner=planner,
        provider=provider,
        provider_config=provider_config,
    )
