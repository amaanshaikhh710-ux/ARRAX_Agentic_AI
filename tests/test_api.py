from __future__ import annotations

import time

import pytest

from arrax import AgentCore, TaskStatus, create_app


@pytest.fixture
def app(tmp_path):
    core = AgentCore(db_path=str(tmp_path / "api.sqlite"))
    application = create_app(core=core, config={"TESTING": True})
    try:
        yield application
    finally:
        application.extensions["arrax"]["shutdown"]()
        core.close()


@pytest.fixture
def client(app):
    return app.test_client()


def wait_for_terminal(client, task_id: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        latest = response.get_json()
        if latest["status"] in {status.value for status in TaskStatus if status.terminal}:
            return latest
        time.sleep(0.01)
    pytest.fail(f"task did not reach a terminal state: {latest}")


def wait_for_status(client, task_id: str, status: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        latest = response.get_json()
        if latest["status"] == status:
            return latest
        time.sleep(0.01)
    pytest.fail(f"task did not reach {status}: {latest}")


def test_health_and_capabilities_report_local_deterministic_mode(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.get_json() == {
        "service": "arrax-api",
        "status": "ready",
        "ready": True,
        "execution_mode": "deterministic_local",
        "provider_mode": "none",
    }

    capabilities = client.get("/api/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.get_json() == {
        "tools": [
            "fixture.approval",
            "fixture.delay",
            "fixture.fail_once",
            "fixture.success",
            "fixture.verify",
        ],
        "execution_mode": "deterministic_local",
        "provider_mode": "none",
    }


def test_create_start_poll_and_read_event_history(client):
    created = client.post("/api/tasks", json={"goal": {"type": "success", "message": "api"}})

    assert created.status_code == 202
    task = created.get_json()
    assert task["task_id"]
    assert task["status"] in {"queued", "running", "completed"}

    finished = wait_for_terminal(client, task["task_id"])
    assert finished["status"] == "completed"
    step = finished["plan"][0]
    assert {
        "step_id",
        "name",
        "tool_name",
        "dependencies",
        "risk",
        "approval_required",
        "approved",
        "status",
        "attempts",
        "max_attempts",
        "timeout_seconds",
        "result",
        "verification_evidence",
    } <= set(step)
    assert step["verification_evidence"]["verified"] is True

    fetched = client.get(f"/api/tasks/{task['task_id']}")
    assert fetched.status_code == 200
    assert fetched.get_json() == finished

    events_response = client.get(f"/api/tasks/{task['task_id']}/events")
    assert events_response.status_code == 200
    events = events_response.get_json()
    assert events["task_id"] == task["task_id"]
    assert events["events"]
    assert {"event_id", "task_id", "step_id", "event_type", "payload", "created_at"} <= set(
        events["events"][-1]
    )
    assert events["events"][-1]["event_type"] == "task.completed"


def test_create_without_start_then_explicit_run(client):
    created = client.post(
        "/api/tasks",
        json={"goal": {"type": "success", "message": "manual"}, "start": False},
    )
    assert created.status_code == 201
    task = created.get_json()
    assert task["status"] == "queued"

    started = client.post(f"/api/tasks/{task['task_id']}/run")
    assert started.status_code == 202
    assert started.get_json()["task_id"] == task["task_id"]
    assert wait_for_terminal(client, task["task_id"])["status"] == "completed"


def test_factory_can_create_and_close_a_core_for_configured_sqlite_path(tmp_path):
    db_path = tmp_path / "configured.sqlite"
    application = create_app(db_path=str(db_path), config={"TESTING": True})
    try:
        response = application.test_client().post(
            "/api/tasks",
            json={"goal": {"type": "success", "message": "persisted"}, "start": False},
        )
        assert response.status_code == 201
        task_id = response.get_json()["task_id"]
    finally:
        application.extensions["arrax"]["shutdown"]()

    restored = AgentCore(db_path=str(db_path))
    try:
        assert restored.get_task(task_id).status is TaskStatus.QUEUED
    finally:
        restored.close()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({}, "invalid_json"),
        ({"data": "{", "content_type": "application/json"}, "invalid_json"),
        ({"json": {"goal": []}}, "invalid_goal"),
        ({"json": {"goal": {}}}, "invalid_goal"),
        ({"json": {"goal": {"type": "success"}, "unsafe": True}}, "invalid_request"),
    ],
)
def test_task_creation_rejects_invalid_request_shapes(client, kwargs, code):
    response = client.post("/api/tasks", **kwargs)

    assert response.status_code == 400
    body = response.get_json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert "Traceback" not in response.get_data(as_text=True)


def test_task_creation_rejects_oversized_body(client):
    oversized = {"goal": {"type": "success", "message": "x" * (64 * 1024)}}

    response = client.post("/api/tasks", json=oversized)

    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "request_too_large"
    assert "Traceback" not in response.get_data(as_text=True)


def test_unknown_task_and_unknown_route_use_structured_errors(client):
    missing = client.get("/api/tasks/does-not-exist")
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "task_not_found"
    assert "Traceback" not in missing.get_data(as_text=True)

    events = client.get("/api/tasks/does-not-exist/events")
    assert events.status_code == 404
    assert events.get_json()["error"]["code"] == "task_not_found"

    unknown_route = client.get("/api/not-a-route")
    assert unknown_route.status_code == 404
    assert unknown_route.get_json()["error"]["code"] == "not_found"


def test_unexpected_failure_has_safe_500_error(app, client, monkeypatch):
    def fail(_task_id):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(app.extensions["arrax_core"], "get_task", fail)

    response = client.get("/api/tasks/anything")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {"code": "internal_error", "message": "internal server error"}
    }
    assert "secret internal detail" not in response.get_data(as_text=True)
    assert "Traceback" not in response.get_data(as_text=True)


def test_background_worker_converts_unexpected_core_error_to_terminal_failure(app, client, monkeypatch):
    original = app.extensions["arrax_core"].run_task

    def fail(_task_id):
        raise RuntimeError("unexpected internal detail")

    monkeypatch.setattr(app.extensions["arrax_core"], "run_task", fail)
    created = client.post(
        "/api/tasks",
        json={"goal": {"type": "success", "message": "worker failure"}, "start": False},
    )
    task_id = created.get_json()["task_id"]

    client.post(f"/api/tasks/{task_id}/run")
    failed = wait_for_terminal(client, task_id)

    assert failed["status"] == "failed"
    assert failed["error"] == "task execution failed unexpectedly"
    assert failed["result"] == {"error_type": "internal_execution_error"}
    assert "unexpected internal detail" not in client.get(f"/api/tasks/{task_id}").get_data(as_text=True)
    monkeypatch.setattr(app.extensions["arrax_core"], "run_task", original)


def test_approval_endpoint_grants_step_and_resumes_execution(client):
    created = client.post("/api/tasks", json={"goal": {"type": "approval", "message": "approve"}})
    task_id = created.get_json()["task_id"]
    awaiting = wait_for_status(client, task_id, "awaiting_approval")
    assert awaiting["plan"][0]["approved"] is False
    assert awaiting["plan"][0]["status"] == "awaiting_approval"

    approved = client.post(f"/api/tasks/{task_id}/approve", json={"step_id": "approval-action"})
    assert approved.status_code == 202
    finished = wait_for_terminal(client, task_id)
    assert finished["status"] == "completed"
    assert finished["plan"][0]["approved"] is True


def test_cancellation_requests_core_cancellation_for_bounded_delay(client):
    created = client.post(
        "/api/tasks",
        json={"goal": {"type": "delay", "seconds": 0.5, "timeout_seconds": 1.0}},
    )
    task_id = created.get_json()["task_id"]
    wait_for_status(client, task_id, "running")

    duplicate_run = client.post(f"/api/tasks/{task_id}/run")
    assert duplicate_run.status_code == 202

    requested = client.post(f"/api/tasks/{task_id}/cancel")
    assert requested.status_code in {200, 202}
    assert requested.get_json()["cancellation_requested"] is True
    cancelled = wait_for_terminal(client, task_id)
    assert cancelled["status"] == "cancelled"
    assert cancelled["error"]


def test_approval_validation_and_shutdown_are_safe(app, client):
    created = client.post(
        "/api/tasks", json={"goal": {"type": "approval"}, "start": False}
    ).get_json()
    response = client.post(
        f"/api/tasks/{created['task_id']}/approve", json={"step_id": "missing"}
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_step"
    assert "Traceback" not in response.get_data(as_text=True)

    delayed = client.post(
        "/api/tasks",
        json={"goal": {"type": "delay", "seconds": 0.1, "timeout_seconds": 1.0}},
    )
    assert delayed.status_code == 202
    app.extensions["arrax"]["shutdown"]()
    assert app.extensions["arrax_worker_manager"].active_count == 0
