from __future__ import annotations

import threading
import time

import pytest

from arrax import AgentCore, RiskLevel, TaskStatus


@pytest.fixture
def core(tmp_path):
    agent = AgentCore(db_path=str(tmp_path / "arrax.sqlite"))
    try:
        yield agent
    finally:
        agent.close()


def test_successful_lifecycle_has_verification_evidence(core):
    task = core.create_task({"type": "success", "message": "hello"})

    assert task.status is TaskStatus.QUEUED
    assert task.plan[0].risk is RiskLevel.READ_ONLY

    finished = core.run_task(task.id)

    assert finished.status is TaskStatus.COMPLETED
    assert finished.plan[0].status is TaskStatus.COMPLETED
    assert finished.plan[0].attempts == 1
    assert finished.plan[0].verification_evidence == {
        "verified": True,
        "observed_fixture": "success",
    }
    event_types = [event.event_type for event in core.get_events(task.id)]
    assert "plan.created" in event_types
    assert "tool.started" in event_types
    assert "verification.completed" in event_types
    assert event_types[-1] == "task.completed"


def test_approval_requirement_pauses_then_explicit_grant_runs(core):
    task = core.create_task({"type": "approval_required", "message": "sensitive"})

    awaiting = core.run_task(task.id)

    assert awaiting.status is TaskStatus.AWAITING_APPROVAL
    assert awaiting.plan[0].status is TaskStatus.AWAITING_APPROVAL
    assert awaiting.plan[0].approval_required is True
    assert awaiting.plan[0].approved is False
    assert not any(event.event_type == "tool.started" for event in core.get_events(task.id))

    granted = core.approve_task(task.id)
    assert granted.plan[0].approved is True
    finished = core.run_task(task.id)
    assert finished.status is TaskStatus.COMPLETED
    assert any(event.event_type == "approval.granted" for event in core.get_events(task.id))


def test_unknown_tool_is_rejected_without_execution(core):
    task = core.create_task(
        {
            "type": "tool",
            "tool_name": "shell.execute",
            "input": {"command": "echo unsafe"},
        }
    )

    finished = core.run_task(task.id)

    assert finished.status is TaskStatus.FAILED
    assert finished.plan[0].result["error_type"] == "unknown_tool"
    assert not any(event.event_type == "verification.completed" for event in core.get_events(task.id))


def test_invalid_tool_input_fails_before_tool_execution(core):
    task = core.create_task(
        {
            "type": "tool",
            "tool_name": "fixture.success",
            "input": {"message": 123},
        }
    )

    finished = core.run_task(task.id)

    assert finished.status is TaskStatus.FAILED
    assert finished.plan[0].result["error_type"] == "invalid_input"
    assert finished.plan[0].attempts == 1


def test_verifier_cannot_be_replaced_with_an_action_tool(core):
    task = core.create_task(
        {
            "type": "tool",
            "tool_name": "fixture.success",
            "input": {"message": "action"},
            "verifier_tool": "fixture.success",
            "verification_input": {"message": "pretend verified", "verified": True},
        }
    )

    finished = core.run_task(task.id)

    assert finished.status is TaskStatus.FAILED
    assert finished.plan[0].result["error_type"] == "invalid_verifier"
    assert not any(event.event_type == "task.completed" for event in core.get_events(task.id))


def test_verifier_receives_actual_action_output(core):
    task = core.create_task(
        {
            "type": "tool",
            "tool_name": "fixture.success",
            "input": {"message": "not verified", "verified": False},
            "verification_input": {"action_output": {"verified": True}},
        }
    )

    finished = core.run_task(task.id)

    assert finished.status is TaskStatus.FAILED
    assert finished.plan[0].result["error_type"] == "verification_failed"
    assert finished.plan[0].verification_evidence is None


def test_failure_injection_uses_bounded_recovery(core):
    task = core.create_task({"type": "failure_recovery", "key": "case-1"})

    finished = core.run_task(task.id)

    assert finished.status is TaskStatus.COMPLETED
    assert finished.plan[0].attempts == 2
    assert finished.plan[0].verification_evidence
    event_types = [event.event_type for event in core.get_events(task.id)]
    assert "step.recovering" in event_types
    assert "task.status" in event_types


def test_verification_failure_cannot_be_reported_as_completion(core):
    task = core.create_task({"type": "verification_failure", "message": "bad evidence"})

    finished = core.run_task(task.id)

    assert finished.status is TaskStatus.FAILED
    assert finished.plan[0].status is TaskStatus.FAILED
    assert finished.plan[0].result["error_type"] == "verification_failed"
    assert finished.plan[0].verification_evidence is None
    assert not any(event.event_type == "task.completed" for event in core.get_events(task.id))


def test_structured_plan_honors_dependencies_and_risk(core):
    task = core.create_task(
        {
            "type": "structured_plan",
            "steps": [
                {
                    "id": "first",
                    "tool": "fixture.success",
                    "input": {"message": "first"},
                    "risk": "read_only",
                },
                {
                    "id": "second",
                    "tool": "fixture.success",
                    "input": {"message": "second"},
                    "risk": "reversible",
                    "dependencies": ["first"],
                },
            ],
        }
    )

    finished = core.run_task(task.id)

    assert finished.status is TaskStatus.COMPLETED
    assert [step.status for step in finished.plan] == [TaskStatus.COMPLETED, TaskStatus.COMPLETED]
    assert finished.plan[1].risk is RiskLevel.REVERSIBLE
    started_steps = [event.step_id for event in core.get_events(task.id) if event.event_type == "tool.started"]
    assert started_steps == ["first", "second"]


def test_cyclic_plan_is_rejected_before_step_persistence_or_execution(core):
    task = core.create_task(
        {
            "type": "structured_plan",
            "steps": [
                {
                    "id": "first",
                    "tool": "fixture.success",
                    "input": {"message": "first"},
                    "dependencies": ["second"],
                },
                {
                    "id": "second",
                    "tool": "fixture.success",
                    "input": {"message": "second"},
                    "dependencies": ["first"],
                },
            ],
        }
    )

    assert task.status is TaskStatus.FAILED
    assert task.error == "plan dependencies contain a cycle"
    assert task.plan == ()
    assert core.store.steps.list(task.id) == []
    assert not any(event.event_type == "tool.started" for event in core.get_events(task.id))
    assert core.get_events(task.id)[-1].event_type == "task.failed"


def test_failed_independent_step_produces_partial_completion(core):
    task = core.create_task(
        {
            "type": "structured_plan",
            "steps": [
                {
                    "id": "completed",
                    "tool": "fixture.success",
                    "input": {"message": "done"},
                },
                {
                    "id": "rejected",
                    "tool": "unknown.tool",
                    "input": {},
                },
            ],
        }
    )

    finished = core.run_task(task.id)

    assert finished.status is TaskStatus.PARTIALLY_COMPLETED
    assert [step.status for step in finished.plan] == [TaskStatus.COMPLETED, TaskStatus.FAILED]
    assert core.get_events(task.id)[-1].event_type == "task.partially_completed"


def test_timeout_is_classified_and_returns_within_bound(core):
    task = core.create_task({"type": "timeout", "seconds": 0.4, "timeout_seconds": 0.02})

    started = time.monotonic()
    finished = core.run_task(task.id)
    elapsed = time.monotonic() - started

    assert elapsed < 0.3
    assert finished.status is TaskStatus.FAILED
    assert finished.plan[0].result["error_type"] == "timeout"
    assert any(event.event_type == "tool.observed" for event in core.get_events(task.id))


def test_cancellation_before_run_is_terminal(core):
    task = core.create_task({"type": "success", "message": "cancel me"})

    cancelled = core.cancel_task(task.id)

    assert cancelled.status is TaskStatus.CANCELLED
    assert all(step.status is TaskStatus.CANCELLED for step in cancelled.plan)
    assert core.run_task(task.id).status is TaskStatus.CANCELLED
    assert any(event.event_type == "task.cancelled" for event in core.get_events(task.id))


def test_cancellation_during_bounded_tool_execution(core):
    task = core.create_task({"type": "timeout", "seconds": 0.5, "timeout_seconds": 1.0})
    result: list = []

    worker = threading.Thread(target=lambda: result.append(core.run_task(task.id)))
    worker.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if core.get_task(task.id).status is TaskStatus.RUNNING:
            break
        time.sleep(0.005)
    core.cancel_task(task.id)
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert result[0].status is TaskStatus.CANCELLED
    assert core.get_task(task.id).status is TaskStatus.CANCELLED


def test_sqlite_persistence_and_event_history(tmp_path):
    db_path = tmp_path / "persisted.sqlite"
    first = AgentCore(db_path=str(db_path))
    task = first.create_task({"type": "success", "message": "persist"})
    first.run_task(task.id)
    before = first.get_events(task.id)
    first.close()

    second = AgentCore(db_path=str(db_path))
    try:
        restored = second.get_task(task.id)
        after = second.get_events(task.id)
    finally:
        second.close()

    assert restored.status is TaskStatus.COMPLETED
    assert restored.plan[0].verification_evidence
    assert len(after) == len(before)
    assert after[-1].event_type == "task.completed"


def test_registry_is_allowlisted_and_contains_no_unsafe_execution():
    core = AgentCore()
    try:
        names = core.registry.names()
        assert names == (
            "fixture.approval",
            "fixture.delay",
            "fixture.fail_once",
            "fixture.success",
            "fixture.verify",
        )
        assert not any(name in names for name in ("shell", "filesystem", "desktop", "network"))
    finally:
        core.close()


def test_invalid_goal_is_persisted_as_safe_planning_failure(core):
    task = core.create_task({"type": "not-recognized"})

    assert task.status is TaskStatus.FAILED
    assert task.error == "unrecognized goal type: not-recognized"
    assert core.get_events(task.id)[-1].event_type == "task.failed"
