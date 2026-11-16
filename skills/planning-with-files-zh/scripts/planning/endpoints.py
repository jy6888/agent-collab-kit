"""Local bridge addresses; never infer or invent a planner address."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import file_lock, save_json


def bridge_endpoints(root: Path, task_id: str | None = None,
                     thread_id: str | None = None, remove: bool = False) -> dict[str, Any]:
    path = root / ".runtime/bridge-endpoints.json"
    # Serialize read/modify/replace so parallel tasks preserve each other's addresses.
    with file_lock(root / ".runtime/bridge-endpoints.lock", None):
        exists = path.exists()
        data = json.loads(path.read_text(encoding="utf-8-sig")) if exists else {
            "schema": 1, "planner": None, "executors": {},
        }
        if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("executors"), dict):
            raise ValueError("invalid bridge-endpoints.json; existing registry was not replaced")
        changed = False
        if task_id is not None:
            if remove:
                changed = task_id in data["executors"]
                data["executors"].pop(task_id, None)
            elif thread_id is not None:
                if not thread_id.strip():
                    raise ValueError("bridge thread ID must not be empty")
                changed = data["executors"].get(task_id) != thread_id
                data["executors"][task_id] = thread_id
        if not exists or changed:
            save_json(path, data)
        return data
