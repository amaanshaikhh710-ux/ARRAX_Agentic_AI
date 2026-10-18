from __future__ import annotations

import json
import os
from pathlib import Path
import threading

import pytest

from arrax import AgentCore, RiskLevel, TaskStatus, create_app
from arrax.tools import ToolInputError
from arrax.workspace import REPORT_FILENAME, REPORT_MARKER


def run_tool(core: AgentCore, name: str, value: dict, *, approved: bool = False):
    return core.registry.run(name, value, threading.Event(), approved=approved)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "approved-workspace"
    root.mkdir()
    (root / "notes").mkdir()
    (root / "notes" / "readme.md").write_text("ARRAX workspace\nneedle here\n", encoding="utf-8")
    return root


@pytest.fixture
def workspace_core(workspace_root: Path, tmp_path: Path):
    core = AgentCore(db_path=str(tmp_path / "workspace.sqlite"), workspace_root=str(workspace_root))
    try:
        yield core
    finally:
        core.close()


def test_workspace_paths_reject_traversal_and_absolute_paths(workspace_core, workspace_root):
    for path in ("../outside.txt", "notes/../../outside.txt", str(workspace_root / "notes")):
        with pytest.raises(ToolInputError):
            workspace_core.registry.validate("workspace.read_text", {"path": path})

    outside = workspace_root.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ToolInputError) as rejected:
        workspace_core.registry.run("workspace.read_text", {"path": "../outside.txt"}, threading.Event())
    assert "absolute" not in str(rejected.value)
    assert str(workspace_root) not in str(rejected.value)


def test_workspace_symlink_escape_is_rejected_when_supported(workspace_core, workspace_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace_root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks are not supported: {exc}")

    listed = run_tool(workspace_core, "workspace.list", {"path": "escape"})
    read = run_tool(workspace_core, "workspace.read_text", {"path": "escape/secret.txt"})
    assert listed.result is not None and not listed.result.success and "symlink" in (listed.result.error or "")
    assert read.result is not None and not read.result.success and "symlink" in (read.result.error or "")


def test_workspace_read_is_bounded_and_rejects_binary(workspace_core, workspace_root):
    (workspace_root / "large.txt").write_text("x" * 32, encoding="utf-8")
    (workspace_root / "binary.dat").write_bytes(b"\x00\x01not text")

    oversized = run_tool(workspace_core, "workspace.read_text", {"path": "large.txt", "max_bytes": 8})
    binary = run_tool(workspace_core, "workspace.read_text", {"path": "binary.dat"})
    assert oversized.result is not None and not oversized.result.success and "size" in (oversized.result.error or "")
    assert binary.result is not None and not binary.result.success and "binary" in (binary.result.error or "")
    with pytest.raises(ToolInputError):
        workspace_core.registry.validate("workspace.read_text", {"path": "large.txt", "max_bytes": 0})


def test_workspace_search_limits_files_results_and_bytes(workspace_core, workspace_root):
    for index in range(5):
        (workspace_root / f"match-{index}.txt").write_text("needle\n" * 3, encoding="utf-8")

    result = run_tool(
        workspace_core,
        "workspace.search",
        {"query": "needle", "max_files": 2, "max_results": 2, "max_bytes": 20},
    )
    assert result.result is not None
    output = result.result.output
    assert len(output["results"]) <= 2
    assert output["files_scanned"] <= 2
    assert output["bytes_scanned"] <= 20
    assert output["truncated"] is True


def test_report_is_root_scoped_deterministic_and_does_not_overwrite(workspace_core, workspace_root):
    first = run_tool(workspace_core, "workspace.create_report", {}, approved=True)
    assert first.result is not None
    output = first.result.output
    report = workspace_root / REPORT_FILENAME
    assert report.is_file()
    assert output["path"] == REPORT_FILENAME
    assert output["bytes"] == report.stat().st_size
    assert output["marker"] == REPORT_MARKER
    assert str(workspace_root) not in json.dumps(output)
    assert str(workspace_root) not in report.read_text(encoding="utf-8")

    rejected = run_tool(workspace_core, "workspace.create_report", {}, approved=True)
    assert rejected.result is not None and not rejected.result.success
    assert "already exists" in (rejected.result.error or "")
    original = report.read_bytes()
    not_approved = run_tool(
        workspace_core,
        "workspace.create_report",
        {"path": REPORT_FILENAME, "overwrite": True},
    )
    assert not_approved.result is None
    assert "approval" in (not_approved.error or "")
    assert report.read_bytes() == original


def test_report_verifier_uses_actual_file_not_forged_verified_flag(workspace_core, workspace_root):
    fake = workspace_root / "fake.md"
    fake.write_text("verified: true\n", encoding="utf-8")
    result = run_tool(
        workspace_core,
        "workspace.verify_report",
        {"path": "fake.md", "action_output": {"path": "fake.md", "verified": True}},
    )
    assert result.result is not None and not result.result.success
    assert "marker" in (result.result.error or "")

    missing = run_tool(
        workspace_core,
        "workspace.verify_report",
        {"path": REPORT_FILENAME, "action_output": {"path": REPORT_FILENAME, "verified": True}},
    )
    assert missing.result is not None and not missing.result.success


def test_registry_risk_floor_rejects_read_only_label_for_workspace_writer(workspace_core):
    task = workspace_core.create_task(
        {
            "type": "tool",
            "tool_name": "workspace.create_report",
            "input": {},
            "risk": "read_only",
            "verifier_tool": "workspace.verify_report",
        }
    )
    assert task.status is TaskStatus.FAILED
    assert "below the tool minimum" in (task.error or "")
    direct = run_tool(workspace_core, "workspace.create_report", {}, approved=False)
    assert direct.result is None
    assert "approval" in (direct.error or "")


def test_no_workspace_registry_remains_fixture_only():
    core = AgentCore()
    try:
        assert core.registry.names() == (
            "fixture.approval",
            "fixture.delay",
            "fixture.fail_once",
            "fixture.success",
            "fixture.verify",
        )
        task = core.create_task({"type": "workspace_report"})
        assert task.status is TaskStatus.FAILED
        assert "ARRAX_WORKSPACE_ROOT" in (task.error or "")
    finally:
        core.close()


def test_configured_registry_has_workspace_tools_and_report_goal_verifies(workspace_core, workspace_root):
    assert {
        "workspace.list",
        "workspace.read_text",
        "workspace.search",
        "workspace.create_report",
        "workspace.verify_report",
        "workspace.verify_snapshot",
    } <= set(workspace_core.registry.names())
    assert workspace_core.registry.risk_for("workspace.create_report") is RiskLevel.SENSITIVE
    assert workspace_core.registry.get("workspace.verify_report").is_verifier is True

    task = workspace_core.create_task({"type": "workspace_report"})
    assert [step.tool_name for step in task.plan] == ["workspace.list", "workspace.create_report"]
    awaiting = workspace_core.run_task(task.id)
    assert awaiting.status is TaskStatus.AWAITING_APPROVAL
    assert awaiting.plan[0].status is TaskStatus.COMPLETED
    assert awaiting.plan[0].verification_evidence["verified"] is True
    assert not (workspace_root / REPORT_FILENAME).exists()

    workspace_core.approve_task(task.id, "workspace-report")
    finished = workspace_core.run_task(task.id)
    assert finished.status is TaskStatus.COMPLETED
    assert (workspace_root / REPORT_FILENAME).is_file()
    report_step = finished.plan[1]
    assert report_step.verification_evidence == {
        "verified": True,
        "path": REPORT_FILENAME,
        "bytes": (workspace_root / REPORT_FILENAME).stat().st_size,
        "marker": REPORT_MARKER,
    }
    assert str(workspace_root) not in json.dumps(workspace_core.get_events(task.id), default=str)


def test_configured_app_reports_safe_workspace_capabilities(tmp_path, monkeypatch):
    root = tmp_path / "configured"
    root.mkdir()
    monkeypatch.setenv("ARRAX_WORKSPACE_ROOT", str(root))
    application = create_app(db_path=str(tmp_path / "api.sqlite"), config={"TESTING": True})
    try:
        response = application.test_client().get("/api/capabilities")
        assert response.status_code == 200
        body = response.get_json()
        assert body["workspace_enabled"] is True
        assert "workspace.create_report" in body["tools"]
        assert str(root) not in json.dumps(body)
    finally:
        application.extensions["arrax"]["shutdown"]()
