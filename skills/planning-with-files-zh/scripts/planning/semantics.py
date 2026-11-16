from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROADMAP_STATES = {"proposed", "accepted", "active", "blocked", "completed", "abandoned", "superseded"}
ROADMAP_TRANSITIONS = {
    "proposed": {"accepted", "abandoned"},
    "accepted": {"active", "abandoned", "superseded"},
    "active": {"blocked", "completed", "abandoned", "superseded"},
    "blocked": {"active", "abandoned", "superseded"},
    "completed": {"superseded"},
    "abandoned": set(),
    "superseded": set(),
}
FINDING_STATES = {"observed", "verified", "disputed", "invalidated", "superseded"}
FINDING_TRANSITIONS = {
    "observed": {"verified", "disputed", "invalidated", "superseded"},
    "verified": {"disputed", "invalidated", "superseded"},
    "disputed": {"verified", "invalidated", "superseded"},
    "invalidated": set(),
    "superseded": set(),
}
DECISION_STATES = {"proposed", "accepted", "rejected", "superseded"}
DECISION_TRANSITIONS = {
    "proposed": {"accepted", "rejected"},
    "accepted": {"superseded"},
    "rejected": set(),
    "superseded": set(),
}


@dataclass(frozen=True)
class Entry:
    item_id: str
    title: str
    fields: dict[str, str]
    start: int
    end: int
    text: str


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "sha256": None, "revision": None}
    text = path.read_text(encoding="utf-8-sig")
    revision_match = re.search(r"(?m)^revision:\s*(?:\"?)(\d+)", text)
    return {
        "exists": True,
        "sha256": sha256_text(text),
        "revision": int(revision_match.group(1)) if revision_match else None,
    }


def parse_entries(text: str, prefix: str | None = None) -> list[Entry]:
    pattern = re.compile(r"(?m)^##\s+([A-Z]+-\d{3}|[A-Z]+-\d{8}-\d{3})\s+—\s+(.+?)\s*$")
    matches = list(pattern.finditer(text))
    entries: list[Entry] = []
    for index, match in enumerate(matches):
        item_id = match.group(1)
        if prefix and not item_id.startswith(prefix + "-"):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start() : end].rstrip()
        fields: dict[str, str] = {}
        for line in block.splitlines()[1:]:
            field_match = re.match(r"^-\s+([^:]+):\s*(.*)$", line.strip())
            if field_match:
                fields[field_match.group(1).strip().lower()] = field_match.group(2).strip()
        entries.append(Entry(item_id, match.group(2).strip(), fields, match.start(), end, block))
    return entries


def find_entry(text: str, item_id: str) -> Entry | None:
    for entry in parse_entries(text):
        if entry.item_id == item_id:
            return entry
    return None


def append_entry(text: str, item_id: str, title: str, fields: list[tuple[str, str]], details: str = "") -> str:
    if find_entry(text, item_id):
        raise ValueError(f"duplicate item id: {item_id}")
    lines = [f"## {item_id} — {title}", ""]
    lines.extend(f"- {key}: {value}" for key, value in fields)
    if details.strip():
        lines.extend(["", details.strip()])
    return text.rstrip() + "\n\n" + "\n".join(lines) + "\n"


def update_entry(text: str, item_id: str, field_updates: dict[str, str], history_line: str | None = None) -> str:
    entry = find_entry(text, item_id)
    if not entry:
        raise ValueError(f"item not found: {item_id}")
    block_lines = entry.text.splitlines()
    updated_keys: set[str] = set()
    for index, line in enumerate(block_lines):
        field_match = re.match(r"^(\s*-\s+)([^:]+):(.*)$", line)
        if not field_match:
            continue
        normalized = field_match.group(2).strip().lower()
        if normalized in field_updates:
            label = field_match.group(2).strip()
            block_lines[index] = f"- {label}: {field_updates[normalized]}"
            updated_keys.add(normalized)
    missing = [(key, value) for key, value in field_updates.items() if key not in updated_keys]
    if missing:
        insert_at = 1
        while insert_at < len(block_lines) and (not block_lines[insert_at].strip() or block_lines[insert_at].lstrip().startswith("- ")):
            insert_at += 1
        additions = [f"- {key.title()}: {value}" for key, value in missing]
        block_lines[insert_at:insert_at] = additions
    if history_line:
        block_lines.extend(["", f"- History: {history_line}"])
    replacement = "\n".join(block_lines).rstrip()
    return text[: entry.start] + replacement + "\n\n" + text[entry.end :].lstrip("\n")


def require_revision(actual: int | None, expected: int | None, document: str) -> None:
    if expected is None:
        raise ValueError(f"expected revision is required for semantic mutation: {document}")
    if actual != expected:
        raise ValueError(f"revision conflict for {document}: expected {expected}, actual {actual}")


def validate_transition(current: str, target: str, transitions: dict[str, set[str]], item_id: str) -> None:
    if current == target:
        return
    if target not in transitions.get(current, set()):
        raise ValueError(f"illegal transition for {item_id}: {current} -> {target}")
