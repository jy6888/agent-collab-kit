from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .model import (
    COMPLETION_POLICIES,
    OPEN_TASK_STATES,
    TASK_STATES,
    LockBusy,
    atomic_write_text,
    checklist_counts,
    extract_section,
    file_lock,
    load_json,
    now_iso,
    parse_frontmatter,
    read_document,
    replace_marker_block,
    replace_section,
    safe_relative_path,
    save_json,
    update_document_metadata,
    write_document,
)
from .repo import code_evidence, git_root, repo_snapshot, run_git
from .endpoints import bridge_endpoints
from .records import LIMITS, RECENT_MARKER, has_recent_block, lint_records
from .semantics import (
    DECISION_TRANSITIONS,
    FINDING_TRANSITIONS,
    ROADMAP_STATES,
    ROADMAP_TRANSITIONS,
    append_entry,
    file_state,
    find_entry,
    parse_entries,
    require_revision,
    update_entry,
    validate_transition,
)


DEFAULT_DOCS_ROOT = "docs/project"
PROJECT_DOCUMENT_NAMES = {"STATUS.md", "PROJECT.md", "ROADMAP.md", "FINDINGS.md", "DECISIONS.md"}


@dataclass(frozen=True)
class Paths:
    root: Path
    docs_root: Path
    runtime_root: Path
    index: Path
    status: Path
    tasks: Path
    locks: Path
    transactions: Path


def resolve_root(start: Path) -> Path:
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".codex" / "planning-config.json").is_file():
            return candidate
        if (candidate / ".codex" / "planning" / "index.json").is_file():
            return candidate
        if (candidate / DEFAULT_DOCS_ROOT / "STATUS.md").is_file():
            return candidate
        if (candidate / ".codex" / "planning" / "current" / "state.json").is_file():
            return candidate
    return git_root(start) or start


def _config_for(root: Path) -> tuple[dict[str, Any], str, str]:
    config_path = root / ".codex" / "planning-config.json"
    raw = load_json(config_path, {})
    config = raw if isinstance(raw, dict) else {}
    mode = config.get("mode")
    if mode in {"off", "explicit-only", "task-only", "project"}:
        return config, str(mode), "explicit v4 mode"
    if config.get("auto_start") is False:
        return config, "off", str(config.get("reason") or "legacy config disables automatic planning")
    if config.get("auto_start") is True:
        return config, "task-only", str(config.get("reason") or "legacy config enables automatic planning")
    index = load_json(root / ".codex" / "planning" / "index.json", {})
    if isinstance(index, dict) and index.get("schema_version") == SCHEMA_VERSION:
        layer = index.get("project_layer")
        return config, "project" if layer == "initialized" else "task-only", "existing v4 state"
    evidence = code_evidence(root)
    if evidence:
        return config, "task-only", "code evidence found"
    return config, "explicit-only", "no code-project marker found"


def policy_path(root: Path) -> Path:
    """Role policy file; projects may relocate it with "policy_path" in planning-config.json."""
    config = load_json(root / ".codex" / "planning-config.json", {})
    relative = config.get("policy_path") if isinstance(config, dict) else None
    return root / str(relative or "tools/codex/project-policy.json")


def probe(root: Path) -> dict[str, Any]:
    config, mode, reason = _config_for(root)
    evidence = code_evidence(root)
    return {
        "root": str(root),
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "auto_start": mode in {"task-only", "project"},
        "project_layer": "initialized" if mode == "project" else "uninitialized",
        "is_git": git_root(root) is not None,
        "reason": reason,
        "evidence": evidence,
        "config_path": str(root / ".codex" / "planning-config.json"),
        "legacy_config": "mode" not in config and "auto_start" in config,
    }


class PlanningService:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.config, self.mode, _ = _config_for(self.root)
        docs_value = str(self.config.get("docs_root") or DEFAULT_DOCS_ROOT).replace("\\", "/")
        self.docs_relative = docs_value.strip("/")
        docs_root = safe_relative_path(self.root, self.docs_relative)
        runtime = self.root / ".codex" / "planning"
        self.paths = Paths(
            root=self.root,
            docs_root=docs_root,
            runtime_root=runtime,
            index=runtime / "index.json",
            status=docs_root / "STATUS.md",
            tasks=docs_root / "tasks",
            locks=runtime / "locks",
            transactions=runtime / "transactions",
        )
        self.skill_root = Path(__file__).resolve().parents[2]

    def _template(self, name: str, **values: str) -> str:
        path = self.skill_root / "templates" / name
        text = path.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        return text

    def _save_config(self, mode: str) -> None:
        data = dict(self.config)
        data.pop("auto_start", None)
        data["schema_version"] = SCHEMA_VERSION
        data["mode"] = mode
        data["docs_root"] = self.docs_relative
        data.setdefault("stop_policy", "advisory")
        data.setdefault("repo_state", "auto")
        save_json(self.root / ".codex" / "planning-config.json", data)
        self.config = data
        self.mode = mode

    def _new_project_id(self) -> str:
        return "P-" + uuid.uuid4().hex[:12]

    def _project_id(self) -> str:
        metadata, _ = read_document(self.paths.status)
        value = metadata.get("project_id")
        if value:
            return str(value)
        index = load_json(self.paths.index, {})
        if isinstance(index, dict) and index.get("project_id"):
            return str(index["project_id"])
        return self._new_project_id()

    def _task_plan_path(self, task_id: str) -> Path:
        return self.paths.tasks / task_id / "PLAN.md"

    def _task_progress_path(self, task_id: str) -> Path:
        return self.paths.tasks / task_id / "PROGRESS.md"

    def _task_findings_path(self, task_id: str) -> Path:
        return self.paths.tasks / task_id / "FINDINGS.md"

    def _scan_tasks(self) -> dict[str, dict[str, Any]]:
        tasks: dict[str, dict[str, Any]] = {}
        if not self.paths.tasks.is_dir():
            return tasks
        for directory in sorted(self.paths.tasks.iterdir()):
            plan = directory / "PLAN.md"
            metadata, _ = read_document(plan)
            task_id = str(metadata.get("task_id") or directory.name)
            if not metadata or not task_id.startswith("T-"):
                continue
            tasks[task_id] = {
                "title": str(metadata.get("title") or task_id),
                "status": str(metadata.get("status") or "draft"),
                "workstream": str(metadata.get("workstream") or ""),
                "completion_policy": str(metadata.get("completion_policy") or "auto"),
                "updated_at": metadata.get("updated_at"),
                "path": str(plan.relative_to(self.root)).replace("\\", "/"),
            }
        return tasks

    def _canonical_document_paths(self) -> dict[str, Path]:
        paths = {"STATUS.md": self.paths.status}
        for filename in ("PROJECT.md", "ROADMAP.md", "FINDINGS.md", "DECISIONS.md"):
            path = self.paths.docs_root / filename
            if path.exists():
                paths[filename] = path
        for task_id in self._scan_tasks():
            paths[f"tasks/{task_id}/PLAN.md"] = self._task_plan_path(task_id)
        return paths

    def _document_states(self) -> dict[str, dict[str, Any]]:
        return {name: file_state(path) for name, path in self._canonical_document_paths().items()}

    def _project_document_name(self, path: Path) -> str | None:
        try:
            relative = path.resolve().relative_to(self.paths.docs_root.resolve())
        except ValueError:
            return None
        if len(relative.parts) == 1 and relative.name in PROJECT_DOCUMENT_NAMES:
            return relative.name
        return None

    def _capture_tool_write_consistency(self, index: dict[str, Any], paths: list[Path]) -> dict[str, bool]:
        recorded = index.get("document_states") or {}
        consistency: dict[str, bool] = {}
        for path in paths:
            name = self._project_document_name(path)
            if not name:
                continue
            registered = recorded.get(name)
            consistency[name] = registered is None or registered == file_state(path)
        return consistency

    def _record_tool_writes(self, index: dict[str, Any], consistency: dict[str, bool]) -> None:
        states = dict(index.get("document_states") or {})
        current = self._document_states()
        for name, was_consistent in consistency.items():
            if was_consistent and name in current:
                states[name] = current[name]
        index["document_states"] = states

    def rebuild_index(self, preserve: dict[str, Any] | None = None) -> dict[str, Any]:
        previous = preserve if isinstance(preserve, dict) else load_json(self.paths.index, {})
        previous = previous if isinstance(previous, dict) else {}
        status_meta, _ = read_document(self.paths.status)
        tasks = self._scan_tasks()
        open_ids = sorted(task_id for task_id, task in tasks.items() if task.get("status") in OPEN_TASK_STATES)
        bindings = {
            str(session): str(task)
            for session, task in (previous.get("session_bindings") or {}).items()
            if str(task) in open_ids
        }
        workstreams: dict[str, list[str]] = {}
        for task_id, task in tasks.items():
            name = str(task.get("workstream") or "").strip()
            if name:
                workstreams.setdefault(name, []).append(task_id)
        workstreams = {name: sorted(task_ids) for name, task_ids in sorted(workstreams.items())}
        current_states = self._document_states()
        recorded_states = previous.get("document_states") or {}
        document_states: dict[str, Any] = {}
        for name, state in current_states.items():
            if name in PROJECT_DOCUMENT_NAMES and name in recorded_states:
                document_states[name] = recorded_states[name]
            else:
                document_states[name] = state
        for name in PROJECT_DOCUMENT_NAMES:
            if name in recorded_states and name not in current_states:
                document_states[name] = recorded_states[name]
        index = dict(previous)
        index.update({
            "schema_version": SCHEMA_VERSION,
            "project_id": str(status_meta.get("project_id") or previous.get("project_id") or self._new_project_id()),
            "project_layer": str(status_meta.get("project_layer") or previous.get("project_layer") or "uninitialized"),
            "docs_root": self.docs_relative,
            "active_task_ids": open_ids,
            "session_bindings": bindings,
            "tasks": tasks,
            "workstreams": workstreams,
            "document_states": document_states,
            "last_repo_fingerprint": previous.get("last_repo_fingerprint"),
            "last_reconciled_fingerprint": previous.get("last_reconciled_fingerprint"),
            "freshness": previous.get("freshness") or "unknown",
            "turn_fingerprints": previous.get("turn_fingerprints") or {},
            "pending_transaction_id": previous.get("pending_transaction_id"),
            "migration_sources": previous.get("migration_sources") or [],
            "updated_at": now_iso(),
        })
        save_json(self.paths.index, index)
        return index

    def _semantic_transaction(
        self,
        kind: str,
        target_path: Path,
        expected_revision: int | None,
        mutate: Any,
        dry_run: bool,
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        target_meta, _ = read_document(target_path)
        require_revision(int(target_meta.get("revision")) if target_meta.get("revision") is not None else None, expected_revision, target_path.name)
        if dry_run:
            return {"applied": False, "dry_run": True, **preview}
        transaction_id = "TX-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        transaction_path = self.paths.transactions / f"{transaction_id}.json"
        with file_lock(self.paths.locks / "project-transaction.lock", None):
            target_meta, _ = read_document(target_path)
            require_revision(int(target_meta.get("revision")) if target_meta.get("revision") is not None else None, expected_revision, target_path.name)
            affected_paths = mutate("paths")
            before_contents: dict[str, str] = {}
            before_states: dict[str, Any] = {}
            for path in affected_paths:
                relative = str(path.relative_to(self.root)).replace("\\", "/")
                before_contents[relative] = path.read_text(encoding="utf-8-sig") if path.exists() else ""
                before_states[relative] = file_state(path)
            transaction = {
                "schema_version": SCHEMA_VERSION,
                "transaction_id": transaction_id,
                "kind": kind,
                "status": "prepared",
                "created_at": now_iso(),
                "before_states": before_states,
                "before_contents": before_contents,
            }
            save_json(transaction_path, transaction)
            index = self.load_index()
            write_consistency = self._capture_tool_write_consistency(index, affected_paths)
            index["pending_transaction_id"] = transaction_id
            save_json(self.paths.index, index)
            try:
                result = mutate("apply")
                after_states = {
                    str(path.relative_to(self.root)).replace("\\", "/"): file_state(path)
                    for path in affected_paths
                }
                transaction.update({"status": "committed", "committed_at": now_iso(), "after_states": after_states})
                save_json(transaction_path, transaction)
                index = self.rebuild_index(index)
                self._record_tool_writes(index, write_consistency)
                index["pending_transaction_id"] = None
                save_json(self.paths.index, index)
                transaction_path.unlink(missing_ok=True)
                return {"applied": True, "transaction_id": transaction_id, **preview, **(result or {})}
            except Exception:
                raise

    def load_index(self) -> dict[str, Any]:
        index = load_json(self.paths.index, {})
        if not isinstance(index, dict) or index.get("schema_version") != SCHEMA_VERSION:
            return self.rebuild_index({})
        return index

    def _ensure_status(self, project_layer: str = "uninitialized", project_title: str | None = None) -> None:
        if self.paths.status.is_file():
            return
        project_id = self._project_id()
        created = now_iso()
        body = self._template(
            "status.md",
            PROJECT_TITLE=project_title or self.root.name,
            NEXT_ACTION="完成当前任务计划中的第一个未完成项。",
        )
        metadata = {
            "project_id": project_id,
            "document_type": "status",
            "project_layer": project_layer,
            "created_at": created,
            "updated_at": created,
            "revision": 0,
        }
        write_document(self.paths.status, metadata, body)

    def ensure_task_layer(self, force: bool = False) -> dict[str, Any]:
        if self.mode == "off" and not force:
            raise ValueError("planning is disabled by project config")
        if self.mode == "explicit-only" and not force:
            raise ValueError("planning requires an explicit request in this non-code project")
        self._ensure_status("uninitialized")
        if self.mode in {"task-only", "project"} and not (self.root / ".codex" / "planning-config.json").is_file():
            self._save_config(self.mode)
        return self.rebuild_index()

    def init_project(self, title: str, vision: str, force: bool = False) -> dict[str, Any]:
        if not vision.strip():
            raise ValueError("project vision is required; do not infer an accepted long-term goal")
        index = self.ensure_task_layer(force=True)
        status_consistency = self._capture_tool_write_consistency(index, [self.paths.status])
        project_id = self._project_id()
        created = now_iso()
        documents = {
            "PROJECT.md": ("project", self._template("project.md", PROJECT_TITLE=title, VISION=vision.strip())),
            "ROADMAP.md": ("roadmap", self._template("roadmap.md")),
            "FINDINGS.md": ("project_findings", self._template("project_findings.md")),
            "DECISIONS.md": ("decisions", self._template("decisions.md")),
        }
        for filename, (document_type, body) in documents.items():
            path = self.paths.docs_root / filename
            if path.exists() and not force:
                continue
            metadata = {
                "project_id": project_id,
                "document_type": document_type,
                "created_at": created,
                "updated_at": created,
                "revision": 0,
            }
            write_document(path, metadata, body)
        status_meta, status_body = read_document(self.paths.status)
        status_meta.update({"project_id": project_id, "project_layer": "initialized"})
        status_body = replace_section(status_body, "当前阶段", "长期项目层已初始化；当前项目阶段以 ROADMAP.md 的 active/accepted 里程碑为准。")
        write_document(self.paths.status, status_meta, status_body)
        self._save_config("project")
        index = self.rebuild_index(index)
        self._record_tool_writes(index, status_consistency)
        snapshot = repo_snapshot(self.root, self.docs_relative)
        if not index.get("last_repo_fingerprint"):
            index["last_repo_fingerprint"] = snapshot["fingerprint"]
        if not index.get("last_reconciled_fingerprint"):
            index["last_reconciled_fingerprint"] = snapshot["fingerprint"]
        index["freshness"] = "fresh"
        save_json(self.paths.index, index)
        return index

    def _new_task_id(self) -> str:
        prefix = "T-" + datetime.now().strftime("%Y%m%d") + "-"
        counters: list[int] = []
        if self.paths.tasks.is_dir():
            for child in self.paths.tasks.iterdir():
                if child.name.startswith(prefix):
                    try:
                        counters.append(int(child.name.rsplit("-", 1)[1]))
                    except ValueError:
                        pass
        return prefix + f"{(max(counters, default=0) + 1):03d}"

    def resolve_task(self, session_id: str | None = None, task_id: str | None = None, workstream: str | None = None) -> tuple[str | None, list[str]]:
        index = self.load_index()
        if task_id:
            return (task_id, []) if task_id in index.get("tasks", {}) else (None, [])
        if session_id and session_id in index.get("session_bindings", {}):
            return str(index["session_bindings"][session_id]), []
        active = list(index.get("active_task_ids") or [])
        if workstream:
            allowed = set((index.get("workstreams") or {}).get(workstream, []))
            active = [candidate for candidate in active if candidate in allowed]
        if len(active) == 1:
            return str(active[0]), []
        return None, [str(item) for item in active]

    def _update_status_tasks(self, index: dict[str, Any]) -> None:
        metadata, body = read_document(self.paths.status)
        if not metadata:
            return
        lines: list[str] = []
        for task_id, task in sorted(index.get("tasks", {}).items()):
            if task.get("status") not in OPEN_TASK_STATES:
                continue
            workstream = f" · {task['workstream']}" if task.get("workstream") else ""
            lines.append(f"- [{task_id}](tasks/{task_id}/PLAN.md) — {task.get('title')} — `{task.get('status')}`{workstream}")
        if not lines:
            lines = ["- 无活动任务。"]
        body = replace_marker_block(body, "active-tasks", lines)
        write_document(self.paths.status, metadata, body)

    def start_task(
        self,
        title: str,
        completion_policy: str = "auto",
        workstream: str = "",
        session_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if completion_policy not in COMPLETION_POLICIES:
            raise ValueError(f"invalid completion policy: {completion_policy}")
        index = self.ensure_task_layer(force=force)
        status_consistency = self._capture_tool_write_consistency(index, [self.paths.status])
        existing_id, _ = self.resolve_task(session_id=session_id)
        if existing_id and session_id:
            return {"created": False, "resumed": True, "task_id": existing_id, "path": str(self._task_plan_path(existing_id))}
        task_id = self._new_task_id()
        created = now_iso()
        task_dir = self.paths.tasks / task_id
        task_dir.mkdir(parents=True, exist_ok=False)
        snapshot = repo_snapshot(self.root, self.docs_relative)
        plan_meta = {
            "project_id": index["project_id"],
            "document_type": "task_plan",
            "task_id": task_id,
            "title": title,
            "status": "active",
            "completion_policy": completion_policy,
            "workstream": workstream,
            "created_at": created,
            "updated_at": created,
            "revision": 0,
            "last_repo_fingerprint": snapshot["fingerprint"],
            "last_reconciled_fingerprint": snapshot["fingerprint"],
        }
        write_document(self._task_plan_path(task_id), plan_meta, self._template("task_plan.md", TITLE=title))
        common_meta = {
            "project_id": index["project_id"],
            "task_id": task_id,
            "created_at": created,
            "updated_at": created,
            "revision": 0,
        }
        progress_meta = dict(common_meta, document_type="task_progress")
        findings_meta = dict(common_meta, document_type="task_findings")
        write_document(self._task_progress_path(task_id), progress_meta, self._template("task_progress.md", TITLE=title, CREATED_AT=created))
        write_document(self._task_findings_path(task_id), findings_meta, self._template("task_findings.md", TITLE=title))
        index = self.rebuild_index(index)
        if session_id:
            index.setdefault("session_bindings", {})[session_id] = task_id
            self._remember_commit_head(index, session_id)
        index["last_repo_fingerprint"] = snapshot["fingerprint"]
        index["last_reconciled_fingerprint"] = snapshot["fingerprint"]
        index["freshness"] = "fresh"
        save_json(self.paths.index, index)
        self._update_status_tasks(index)
        status_meta, status_body = read_document(self.paths.status)
        status_body = replace_section(status_body, "下一步", f"- 执行 [{task_id}](tasks/{task_id}/PLAN.md) 的第一个未完成验收项。")
        write_document(self.paths.status, status_meta, status_body)
        index = self.rebuild_index(index)
        self._record_tool_writes(index, status_consistency)
        save_json(self.paths.index, index)
        return {"created": True, "task_id": task_id, "path": str(self._task_plan_path(task_id)), "index": index}

    def _remember_commit_head(self, index: dict[str, Any], session_id: str) -> None:
        heads = index.setdefault("session_commit_heads", {})
        if session_id not in heads:
            head = run_git(self.root, "rev-parse", "--verify", "HEAD")
            heads[session_id] = head.stdout.strip() if head and head.returncode == 0 else None

    def bind_task(self, task_id: str, session_id: str, takeover: bool = False,
                  bridge_thread_id: str | None = None) -> dict[str, Any]:
        index = self.load_index()
        task = index.get("tasks", {}).get(task_id)
        if not task or task.get("status") not in OPEN_TASK_STATES:
            raise ValueError("task is not open")
        owners = [session for session, bound in index.get("session_bindings", {}).items() if bound == task_id and session != session_id]
        if owners and not takeover:
            raise ValueError(f"task already has a writer: {owners[0]}; use takeover explicitly")
        if bridge_thread_id is not None:
            bridge_endpoints(self.root, task_id, bridge_thread_id)
        if takeover:
            for owner in owners:
                index["session_bindings"].pop(owner, None)
            self._append_event(task_id, "takeover", [f"session `{session_id}` 接管任务；原 session：{', '.join(owners) or 'none'}。"])
        index.setdefault("session_bindings", {})[session_id] = task_id
        self._remember_commit_head(index, session_id)
        save_json(self.paths.index, index)
        return {"bound": True, "task_id": task_id, "session_id": session_id, "takeover": bool(owners)}

    def _append_event(self, task_id: str, event_type: str, lines: list[str],
                      fingerprint: str | None = None) -> None:
        path = self._task_plan_path(task_id).with_name("events.jsonl")
        metadata, _ = read_document(self._task_plan_path(task_id))
        if not metadata:
            raise ValueError(f"missing task plan: {task_id}")
        entry = {"time": now_iso(), "event_type": event_type, "summary": "；".join(lines),
                 "fingerprint": str(fingerprint or metadata.get("last_repo_fingerprint") or "")[:12]}
        with file_lock(self.paths.locks / f"events-{task_id}.lock", None):
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def sync_task(
        self,
        task_id: str | None = None,
        session_id: str | None = None,
        note: str | None = None,
        turn_id: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        resolved, ambiguous = self.resolve_task(session_id=session_id, task_id=task_id)
        if not resolved:
            return {"changed": False, "reason": "ambiguous active tasks" if ambiguous else "no active task", "candidates": ambiguous}
        task_id = resolved
        lock_path = self.paths.locks / f"task-{task_id}.lock"
        with file_lock(lock_path, session_id, timeout=0.25):
            plan_path = self._task_plan_path(task_id)
            plan_meta, _ = read_document(plan_path)
            if plan_meta.get("status") not in OPEN_TASK_STATES:
                return {"changed": False, "reason": "task is not open", "task_id": task_id}
            snapshot = repo_snapshot(self.root, self.docs_relative)
            fingerprint = str(snapshot["fingerprint"])
            index = self.load_index()
            stale = self._staleness(index=index, snapshot=snapshot)
            key = f"{turn_id or 'manual'}:{fingerprint}"
            turn_fingerprints = dict(index.get("turn_fingerprints") or {})
            if key in turn_fingerprints and not note:
                return {"changed": False, "reason": "duplicate turn fingerprint", "task_id": task_id, "summary": snapshot["summary"]}
            changed = fingerprint != plan_meta.get("last_repo_fingerprint") or bool(note)
            if not changed:
                return {"changed": False, "reason": "repo state unchanged", "task_id": task_id, "summary": snapshot["summary"]}
            previous_paths_by_task = dict(index.get("task_sync_paths") or {})
            had_previous_paths = task_id in previous_paths_by_task
            previous_paths = set(previous_paths_by_task.get(task_id) or [])
            current_paths = set(snapshot.get("changed_paths") or [])
            untracked_paths = set(snapshot.get("untracked_paths") or [])
            added_paths = sorted(current_paths - previous_paths)
            cleared_paths = sorted(previous_paths - current_paths)
            tracked_count = len(current_paths - untracked_paths)
            compact_parts = [f"当前 {len(current_paths)} 项变更（{tracked_count} tracked，{len(untracked_paths)} untracked）"]
            if had_previous_paths:
                if added_paths:
                    shown = "、".join(f"`{path}`" for path in added_paths[:20])
                    compact_parts.append(f"新增 {shown}" + (f"，另 {len(added_paths) - 20} 项" if len(added_paths) > 20 else ""))
                if cleared_paths:
                    shown = "、".join(f"`{path}`" for path in cleared_paths[:20])
                    compact_parts.append(f"不再变更 {shown}" + (f"，另 {len(cleared_paths) - 20} 项" if len(cleared_paths) > 20 else ""))
            else:
                compact_parts.append("首次同步仅记录总数")
            compact_summary = "；".join(compact_parts)
            lines = []
            if note:
                lines.append(note)
            if tool_name:
                lines.append(f"工具：`{tool_name}`。")
            lines.extend([
                f"代码状态：{compact_summary}。",
                f"验证模式：`{snapshot['mode']}`；fingerprint `{fingerprint[:12]}`。",
            ])
            self._append_event(task_id, "code-sync", lines, fingerprint)
            update_document_metadata(plan_path, last_repo_fingerprint=fingerprint)
            index = self.rebuild_index(index)
            index["last_repo_fingerprint"] = fingerprint
            if stale["status"] in {"semantic-drift", "conflict"}:
                index["freshness"] = stale["status"]
                index["semantic_conflicts"] = stale["document_drift"]
            else:
                index["freshness"] = "fresh" if fingerprint == index.get("last_reconciled_fingerprint") else "reconciliation-pending"
            previous_paths_by_task[task_id] = sorted(current_paths)
            index["task_sync_paths"] = previous_paths_by_task
            turn_fingerprints[key] = now_iso()
            index["turn_fingerprints"] = dict(list(turn_fingerprints.items())[-30:])
            save_json(self.paths.index, index)
            return {"changed": True, "task_id": task_id, "summary": compact_summary, "mode": snapshot["mode"], "fingerprint": fingerprint}

    def set_task_status(self, task_id: str, status: str, reason: str | None = None) -> dict[str, Any]:
        if status not in TASK_STATES:
            raise ValueError(f"invalid task status: {status}")
        path = self._task_plan_path(task_id)
        metadata, _ = read_document(path)
        current = str(metadata.get("status") or "")
        allowed = {
            "draft": {"active", "cancelled"},
            "active": {"blocked", "paused", "ready_for_review", "completed", "cancelled"},
            "blocked": {"active", "paused", "cancelled"},
            "paused": {"active", "cancelled"},
            "ready_for_review": {"active", "completed", "cancelled"},
            "completed": {"active"},
            "cancelled": {"active"},
        }
        if status != current and status not in allowed.get(current, set()):
            raise ValueError(f"illegal task transition: {current} -> {status}")
        previous_index = self.load_index()
        status_consistency = self._capture_tool_write_consistency(previous_index, [self.paths.status])
        update_document_metadata(path, status=status)
        if reason:
            self._append_event(task_id, "status-change", [f"`{current}` → `{status}`：{reason}"])
        index = self.rebuild_index()
        for session, bound in list(index.get("session_bindings", {}).items()):
            if bound == task_id and status not in OPEN_TASK_STATES:
                index["session_bindings"].pop(session, None)
        save_json(self.paths.index, index)
        # Executor transitions own task files only; the planner reconciles STATUS.
        policy = load_json(policy_path(self.root), {})
        if not policy.get("enforce_executor_role"):
            self._update_status_tasks(index)
            index = self.rebuild_index(index)
            self._record_tool_writes(index, status_consistency)
        save_json(self.paths.index, index)
        if status in {"completed", "cancelled"}:
            bridge_endpoints(self.root, task_id, remove=True)
        return {"task_id": task_id, "previous": current, "status": status}

    def complete_task(
        self,
        task_id: str | None,
        session_id: str | None,
        review_approved: bool = False,
        project_impact: str | None = None,
        reconciliation_note: str | None = None,
        next_action: str | None = None,
        findings_reviewed: bool = False,
        findings_note: str | None = None,
        finding_id: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        resolved, ambiguous = self.resolve_task(session_id=session_id, task_id=task_id)
        if not resolved:
            return 3, {"completed": False, "reason": "ambiguous active tasks" if ambiguous else "no active task", "candidates": ambiguous}
        task_id = resolved
        current_index = self.load_index()
        if current_index.get("pending_transaction_id"):
            return 6, {
                "completed": False,
                "reason": "unfinished planning transaction exists",
                "transaction_id": current_index.get("pending_transaction_id"),
                "required": "run recover-transactions before retrying completion",
            }
        plan_path = self._task_plan_path(task_id)
        metadata, body = read_document(plan_path)
        checked, unchecked = checklist_counts(body)
        if checked == 0 or unchecked > 0:
            return 4, {"completed": False, "reason": "acceptance criteria incomplete", "checked": checked, "unchecked": unchecked, "task_id": task_id}
        _, task_findings_body = read_document(self._task_findings_path(task_id))
        pending_findings = [
            line.strip()[2:].strip()
            for line in extract_section(task_findings_body, "待晋升项目发现").splitlines()
            if line.strip().startswith("- ") and not line.strip()[2:].strip().startswith("暂无")
        ]
        if pending_findings and (not findings_reviewed or not (findings_note or "").strip()):
            return 5, {
                "completed": False,
                "reason": "task findings require explicit classification before completion",
                "pending_findings": pending_findings,
                "required": "--findings-reviewed and --findings-note describing promotion/local/discard disposition",
                "task_id": task_id,
            }
        policy = str(metadata.get("completion_policy") or "auto")
        if policy == "user-review" and not review_approved:
            result = self.set_task_status(task_id, "ready_for_review", "客观验收项已完成，等待用户最终确认。")
            index = self.load_index()
            status_consistency = self._capture_tool_write_consistency(index, [self.paths.status])
            status_meta, status_body = read_document(self.paths.status)
            status_body = replace_section(status_body, "下一步", f"- 请用户审核任务 {task_id}《{metadata.get('title')}》；未通过则 reopen-task。")
            write_document(self.paths.status, status_meta, status_body)
            index = self.rebuild_index(index)
            self._record_tool_writes(index, status_consistency)
            save_json(self.paths.index, index)
            return 0, {"completed": False, "ready_for_review": True, **result}
        status_meta, _ = read_document(self.paths.status)
        if status_meta.get("project_layer") == "initialized":
            if project_impact not in {"none", "updated"}:
                return 5, {
                    "completed": False,
                    "reason": "project reconciliation must be explicit",
                    "required": "--project-impact none|updated and --reconciliation-note",
                    "task_id": task_id,
                }
            if not (reconciliation_note or "").strip():
                return 5, {"completed": False, "reason": "reconciliation note is required", "task_id": task_id}
            if not (next_action or "").strip():
                return 5, {"completed": False, "reason": "next action is required for project completion", "task_id": task_id}
        else:
            project_impact = project_impact or "none"
            reconciliation_note = reconciliation_note or "task-only：不存在长期项目层，无需项目语义更新。"
            next_action = next_action or "等待用户提出下一项任务。"
        _, status_body = read_document(self.paths.status)
        _, project_findings = read_document(self.paths.docs_root / "FINDINGS.md")
        finding = find_entry(project_findings, finding_id) if finding_id else None
        if finding_id and (not finding_id.startswith("F-") or finding is None):
            return 5, {"completed": False, "reason": "finding ID does not exist in project FINDINGS", "task_id": task_id}
        if has_recent_block(status_body) and not finding_id:
            return 5, {"completed": False, "reason": "--finding-id is required when STATUS has a recent-verifications block", "task_id": task_id}
        if has_recent_block(status_body):
            try:
                self.render_status()
            except ValueError as exc:
                return 5, {"completed": False, "reason": str(exc), "task_id": task_id}
        transaction_id = "TX-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        transaction_path = self.paths.transactions / f"{transaction_id}.json"
        index = self.load_index()
        transaction = {
            "schema_version": SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "kind": "complete-task",
            "task_id": task_id,
            "status": "prepared",
            "created_at": now_iso(),
            "before_revision": metadata.get("revision"),
        }
        save_json(transaction_path, transaction)
        index["pending_transaction_id"] = transaction_id
        save_json(self.paths.index, index)
        sync = self.sync_task(task_id=task_id, session_id=session_id, note="任务完成前最终代码状态同步。")
        snapshot = repo_snapshot(self.root, self.docs_relative)
        reconciliation_line = (
            "project reconciliation: no semantic change — " + str(reconciliation_note)
            if project_impact == "none"
            else "project reconciliation: canonical project files updated — " + str(reconciliation_note)
        )
        findings_line = (
            "task findings classification: " + str(findings_note).strip()
            if pending_findings
            else "task findings classification: no promotion candidates"
        )
        self._append_event(task_id, "reconciliation", [findings_line, reconciliation_line], snapshot["fingerprint"])
        update_document_metadata(
            plan_path,
            status="completed",
            last_repo_fingerprint=snapshot["fingerprint"],
            last_reconciled_fingerprint=snapshot["fingerprint"],
            findings_reconciled_at=now_iso(),
            findings_reconciliation_note=findings_line,
            completed_at=now_iso(),
            completion_finding_id=finding_id or "",
        )
        index = self.rebuild_index(index)
        index["last_repo_fingerprint"] = snapshot["fingerprint"]
        index["last_reconciled_fingerprint"] = snapshot["fingerprint"]
        index["freshness"] = "fresh"
        index["pending_transaction_id"] = None
        for session, bound in list(index.get("session_bindings", {}).items()):
            if bound == task_id:
                index["session_bindings"].pop(session, None)
        save_json(self.paths.index, index)
        status_consistency = self._capture_tool_write_consistency(index, [self.paths.status])
        self._update_status_tasks(index)
        status_meta, status_body = read_document(self.paths.status)
        status_body = replace_section(status_body, "下一步", "- " + str(next_action).strip().lstrip("- "))
        write_document(self.paths.status, status_meta, status_body)
        self.render_status(apply=True, update_index=False)
        bridge_endpoints(self.root, task_id, remove=True)
        index = self.rebuild_index(index)
        self._record_tool_writes(index, status_consistency)
        index["last_repo_fingerprint"] = snapshot["fingerprint"]
        index["last_reconciled_fingerprint"] = snapshot["fingerprint"]
        index["freshness"] = "fresh"
        index["pending_transaction_id"] = None
        save_json(self.paths.index, index)
        transaction["status"] = "committed"
        transaction["committed_at"] = now_iso()
        save_json(transaction_path, transaction)
        transaction_path.unlink(missing_ok=True)
        archive_reminders = [w for w in self.lint_records()["warnings"]
                             if w["rule"] == "L8" or (w["rule"] == "L7" and task_id in w["message"])]
        return 0, {"completed": True, "task_id": task_id, "sync": sync, "fingerprint": snapshot["fingerprint"],
                   "archive_reminders": archive_reminders}

    def recover_transactions(self, rollback: bool = False) -> dict[str, Any]:
        index = self.load_index()
        transaction_id = index.get("pending_transaction_id")
        candidates = list(self.paths.transactions.glob("*.json")) if self.paths.transactions.is_dir() else []
        if not transaction_id and not candidates:
            return {"recovered": False, "reason": "no unfinished transaction"}
        recovered: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for path in candidates:
            transaction = load_json(path, {})
            if not isinstance(transaction, dict):
                recovered.append({"path": str(path), "action": "left-invalid"})
                continue
            task_id = str(transaction.get("task_id") or "")
            task_meta, _ = read_document(self._task_plan_path(task_id)) if task_id else ({}, "")
            if transaction.get("kind") == "complete-task" and task_meta.get("status") == "completed":
                action = "finalized-committed"
            elif transaction.get("kind") == "complete-task":
                action = "rolled-back-runtime-marker"
            else:
                before_contents = transaction.get("before_contents") or {}
                before_states = transaction.get("before_states") or {}
                unchanged = True
                for relative, state in before_states.items():
                    current = file_state(self.root / relative)
                    if current.get("sha256") != state.get("sha256"):
                        unchanged = False
                        break
                if unchanged:
                    action = "cleared-unapplied"
                elif rollback:
                    for relative, content in before_contents.items():
                        atomic_write_text(self.root / relative, str(content))
                    action = "rolled-back-markdown"
                else:
                    unresolved.append({
                        "transaction_id": transaction.get("transaction_id"),
                        "kind": transaction.get("kind"),
                        "action": "requires-explicit-rollback",
                    })
                    continue
            path.unlink(missing_ok=True)
            recovered.append({"transaction_id": transaction.get("transaction_id"), "task_id": task_id, "action": action})
        if unresolved:
            return {"recovered": False, "items": recovered, "unresolved": unresolved, "hint": "rerun recover-transactions --rollback after reviewing before_contents"}
        index = self.rebuild_index(index)
        index["pending_transaction_id"] = None
        save_json(self.paths.index, index)
        return {"recovered": True, "items": recovered}

    def status(self, session_id: str | None = None, workstream: str | None = None) -> dict[str, Any]:
        info = probe(self.root)
        if not self.paths.status.exists():
            return {"root": str(self.root), "probe": info, "initialized": False, "legacy_v3": self.legacy_state() is not None}
        index = self.load_index()
        task_id, ambiguous = self.resolve_task(session_id=session_id, workstream=workstream)
        status_meta, status_body = read_document(self.paths.status)
        return {
            "root": str(self.root),
            "probe": info,
            "initialized": True,
            "project_id": index.get("project_id"),
            "project_layer": status_meta.get("project_layer"),
            "freshness": index.get("freshness"),
            "task_id": task_id,
            "ambiguous_tasks": ambiguous,
            "active_task_ids": index.get("active_task_ids"),
            "workstreams": index.get("workstreams") or {},
            "selected_workstream": workstream,
            "next_actions": extract_section(status_body, "下一步"),
            "index": index,
        }

    def resume_context(self, session_id: str | None = None, workstream: str | None = None) -> dict[str, Any]:
        status = self.status(session_id=session_id, workstream=workstream)
        if not status.get("initialized"):
            return {"resume_usable": False, "reason": "planning state is not initialized", "root": str(self.root)}
        status_meta, status_body = read_document(self.paths.status)
        result: dict[str, Any] = {
            "resume_usable": True,
            "project_id": status.get("project_id"),
            "project_layer": status.get("project_layer"),
            "current_phase": extract_section(status_body, "当前阶段"),
            "active_tasks": [
                {"task_id": task_id, **status["index"].get("tasks", {}).get(task_id, {})}
                for task_id in status.get("active_task_ids") or []
            ],
            "selected_task_id": status.get("task_id"),
            "ambiguous_tasks": status.get("ambiguous_tasks") or [],
            "next_action": extract_section(status_body, "下一步"),
            "blockers": extract_section(status_body, "阻塞"),
            "freshness": status.get("freshness"),
            "workstreams": status.get("workstreams") or {},
            "bridge_endpoints": bridge_endpoints(self.root),
        }
        if status_meta.get("project_layer") == "initialized":
            _, project_body = read_document(self.paths.docs_root / "PROJECT.md")
            _, roadmap_body = read_document(self.paths.docs_root / "ROADMAP.md")
            goals = parse_entries(project_body, "G")
            milestones = parse_entries(roadmap_body, "M")
            result.update({
                "vision": extract_section(project_body, "愿景"),
                "goals": [
                    {"goal_id": item.item_id, "title": item.title, "status": item.fields.get("status")}
                    for item in goals
                ],
                "active_milestones": [
                    {"milestone_id": item.item_id, "title": item.title, "status": item.fields.get("status")}
                    for item in milestones if item.fields.get("status") in {"accepted", "active", "blocked"}
                ],
                "inactive_directions": [
                    {"item_id": item.item_id, "title": item.title, "status": item.fields.get("status"), "reason": item.fields.get("reason")}
                    for item in goals + milestones if item.fields.get("status") in {"abandoned", "superseded"}
                ],
            })
        return result

    def _staleness(self, index: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        snapshot = snapshot or repo_snapshot(self.root, self.docs_relative)
        current_documents = self._document_states()
        recorded_documents = index.get("document_states") or {}
        document_drift = sorted(
            name for name in set(current_documents) | set(recorded_documents)
            if current_documents.get(name) != recorded_documents.get(name)
        )
        if index.get("pending_transaction_id"):
            freshness = "conflict"
        elif document_drift:
            freshness = "semantic-drift"
        elif snapshot["fingerprint"] != index.get("last_repo_fingerprint"):
            freshness = "code-drift"
        elif snapshot["fingerprint"] != index.get("last_reconciled_fingerprint"):
            freshness = "reconciliation-pending"
        else:
            freshness = "fresh"
        return {"status": freshness, "fresh": freshness == "fresh", "snapshot": snapshot, "document_drift": document_drift, "last_reconciled_fingerprint": index.get("last_reconciled_fingerprint")}

    def check_staleness(self, persist: bool = True) -> dict[str, Any]:
        if not self.paths.status.exists():
            return {"status": "uninitialized", "fresh": False}
        index = self.load_index()
        result = self._staleness(index)
        if persist:
            index["freshness"] = result["status"]
            index["semantic_conflicts"] = result["document_drift"]
            save_json(self.paths.index, index)
        return result

    def acknowledge_document_drift(self, note: str, dry_run: bool = True) -> dict[str, Any]:
        if not note.strip():
            raise ValueError("acknowledgement note is required")
        index = self.load_index()
        current = self._document_states()
        recorded = index.get("document_states") or {}
        drift = sorted(name for name in set(current) | set(recorded) if current.get(name) != recorded.get(name))
        preview = {"operation": "acknowledge-document-drift", "documents": drift, "note": note}
        if dry_run:
            return {"applied": False, "dry_run": True, **preview}
        if index.get("pending_transaction_id"):
            raise ValueError("cannot acknowledge drift while a transaction is unfinished")
        code, validation = self.validate(strict=False)
        if code != 0:
            raise ValueError("documents are not resume-usable: " + "; ".join(validation["errors"]))
        index["document_states"] = current
        index["semantic_conflicts"] = []
        index["freshness"] = "reconciliation-pending" if index.get("last_repo_fingerprint") != index.get("last_reconciled_fingerprint") else "fresh"
        index.setdefault("document_acknowledgements", []).append({"at": now_iso(), "note": note, "documents": drift})
        index["document_acknowledgements"] = index["document_acknowledgements"][-20:]
        save_json(self.paths.index, index)
        return {"applied": True, **preview}

    def validate(self, strict: bool = False) -> tuple[int, dict[str, Any]]:
        errors: list[str] = []
        warnings: list[str] = []
        if not self.paths.status.is_file():
            errors.append("missing STATUS.md")
            return 2, {"resume_usable": False, "strict_ok": False, "errors": errors, "warnings": warnings}
        status_meta, status_body = read_document(self.paths.status)
        if status_meta.get("planning_schema") != SCHEMA_VERSION:
            errors.append("STATUS.md has invalid planning_schema")
        if not status_meta.get("project_id"):
            errors.append("STATUS.md is missing project_id")
        if not extract_section(status_body, "下一步"):
            errors.append("STATUS.md is missing an actionable 下一步 section")
        layer = status_meta.get("project_layer")
        if layer not in {"uninitialized", "initialized"}:
            errors.append("STATUS.md has invalid project_layer")
        if layer == "initialized":
            for filename in ("PROJECT.md", "ROADMAP.md", "FINDINGS.md", "DECISIONS.md"):
                path = self.paths.docs_root / filename
                metadata, _ = read_document(path)
                if not metadata:
                    errors.append(f"missing project document: {filename}")
                elif metadata.get("project_id") != status_meta.get("project_id"):
                    errors.append(f"project_id mismatch: {filename}")
            _, project_body = read_document(self.paths.docs_root / "PROJECT.md")
            _, roadmap_body = read_document(self.paths.docs_root / "ROADMAP.md")
            _, findings_body = read_document(self.paths.docs_root / "FINDINGS.md")
            _, decisions_body = read_document(self.paths.docs_root / "DECISIONS.md")
            for entry in parse_entries(project_body, "G") + parse_entries(roadmap_body, "M"):
                if entry.fields.get("status") not in ROADMAP_STATES:
                    errors.append(f"invalid roadmap state: {entry.item_id}")
                if entry.fields.get("status") == "superseded" and entry.fields.get("supersedes") in {None, "", "none"}:
                    errors.append(f"superseded item lacks replacement: {entry.item_id}")
            for entry in parse_entries(findings_body, "F"):
                if entry.fields.get("status") not in FINDING_TRANSITIONS:
                    errors.append(f"invalid finding state: {entry.item_id}")
            for entry in parse_entries(decisions_body, "D"):
                if entry.fields.get("status") not in DECISION_TRANSITIONS:
                    errors.append(f"invalid decision state: {entry.item_id}")
            next_actions = extract_section(status_body, "下一步")
            for entry in parse_entries(project_body, "G") + parse_entries(roadmap_body, "M"):
                if entry.fields.get("status") in {"abandoned", "superseded"} and entry.item_id in next_actions:
                    errors.append(f"next action references inactive roadmap item: {entry.item_id}")
        tasks = self._scan_tasks()
        for task_id, task in tasks.items():
            if task.get("status") not in TASK_STATES:
                errors.append(f"invalid task status: {task_id}")
            plan_meta, plan_body = read_document(self._task_plan_path(task_id))
            for companion in (self._task_progress_path(task_id), self._task_findings_path(task_id)):
                companion_meta, _ = read_document(companion)
                if not companion_meta:
                    errors.append(f"missing task companion: {companion.relative_to(self.root)}")
            if task.get("status") == "completed":
                checked, unchecked = checklist_counts(plan_body)
                if checked == 0 or unchecked:
                    errors.append(f"completed task has incomplete acceptance criteria: {task_id}")
                if plan_meta.get("last_repo_fingerprint") != plan_meta.get("last_reconciled_fingerprint"):
                    errors.append(f"completed task is not reconciled: {task_id}")
                _, task_findings_body = read_document(self._task_findings_path(task_id))
                pending_findings = [
                    line.strip()[2:].strip()
                    for line in extract_section(task_findings_body, "待晋升项目发现").splitlines()
                    if line.strip().startswith("- ") and not line.strip()[2:].strip().startswith("暂无")
                ]
                if pending_findings and not plan_meta.get("findings_reconciled_at"):
                    errors.append(f"completed task has unclassified project finding candidates: {task_id}")
        index = self.load_index()
        current_document_states = self._document_states()
        if current_document_states != (index.get("document_states") or {}):
            warnings.append("canonical documents changed outside an acknowledged semantic transaction")
            if strict:
                errors.append("strict: canonical document revision conflict")
        expected = sorted(task_id for task_id, task in tasks.items() if task.get("status") in OPEN_TASK_STATES)
        if sorted(index.get("active_task_ids") or []) != expected:
            warnings.append("index active_task_ids is stale and can be rebuilt")
            if strict:
                errors.append("strict: stale active_task_ids")
        prepared = list(self.paths.transactions.glob("*.json")) if self.paths.transactions.is_dir() else []
        if prepared or index.get("pending_transaction_id"):
            errors.append("unfinished planning transaction exists")
        resume_usable = not any(error.startswith("missing STATUS") or "下一步" in error or "invalid planning_schema" in error for error in errors)
        strict_ok = not errors and not warnings
        if strict:
            warnings.extend(self.lint_records()["warnings"])
        code = 0 if (strict_ok if strict else resume_usable) else 2
        return code, {"resume_usable": resume_usable, "strict_ok": strict_ok, "errors": errors, "warnings": warnings, "project_layer": layer, "task_count": len(tasks)}

    def lint_records(self) -> dict[str, Any]:
        return lint_records(self.root, self.paths.docs_root, _config_for(self.root)[0])

    def render_status(self, apply: bool = False, update_index: bool = True) -> dict[str, Any]:
        metadata, body = read_document(self.paths.status)
        if not has_recent_block(body):
            return {"applied": False, "reason": "recent-verifications block absent", "lines": []}
        completed = []
        for task_id, task in self._scan_tasks().items():
            if task["status"] == "completed":
                plan, _ = read_document(self._task_plan_path(task_id))
                completed.append((str(plan.get("completed_at") or plan.get("updated_at") or ""), task_id, plan))
        _, findings_body = read_document(self.paths.docs_root / "FINDINGS.md")
        lines = []
        for date, task_id, plan in sorted(completed, reverse=True)[:LIMITS["recent_tasks"]]:
            finding_id = str(plan.get("completion_finding_id") or "")
            entry = find_entry(findings_body, finding_id)
            if not entry or not finding_id.startswith("F-"):
                raise ValueError(f"{task_id} lacks an existing project F reference; STATUS was not rendered")
            title = str(plan.get("title") or task_id)
            short_title = title if len(title) <= 24 else title[:23] + "…"
            lines.append(f"- {date[:10]} {task_id}（{short_title}）：{entry.title}（{finding_id}）")
        if not completed:
            return {"applied": False, "reason": "no completed tasks; placeholder preserved", "lines": []}
        rendered = replace_marker_block(body, RECENT_MARKER, lines)
        changed = rendered != body
        if apply and changed:
            index = self.load_index()
            consistency = self._capture_tool_write_consistency(index, [self.paths.status])
            write_document(self.paths.status, metadata, rendered)
            if update_index:
                index = self.rebuild_index(index)
                self._record_tool_writes(index, consistency)
                save_json(self.paths.index, index)
        return {"applied": apply and changed, "changed": changed, "lines": lines}

    def _next_entry_id(self, prefix: str, path: Path) -> str:
        date = datetime.now().strftime("%Y%m%d")
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        matches = [int(value) for value in re.findall(rf"{re.escape(prefix)}-{date}-(\d{{3}})", text)]
        return f"{prefix}-{date}-{max(matches, default=0) + 1:03d}"

    def _next_sequential_id(self, prefix: str, path: Path) -> str:
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        matches = [int(value) for value in re.findall(rf"(?m)^##\s+{re.escape(prefix)}-(\d{{3}})\s+—", text)]
        return f"{prefix}-{max(matches, default=0) + 1:03d}"

    def _require_project(self) -> None:
        status_meta, _ = read_document(self.paths.status)
        if status_meta.get("project_layer") != "initialized":
            raise ValueError("this semantic operation requires an initialized project layer")

    def _roadmap_next_action(self) -> str:
        _, roadmap_body = read_document(self.paths.docs_root / "ROADMAP.md")
        milestones = parse_entries(roadmap_body, "M")
        for status in ("active", "accepted", "blocked"):
            for item in milestones:
                if item.fields.get("status") == status:
                    verb = "继续" if status == "active" else "开始" if status == "accepted" else "解除阻塞后继续"
                    return f"{verb} {item.item_id}《{item.title}》。"
        return "等待用户确认下一个 proposed 里程碑，或建立新的里程碑。"

    def add_goal(
        self,
        title: str,
        acceptance: str,
        status: str,
        authority: str,
        expected_revision: int | None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        self._require_project()
        if status not in {"proposed", "accepted"}:
            raise ValueError("new goal status must be proposed or accepted")
        if status == "accepted" and authority != "user":
            raise ValueError("only user authority can add an accepted goal")
        path = self.paths.docs_root / "PROJECT.md"
        goal_id = self._next_sequential_id("G", path)
        preview = {"operation": "add-goal", "goal_id": goal_id, "status": status, "authority": authority}

        def mutate(mode: str) -> Any:
            if mode == "paths":
                return [path]
            metadata, body = read_document(path)
            body = append_entry(body, goal_id, title, [
                ("Status", status), ("Authority", authority), ("Updated", now_iso()),
                ("Supersedes", "none"), ("Evidence", "not yet"),
            ], f"Acceptance: {acceptance}")
            write_document(path, metadata, body)
            return {"path": str(path)}

        return self._semantic_transaction("add-goal", path, expected_revision, mutate, dry_run, preview)

    def add_milestone(
        self,
        title: str,
        acceptance: str,
        status: str,
        authority: str,
        depends_on: str,
        expected_revision: int | None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        self._require_project()
        if status not in {"proposed", "accepted"}:
            raise ValueError("new milestone status must be proposed or accepted")
        if status == "accepted" and authority != "user":
            raise ValueError("only user authority can add an accepted milestone")
        path = self.paths.docs_root / "ROADMAP.md"
        milestone_id = self._next_sequential_id("M", path)
        preview = {"operation": "add-milestone", "milestone_id": milestone_id, "status": status, "authority": authority}

        def mutate(mode: str) -> Any:
            if mode == "paths":
                return [path]
            metadata, body = read_document(path)
            body = append_entry(body, milestone_id, title, [
                ("Status", status), ("Authority", authority), ("Updated", now_iso()),
                ("Depends on", depends_on or "none"), ("Supersedes", "none"), ("Evidence", "not yet"),
            ], f"Acceptance: {acceptance}")
            write_document(path, metadata, body)
            return {"path": str(path)}

        return self._semantic_transaction("add-milestone", path, expected_revision, mutate, dry_run, preview)

    def update_roadmap_item(
        self,
        item_id: str,
        target_status: str,
        authority: str,
        reason: str,
        evidence: str,
        replacement_id: str | None,
        expected_revision: int | None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        self._require_project()
        if target_status not in ROADMAP_STATES:
            raise ValueError("invalid roadmap status")
        target_path = self.paths.docs_root / ("PROJECT.md" if item_id.startswith("G-") else "ROADMAP.md")
        target_meta, target_body = read_document(target_path)
        item = find_entry(target_body, item_id)
        if not item:
            raise ValueError(f"roadmap item not found: {item_id}")
        current = item.fields.get("status") or "proposed"
        validate_transition(current, target_status, ROADMAP_TRANSITIONS, item_id)
        if target_status in {"accepted", "abandoned", "superseded"} and authority != "user":
            raise ValueError(f"{target_status} requires user authority")
        if target_status == "completed" and not evidence.strip():
            raise ValueError("completed roadmap item requires evidence")
        if target_status == "superseded" and not replacement_id:
            raise ValueError("superseded roadmap item requires replacement_id")
        decisions_path = self.paths.docs_root / "DECISIONS.md"
        status_path = self.paths.status
        decision_id = self._next_entry_id("D", decisions_path)
        preview = {
            "operation": "update-roadmap",
            "item_id": item_id,
            "from": current,
            "to": target_status,
            "decision_id": decision_id if target_status in {"accepted", "abandoned", "superseded"} else None,
        }

        def mutate(mode: str) -> Any:
            paths = [target_path, status_path]
            if target_status in {"accepted", "abandoned", "superseded"}:
                paths.append(decisions_path)
            if mode == "paths":
                return paths
            metadata, body = read_document(target_path)
            updates = {
                "status": target_status,
                "authority": authority,
                "updated": now_iso(),
                "evidence": evidence or item.fields.get("evidence", "not yet"),
            }
            if replacement_id:
                updates["supersedes"] = replacement_id
            body = update_entry(body, item_id, updates, f"{now_iso()} {current} -> {target_status}: {reason}")
            write_document(target_path, metadata, body)
            if target_status in {"accepted", "abandoned", "superseded"}:
                decision_meta, decision_body = read_document(decisions_path)
                decision_body = append_entry(decision_body, decision_id, f"{item_id} {target_status}", [
                    ("Status", "accepted"), ("Authority", "user"), ("Date", now_iso()),
                    ("Supersedes", replacement_id or "none"), ("Reason", reason),
                ])
                write_document(decisions_path, decision_meta, decision_body)
            status_meta, status_body = read_document(status_path)
            next_action = self._roadmap_next_action() if item_id.startswith("M-") else "复核 ROADMAP，确保所有里程碑仍服务于有效项目目标。"
            status_body = replace_section(status_body, "下一步", "- " + next_action)
            status_body = replace_section(status_body, "最近验证", f"- {now_iso()}：{item_id} `{current}` → `{target_status}`；{reason}")
            write_document(status_path, status_meta, status_body)
            return {"next_action": next_action}

        return self._semantic_transaction("update-roadmap", target_path, expected_revision, mutate, dry_run, preview)

    def record_finding(
        self,
        title: str,
        evidence: str,
        impact: str,
        status: str = "observed",
        task_id: str | None = None,
        expected_revision: int | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if self.mode != "project":
            raise ValueError("project findings require an initialized project layer; keep this in task FINDINGS.md")
        if status not in {"observed", "verified", "disputed", "invalidated", "superseded"}:
            raise ValueError("invalid finding status")
        path = self.paths.docs_root / "FINDINGS.md"
        finding_id = self._next_entry_id("F", path)
        preview = {"operation": "record-finding", "finding_id": finding_id, "status": status}

        def mutate(mode: str) -> Any:
            if mode == "paths":
                return [path]
            metadata, body = read_document(path)
            body = append_entry(body, finding_id, title, [
                ("Status", status), ("Evidence", evidence), ("Observed", now_iso()),
                ("Last verified", now_iso() if status == "verified" else "not verified"),
                ("Related task", task_id or "none"), ("Impact", impact),
            ])
            write_document(path, metadata, body)
            return {"recorded": True, "path": str(path)}

        return self._semantic_transaction("record-finding", path, expected_revision, mutate, dry_run, preview)

    def update_finding(self, finding_id: str, target_status: str, reason: str, evidence: str, expected_revision: int | None, dry_run: bool = True) -> dict[str, Any]:
        self._require_project()
        path = self.paths.docs_root / "FINDINGS.md"
        metadata, body = read_document(path)
        entry = find_entry(body, finding_id)
        if not entry:
            raise ValueError(f"finding not found: {finding_id}")
        current = entry.fields.get("status") or "observed"
        validate_transition(current, target_status, FINDING_TRANSITIONS, finding_id)
        preview = {"operation": "update-finding", "finding_id": finding_id, "from": current, "to": target_status}

        def mutate(mode: str) -> Any:
            if mode == "paths":
                return [path]
            current_meta, current_body = read_document(path)
            updates = {"status": target_status, "evidence": evidence or entry.fields.get("evidence", ""), "last verified": now_iso()}
            current_body = update_entry(current_body, finding_id, updates, f"{now_iso()} {current} -> {target_status}: {reason}")
            write_document(path, current_meta, current_body)
            return {}

        return self._semantic_transaction("update-finding", path, expected_revision, mutate, dry_run, preview)

    def record_decision(
        self,
        title: str,
        status: str,
        authority: str,
        reason: str,
        supersedes: str = "none",
        expected_revision: int | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if self.mode != "project":
            raise ValueError("project decisions require an initialized project layer")
        if status not in {"proposed", "accepted", "rejected", "superseded"}:
            raise ValueError("invalid decision status")
        if authority not in {"user", "agent"}:
            raise ValueError("authority must be user or agent")
        if status in {"accepted", "rejected"} and authority != "user":
            raise ValueError("only user authority can accept or reject a project decision")
        path = self.paths.docs_root / "DECISIONS.md"
        decision_id = self._next_entry_id("D", path)
        preview = {"operation": "record-decision", "decision_id": decision_id, "status": status}

        def mutate(mode: str) -> Any:
            if mode == "paths":
                return [path]
            metadata, body = read_document(path)
            body = append_entry(body, decision_id, title, [
                ("Status", status), ("Authority", authority), ("Date", now_iso()),
                ("Supersedes", supersedes), ("Reason", reason),
            ])
            write_document(path, metadata, body)
            return {"recorded": True, "path": str(path)}

        return self._semantic_transaction("record-decision", path, expected_revision, mutate, dry_run, preview)

    def update_decision(self, decision_id: str, target_status: str, authority: str, reason: str, supersedes: str, expected_revision: int | None, dry_run: bool = True) -> dict[str, Any]:
        self._require_project()
        if authority != "user" and target_status in {"accepted", "rejected", "superseded"}:
            raise ValueError(f"{target_status} requires user authority")
        path = self.paths.docs_root / "DECISIONS.md"
        metadata, body = read_document(path)
        entry = find_entry(body, decision_id)
        if not entry:
            raise ValueError(f"decision not found: {decision_id}")
        current = entry.fields.get("status") or "proposed"
        validate_transition(current, target_status, DECISION_TRANSITIONS, decision_id)
        preview = {"operation": "update-decision", "decision_id": decision_id, "from": current, "to": target_status}

        def mutate(mode: str) -> Any:
            if mode == "paths":
                return [path]
            current_meta, current_body = read_document(path)
            current_body = update_entry(current_body, decision_id, {
                "status": target_status, "authority": authority, "reason": reason,
                "supersedes": supersedes or entry.fields.get("supersedes", "none"), "date": now_iso(),
            }, f"{now_iso()} {current} -> {target_status}: {reason}")
            write_document(path, current_meta, current_body)
            return {}

        return self._semantic_transaction("update-decision", path, expected_revision, mutate, dry_run, preview)

    def legacy_state(self) -> dict[str, Any] | None:
        value = load_json(self.root / ".codex" / "planning" / "current" / "state.json", None)
        return value if isinstance(value, dict) else None

    def _migration_tracked_files(self) -> dict[str, Path]:
        files: dict[str, Path] = {}
        if self.paths.docs_root.is_dir():
            for path in self.paths.docs_root.rglob("*"):
                if path.is_file():
                    files[str(path.relative_to(self.root)).replace("\\", "/")] = path
        config = self.root / ".codex" / "planning-config.json"
        if config.is_file():
            files[str(config.relative_to(self.root)).replace("\\", "/")] = config
        if self.paths.index.is_file():
            files[str(self.paths.index.relative_to(self.root)).replace("\\", "/")] = self.paths.index
        return files

    def migrate_v3(self, dry_run: bool = False, session_id: str | None = None) -> dict[str, Any]:
        legacy_dir = self.root / ".codex" / "planning" / "current"
        legacy = self.legacy_state()
        if not legacy:
            return {"migrated": False, "reason": "no v3 current state"}
        if self.paths.index.exists():
            return {"migrated": False, "reason": "v4 state already exists"}
        title = str(legacy.get("title") or "Imported v3 task")
        legacy_plan = (legacy_dir / "task_plan.md").read_text(encoding="utf-8-sig") if (legacy_dir / "task_plan.md").exists() else ""
        checked, unchecked = checklist_counts(legacy_plan)
        legacy_status = str(legacy.get("status") or "active").lower()
        warnings: list[str] = []
        if legacy_status in {"complete", "completed"}:
            if checked > 0 and unchecked == 0:
                mapped_status = "completed"
            else:
                mapped_status = "ready_for_review"
                warnings.append("legacy task reported complete but acceptance criteria are incomplete; review required")
        elif legacy_status == "blocked":
            mapped_status = "blocked"
        elif legacy_status == "active":
            mapped_status = "active"
        else:
            mapped_status = "paused"
            warnings.append(f"unknown legacy status {legacy_status!r}; migrated as paused")
        preview = {
            "source": str(legacy_dir),
            "title": title,
            "legacy_status": legacy_status,
            "mapped_status": mapped_status,
            "acceptance": {"checked": checked, "unchecked": unchecked},
            "warnings": warnings,
            "target_mode": "task-only",
            "project_goals_inferred": False,
            "backup_required": True,
            "rollback_supported": True,
        }
        if dry_run:
            return {"migrated": False, "dry_run": True, **preview}
        migration_id = "MIG-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        backup_dir = self.paths.runtime_root / "migration-backups" / migration_id
        legacy_backup = backup_dir / "legacy-current"
        before_backup = backup_dir / "before"
        before_files = self._migration_tracked_files()
        legacy_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy_dir, legacy_backup)
        for relative, source in before_files.items():
            target = before_backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "migration_id": migration_id,
            "kind": "v3-current-to-v4",
            "status": "prepared",
            "created_at": now_iso(),
            "source": str(legacy_dir),
            "source_backup": str(legacy_backup),
            "before_files": {relative: file_state(path) for relative, path in before_files.items()},
            "mapping": {},
        }
        save_json(backup_dir / "MANIFEST.json", manifest)
        original_mode = self.mode
        result = self.start_task(title, session_id=session_id, force=True)
        task_id = str(result["task_id"])
        plan_path = self._task_plan_path(task_id)
        plan_meta, _ = read_document(plan_path)
        legacy_fingerprint = legacy.get("git_fingerprint")
        plan_meta.update({
            "status": mapped_status,
            "created_at": legacy.get("created_at") or plan_meta.get("created_at"),
            "legacy_updated_at": legacy.get("updated_at"),
            "legacy_session_id": legacy.get("session_id"),
            "legacy_schema_version": legacy.get("schema_version"),
        })
        if legacy_fingerprint:
            plan_meta["last_repo_fingerprint"] = legacy_fingerprint
            plan_meta["last_reconciled_fingerprint"] = legacy_fingerprint
        write_document(plan_path, plan_meta, legacy_plan or self._template("task_plan.md", TITLE=title))
        for legacy_name, target in (("findings.md", self._task_findings_path(task_id)), ("progress.md", self._task_progress_path(task_id))):
            source = legacy_dir / legacy_name
            if source.exists():
                target_meta, target_body = read_document(target)
                imported = source.read_text(encoding="utf-8-sig")
                write_document(target, target_meta, target_body.rstrip() + "\n\n## Imported v3 content\n\n" + imported.strip() + "\n")
        if original_mode == "off":
            self._save_config("off")
        elif original_mode == "explicit-only":
            self._save_config("explicit-only")
        else:
            self._save_config("task-only")
        index = self.rebuild_index(self.load_index())
        index.setdefault("migration_sources", []).append({
            "kind": "v3-current",
            "path": str(legacy_dir),
            "migrated_at": now_iso(),
            "task_id": task_id,
            "legacy_status": legacy_status,
            "mapped_status": mapped_status,
            "migration_id": migration_id,
            "backup": str(backup_dir),
            "warnings": warnings,
        })
        save_json(self.paths.index, index)
        after_files = self._migration_tracked_files()
        before_names = set(before_files)
        after_names = set(after_files)
        created_files = sorted(after_names - before_names)
        modified_files = sorted(
            relative for relative in after_names & before_names
            if file_state(after_files[relative]).get("sha256") != manifest["before_files"][relative].get("sha256")
        )
        manifest.update({
            "status": "applied",
            "applied_at": now_iso(),
            "task_id": task_id,
            "legacy_status": legacy_status,
            "mapped_status": mapped_status,
            "mapping": {
                "state.json": f"{self.docs_relative}/tasks/{task_id}/PLAN.md front matter",
                "task_plan.md": f"{self.docs_relative}/tasks/{task_id}/PLAN.md",
                "progress.md": f"{self.docs_relative}/tasks/{task_id}/PROGRESS.md",
                "findings.md": f"{self.docs_relative}/tasks/{task_id}/FINDINGS.md",
            },
            "created_files": created_files,
            "modified_files": modified_files,
            "after_states": {relative: file_state(after_files[relative]) for relative in created_files + modified_files},
        })
        save_json(backup_dir / "MANIFEST.json", manifest)
        return {"migrated": True, "task_id": task_id, "migration_id": migration_id, "backup": str(backup_dir), "mapping": manifest["mapping"], **preview}

    def rollback_v3_migration(self, migration_id: str, force: bool = False) -> dict[str, Any]:
        backup_dir = self.paths.runtime_root / "migration-backups" / migration_id
        manifest_path = backup_dir / "MANIFEST.json"
        manifest = load_json(manifest_path, None)
        if not isinstance(manifest, dict) or manifest.get("migration_id") != migration_id:
            raise ValueError("migration backup not found")
        if manifest.get("status") == "rolled-back":
            return {"rolled_back": False, "reason": "migration already rolled back", "migration_id": migration_id}
        if manifest.get("status") != "applied":
            raise ValueError("migration is not in applied state")
        tracked = list(manifest.get("created_files") or []) + list(manifest.get("modified_files") or [])
        conflicts: list[str] = []
        for relative in tracked:
            expected = (manifest.get("after_states") or {}).get(relative, {})
            current = file_state(self.root / relative)
            if current.get("sha256") != expected.get("sha256"):
                conflicts.append(relative)
        if conflicts and not force:
            return {
                "rolled_back": False,
                "reason": "v4 migration artifacts changed after migration",
                "conflicts": conflicts,
                "required": "review changes, then rerun with --force to archive them before rollback",
            }
        rollback_dir = self.paths.runtime_root / "migration-rollbacks" / migration_id
        for relative in tracked:
            current = self.root / relative
            if current.is_file():
                archive = rollback_dir / "v4-artifacts" / relative
                archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, archive)
        for relative in manifest.get("created_files") or []:
            path = self.root / relative
            if path.is_file():
                path.unlink()
        for relative in manifest.get("modified_files") or []:
            source = backup_dir / "before" / relative
            if not source.is_file():
                raise ValueError(f"missing before backup for {relative}")
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        manifest["status"] = "rolled-back"
        manifest["rolled_back_at"] = now_iso()
        manifest["rollback_archive"] = str(rollback_dir)
        manifest["forced"] = bool(force)
        manifest["conflicts_archived"] = conflicts
        save_json(manifest_path, manifest)
        return {
            "rolled_back": True,
            "migration_id": migration_id,
            "restored_files": list(manifest.get("modified_files") or []),
            "removed_created_files": list(manifest.get("created_files") or []),
            "archive": str(rollback_dir),
            "legacy_source_preserved": (self.root / ".codex" / "planning" / "current" / "state.json").is_file(),
        }

    def export_handoff(self, output: Path | None = None, force: bool = False) -> dict[str, Any]:
        status_meta, status_body = read_document(self.paths.status)
        if not status_meta:
            raise ValueError("planning state is not initialized")
        index = self.load_index()
        lines = [
            "# Project Handoff (derived)",
            "",
            f"- Source project: `{index.get('project_id')}`",
            f"- Source revision: `{status_meta.get('revision')}`",
            f"- Exported: {now_iso()}",
            "- Canonical source: `docs/project/STATUS.md` and linked planning files",
            "",
            "> This is a derived export. Do not edit it as the project source of truth.",
            "",
            status_body.strip(),
            "",
        ]
        text = "\n".join(lines)
        if output is None:
            return {"exported": True, "content": text}
        resolved = output if output.is_absolute() else (self.root / output)
        try:
            resolved.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ValueError("handoff output must stay inside project root") from exc
        if resolved.exists() and not force:
            raise ValueError("handoff output exists; use --force to replace")
        atomic_write_text(resolved, text)
        return {"exported": True, "path": str(resolved), "source_revision": status_meta.get("revision")}

    def snapshot(self, kind: str, reason: str) -> dict[str, Any]:
        allowed = {"decision", "milestone", "risk", "migration", "release"}
        if kind not in allowed:
            raise ValueError(f"snapshot kind must be one of: {', '.join(sorted(allowed))}")
        if not reason.strip():
            raise ValueError("snapshot reason is required")
        if not self.paths.status.exists():
            raise ValueError("planning state is not initialized")
        slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", reason).strip("-").lower()[:40] or "checkpoint"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.paths.docs_root / "snapshots" / f"{stamp}-{kind}-{slug}"
        counter = 1
        while destination.exists():
            destination = self.paths.docs_root / "snapshots" / f"{stamp}-{kind}-{slug}-{counter}"
            counter += 1
        source_paths: list[Path] = []
        for filename in ("PROJECT.md", "ROADMAP.md", "STATUS.md", "FINDINGS.md", "DECISIONS.md"):
            path = self.paths.docs_root / filename
            if path.exists():
                source_paths.append(path)
        index = self.load_index()
        for task_id in index.get("active_task_ids") or []:
            for filename in ("PLAN.md", "PROGRESS.md", "FINDINGS.md"):
                path = self.paths.tasks / task_id / filename
                if path.exists():
                    source_paths.append(path)
        manifest_files: dict[str, Any] = {}
        for source in source_paths:
            relative = source.relative_to(self.paths.docs_root)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            manifest_files[str(relative).replace("\\", "/")] = file_state(source)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_kind": kind,
            "snapshot_reason": reason,
            "created_at": now_iso(),
            "project_id": index.get("project_id"),
            "canonical": False,
            "files": manifest_files,
        }
        save_json(destination / "MANIFEST.json", manifest)
        return {"created": True, "path": str(destination), "kind": kind, "file_count": len(source_paths), "canonical": False}

    def import_handoff(self, source: Path, apply: bool = False) -> dict[str, Any]:
        resolved = source if source.is_absolute() else (self.root / source)
        resolved = resolved.resolve()
        if not resolved.is_file():
            raise ValueError("handoff source does not exist")
        if resolved.stat().st_size > 1024 * 1024:
            raise ValueError("handoff source exceeds 1 MiB")
        text = resolved.read_text(encoding="utf-8-sig")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        import_id = "HI-" + digest[:12]
        candidate = {
            "schema_version": SCHEMA_VERSION,
            "import_id": import_id,
            "status": "pending-reconciliation",
            "source_path": str(resolved),
            "source_sha256": digest,
            "created_at": now_iso(),
            "canonical": False,
            "candidates": {
                "tldr": extract_section(text, "TL;DR"),
                "current_state": extract_section(text, "Current State"),
                "next_actions": extract_section(text, "Next Actions"),
                "resume_prompt": extract_section(text, "Resume Prompt"),
            },
            "warning": "Imported content is unverified and cannot overwrite canonical planning files.",
        }
        if not apply:
            return {"registered": False, "dry_run": True, **candidate}
        import_path = self.paths.runtime_root / "imports" / f"{import_id}.json"
        if import_path.exists():
            existing = load_json(import_path, {})
            return {"registered": False, "duplicate": True, **existing}
        save_json(import_path, candidate)
        return {"registered": True, "path": str(import_path), **candidate}

    def resolve_handoff_import(self, import_id: str, disposition: str, authority: str, note: str) -> dict[str, Any]:
        if disposition not in {"accept-for-reconciliation", "reject"}:
            raise ValueError("invalid handoff import disposition")
        if authority != "user":
            raise ValueError("handoff import disposition requires user authority")
        if not note.strip():
            raise ValueError("handoff import disposition note is required")
        path = self.paths.runtime_root / "imports" / f"{import_id}.json"
        candidate = load_json(path, None)
        if not isinstance(candidate, dict):
            raise ValueError("handoff import not found")
        candidate["status"] = disposition
        candidate["resolved_at"] = now_iso()
        candidate["authority"] = authority
        candidate["resolution_note"] = note
        candidate["canonical"] = False
        save_json(path, candidate)
        return {"resolved": True, "import_id": import_id, "status": disposition, "canonical_changed": False}
