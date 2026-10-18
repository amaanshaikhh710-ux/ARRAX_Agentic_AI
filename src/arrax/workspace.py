"""Opt-in, bounded workspace tools.

The workspace adapter is deliberately separate from the fixture tools.  It is
only constructed when an application explicitly supplies a workspace root.
Every public path is relative to that root and every operation checks existing
path components with ``lstat`` before resolving the path for containment.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import tempfile
from typing import Any, Iterable, Mapping

from .domain import RiskLevel
from .tools import ToolContext, ToolInputError, ToolResult


REPORT_FILENAME = "ARRAX_READINESS_REPORT.md"
REPORT_MARKER = "ARRAX-READINESS-REPORT-V1"

MAX_PATH_LENGTH = 512
MAX_QUERY_LENGTH = 256
MAX_LIST_ENTRIES = 200
MAX_SEARCH_FILES = 100
MAX_SEARCH_RESULTS = 100
MAX_SEARCH_BYTES = 256 * 1024
MAX_SEARCH_FILE_BYTES = 64 * 1024
MAX_READ_BYTES = 64 * 1024
MAX_REPORT_BYTES = 256 * 1024
MAX_SNAPSHOT_ENTRIES = 500
MAX_RESULT_LINE_LENGTH = 512


class WorkspacePathError(ToolInputError):
    """Raised for a path that is not safe inside the approved root."""


def _bounded_integer(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolInputError(f"{field_name} must be an integer between {minimum} and {maximum}")
    return value


def _string(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{field_name} must be a non-empty string")
    if len(value) > maximum:
        raise ToolInputError(f"{field_name} exceeds the input limit")
    return value


def _object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ToolInputError(f"{name} input must be an object")
    return dict(value)


def normalize_workspace_root(value: str | os.PathLike[str] | None) -> str | None:
    """Return a meaningful configured root, leaving blank configuration off."""

    if value is None:
        return None
    text = os.fspath(value)
    if not isinstance(text, str) or not text.strip():
        return None
    return text


class WorkspaceAccess:
    """Contain all filesystem access below one explicitly approved directory."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        normalized = normalize_workspace_root(root)
        if normalized is None:
            raise ValueError("workspace_root must be an explicit non-empty directory")
        candidate = Path(normalized).expanduser()
        try:
            self._assert_no_symlink_components(candidate)
            resolved = candidate.resolve(strict=True)
            root_stat = os.lstat(resolved)
        except (OSError, RuntimeError) as exc:
            raise ValueError("workspace_root must be an existing directory") from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("workspace_root must be an existing directory")
        self.root = resolved

    @staticmethod
    def _assert_no_symlink_components(candidate: Path) -> None:
        """Inspect the lexical root path without resolving through links."""

        lexical = Path(os.path.abspath(candidate))
        current = Path(lexical.anchor) if lexical.anchor else Path(".")
        parts = lexical.parts[1:] if lexical.anchor else lexical.parts
        for part in parts:
            current = current / part
            try:
                current_stat = os.lstat(current)
            except OSError as exc:
                raise ValueError("workspace_root must be an existing directory") from exc
            if stat.S_ISLNK(current_stat.st_mode):
                raise ValueError("workspace_root must not contain symlink components")

    def relative_path(self, value: Any, *, default: str | None = None) -> Path:
        if value is None:
            value = default
        if not isinstance(value, str) or not value.strip():
            raise WorkspacePathError("path must be a non-empty relative path")
        if len(value) > MAX_PATH_LENGTH:
            raise WorkspacePathError("path exceeds the input limit")

        # Check both platform grammars.  This keeps a Windows drive or UNC path
        # rejected even if a caller submits it with separators from another OS.
        path = Path(value)
        windows_path = PureWindowsPath(value)
        posix_path = PurePosixPath(value)
        if (
            path.is_absolute()
            or windows_path.is_absolute()
            or posix_path.is_absolute()
            or windows_path.root
            or posix_path.root
        ):
            raise WorkspacePathError("absolute paths are not allowed")
        if path.drive or windows_path.drive:
            raise WorkspacePathError("drive-qualified paths are not allowed")
        parts = path.parts
        if any(part == ".." for part in parts):
            raise WorkspacePathError("parent traversal is not allowed")
        cleaned = tuple(part for part in parts if part not in {"", "."})
        return Path(*cleaned) if cleaned else Path(".")

    def _candidate(self, relative: Path, *, allow_missing: bool = False) -> Path:
        candidate = self.root.joinpath(relative)
        parts = relative.parts if relative != Path(".") else ()
        current = self.root
        for index, part in enumerate(parts):
            current = current / part
            try:
                current_stat = os.lstat(current)
            except FileNotFoundError as exc:
                if allow_missing and index == len(parts) - 1:
                    break
                raise WorkspacePathError("path does not exist") from exc
            except OSError as exc:
                raise WorkspacePathError("path could not be inspected") from exc
            if stat.S_ISLNK(current_stat.st_mode):
                raise WorkspacePathError("symlinks are not allowed")
            if index < len(parts) - 1 and not stat.S_ISDIR(current_stat.st_mode):
                raise WorkspacePathError("a path component is not a directory")

        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspacePathError("path is outside the approved workspace") from exc
        return candidate

    def directory(self, value: Any, *, default: str = ".") -> tuple[Path, Path]:
        relative = self.relative_path(value, default=default)
        candidate = self._candidate(relative)
        try:
            current_stat = os.lstat(candidate)
        except OSError as exc:
            raise WorkspacePathError("directory does not exist") from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise WorkspacePathError("symlink directories are not allowed")
        if not stat.S_ISDIR(current_stat.st_mode):
            raise WorkspacePathError("path must be a directory")
        return relative, candidate

    def file(self, value: Any, *, allow_missing: bool = False) -> tuple[Path, Path, bool]:
        relative = self.relative_path(value)
        candidate = self._candidate(relative, allow_missing=allow_missing)
        try:
            current_stat = os.lstat(candidate)
        except FileNotFoundError:
            if allow_missing:
                return relative, candidate, False
            raise WorkspacePathError("file does not exist")
        except OSError as exc:
            raise WorkspacePathError("file could not be inspected") from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise WorkspacePathError("symlinks are not allowed")
        if not stat.S_ISREG(current_stat.st_mode):
            raise WorkspacePathError("path must be a regular file")
        return relative, candidate, True

    def _open_read(self, candidate: Path):
        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.fdopen(os.open(candidate, flags), "rb")
        except OSError as exc:
            raise WorkspacePathError("file could not be opened safely") from exc

    def read_file(self, relative: Path, candidate: Path, maximum: int) -> bytes:
        try:
            before = os.lstat(candidate)
        except OSError as exc:
            raise WorkspacePathError("file could not be inspected") from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise WorkspacePathError("path must be a regular file")
        if before.st_size > maximum:
            raise WorkspacePathError("file exceeds the configured size limit")
        with self._open_read(candidate) as handle:
            after = os.fstat(handle.fileno())
            if stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode):
                raise WorkspacePathError("path must be a regular file")
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise WorkspacePathError("file exceeds the configured size limit")
        return data

    def read_prefix(self, candidate: Path, maximum: int) -> tuple[bytes, bool]:
        try:
            before = os.lstat(candidate)
        except OSError as exc:
            raise WorkspacePathError("file could not be inspected") from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise WorkspacePathError("path must be a regular file")
        truncated = before.st_size > maximum
        with self._open_read(candidate) as handle:
            after = os.fstat(handle.fileno())
            if stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode):
                raise WorkspacePathError("path must be a regular file")
            return handle.read(maximum), truncated

    def immediate_entries(self, relative: Path, directory: Path, maximum: int) -> tuple[list[dict[str, Any]], bool]:
        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if len(entries) >= maximum:
                        truncated = True
                        break
                    try:
                        entry_stat = os.lstat(entry.path)
                    except OSError as exc:
                        raise WorkspacePathError("workspace entry could not be inspected") from exc
                    if stat.S_ISLNK(entry_stat.st_mode):
                        raise WorkspacePathError("symlinks are not allowed")
                    if stat.S_ISDIR(entry_stat.st_mode):
                        entry_type = "directory"
                        size = None
                    elif stat.S_ISREG(entry_stat.st_mode):
                        entry_type = "file"
                        size = entry_stat.st_size
                    else:
                        raise WorkspacePathError("unsupported workspace entry")
                    entries.append({"name": entry.name, "type": entry_type, "bytes": size})
                if not truncated:
                    try:
                        next(iterator)
                    except StopIteration:
                        pass
                    else:
                        truncated = True
        except OSError as exc:
            raise WorkspacePathError("workspace directory could not be listed") from exc
        entries.sort(key=lambda item: item["name"])
        return entries, truncated

    def files(self, relative: Path, directory: Path, maximum: int) -> tuple[list[tuple[Path, Path, os.stat_result]], bool]:
        found: list[tuple[Path, Path, os.stat_result]] = []
        pending: list[tuple[Path, Path]] = [(relative, directory)]
        truncated = False
        while pending and len(found) <= maximum:
            current_relative, current_directory = pending.pop()
            children: list[tuple[str, Path, os.stat_result, str]] = []
            try:
                with os.scandir(current_directory) as iterator:
                    for entry in iterator:
                        try:
                            entry_stat = os.lstat(entry.path)
                        except OSError as exc:
                            raise WorkspacePathError("workspace entry could not be inspected") from exc
                        if stat.S_ISLNK(entry_stat.st_mode):
                            raise WorkspacePathError("symlinks are not allowed")
                        if stat.S_ISDIR(entry_stat.st_mode):
                            kind = "directory"
                        elif stat.S_ISREG(entry_stat.st_mode):
                            kind = "file"
                        else:
                            raise WorkspacePathError("unsupported workspace entry")
                        children.append((entry.name, Path(entry.path), entry_stat, kind))
            except OSError as exc:
                raise WorkspacePathError("workspace directory could not be listed") from exc
            children.sort(key=lambda item: item[0])
            for name, child, child_stat, kind in children:
                child_relative = current_relative / name
                if kind == "directory":
                    pending.append((child_relative, child))
                elif len(found) < maximum:
                    found.append((child_relative, child, child_stat))
                else:
                    truncated = True
                    break
            if truncated:
                break
        if pending:
            truncated = True
        found.sort(key=lambda item: item[0].as_posix())
        return found[:maximum], truncated

    def snapshot(self, value: Any = ".", *, maximum: int = MAX_SNAPSHOT_ENTRIES, exclude: Iterable[Path] = ()) -> dict[str, Any]:
        relative, directory = self.directory(value)
        excluded = {item.as_posix() for item in exclude}
        entries: list[dict[str, Any]] = []
        pending: list[tuple[Path, Path]] = [(relative, directory)]
        while pending:
            current_relative, current_directory = pending.pop()
            children: list[tuple[str, Path, os.stat_result, str]] = []
            try:
                with os.scandir(current_directory) as iterator:
                    for entry in iterator:
                        try:
                            entry_stat = os.lstat(entry.path)
                        except OSError as exc:
                            raise WorkspacePathError("workspace entry could not be inspected") from exc
                        if stat.S_ISLNK(entry_stat.st_mode):
                            raise WorkspacePathError("symlinks are not allowed")
                        if stat.S_ISDIR(entry_stat.st_mode):
                            kind = "directory"
                        elif stat.S_ISREG(entry_stat.st_mode):
                            kind = "file"
                        else:
                            raise WorkspacePathError("unsupported workspace entry")
                        children.append((entry.name, Path(entry.path), entry_stat, kind))
            except OSError as exc:
                raise WorkspacePathError("workspace directory could not be listed") from exc
            for name, child, child_stat, kind in sorted(children, key=lambda item: item[0], reverse=True):
                child_relative = current_relative / name
                child_name = child_relative.as_posix()
                if child_name in excluded:
                    continue
                if len(entries) >= maximum:
                    raise WorkspacePathError("workspace snapshot exceeds the configured bound")
                if kind == "directory":
                    entries.append({"path": child_name, "type": "directory", "bytes": None})
                    pending.append((child_relative, child))
                else:
                    entries.append({"path": child_name, "type": "file", "bytes": child_stat.st_size})
        entries.sort(key=lambda item: (item["path"], item["type"]))
        payload = {"path": relative.as_posix(), "entries": entries}
        payload["snapshot_id"] = _digest(payload)
        payload["entry_count"] = len(entries)
        return payload


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return sha256(encoded).hexdigest()


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise WorkspacePathError("binary content is not supported")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspacePathError("file is not valid UTF-8 text") from exc
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\t\n\r\f\b")
    if controls and controls / max(1, len(text)) > 0.01:
        raise WorkspacePathError("unsupported binary content")
    return text


def _safe_failure(exc: Exception) -> ToolResult:
    if isinstance(exc, ToolInputError):
        return ToolResult(False, {}, str(exc))
    return ToolResult(False, {}, "workspace operation was rejected")


class WorkspaceListTool:
    name = "workspace.list"
    default_timeout_seconds = 2.0
    is_verifier = False
    risk_level = RiskLevel.READ_ONLY

    def __init__(self, access: WorkspaceAccess) -> None:
        self.access = access

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        unknown = set(data) - {"path", "max_entries"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")
        self.access.relative_path(data.get("path"), default=".")
        _bounded_integer(data.get("max_entries", MAX_LIST_ENTRIES), "max_entries", 1, MAX_LIST_ENTRIES)

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.cancelled:
            return ToolResult(False, {}, "cancelled")
        try:
            relative, directory = self.access.directory(value.get("path"), default=".")
            maximum = int(value.get("max_entries", MAX_LIST_ENTRIES))
            entries, truncated = self.access.immediate_entries(relative, directory, maximum)
            payload = {"path": relative.as_posix(), "entries": entries}
            payload["snapshot_id"] = _digest(payload)
            payload.update({"entry_count": len(entries), "truncated": truncated})
            return ToolResult(True, payload)
        except Exception as exc:
            return _safe_failure(exc)


class WorkspaceReadTextTool:
    name = "workspace.read_text"
    default_timeout_seconds = 2.0
    is_verifier = False
    risk_level = RiskLevel.READ_ONLY

    def __init__(self, access: WorkspaceAccess) -> None:
        self.access = access

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        unknown = set(data) - {"path", "max_bytes"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")
        self.access.relative_path(data.get("path"))
        _bounded_integer(data.get("max_bytes", MAX_READ_BYTES), "max_bytes", 1, MAX_READ_BYTES)

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.cancelled:
            return ToolResult(False, {}, "cancelled")
        try:
            relative, candidate, _ = self.access.file(value.get("path"))
            maximum = int(value.get("max_bytes", MAX_READ_BYTES))
            data = self.access.read_file(relative, candidate, maximum)
            text = _decode_text(data)
            return ToolResult(True, {"path": relative.as_posix(), "text": text, "bytes": len(data)})
        except Exception as exc:
            return _safe_failure(exc)


class WorkspaceSearchTool:
    name = "workspace.search"
    default_timeout_seconds = 3.0
    is_verifier = False
    risk_level = RiskLevel.READ_ONLY

    def __init__(self, access: WorkspaceAccess) -> None:
        self.access = access

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        unknown = set(data) - {"query", "path", "max_files", "max_results", "max_bytes", "case_sensitive"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")
        _string(data.get("query"), "query", MAX_QUERY_LENGTH)
        self.access.relative_path(data.get("path"), default=".")
        _bounded_integer(data.get("max_files", MAX_SEARCH_FILES), "max_files", 1, MAX_SEARCH_FILES)
        _bounded_integer(data.get("max_results", MAX_SEARCH_RESULTS), "max_results", 1, MAX_SEARCH_RESULTS)
        _bounded_integer(data.get("max_bytes", MAX_SEARCH_BYTES), "max_bytes", 1, MAX_SEARCH_BYTES)
        if "case_sensitive" in data and not isinstance(data["case_sensitive"], bool):
            raise ToolInputError("case_sensitive must be boolean")

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.cancelled:
            return ToolResult(False, {}, "cancelled")
        try:
            relative, directory = self.access.directory(value.get("path"), default=".")
            query = str(value["query"])
            case_sensitive = value.get("case_sensitive", True)
            maximum_files = int(value.get("max_files", MAX_SEARCH_FILES))
            maximum_results = int(value.get("max_results", MAX_SEARCH_RESULTS))
            maximum_bytes = int(value.get("max_bytes", MAX_SEARCH_BYTES))
            files, file_truncated = self.access.files(relative, directory, maximum_files)
            results: list[dict[str, Any]] = []
            bytes_scanned = 0
            binary_files = 0
            truncated = file_truncated
            needle = query if case_sensitive else query.casefold()
            for file_relative, candidate, _file_stat in files:
                if context.cancelled:
                    return ToolResult(False, {}, "cancelled")
                remaining = maximum_bytes - bytes_scanned
                if remaining <= 0:
                    truncated = True
                    break
                read_limit = min(remaining, MAX_SEARCH_FILE_BYTES)
                data, file_was_truncated = self.access.read_prefix(candidate, read_limit)
                bytes_scanned += len(data)
                truncated = truncated or file_was_truncated
                try:
                    text = _decode_text(data)
                except WorkspacePathError:
                    binary_files += 1
                    continue
                haystack = text if case_sensitive else text.casefold()
                for line_number, line in enumerate(text.splitlines(), start=1):
                    comparable = line if case_sensitive else line.casefold()
                    if needle in comparable:
                        results.append(
                            {
                                "path": file_relative.as_posix(),
                                "line": line_number,
                                "text": line[:MAX_RESULT_LINE_LENGTH],
                            }
                        )
                        if len(results) >= maximum_results:
                            truncated = True
                            break
                if len(results) >= maximum_results:
                    break
                # Keep the local variable meaningful for reviewers and avoid
                # accidentally changing the bounded text-search contract.
                _ = haystack
            return ToolResult(
                True,
                {
                    "path": relative.as_posix(),
                    "query": query,
                    "results": results,
                    "files_scanned": len(files),
                    "bytes_scanned": bytes_scanned,
                    "binary_files_skipped": binary_files,
                    "truncated": truncated,
                },
            )
        except Exception as exc:
            return _safe_failure(exc)


class WorkspaceCreateReportTool:
    name = "workspace.create_report"
    default_timeout_seconds = 4.0
    is_verifier = False
    risk_level = RiskLevel.SENSITIVE

    def __init__(self, access: WorkspaceAccess) -> None:
        self.access = access

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        unknown = set(data) - {"path", "overwrite"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")
        if "path" in data:
            self.access.relative_path(data["path"])
        if "overwrite" in data and not isinstance(data["overwrite"], bool):
            raise ToolInputError("overwrite must be boolean")

    def _write_atomic(self, target: Path, content: bytes, overwrite: bool) -> None:
        parent = target.parent
        try:
            parent_stat = os.lstat(parent)
        except OSError as exc:
            raise WorkspacePathError("report directory does not exist") from exc
        if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
            raise WorkspacePathError("report directory is not safe")

        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".arrax-report-", suffix=".tmp", dir=str(parent))
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(temporary_name, target)
                temporary_name = None
            else:
                # Linking the completed temporary file creates the destination
                # atomically without replacing a file that appeared meanwhile.
                try:
                    os.link(temporary_name, target)
                except FileExistsError as exc:
                    raise WorkspacePathError("report path already exists; overwrite requires approval") from exc
                os.unlink(temporary_name)
                temporary_name = None
        except WorkspacePathError:
            raise
        except (OSError, ValueError) as exc:
            raise WorkspacePathError("report could not be written safely") from exc
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.cancelled:
            return ToolResult(False, {}, "cancelled")
        try:
            path_value = value.get("path", REPORT_FILENAME)
            relative, target, exists = self.access.file(path_value, allow_missing=True)
            overwrite = bool(value.get("overwrite", False))
            if exists and not overwrite:
                return ToolResult(False, {}, "report path already exists; set overwrite true and obtain approval")
            if overwrite and not context.approved:
                return ToolResult(False, {}, "report overwrite requires explicit approval")
            snapshot = self.access.snapshot(".", exclude={relative})
            files = sum(1 for item in snapshot["entries"] if item["type"] == "file")
            directories = sum(1 for item in snapshot["entries"] if item["type"] == "directory")
            lines = [
                "# ARRAX Workspace Readiness Report",
                "",
                f"<!-- {REPORT_MARKER} -->",
                "",
                "This deterministic report was generated from the approved workspace snapshot.",
                "",
                "## Snapshot",
                "",
                f"- Entries: {snapshot['entry_count']}",
                f"- Files: {files}",
                f"- Directories: {directories}",
                f"- Snapshot ID: `{snapshot['snapshot_id']}`",
                "",
                "## Entries",
                "",
            ]
            if snapshot["entries"]:
                lines.extend(
                    f"- `{item['path']}` — {item['type']}"
                    + (f" — {item['bytes']} bytes" if item["type"] == "file" else "")
                    for item in snapshot["entries"]
                )
            else:
                lines.append("- (empty workspace)")
            content = ("\n".join(lines) + "\n").encode("utf-8")
            if len(content) > MAX_REPORT_BYTES:
                return ToolResult(False, {}, "generated report exceeds the output limit")
            self._write_atomic(target, content, overwrite)
            return ToolResult(
                True,
                {
                    "path": relative.as_posix(),
                    "bytes": len(content),
                    "marker": REPORT_MARKER,
                    "snapshot_id": snapshot["snapshot_id"],
                },
            )
        except Exception as exc:
            return _safe_failure(exc)


class WorkspaceVerifyReportTool:
    name = "workspace.verify_report"
    default_timeout_seconds = 2.0
    is_verifier = True
    risk_level = RiskLevel.READ_ONLY

    def __init__(self, access: WorkspaceAccess) -> None:
        self.access = access

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        unknown = set(data) - {"action_output", "path"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")
        if not isinstance(data.get("action_output"), Mapping):
            raise ToolInputError("action_output must be an object")
        if "path" in data:
            self.access.relative_path(data["path"])

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.cancelled:
            return ToolResult(False, {}, "cancelled")
        try:
            action_output = value["action_output"]
            expected_path = value.get("path", action_output.get("path"))
            relative, candidate, exists = self.access.file(expected_path)
            if not exists:
                return ToolResult(False, {}, "generated report does not exist")
            data = self.access.read_file(relative, candidate, MAX_REPORT_BYTES)
            text = _decode_text(data)
            if REPORT_MARKER not in text:
                return ToolResult(False, {}, "generated report marker was not found")
            claimed_path = action_output.get("path")
            if claimed_path is not None and claimed_path != relative.as_posix():
                return ToolResult(False, {}, "report evidence path did not match the actual report")
            return ToolResult(
                True,
                {"verified": True, "path": relative.as_posix(), "bytes": len(data), "marker": REPORT_MARKER},
            )
        except Exception as exc:
            return _safe_failure(exc)


class WorkspaceVerifySnapshotTool:
    name = "workspace.verify_snapshot"
    default_timeout_seconds = 2.0
    is_verifier = True
    risk_level = RiskLevel.READ_ONLY

    def __init__(self, access: WorkspaceAccess) -> None:
        self.access = access

    def validate_input(self, value: Mapping[str, Any]) -> None:
        data = _object(value, self.name)
        unknown = set(data) - {"action_output", "path", "max_entries"}
        if unknown:
            raise ToolInputError(f"unknown input fields: {sorted(unknown)}")
        if not isinstance(data.get("action_output"), Mapping):
            raise ToolInputError("action_output must be an object")
        if "path" in data:
            self.access.relative_path(data["path"], default=".")
        _bounded_integer(data.get("max_entries", MAX_LIST_ENTRIES), "max_entries", 1, MAX_LIST_ENTRIES)

    def execute(self, value: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if context.cancelled:
            return ToolResult(False, {}, "cancelled")
        try:
            action_output = value["action_output"]
            relative = value.get("path", action_output.get("path", "."))
            maximum = int(value.get("max_entries", MAX_LIST_ENTRIES))
            actual_relative, directory = self.access.directory(relative, default=".")
            actual_entries, truncated = self.access.immediate_entries(actual_relative, directory, maximum)
            actual = {"path": actual_relative.as_posix(), "entries": actual_entries}
            actual["snapshot_id"] = _digest(actual)
            actual.update({"entry_count": len(actual_entries), "truncated": truncated})
            if action_output.get("path") != actual["path"] or action_output.get("entries") != actual["entries"]:
                return ToolResult(False, {}, "workspace snapshot did not match the actual directory")
            if action_output.get("snapshot_id") != actual["snapshot_id"]:
                return ToolResult(False, {}, "workspace snapshot digest did not match the actual directory")
            return ToolResult(
                True,
                {
                    "verified": True,
                    "path": actual["path"],
                    "entries": actual["entry_count"],
                    "snapshot_id": actual["snapshot_id"],
                },
            )
        except Exception as exc:
            return _safe_failure(exc)


def workspace_tools(workspace_root: str | os.PathLike[str]) -> list[Any]:
    """Construct the opt-in workspace tool set for one approved root."""

    access = WorkspaceAccess(workspace_root)
    return [
        WorkspaceListTool(access),
        WorkspaceReadTextTool(access),
        WorkspaceSearchTool(access),
        WorkspaceCreateReportTool(access),
        WorkspaceVerifyReportTool(access),
        WorkspaceVerifySnapshotTool(access),
    ]


__all__ = [
    "MAX_LIST_ENTRIES",
    "MAX_READ_BYTES",
    "MAX_REPORT_BYTES",
    "MAX_SEARCH_BYTES",
    "MAX_SEARCH_FILES",
    "MAX_SEARCH_RESULTS",
    "REPORT_FILENAME",
    "REPORT_MARKER",
    "WorkspaceAccess",
    "WorkspaceCreateReportTool",
    "WorkspaceListTool",
    "WorkspacePathError",
    "WorkspaceReadTextTool",
    "WorkspaceSearchTool",
    "WorkspaceVerifyReportTool",
    "WorkspaceVerifySnapshotTool",
    "normalize_workspace_root",
    "workspace_tools",
]
