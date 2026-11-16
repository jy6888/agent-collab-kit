#!/usr/bin/env python3
"""Stdio MCP server for the planning CLI. Standard library only.

Every tool shells out to skills/planning-with-files-zh/scripts/planning_state.py, so the
CLI stays the single implementation and its dry-run / expected-revision rules still apply.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

KIT_ROOT = Path(__file__).resolve().parent.parent
CLI = KIT_ROOT / "skills" / "planning-with-files-zh" / "scripts" / "planning_state.py"
SERVER = {"name": "agent-collab-planning", "version": "1.0.0"}
DEFAULT_PROTOCOL = "2024-11-05"
TIMEOUT_SECONDS = 120
# Hooks read a payload from stdin and belong to the host's hook runner, not to a tool call.
BLOCKED_COMMANDS = {"hook"}

ROOT_PROPERTY = {
    "type": "string",
    "description": "Absolute path of the project directory. Required: the server does not know which project the conversation is in.",
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required,
                            "additionalProperties": False}}


TOOLS = [
    _tool("planning_resume_context",
          "Read-only. Return the project's vision, roadmap position, active tasks, blockers and next step. "
          "Call this first when starting or resuming work in a project.",
          {"root": ROOT_PROPERTY,
           "session_id": {"type": "string", "description": "Conversation ID, to resolve the task bound to it."}},
          ["root"]),
    _tool("planning_validate",
          "Read-only. Check that the planning files are recoverable; strict also checks schema, references, "
          "document drift and record-lint warnings (L1-L8).",
          {"root": ROOT_PROPERTY, "strict": {"type": "boolean", "description": "Default true."}},
          ["root"]),
    _tool("planning_lint_records",
          "Read-only. Advisory record lint: over-long progress/decisions/findings, duplicated numbers, "
          "decisions of closed tasks still in the canonical file (L7), entry pile-up (L8).",
          {"root": ROOT_PROPERTY}, ["root"]),
    _tool("planning_run",
          "Run any other planning CLI command (start-task, bind-task, sync, ready-for-review, block-task, "
          "complete, record-decision, record-finding, acknowledge-document-drift, ...). Semantic commands "
          "preview by default and only write when args contain --apply with the right --expected-revision. "
          "Use command 'help' to list commands, or args ['--help'] for one command's options.",
          {"root": ROOT_PROPERTY,
           "command": {"type": "string", "description": "CLI subcommand, e.g. 'start-task', or 'help'."},
           "args": {"type": "array", "items": {"type": "string"},
                    "description": "Arguments after the subcommand, one token per item. Do not pass --root."}},
          ["root", "command"]),
]


def run_cli(root: str, command: str, args: list[str]) -> tuple[bool, str]:
    if command in BLOCKED_COMMANDS:
        return False, f"'{command}' is run by the host's hook runner, not through this server."
    if any(arg == "--root" or arg.startswith("--root=") for arg in args):
        return False, "Pass the project directory as 'root', not as --root in args."
    project = Path(root)
    if not project.is_absolute() or not project.is_dir():
        return False, f"root must be an existing absolute directory: {root}"
    argv = [sys.executable, "-s", str(CLI)]
    argv += ["--help"] if command == "help" else [command, *args]
    if command != "help" and "--help" not in args and "-h" not in args:
        argv += ["--root", str(project)]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              cwd=str(project), env=env, timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"planning CLI timed out after {TIMEOUT_SECONDS}s"
    output = done.stdout.strip()
    if done.stderr.strip():
        output = (output + "\n" if output else "") + "[stderr] " + done.stderr.strip()
    # The CLI reports refusals (stale revision, incomplete acceptance) through non-zero exit codes.
    if done.returncode != 0:
        output = f"[exit {done.returncode}] " + output
    return done.returncode == 0, output or "(no output)"


def call_tool(name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    root = arguments.get("root")
    if not isinstance(root, str) or not root:
        return False, "'root' is required."
    if name == "planning_resume_context":
        session = arguments.get("session_id")
        return run_cli(root, "resume-context", ["--session-id", session] if session else [])
    if name == "planning_validate":
        return run_cli(root, "validate", ["--strict"] if arguments.get("strict", True) else [])
    if name == "planning_lint_records":
        return run_cli(root, "lint-records", [])
    if name == "planning_run":
        command = arguments.get("command")
        args = arguments.get("args") or []
        if not isinstance(command, str) or not command:
            return False, "'command' is required."
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            return False, "'args' must be a list of strings."
        return run_cli(root, command, args)
    return False, f"Unknown tool: {name}"


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None  # notifications (e.g. notifications/initialized) get no response
    params = message.get("params") or {}

    def result(value: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": value}

    if method == "initialize":
        return result({"protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                       "capabilities": {"tools": {}}, "serverInfo": SERVER})
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        ok, text = call_tool(str(params.get("name")), params.get("arguments") or {})
        return result({"content": [{"type": "text", "text": text}], "isError": not ok})
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32700, "message": "Parse error"}}
        else:
            response = handle(message) if isinstance(message, dict) else None
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
