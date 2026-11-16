"""Watch the bridge state directory and print new messages addressed to the planner.

Claude Code holds bridge messages in auto mode, and project/local settings cannot
switch that to accept. The planning session runs this script under its Monitor
tool instead: each printed line wakes the planner. Read-only; nothing is sent.

Usage: python -I -X utf8 <kit>/tools/bridge_inbox_watch.py [--root <project>] [--once]
The project directory defaults to the current directory; the endpoint registry is
<project>/.runtime/bridge-endpoints.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[sys.argv.index("--root") + 1] if "--root" in sys.argv[:-1] else os.getcwd()).resolve()
REGISTRY = ROOT / ".runtime" / "bridge-endpoints.json"
SEEN = ROOT / ".runtime" / "bridge-inbox-seen.json"
STATE_DIR = Path(os.environ.get(
    "CODEX_CLAUDE_BRIDGE_STATE_DIR",
    Path.home() / ".local" / "share" / "codex-claude-desktop-bridge",
))
INTERVAL = 5


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def planner_id() -> str | None:
    return (load(REGISTRY, {}).get("planner") or {}).get("session_id")


def scan(target: str):
    for state in STATE_DIR.glob("*/state.json"):
        for msg in (load(state, {}).get("messages") or {}).values():
            route = msg.get("route") or {}
            if msg.get("direction") == "to_claude" and (route.get("to") or {}).get("id") == target:
                yield msg


def main() -> None:
    once = "--once" in sys.argv
    seen = set(load(SEEN, []))
    while True:
        target = planner_id()
        if target:
            fresh = sorted((m for m in scan(target) if m.get("id") not in seen), key=lambda m: m.get("createdAt", 0))
            for msg in fresh:
                sender = ((msg.get("route") or {}).get("from") or {}).get("id")
                body = (msg.get("message") or "").replace("\n", " ⏎ ")
                print(f"BRIDGE-INBOX from={sender} id={msg.get('id')} :: {body}", flush=True)
                seen.add(msg.get("id"))
            if fresh:
                SEEN.parent.mkdir(parents=True, exist_ok=True)
                tmp = SEEN.with_suffix(".tmp")
                tmp.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
                tmp.replace(SEEN)
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
