"""Bounded, capability-aware local system measurements.

The service deliberately uses observation APIs only.  It never starts a shell,
opens arbitrary files, or returns paths, process IDs, command lines, or user
names.  ``psutil`` is an optional runtime backend so an embedding application
can still start safely when the dependency is unavailable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import shutil
from typing import Any

try:  # pragma: no cover - the fallback is exercised through injection.
    import psutil as _psutil
except ImportError:  # pragma: no cover - depends on the runtime environment.
    _psutil = None


psutil = _psutil

DEFAULT_PROCESS_LIMIT = 5
DEFAULT_PROCESS_SCAN_LIMIT = 50
MAX_PROCESS_LIMIT = 20
MAX_PROCESS_SCAN_LIMIT = 100
CPU_RECOMMENDATION_THRESHOLD = 90.0
MEMORY_RECOMMENDATION_THRESHOLD = 90.0
DISK_RECOMMENDATION_THRESHOLD = 90.0
_DEFAULT_BACKEND = object()
_PSUTIL_UNAVAILABLE = "optional psutil capability is unavailable"
_COLLECTION_FAILED = "metric collection failed safely"


def _unavailable(*fields: str, reason: str = _PSUTIL_UNAVAILABLE) -> dict[str, Any]:
    """Create a stable metric shape without inventing values."""

    result: dict[str, Any] = {
        "available": False,
        "status": "unavailable",
        "source": None,
        "reason": reason,
    }
    result.update({field: None for field in fields})
    return result


def _bounded_number(value: Any, *, maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return None
    if maximum is not None and numeric > maximum:
        return None
    return numeric


def _non_negative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _process_name(value: Any) -> str:
    """Return a bounded process basename rather than a possible executable path."""

    if not isinstance(value, str):
        return "unknown"
    name = re.split(r"[\\/]", value)[-1].strip()
    return (name or "unknown")[:128]


def _utc_timestamp(clock: Callable[[], datetime]) -> str:
    try:
        value = clock()
        if not isinstance(value, datetime):
            raise TypeError
    except Exception:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SystemSnapshotService:
    """Collect a small, non-blocking snapshot from a local observation backend.

    ``disk_path`` is an application-owned configuration value.  It is never
    accepted from an HTTP request and its value is never included in a result;
    responses expose only ``configured_path`` or ``working_directory``.
    ``process_scan_limit`` bounds how many process records are inspected, while
    ``process_limit`` bounds how many summaries are returned.
    """

    def __init__(
        self,
        *,
        disk_path: str | os.PathLike[str] | None = None,
        process_limit: int = DEFAULT_PROCESS_LIMIT,
        process_scan_limit: int = DEFAULT_PROCESS_SCAN_LIMIT,
        clock: Callable[[], datetime] | None = None,
        psutil_module: Any = _DEFAULT_BACKEND,
    ) -> None:
        if isinstance(process_limit, bool) or not isinstance(process_limit, int):
            raise ValueError("process_limit must be an integer")
        if not 1 <= process_limit <= MAX_PROCESS_LIMIT:
            raise ValueError(f"process_limit must be between 1 and {MAX_PROCESS_LIMIT}")
        if isinstance(process_scan_limit, bool) or not isinstance(process_scan_limit, int):
            raise ValueError("process_scan_limit must be an integer")
        if not 1 <= process_scan_limit <= MAX_PROCESS_SCAN_LIMIT:
            raise ValueError(
                f"process_scan_limit must be between 1 and {MAX_PROCESS_SCAN_LIMIT}"
            )

        self.process_limit = process_limit
        self.process_scan_limit = process_scan_limit
        configured_disk_path = os.fspath(disk_path) if disk_path is not None else None
        if isinstance(configured_disk_path, (str, bytes)) and not configured_disk_path.strip():
            configured_disk_path = None
        self._disk_path = configured_disk_path
        self._disk_target = "configured_path" if configured_disk_path is not None else "working_directory"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._psutil = psutil if psutil_module is _DEFAULT_BACKEND else psutil_module

    @property
    def disk_target(self) -> str:
        """Return the safe public label for the configured disk target."""

        return self._disk_target

    def _collect_cpu(self) -> dict[str, Any]:
        if self._psutil is None:
            return _unavailable("percent")
        try:
            value = _bounded_number(self._psutil.cpu_percent(interval=0.0), maximum=100.0)
        except Exception:
            value = None
        if value is None:
            return _unavailable("percent", reason=_COLLECTION_FAILED)
        return {"available": True, "status": "measured", "source": "psutil", "percent": value}

    def _collect_memory(self) -> dict[str, Any]:
        fields = ("total_bytes", "used_bytes", "percent")
        if self._psutil is None:
            return _unavailable(*fields)
        try:
            memory = self._psutil.virtual_memory()
            total = _non_negative_integer(getattr(memory, "total", None))
            used = _non_negative_integer(getattr(memory, "used", None))
            percent = _bounded_number(getattr(memory, "percent", None), maximum=100.0)
        except Exception:
            total = used = percent = None
        if total is None or used is None or percent is None or used > total:
            return _unavailable(*fields, reason=_COLLECTION_FAILED)
        return {
            "available": True,
            "status": "measured",
            "source": "psutil",
            "total_bytes": total,
            "used_bytes": used,
            "percent": percent,
        }

    def _collect_disk(self) -> dict[str, Any]:
        fields = ("total_bytes", "used_bytes", "free_bytes", "percent")
        target = self._disk_path if self._disk_path is not None else Path.cwd()
        try:
            usage = shutil.disk_usage(target)
            total = _non_negative_integer(usage.total)
            used = _non_negative_integer(usage.used)
            free = _non_negative_integer(usage.free)
        except Exception:
            total = used = free = None
        if (
            total is None
            or used is None
            or free is None
            or total <= 0
            or used > total
            or free > total
        ):
            return _unavailable(*fields, reason="disk usage could not be measured safely")
        percent = _bounded_number((used / total) * 100.0, maximum=100.0)
        if percent is None:
            return _unavailable(*fields, reason="disk usage could not be measured safely")
        return {
            "available": True,
            "status": "measured",
            "source": "shutil.disk_usage",
            "target": self._disk_target,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "percent": percent,
        }

    def _collect_network(self) -> dict[str, Any]:
        fields = ("bytes_sent", "bytes_received")
        if self._psutil is None:
            return _unavailable(*fields)
        try:
            counters = self._psutil.net_io_counters()
            sent = _non_negative_integer(getattr(counters, "bytes_sent", None))
            received = _non_negative_integer(getattr(counters, "bytes_recv", None))
        except Exception:
            sent = received = None
        if sent is None or received is None:
            return _unavailable(*fields, reason=_COLLECTION_FAILED)
        return {
            "available": True,
            "status": "measured",
            "source": "psutil",
            "bytes_sent": sent,
            "bytes_received": received,
        }

    def _collect_processes(self) -> dict[str, Any]:
        base = {
            "available": False,
            "status": "unavailable",
            "source": None,
            "reason": _PSUTIL_UNAVAILABLE,
            "items": [],
            "limit": self.process_limit,
            "sampled": 0,
            "truncated": False,
        }
        if self._psutil is None:
            return base

        try:
            process_iterator = self._psutil.process_iter(
                attrs=("name", "cpu_percent", "memory_percent")
            )
        except Exception:
            base["reason"] = _COLLECTION_FAILED
            return base

        items: list[dict[str, Any]] = []
        sampled = 0
        truncated = False
        iterator_failed = False
        try:
            for process in process_iterator:
                if sampled >= self.process_scan_limit:
                    truncated = True
                    break
                sampled += 1
                try:
                    info = getattr(process, "info", None)
                    if not isinstance(info, Mapping):
                        continue
                    cpu = _bounded_number(info.get("cpu_percent"))
                    memory = _bounded_number(info.get("memory_percent"), maximum=100.0)
                    if cpu is None or memory is None:
                        continue
                    items.append(
                        {
                            "name": _process_name(info.get("name")),
                            "cpu_percent": cpu,
                            "memory_percent": memory,
                        }
                    )
                except Exception:
                    # A process can exit or become inaccessible between the
                    # bounded iterator call and its metadata read.
                    continue
        except Exception:
            iterator_failed = True

        items.sort(key=lambda item: (item["cpu_percent"], item["memory_percent"]), reverse=True)
        if not items and iterator_failed:
            base["reason"] = _COLLECTION_FAILED
            base["sampled"] = sampled
            return base
        return {
            "available": True,
            "status": "measured",
            "source": "psutil",
            "items": items[: self.process_limit],
            "limit": self.process_limit,
            "sampled": sampled,
            "truncated": truncated or iterator_failed,
        }

    @staticmethod
    def _recommendation(
        *, code: str, metric: str, value: float, threshold: float, message: str
    ) -> dict[str, Any]:
        return {
            "code": code,
            "metric": metric,
            "observed": value,
            "threshold": {"operator": ">=", "value": threshold},
            "message": message,
        }

    def _recommendations(
        self,
        *,
        cpu: Mapping[str, Any],
        memory: Mapping[str, Any],
        disk: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        recommendations: list[dict[str, Any]] = []
        if cpu.get("status") == "measured" and cpu.get("percent", 0) >= CPU_RECOMMENDATION_THRESHOLD:
            recommendations.append(
                self._recommendation(
                    code="high_cpu",
                    metric="cpu.percent",
                    value=cpu["percent"],
                    threshold=CPU_RECOMMENDATION_THRESHOLD,
                    message="Review CPU-intensive work if utilization remains high.",
                )
            )
        if memory.get("status") == "measured" and memory.get("percent", 0) >= MEMORY_RECOMMENDATION_THRESHOLD:
            recommendations.append(
                self._recommendation(
                    code="high_memory",
                    metric="memory.percent",
                    value=memory["percent"],
                    threshold=MEMORY_RECOMMENDATION_THRESHOLD,
                    message="Review memory-intensive work if utilization remains high.",
                )
            )
        if disk.get("status") == "measured" and disk.get("percent", 0) >= DISK_RECOMMENDATION_THRESHOLD:
            recommendations.append(
                self._recommendation(
                    code="high_disk_usage",
                    metric="disk.percent",
                    value=disk["percent"],
                    threshold=DISK_RECOMMENDATION_THRESHOLD,
                    message="Review disk usage and reclaim space if utilization remains high.",
                )
            )
        return recommendations

    def snapshot(self) -> dict[str, Any]:
        """Return one bounded observation with explicit metric availability."""

        measured_at = _utc_timestamp(self._clock)
        cpu = self._collect_cpu()
        memory = self._collect_memory()
        disk = self._collect_disk()
        network = self._collect_network()
        processes = self._collect_processes()
        psutil_metrics = (cpu, memory, network, processes)
        available = self._psutil is not None and any(
            metric.get("status") == "measured" for metric in psutil_metrics
        )
        return {
            "measured_at": measured_at,
            "available": available,
            "cpu": cpu,
            "memory": memory,
            "disk": disk,
            "network": network,
            "processes": processes,
            "recommendations": self._recommendations(cpu=cpu, memory=memory, disk=disk),
        }

    def capabilities(self) -> dict[str, Any]:
        """Describe supported measurements without collecting a snapshot."""

        psutil_available = self._psutil is not None
        psutil_status = "available" if psutil_available else "unavailable"
        psutil_reason = None if psutil_available else _PSUTIL_UNAVAILABLE
        return {
            "service": "system_snapshot",
            "available": psutil_available,
            "backend": "psutil" if psutil_available else None,
            "capabilities": {
                "cpu": {
                    "available": psutil_available,
                    "status": psutil_status,
                    "source": "psutil" if psutil_available else None,
                    "blocking": False,
                    "reason": psutil_reason,
                },
                "memory": {
                    "available": psutil_available,
                    "status": psutil_status,
                    "source": "psutil" if psutil_available else None,
                    "blocking": False,
                    "reason": psutil_reason,
                },
                "disk": {
                    "available": True,
                    "status": "available",
                    "source": "shutil.disk_usage",
                    "blocking": False,
                    "target": self._disk_target,
                    "reason": None,
                },
                "network": {
                    "available": psutil_available,
                    "status": psutil_status,
                    "source": "psutil" if psutil_available else None,
                    "blocking": False,
                    "reason": psutil_reason,
                },
                "processes": {
                    "available": psutil_available,
                    "status": psutil_status,
                    "source": "psutil" if psutil_available else None,
                    "blocking": False,
                    "limit": self.process_limit,
                    "scan_limit": self.process_scan_limit,
                    "reason": psutil_reason,
                },
            },
            "limits": {
                "process_output": self.process_limit,
                "process_scan": self.process_scan_limit,
            },
        }


# A short alias keeps the injection surface convenient for callers embedding
# the service while retaining the more descriptive public class name.
SystemService = SystemSnapshotService


__all__ = ["SystemService", "SystemSnapshotService"]
