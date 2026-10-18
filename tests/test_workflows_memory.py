from __future__ import annotations

import json
import time

import pytest

from arrax import AgentCore, MemoryRecord, SQLiteStore, Workflow, create_app
from arrax.domain import TaskStatus


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


@pytest.fixture
def app_and_core(tmp_path):
    core = AgentCore(db_path=str(tmp_path / "workflow-memory.sqlite"))
    application = create_app(core=core, config={"TESTING": True})
    try:
        yield application, core
    finally:
        application.extensions["arrax"]["shutdown"]()
        core.close()


def test_workflow_and_memory_schema_survive_store_close_and_reload(tmp_path):
    db_path = tmp_path / "reload.sqlite"
    first = SQLiteStore(str(db_path))
    workflow = first.workflows.create(
        Workflow(
            id="workflow-1",
            name="Reload workflow",
            description="A persisted deterministic workflow",
            goal={"type": "success", "message": "reload"},
            version=1,
        )
    )
    memory, created = first.memory.upsert(
        MemoryRecord(id="memory-1", key="favorite_color", value="blue", category="preference")
    )
    first.close()

    second = SQLiteStore(str(db_path))
    try:
        assert second.workflows.get(workflow.id) == workflow
        assert second.workflows.list()[0].goal == {"type": "success", "message": "reload"}
        assert created is True
        assert second.memory.get(memory.id) == memory
    finally:
        second.close()


def test_workflow_save_does_not_create_or_execute_a_task_and_run_uses_worker(app_and_core):
    application, core = app_and_core
    client = application.test_client()

    saved = client.post(
        "/api/workflows",
        json={
            "name": "Offline success",
            "description": "Run the safe fixture later",
            "goal": {"type": "success", "message": "workflow run"},
        },
    )
    assert saved.status_code == 201
    workflow = saved.get_json()
    assert workflow["version"] == 1
    assert workflow["id"]
    assert core.store.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    started = client.post(f"/api/workflows/{workflow['id']}/run", json={})
    assert started.status_code == 202
    task = started.get_json()
    assert task["task_id"]
    assert task["goal"] == workflow["goal"]
    finished = wait_for_terminal(client, task["task_id"])
    assert finished["status"] == "completed"
    assert finished["plan"][0]["verification_evidence"]["verified"] is True

    deleted = client.delete(f"/api/workflows/{workflow['id']}")
    assert deleted.status_code == 200
    assert client.get(f"/api/workflows/{workflow['id']}").status_code == 404


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            {
                "name": "Bad fields",
                "description": "Rejected",
                "goal": {"type": "success"},
                "extra": True,
            },
            "invalid_request",
        ),
        (
            {
                "name": "Unknown tool",
                "description": "Rejected",
                "goal": {"type": "tool", "tool_name": "shell.execute", "input": {"command": "x"}},
            },
            "workflow_goal_invalid",
        ),
        (
            {
                "name": "Unknown goal field",
                "description": "Rejected",
                "goal": {"type": "success", "message": "x", "command": "x"},
            },
            "workflow_goal_invalid",
        ),
        (
            {
                "name": "Bad version",
                "description": "Rejected",
                "version": 99,
                "goal": {"type": "success"},
            },
            "unsupported_workflow_version",
        ),
    ],
)
def test_workflow_validation_rejects_unknown_fields_unsafe_tools_and_bad_versions(app_and_core, payload, code):
    client = app_and_core[0].test_client()

    response = client.post("/api/workflows", json=payload)

    assert response.status_code == 400
    body = response.get_json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert "shell.execute" not in response.get_data(as_text=True)


def test_memory_crud_upsert_and_explicit_reset(app_and_core):
    client = app_and_core[0].test_client()

    workflow = client.post(
        "/api/workflows",
        json={
            "name": "Retained workflow",
            "description": "Reset must not remove workflows",
            "goal": {"type": "success"},
        },
    ).get_json()

    created = client.post(
        "/api/memory", json={"key": "timezone", "value": "UTC", "category": "preference"}
    )
    assert created.status_code == 201
    first = created.get_json()
    updated = client.post(
        "/api/memory", json={"key": "timezone", "value": "Europe/London", "category": "preference"}
    )
    assert updated.status_code == 200
    assert updated.get_json()["id"] == first["id"]

    added = client.post("/api/memory", json={"key": "language", "value": {"name": "English"}})
    assert added.status_code == 201
    listed = client.get("/api/memory")
    assert listed.status_code == 200
    assert {item["key"] for item in listed.get_json()["memory"]} == {"timezone", "language"}
    json.dumps(listed.get_json(), allow_nan=False)

    deleted = client.delete(f"/api/memory/{first['id']}")
    assert deleted.status_code == 200
    assert client.get("/api/memory").get_json()["memory"][0]["key"] == "language"

    reset = client.post("/api/memory/reset", json={})
    assert reset.status_code == 200
    assert reset.get_json() == {"deleted": 1}
    assert client.get("/api/memory").get_json() == {"memory": []}
    assert client.get(f"/api/workflows/{workflow['id']}").status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"key": "api_key", "value": "redacted"},
        {"key": "safe_note", "value": "the bearer token is redacted"},
        {"key": "safe_note", "value": {"private_key": "redacted"}},
        {"key": "safe_note", "value": "password=redacted"},
    ],
)
def test_memory_rejects_secret_like_keys_and_values_without_echoing_them(app_and_core, payload):
    client = app_and_core[0].test_client()

    response = client.post("/api/memory", json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "memory_secret_rejected"
    assert "redacted" not in response.get_data(as_text=True)
    assert client.get("/api/memory").get_json() == {"memory": []}


def test_memory_rejects_machine_paths_without_echoing_them(app_and_core):
    client = app_and_core[0].test_client()

    response = client.post("/api/memory", json={"key": "note", "value": r"C:\Users\Admin\private.txt"})

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "memory_path_rejected"
    assert "C:\\Users\\Admin" not in response.get_data(as_text=True)
    assert client.get("/api/memory").get_json() == {"memory": []}


def test_workflow_and_memory_api_errors_are_structured_and_do_not_leak_paths(app_and_core):
    client = app_and_core[0].test_client()

    missing_workflow = client.get("/api/workflows/missing")
    assert missing_workflow.status_code == 404
    assert missing_workflow.get_json() == {
        "error": {"code": "workflow_not_found", "message": "workflow was not found"}
    }
    reset_with_fields = client.post("/api/memory/reset", json={"confirm": True})
    assert reset_with_fields.status_code == 400
    assert reset_with_fields.get_json()["error"]["code"] == "invalid_request"
    missing_memory = client.delete("/api/memory/missing")
    assert missing_memory.status_code == 404
    assert "ARRAX_Agentic_AI" not in missing_memory.get_data(as_text=True)
