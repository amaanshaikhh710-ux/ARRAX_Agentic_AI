"""SQLite persistence boundary using only the Python standard library."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from .domain import PlanStep, RiskLevel, Task, TaskStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


@dataclass(frozen=True)
class Event:
    event_id: int | None
    task_id: str
    event_type: str
    payload: dict[str, Any]
    step_id: str | None
    created_at: str


@dataclass(frozen=True)
class Workflow:
    """A reusable, validated structured goal."""

    id: str
    name: str
    description: str
    goal: dict[str, Any]
    created_at: str | None = None
    updated_at: str | None = None
    version: int = 1

    @property
    def workflow_id(self) -> str:
        """Compatibility alias for callers that use the domain name."""

        return self.id


@dataclass(frozen=True)
class MemoryRecord:
    """A user-controlled JSON-safe memory record."""

    id: str
    key: str
    value: Any
    category: str
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def memory_id(self) -> str:
        """Compatibility alias for callers that use the domain name."""

        return self.id


class SQLiteStore:
    """Own one SQLite connection and expose task, step, and event repositories."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize_schema()
        self.tasks = TaskRepository(self)
        self.steps = StepRepository(self)
        self.events = EventRepository(self)
        self.workflows = WorkflowRepository(self)
        self.memory = MemoryRepository(self)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _initialize_schema(self) -> None:
        with self._lock:
            self.connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    goal_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    task_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    approval_required INTEGER NOT NULL,
                    approved INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    timeout_seconds REAL NOT NULL,
                    verifier_tool TEXT NOT NULL,
                    verification_input_json TEXT,
                    attempts INTEGER NOT NULL,
                    result_json TEXT,
                    verification_evidence_json TEXT,
                    status TEXT NOT NULL,
                    PRIMARY KEY (task_id, step_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS events_task_id_idx ON events(task_id, event_id);
                CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    goal_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workflows_updated_at_idx ON workflows(updated_at, workflow_id);
                CREATE TABLE IF NOT EXISTS memory_records (
                    memory_id TEXT PRIMARY KEY,
                    memory_key TEXT NOT NULL UNIQUE,
                    value_json TEXT NOT NULL,
                    category TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_records_updated_at_idx ON memory_records(updated_at, memory_id);
                """
            )

    def close(self) -> None:
        with self._lock:
            self.connection.close()


class TaskRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create(self, task: Task) -> Task:
        now = task.created_at or utc_now()
        updated = task.updated_at or now
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO tasks
                (task_id, goal_json, status, result_json, error, cancellation_requested, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.task_id,
                    _json(task.goal),
                    task.status.value,
                    _json(task.result) if task.result is not None else None,
                    task.error,
                    int(task.cancellation_requested),
                    now,
                    updated,
                ),
            )
        return task.with_updates(created_at=now, updated_at=updated)

    def get(self, task_id: str) -> Task | None:
        with self.store._lock:
            row = self.store.connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        steps = tuple(self.store.steps.list(task_id))
        return Task(
            task_id=row["task_id"],
            goal=_loads(row["goal_json"]),
            status=TaskStatus(row["status"]),
            plan=steps,
            result=_loads(row["result_json"]),
            error=row["error"],
            cancellation_requested=bool(row["cancellation_requested"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update(self, task: Task) -> Task:
        updated = utc_now()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """UPDATE tasks SET status = ?, result_json = ?, error = ?,
                cancellation_requested = ?, updated_at = ? WHERE task_id = ?""",
                (
                    task.status.value,
                    _json(task.result) if task.result is not None else None,
                    task.error,
                    int(task.cancellation_requested),
                    updated,
                    task.task_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown task: {task.task_id}")
        return task.with_updates(updated_at=updated)

    def cancellation_requested(self, task_id: str) -> bool:
        with self.store._lock:
            row = self.store.connection.execute(
                "SELECT cancellation_requested FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        return bool(row[0])


class StepRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def replace_for_task(self, task_id: str, steps: tuple[PlanStep, ...]) -> None:
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM steps WHERE task_id = ?", (task_id,))
            for position, step in enumerate(steps):
                conn.execute(
                    """INSERT INTO steps
                    (task_id, step_id, position, name, tool_name, input_json, dependencies_json,
                     risk, approval_required, approved, max_attempts, timeout_seconds, verifier_tool,
                     verification_input_json, attempts, result_json, verification_evidence_json, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task_id,
                        step.step_id,
                        position,
                        step.name,
                        step.tool_name,
                        _json(step.input),
                        _json(list(step.dependencies)),
                        step.risk.value,
                        int(step.approval_required),
                        int(step.approved),
                        step.max_attempts,
                        step.timeout_seconds,
                        step.verifier_tool,
                        _json(step.verification_input) if step.verification_input is not None else None,
                        step.attempts,
                        _json(step.result) if step.result is not None else None,
                        _json(step.verification_evidence) if step.verification_evidence is not None else None,
                        step.status.value,
                    ),
                )

    def list(self, task_id: str) -> list[PlanStep]:
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT * FROM steps WHERE task_id = ? ORDER BY position", (task_id,)
            ).fetchall()
        return [
            PlanStep(
                step_id=row["step_id"],
                name=row["name"],
                tool_name=row["tool_name"],
                input=_loads(row["input_json"]),
                dependencies=tuple(_loads(row["dependencies_json"])),
                risk=RiskLevel(row["risk"]),
                approval_required=bool(row["approval_required"]),
                approved=bool(row["approved"]),
                max_attempts=row["max_attempts"],
                timeout_seconds=row["timeout_seconds"],
                verifier_tool=row["verifier_tool"],
                verification_input=_loads(row["verification_input_json"]),
                attempts=row["attempts"],
                result=_loads(row["result_json"]),
                verification_evidence=_loads(row["verification_evidence_json"]),
                status=TaskStatus(row["status"]),
            )
            for row in rows
        ]

    def update(self, task_id: str, step: PlanStep) -> None:
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """UPDATE steps SET name = ?, tool_name = ?, input_json = ?, dependencies_json = ?,
                risk = ?, approval_required = ?, approved = ?, max_attempts = ?, timeout_seconds = ?,
                verifier_tool = ?, verification_input_json = ?, attempts = ?, result_json = ?,
                verification_evidence_json = ?, status = ? WHERE task_id = ? AND step_id = ?""",
                (
                    step.name,
                    step.tool_name,
                    _json(step.input),
                    _json(list(step.dependencies)),
                    step.risk.value,
                    int(step.approval_required),
                    int(step.approved),
                    step.max_attempts,
                    step.timeout_seconds,
                    step.verifier_tool,
                    _json(step.verification_input) if step.verification_input is not None else None,
                    step.attempts,
                    _json(step.result) if step.result is not None else None,
                    _json(step.verification_evidence) if step.verification_evidence is not None else None,
                    step.status.value,
                    task_id,
                    step.step_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown step {step.step_id!r} for task {task_id!r}")


class EventRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def append(self, task_id: str, event_type: str, payload: dict[str, Any], step_id: str | None = None) -> Event:
        created_at = utc_now()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO events (task_id, step_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (task_id, step_id, event_type, _json(payload), created_at),
            )
            event_id = int(cursor.lastrowid)
        return Event(event_id, task_id, event_type, payload, step_id, created_at)

    def list(self, task_id: str) -> list[Event]:
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY event_id", (task_id,)
            ).fetchall()
        return [
            Event(
                event_id=row["event_id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                payload=_loads(row["payload_json"]),
                step_id=row["step_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]


class WorkflowRepository:
    """Transactional persistence for reusable workflows."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create(self, workflow: Workflow) -> Workflow:
        created_at = workflow.created_at or utc_now()
        updated_at = workflow.updated_at or created_at
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO workflows
                (workflow_id, name, description, goal_json, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    workflow.id,
                    workflow.name,
                    workflow.description,
                    _json(workflow.goal),
                    workflow.version,
                    created_at,
                    updated_at,
                ),
            )
        return Workflow(
            id=workflow.id,
            name=workflow.name,
            description=workflow.description,
            goal=dict(workflow.goal),
            created_at=created_at,
            updated_at=updated_at,
            version=workflow.version,
        )

    def get(self, workflow_id: str) -> Workflow | None:
        with self.store._lock:
            row = self.store.connection.execute(
                "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list(self) -> list[Workflow]:
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT * FROM workflows ORDER BY created_at, workflow_id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def delete(self, workflow_id: str) -> bool:
        with self.store.transaction() as conn:
            cursor = conn.execute("DELETE FROM workflows WHERE workflow_id = ?", (workflow_id,))
        return cursor.rowcount == 1

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Workflow:
        return Workflow(
            id=row["workflow_id"],
            name=row["name"],
            description=row["description"],
            goal=_loads(row["goal_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=int(row["version"]),
        )


class MemoryRepository:
    """Transactional persistence for user-controlled memory records."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create(self, record: MemoryRecord) -> MemoryRecord:
        created_at = record.created_at or utc_now()
        updated_at = record.updated_at or created_at
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO memory_records
                (memory_id, memory_key, value_json, category, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.key,
                    _json(record.value),
                    record.category,
                    created_at,
                    updated_at,
                ),
            )
        return MemoryRecord(
            id=record.id,
            key=record.key,
            value=record.value,
            category=record.category,
            created_at=created_at,
            updated_at=updated_at,
        )

    def upsert(self, record: MemoryRecord) -> tuple[MemoryRecord, bool]:
        """Insert a new key or update its value while retaining its identity."""

        now = utc_now()
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM memory_records WHERE memory_key = ?", (record.key,)
            ).fetchone()
            if existing is None:
                created_at = record.created_at or now
                updated_at = record.updated_at or created_at
                conn.execute(
                    """INSERT INTO memory_records
                    (memory_id, memory_key, value_json, category, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        record.id,
                        record.key,
                        _json(record.value),
                        record.category,
                        created_at,
                        updated_at,
                    ),
                )
                return (
                    MemoryRecord(
                        id=record.id,
                        key=record.key,
                        value=record.value,
                        category=record.category,
                        created_at=created_at,
                        updated_at=updated_at,
                    ),
                    True,
                )

            updated_at = record.updated_at or now
            conn.execute(
                """UPDATE memory_records
                SET value_json = ?, category = ?, updated_at = ? WHERE memory_id = ?""",
                (_json(record.value), record.category, updated_at, existing["memory_id"]),
            )
            return (
                MemoryRecord(
                    id=existing["memory_id"],
                    key=existing["memory_key"],
                    value=record.value,
                    category=record.category,
                    created_at=existing["created_at"],
                    updated_at=updated_at,
                ),
                False,
            )

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self.store._lock:
            row = self.store.connection.execute(
                "SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list(self) -> list[MemoryRecord]:
        with self.store._lock:
            rows = self.store.connection.execute(
                "SELECT * FROM memory_records ORDER BY created_at, memory_id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def delete(self, memory_id: str) -> bool:
        with self.store.transaction() as conn:
            cursor = conn.execute("DELETE FROM memory_records WHERE memory_id = ?", (memory_id,))
        return cursor.rowcount == 1

    def reset(self) -> int:
        with self.store.transaction() as conn:
            cursor = conn.execute("DELETE FROM memory_records")
        return cursor.rowcount

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["memory_id"],
            key=row["memory_key"],
            value=_loads(row["value_json"]),
            category=row["category"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
