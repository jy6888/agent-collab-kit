from __future__ import annotations

import json
import os
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .model import LockBusy, checklist_counts, extract_section, load_json, now_iso, read_document, save_json
from .repo import repo_snapshot, run_git
from .records import LIMITS
from .service import PlanningService, policy_path, probe, resolve_root


def _delegates_to_project(root: Path) -> bool:
    """A global hook yields before constructing a service or reading its index.

    A project hook that calls a shared install of this skill sets PLANNING_HOOK_SCOPE=project
    so the shared copy does the work instead of yielding to itself.
    """
    if os.environ.get("PLANNING_HOOK_SCOPE") == "project":
        return False
    local_entry = root / "tools/codex/planning-with-files-zh/scripts/planning/hooks.py"
    if Path(__file__).resolve() == local_entry.resolve():
        return False
    if policy_path(root).exists():
        return True
    config = load_json(root / ".codex/hooks.json", {})
    def has_entry(value: Any) -> bool:
        if isinstance(value, dict):
            return any((key == "command" and isinstance(item, str) and "planning_state.py" in item)
                       or has_entry(item) for key, item in value.items())
        return isinstance(value, list) and any(has_entry(item) for item in value)
    return has_entry(config)


def read_hook_payload() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        value = json.loads(raw) if raw.strip() else {}
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def emit_hook(message: str, continue_turn: bool | None = None, stop_reason: str | None = None) -> None:
    payload: dict[str, Any] = {"systemMessage": message[:4000]}
    if continue_turn is not None:
        payload["continue"] = continue_turn
    if stop_reason:
        payload["stopReason"] = stop_reason
    print(json.dumps(payload, ensure_ascii=False))


def _policy(root: Path) -> dict[str, Any]:
    value = load_json(policy_path(root), {})
    return value if isinstance(value, dict) else {}


def _matches(path: str, patterns: list[str], root: Path | None = None) -> bool:
    candidate = Path(path)
    if root is not None and candidate.is_absolute():
        try:
            path = str(candidate.resolve().relative_to(root.resolve()))
        except ValueError:
            return False
    normalized = path.replace("\\", "/").lstrip("./")
    return any(fnmatchcase(normalized, pattern.replace("\\", "/").lstrip("./")) for pattern in patterns)


def _apply_patch_paths(command: str) -> list[str]:
    paths: list[str] = []
    for line in command.splitlines():
        for prefix in ("*** Add File:", "*** Update File:", "*** Delete File:"):
            if line.startswith(prefix):
                paths.append(line[len(prefix):].strip())
                break
    return paths


def _deny_planner_owned_patch(root: Path, payload: dict[str, Any]) -> bool:
    if str(payload.get("tool_name") or "") != "apply_patch":
        return False
    policy = _policy(root)
    if not policy.get("enforce_executor_role"):
        return False
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    protected = [str(item) for item in policy.get("planner_owned_paths") or []]
    blocked = sorted(path for path in _apply_patch_paths(command) if _matches(path, protected, root))
    if not blocked:
        return False
    reason = (
        "本项目中 Codex 是执行者，不能修改规划者文件：" + "、".join(blocked[:8]) +
        "。请把建议写入当前任务 PROGRESS/FINDINGS 的“需规划者决定”，由 Claude 维护长期规划。"
    )
    print(json.dumps({
        "systemMessage": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }, ensure_ascii=False))
    return True


def _experiment_commit_guard(service: PlanningService, payload: dict[str, Any]) -> bool:
    policy = _policy(service.root)
    message = str(payload.get("last_assistant_message") or "").casefold()
    terms = [str(item).casefold() for item in policy.get("experiment_completion_terms") or []]
    if not message or not any(term in message for term in terms):
        return False
    snapshot = repo_snapshot(service.root, service.docs_relative)
    executor_patterns = [str(item) for item in policy.get("executor_owned_paths") or []]
    dirty = sorted(path for path in snapshot.get("changed_paths") or [] if _matches(path, executor_patterns, service.root))
    if not dirty:
        return False
    shown = "、".join(dirty[:12])
    extra = f"，另 {len(dirty) - 12} 项" if len(dirty) > 12 else ""
    reason = (
        "检测到本轮宣告实验完成，但执行方产物尚未提交：" + shown + extra +
        "。请先只暂存本次实验的代码、测试、报告和协议产物，使用带 vX.Y.Z 的提交消息完成一次本地提交；不要混入 Claude 的规划改动。"
    )
    print(json.dumps({"decision": "block", "reason": reason, "systemMessage": reason}, ensure_ascii=False))
    return True


def _legacy_sync(service: PlanningService, note: str | None = None) -> dict[str, Any]:
    state_path = service.root / ".codex" / "planning" / "current" / "state.json"
    progress_path = service.root / ".codex" / "planning" / "current" / "progress.md"
    state = load_json(state_path, {})
    if not isinstance(state, dict) or state.get("status") != "active":
        return {"changed": False, "reason": "no active legacy task"}
    snapshot = repo_snapshot(service.root, service.docs_relative)
    changed = snapshot["fingerprint"] != state.get("git_fingerprint") or bool(note)
    if changed and progress_path.exists():
        body = progress_path.read_text(encoding="utf-8-sig")
        lines = [note] if note else []
        lines.append(f"Git/代码状态：{snapshot['summary']}")
        body += "\n\n### " + now_iso() + " — v4 兼容同步\n\n" + "\n".join(f"- {line}" for line in lines) + "\n"
        progress_path.write_text(body, encoding="utf-8", newline="\n")
        state["git_fingerprint"] = snapshot["fingerprint"]
        state["updated_at"] = now_iso()
        save_json(state_path, state)
    return {"changed": changed, "summary": snapshot["summary"]}


def _stop_guard(service: PlanningService, payload: dict[str, Any], message: str) -> None:
    session_id = str(payload.get("session_id") or "unknown")
    turn_id = str(payload.get("turn_id") or "unknown")
    runtime_path = service.paths.runtime_root / "runtime" / f"{session_id}.json"
    runtime = load_json(runtime_path, {})
    runtime = runtime if isinstance(runtime, dict) else {}
    if runtime.get("stop_guard_turn") == turn_id:
        emit_hook(message)
        return
    runtime["stop_guard_turn"] = turn_id
    runtime["updated_at"] = now_iso()
    save_json(runtime_path, runtime)
    emit_hook(message, continue_turn=False, stop_reason="Planning state has an unfinished transaction or contradiction; reconcile once before stopping.")


def _project_summary(service: PlanningService, session_id: str | None = None) -> str:
    context = service.resume_context(session_id=session_id)
    if context.get("project_layer") != "initialized":
        return ""
    goals = ", ".join(
        f"{item['goal_id']} {item['title']}({item['status']})"
        for item in context.get("goals", [])
    ) or str(context.get("vision") or "未结构化愿景")
    milestones = ", ".join(
        f"{item['milestone_id']} {item['title']}({item['status']})"
        for item in context.get("active_milestones", [])
    ) or "无 active/accepted milestone"
    inactive = ", ".join(
        f"{item['item_id']} {item['title']}({item['status']})"
        for item in context.get("inactive_directions", [])
    ) or "无"
    return f"项目目标：{goals[:260]}；当前路线：{milestones[:260]}；已放弃/替代：{inactive[:220]}；下一步：{str(context.get('next_action') or '')[:260]}"


def _commit_subject_reminder(service: PlanningService, session_id: str) -> str:
    index = service.load_index()
    heads = index.get("session_commit_heads") or {}
    if session_id not in heads:
        service._remember_commit_head(index, session_id)
        save_json(service.paths.index, index)
        return ""
    base = heads[session_id]
    if base:
        ancestor = run_git(service.root, "merge-base", "--is-ancestor", base, "HEAD")
        if not ancestor or ancestor.returncode != 0:
            return "提交标题检查：会话基线已不在当前历史中，未扫描旧提交。"
    result = run_git(service.root, "log", "--format=%h%x00%s", f"{base}..HEAD" if base else "HEAD")
    if not result or result.returncode != 0:
        return ""
    long_subjects = []
    for line in result.stdout.splitlines():
        commit, separator, subject = line.partition("\0")
        if separator and len(subject) > LIMITS["commit_subject"]:
            long_subjects.append(f"{commit}（{len(subject)} 字）")
    return "提交标题超过 72 字，请缩短：" + "、".join(long_subjects) + "。此项只提醒。" if long_subjects else ""


def run_hook(event: str) -> int:
    payload = read_hook_payload()
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    root = resolve_root(cwd)
    if _delegates_to_project(root):
        return 0
    info = probe(root)
    service = PlanningService(root)
    if event == "pre_tool" and _deny_planner_owned_patch(root, payload):
        return 0
    if info["mode"] in {"off", "explicit-only"} and not service.paths.status.exists():
        return 0
    session_id = str(payload.get("session_id") or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID") or "") or None
    turn_id = str(payload.get("turn_id") or "") or None
    tool_name = str(payload.get("tool_name") or "") or None

    if not service.paths.status.exists():
        legacy = service.legacy_state()
        if legacy and legacy.get("status") == "active":
            title = str(legacy.get("title") or "legacy task")
            if event in {"session_start", "user_prompt_submit", "pre_tool"}:
                emit_hook(f"[planning-with-files-zh] 检测到 v3 活跃任务《{title}》。继续兼容记录；在合适检查点运行 `migrate-v3`，迁移不会推断长期项目目标。")
            elif event == "post_tool":
                result = _legacy_sync(service)
                if result.get("changed"):
                    emit_hook("[planning-with-files-zh] 已用 v4 兼容路径同步 v3 任务代码状态；建议迁移到稳定 task ID。")
            elif event == "stop":
                _legacy_sync(service, "Stop 前最终兼容同步。")
            return 0
        return 0

    index = service.load_index()
    if not session_id or session_id not in index.get("session_bindings", {}):
        reminder = _commit_subject_reminder(service, session_id) if event == "stop" and session_id in (index.get("session_commit_heads") or {}) else ""
        emit_hook("[planning-with-files-zh] 当前会话未绑定任务；请先执行 bind-task。本次跳过自动同步，不选择唯一 active 任务。" + reminder)
        return 0
    if event == "session_start" and session_id not in (index.get("session_commit_heads") or {}):
        service._remember_commit_head(index, session_id)
        save_json(service.paths.index, index)
    status = service.status(session_id=session_id)
    task_id = status.get("task_id")
    ambiguous = status.get("ambiguous_tasks") or []
    if ambiguous:
        emit_hook(f"[planning-with-files-zh] 活动任务存在歧义：{', '.join(ambiguous)}。先显式 bind-task，不按最近修改时间猜测。")
        return 0
    if not task_id:
        if event == "session_start" and status.get("project_layer") == "initialized":
            emit_hook(f"[planning-with-files-zh] 已恢复项目层，当前无活动任务。{_project_summary(service, session_id)[:1100]}")
        elif event == "user_prompt_submit" and status.get("project_layer") == "initialized":
            emit_hook("[planning-with-files-zh] 当前项目层已初始化但没有活动任务。若用户改变长期目标、范围、决定、约束或放弃项，请直接更新 canonical 项目文件并保留历史。")
        elif event == "pre_tool" and status.get("project_layer") == "initialized":
            emit_hook("[planning-with-files-zh] 当前没有活动任务；写代码前先 start-task，纯项目规划文件更新可以继续。")
        return 0

    index = status["index"]
    task = index.get("tasks", {}).get(task_id, {})
    title = str(task.get("title") or task_id)
    plan_meta, plan_body = read_document(service._task_plan_path(task_id))
    phase = extract_section(plan_body, "当前阶段") or "未记录"
    if event == "session_start":
        stale = service.check_staleness(persist=False)
        project_summary = _project_summary(service, session_id)
        project_read = "并读取 STATUS.md、PROJECT.md、ROADMAP.md、DECISIONS.md、FINDINGS.md；" if status.get("project_layer") == "initialized" else ""
        emit_hook(
            f"[planning-with-files-zh] 恢复任务 {task_id}《{title}》（{task.get('status')}）。"
            f"先读取 {service._task_plan_path(task_id)}、PROGRESS.md、FINDINGS.md；{project_read}"
            f"当前阶段：{phase[:260]}；freshness：{stale['status']}。{project_summary}"
            "本项目 Codex 只负责执行；长期规划和规划者文件只读。每次实验完成（包括失败结论）后，单独提交本次执行产物，提交消息带 vX.Y.Z。"
        )
    elif event == "user_prompt_submit":
        emit_hook(
            f"[planning-with-files-zh] 当前任务 {task_id}《{title}》。若本次输入明确改变长期目标、范围、决定、持续约束或放弃项，"
            "不要由 Codex 修改 canonical 项目文件；把需变更事项写入任务 PROGRESS/FINDINGS，交给 Claude 处理。每次实验完成后单独提交执行产物。"
        )
    elif event == "pre_tool":
        stale = service.check_staleness(persist=False)
        project_summary = _project_summary(service, session_id)
        emit_hook(f"[planning-with-files-zh] {task_id}《{title}》；阶段：{phase[:240]}；状态：{stale['status']}。写入应符合任务验收条件。{project_summary}")
    elif event == "post_tool":
        try:
            result = service.sync_task(session_id=session_id, turn_id=turn_id, tool_name=tool_name)
        except LockBusy:
            emit_hook(f"[planning-with-files-zh] {task_id} 进度锁正在被另一 session 使用；未覆盖其状态，Stop 时将重试同步。")
            return 0
        if result.get("changed"):
            emit_hook(f"[planning-with-files-zh] 已去重同步 {task_id} 的机械代码状态；需要长期语义变化时由代理执行项目事务。{str(result.get('summary'))[:700]}")
    elif event == "stop":
        if _experiment_commit_guard(service, payload):
            return 0
        reminder = _commit_subject_reminder(service, session_id)
        try:
            service.sync_task(session_id=session_id, turn_id=turn_id)
        except LockBusy:
            pass
        _, validation = service.validate(strict=True)
        policy = str(service.config.get("stop_policy") or "advisory")
        if validation["errors"]:
            message = "[planning-with-files-zh] 结束前检查发现：" + "；".join(validation["errors"][:4]) + reminder
            if policy == "strict" and any("transaction" in item or "completed task" in item for item in validation["errors"]):
                _stop_guard(service, payload, message)
            else:
                emit_hook(message)
        else:
            checked, unchecked = checklist_counts(plan_body)
            if unchecked:
                emit_hook(f"[planning-with-files-zh] {task_id} 仍有 {unchecked} 个未完成验收项；这是正常暂停，不会为了消除提醒自动标记完成。" + reminder)
            elif plan_meta.get("completion_policy") == "user-review" and plan_meta.get("status") != "ready_for_review":
                emit_hook(f"[planning-with-files-zh] {task_id} 的客观验收项已完成；将状态更新为 ready_for_review 后等待用户确认。" + reminder)
            elif reminder:
                emit_hook("[planning-with-files-zh] " + reminder)
    return 0
