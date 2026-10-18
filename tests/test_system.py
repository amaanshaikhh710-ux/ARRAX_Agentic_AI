from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import arrax.system as system_module
from arrax import AgentCore, SystemSnapshotService, create_app


class FakeProcess:
    def __init__(self, name: str, cpu: float, memory: float) -> None:
        self.info = {
            "name": name,
            "cpu_percent": cpu,
            "memory_percent": memory,
        }


class FakePsutil:
    def __init__(self, processes: list[FakeProcess] | None = None) -> None:
        self.processes = processes or [FakeProcess("worker", 12.5, 3.0)]
        self.process_attrs = None

    def cpu_percent(self, *, interval: float) -> float:
        assert interval == 0.0
        return 92.5

    def virtual_memory(self):
        return SimpleNamespace(total=1000, used=910, percent=91.0)

    def net_io_counters(self):
        return SimpleNamespace(bytes_sent=1234, bytes_recv=5678)

    def process_iter(self, *, attrs):
        self.process_attrs = attrs
        return iter(self.processes)


def _fixed_disk_usage(_path):
    return SimpleNamespace(total=1000, used=950, free=50)


def test_snapshot_schema_is_measured_and_api_injection_is_supported(monkeypatch, tmp_path):
    monkeypatch.setattr(system_module.shutil, "disk_usage", _fixed_disk_usage)
    service = SystemSnapshotService(
        disk_path=tmp_path / "approved-target",
        psutil_module=FakePsutil(),
        clock=lambda: datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc),
    )

    snapshot = service.snapshot()
    assert snapshot["measured_at"] == "2026-10-18T12:00:00.000Z"
    assert snapshot["available"] is True
    assert snapshot["cpu"] == {
        "available": True,
        "status": "measured",
        "source": "psutil",
        "percent": 92.5,
    }
    assert snapshot["memory"]["total_bytes"] == 1000
    assert snapshot["memory"]["used_bytes"] == 910
    assert snapshot["memory"]["percent"] == 91.0
    assert snapshot["disk"]["target"] == "configured_path"
    assert snapshot["network"] == {
        "available": True,
        "status": "measured",
        "source": "psutil",
        "bytes_sent": 1234,
        "bytes_received": 5678,
    }
    assert snapshot["recommendations"]
    assert {item["code"] for item in snapshot["recommendations"]} >= {
        "high_cpu",
        "high_memory",
        "high_disk_usage",
    }
    json.dumps(snapshot)

    app = create_app(
        core=AgentCore(":memory:"),
        system_service=service,
        config={"TESTING": True},
    )
    try:
        response = app.test_client().get("/api/system/snapshot")
        assert response.status_code == 200
        assert response.get_json() == snapshot

        capabilities = app.test_client().get("/api/system/capabilities")
        assert capabilities.status_code == 200
        capabilities_body = capabilities.get_json()
        assert capabilities_body["service"] == "system_snapshot"
        assert capabilities_body["available"] is True
        assert capabilities_body["capabilities"]["processes"]["limit"] == 5
    finally:
        app.extensions["arrax"]["shutdown"]()


def test_unavailable_psutil_is_explicit_and_does_not_create_fake_values(monkeypatch, tmp_path):
    monkeypatch.setattr(system_module.shutil, "disk_usage", _fixed_disk_usage)
    service = SystemSnapshotService(disk_path=tmp_path, psutil_module=None)

    snapshot = service.snapshot()
    assert snapshot["available"] is False
    for metric_name in ("cpu", "memory", "network", "processes"):
        assert snapshot[metric_name]["available"] is False
        assert snapshot[metric_name]["status"] == "unavailable"
    assert snapshot["cpu"]["percent"] is None
    assert snapshot["memory"]["total_bytes"] is None
    assert snapshot["network"]["bytes_sent"] is None
    assert snapshot["processes"]["items"] == []
    assert snapshot["disk"]["status"] == "measured"
    assert [item["code"] for item in snapshot["recommendations"]] == ["high_disk_usage"]

    capabilities = service.capabilities()
    assert capabilities["available"] is False
    assert capabilities["backend"] is None
    assert capabilities["capabilities"]["cpu"]["status"] == "unavailable"
    assert capabilities["capabilities"]["processes"]["available"] is False
    assert capabilities["capabilities"]["disk"]["available"] is True


def test_process_output_and_scan_are_bounded_and_only_contain_safe_summary_fields():
    fake = FakePsutil(
        [
            FakeProcess(f"C:\\private\\worker-{index}.exe", float(index), float(index) / 2)
            for index in range(100)
        ]
    )
    service = SystemSnapshotService(
        psutil_module=fake,
        process_limit=3,
        process_scan_limit=7,
    )

    processes = service.snapshot()["processes"]
    assert fake.process_attrs == ("name", "cpu_percent", "memory_percent")
    assert processes["sampled"] == 7
    assert processes["truncated"] is True
    assert len(processes["items"]) == 3
    assert all(set(item) == {"name", "cpu_percent", "memory_percent"} for item in processes["items"])
    assert all("private" not in item["name"] and "\\" not in item["name"] for item in processes["items"])
    assert processes["items"][0]["name"] == "worker-6.exe"


def test_configured_paths_and_process_paths_never_leak_into_serialized_output(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(system_module.shutil, "disk_usage", _fixed_disk_usage)
    secret_path = tmp_path / "machine-specific" / "private-root"
    service = SystemSnapshotService(
        disk_path=secret_path,
        psutil_module=FakePsutil(
            [FakeProcess(r"C:\Users\Admin\private\secret-agent.exe", 1.0, 1.0)]
        ),
    )

    serialized = json.dumps({"snapshot": service.snapshot(), "capabilities": service.capabilities()})
    assert str(secret_path) not in serialized
    assert "C:\\Users\\Admin\\private" not in serialized
    assert "secret-agent.exe" in serialized
    assert "command" not in serialized.lower()
    assert "user" not in serialized.lower()
