from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import SCHEMA_VERSION


VALID_MODES = {"off", "explicit-only", "task-only", "project"}
TASK_STATES = {"draft", "active", "blocked", "paused", "ready_for_review", "completed", "cancelled"}
OPEN_TASK_STATES = {"active", "blocked", "paused", "ready_for_review"}
TERMINAL_TASK_STATES = {"completed", "cancelled"}
COMPLETION_POLICIES = {"auto", "user-review"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def save_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return {}, normalized
    metadata: dict[str, Any] = {}
    for line in normalized[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = _parse_scalar(value)
    return metadata, normalized[end + 5 :]


def read_document(path: Path) -> tuple[dict[str, Any], str]:
    try:
        return parse_frontmatter(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}, ""


def _dump_scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_document(metadata: dict[str, Any], body: str) -> str:
    ordered = [
        "planning_schema", "project_id", "document_type", "project_layer",
        "task_id", "title", "status", "completion_policy", "workstream",
        "created_at", "updated_at", "revision", "last_repo_fingerprint",
        "last_reconciled_fingerprint",
    ]
    keys = [key for key in ordered if key in metadata]
    keys.extend(sorted(key for key in metadata if key not in keys))
    lines = ["---"]
    lines.extend(f"{key}: {_dump_scalar(metadata[key])}" for key in keys)
    lines.extend(["---", "", body.lstrip("\n").rstrip() + "", ""])
    return "\n".join(lines)


def write_document(path: Path, metadata: dict[str, Any], body: str) -> None:
    current_meta, _ = read_document(path)
    revision = int(current_meta.get("revision") or metadata.get("revision") or 0) + 1
    metadata = dict(metadata)
    metadata["planning_schema"] = SCHEMA_VERSION
    metadata["updated_at"] = now_iso()
    metadata["revision"] = revision
    atomic_write_text(path, render_document(metadata, body))


def update_document_metadata(path: Path, **changes: Any) -> dict[str, Any]:
    metadata, body = read_document(path)
    if not metadata:
        raise ValueError(f"missing planning front matter: {path}")
    metadata.update(changes)
    write_document(path, metadata, body)
    return metadata


def extract_section(text: str, heading: str) -> str:
    lines = text.replace("\r\n", "\n").splitlines()
    target = f"## {heading}".strip()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == target:
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def replace_section(text: str, heading: str, content: str) -> str:
    lines = text.replace("\r\n", "\n").splitlines()
    target = f"## {heading}".strip()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == target:
            start = index
            break
    replacement = [target, "", content.strip(), ""]
    if start is None:
        return text.rstrip() + "\n\n" + "\n".join(replacement)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[:start] + replacement + lines[end:]).rstrip() + "\n"


def checklist_counts(text: str, heading: str = "验收条件") -> tuple[int, int]:
    section = extract_section(text, heading)
    checked = 0
    unchecked = 0
    for line in section.splitlines():
        value = line.strip().lower()
        if value.startswith("- [x]"):
            checked += 1
        elif value.startswith("- [ ]"):
            unchecked += 1
    return checked, unchecked


def replace_marker_block(text: str, name: str, lines: list[str]) -> str:
    start_marker = f"<!-- planning:{name}:start -->"
    end_marker = f"<!-- planning:{name}:end -->"
    block = "\n".join([start_marker, *lines, end_marker])
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start >= 0 and end >= start:
        end += len(end_marker)
        return text[:start] + block + text[end:]
    return text.rstrip() + "\n\n" + block + "\n"


def safe_relative_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError("path must be project-relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("path escapes project root") from exc
    return resolved


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # Windows os.kill(pid, 0) calls TerminateProcess, unlike POSIX.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: conservatively alive
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class LockBusy(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path, session_id: str | None, timeout: float = 1.0, stale_after: float = 300.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "session_id": session_id,
        "created_at": now_iso(),
        "created_epoch": time.time(),
    }
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            break
        except FileExistsError:
            existing = load_json(path, {})
            age = time.time() - float(existing.get("created_epoch") or time.time())
            same_host = existing.get("host") == socket.gethostname()
            owner_alive = same_host and _pid_alive(int(existing.get("pid") or 0))
            if age > stale_after and not owner_alive:
                try:
                    path.unlink()
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise LockBusy(f"planning lock is busy: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            current = load_json(path, {})
            if int(current.get("pid") or 0) == os.getpid():
                path.unlink(missing_ok=True)
        except OSError:
            pass
