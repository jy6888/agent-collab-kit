"""Advisory record lint. This module never changes canonical documents."""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from .semantics import parse_entries


LIMITS = {"progress_lines": 40, "decision_title": 60, "finding_body": 200,
          "evolution_lines": 25, "numeric_digits": 5, "manual_verifications": 3,
          "recent_tasks": 3, "commit_subject": 72,
          "canonical_decisions": 70, "canonical_findings": 60}
ARCHIVE_RULES = {"L7", "L8"}
CLOSED_STATUSES = {"completed", "cancelled"}
RECENT_MARKER = "recent-verifications"
# Project overrides live in .codex/planning-config.json:
#   "lint_since": "YYYY-MM-DD"   records dated earlier are history: not linted, not rewritten
#   "lint_limits": {...}         per-project overrides of LIMITS
#   "handoff_readmes": [...]     executor-facing index files checked for duplicated numbers
HANDOFF_READMES = ("docs/codex/README.md", "docs/design/codex/README.md")
FINDING_METADATA_FIELDS = {"status", "observed", "last verified", "related task"}


def _finding_body_size(entry_text: str) -> int:
    """Count free text, excluding generated metadata and Markdown field labels."""
    size = 0
    for line in entry_text.splitlines()[1:]:
        field = re.match(r"^\s*-\s+([A-Za-z][A-Za-z ]*):\s*(.*)$", line)
        if field and field.group(1).strip().casefold() in FINDING_METADATA_FIELDS:
            continue
        size += len((field.group(2) if field else line).strip())
    return size


def has_recent_block(text: str) -> bool:
    start = text.find(f"<!-- planning:{RECENT_MARKER}:start -->")
    end = text.find(f"<!-- planning:{RECENT_MARKER}:end -->")
    return start >= 0 and end > start


def lint_records(root: Path, docs: Path, config: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    config = config or {}
    since = re.sub(r"\D", "", str(config.get("lint_since") or ""))[:8]
    overrides = config.get("lint_limits") if isinstance(config.get("lint_limits"), dict) else {}
    limits = {**LIMITS, **{key: value for key, value in overrides.items() if key in LIMITS and isinstance(value, int)}}
    readmes = config.get("handoff_readmes") if isinstance(config.get("handoff_readmes"), list) else HANDOFF_READMES
    warnings: list[dict[str, Any]] = []

    def warn(path: Path, line: int, rule: str, message: str) -> None:
        warnings.append({"file": path.relative_to(root).as_posix(), "line": line,
                         "rule": rule, "message": message})

    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8-sig") if path.is_file() else ""

    def task_is_new(task_dir: Path) -> bool:
        created = re.search(r'(?m)^created_at:\s*"?(\d{4})-(\d{2})-(\d{2})', read(task_dir / "PLAN.md"))
        return not created or "".join(created.groups()) >= since

    def entry_is_new(item_id: str) -> bool:
        date = re.match(r"^[A-Z]+-(\d{8})-", item_id)
        return not date or date.group(1) >= since

    for path in sorted((docs / "tasks").glob("*/PROGRESS.md")):
        if not task_is_new(path.parent):
            continue
        lines = read(path).splitlines()
        if len(lines) > limits["progress_lines"]:
            warn(path, limits["progress_lines"] + 1, "L1", f"PROGRESS 超过 {limits['progress_lines']} 行")
        for number, line in enumerate(lines, 1):
            if re.match(r"^#{1,6}\s+会话日志(?:\s|$)", line):
                warn(path, number, "L1", "PROGRESS 含会话日志标题")

    entry_files = [(docs / "DECISIONS.md", "D", "L2"), (docs / "FINDINGS.md", "F", "L3")]
    entry_files += [(path, "F", "L3") for path in sorted((docs / "tasks").glob("*/FINDINGS.md"))
                     if task_is_new(path.parent)]
    for path, prefix, rule in entry_files:
        text = read(path)
        entries = parse_entries(text, prefix)
        for entry in entries:
            if not entry_is_new(entry.item_id):
                continue
            count = len(entry.title) if rule == "L2" else _finding_body_size(entry.text)
            limit = limits["decision_title" if rule == "L2" else "finding_body"]
            if count > limit:
                warn(path, text.count("\n", 0, entry.start) + 1, rule,
                     f"{entry.item_id} {'标题' if rule == 'L2' else '正文'}超过 {limit} 字")
        if rule == "L3" and not entries:
            # Task findings normally use list entries instead of numbered project F headings.
            for match in re.finditer(r"(?m)^- (.+(?:\n(?!- |#|\s*$).+)*)", text):
                if len(match.group(1).strip()) > limits["finding_body"]:
                    warn(path, text.count("\n", 0, match.start()) + 1, "L3", f"发现条目正文超过 {limits['finding_body']} 字")

    # L7: a decision whose every referenced task is closed should leave canonical DECISIONS,
    # unless it carries "- Keep: 原因". The newest entry stays so the CLI does not reuse its number.
    def task_status(task_id: str) -> str:
        status = re.search(r'(?m)^status:\s*"?([a-z_]+)', read(docs / "tasks" / task_id / "PLAN.md"))
        return status.group(1) if status else ""

    path = docs / "DECISIONS.md"
    text = read(path)
    decisions = parse_entries(text, "D")
    newest = max((entry.item_id for entry in decisions), default="")
    cited_by: dict[str, set[str]] = {}
    for plan in sorted((docs / "tasks").glob("*/PLAN.md")):
        for decision_id in set(re.findall(r"D-\d{8}-\d{3}", read(plan))):
            cited_by.setdefault(decision_id, set()).add(plan.parent.name)
    for entry in decisions:
        if not entry_is_new(entry.item_id) or entry.item_id == newest or entry.fields.get("keep"):
            continue
        # A decision belongs to the tasks it names plus the tasks whose PLAN cites it.
        tasks = sorted(set(re.findall(r"T-\d{8}-\d{3}", entry.text)) | cited_by.get(entry.item_id, set()))
        if tasks and all(task_status(task) in CLOSED_STATUSES for task in tasks):
            warn(path, text.count("\n", 0, entry.start) + 1, "L7",
                 f"{entry.item_id} 关联任务 {'、'.join(tasks)} 已结项，决定仍在 canonical："
                 "只属该任务的移入 archive/decisions-archive.md，仍约束后续的加一行“- Keep: 原因”")
    # L8: size backstop for decisions the task link cannot see.
    for name, prefix, key, label in (("DECISIONS.md", "D", "canonical_decisions", "决定"),
                                     ("FINDINGS.md", "F", "canonical_findings", "发现")):
        count = len(parse_entries(read(docs / name), prefix))
        if count > limits[key]:
            warn(docs / name, 1, "L8", f"canonical {label} {count} 条，超过 {limits[key]} 条：把已结项、不再约束后续的条目移入 archive/")

    evolution = root / "PROJECT_EVOLUTION.md"
    text = read(evolution)
    headings = list(re.finditer(r"(?m)^##\s+E\d{3}\b.*$", text))
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        if len(text[match.start():end].rstrip().splitlines()) > limits["evolution_lines"]:
            warn(evolution, text.count("\n", 0, match.start()) + 1, "L4", f"演进条目超过 {limits['evolution_lines']} 行")

    canonical = [docs / name for name in ("STATUS.md", "DECISIONS.md", "FINDINGS.md", "ROADMAP.md")]
    canonical += [evolution] + [root / str(name) for name in readmes if (root / str(name)).is_file()]
    # Exclude dates, IDs, version numbers, front matter and link destinations: these are references.
    facts: dict[str, dict[Path, int]] = {}
    number_pattern = re.compile(r"(?<![A-Za-z0-9_.])(?:\d+\.\d+[%％]|\d{1,3}(?:,\d{3})+|\d{5,})(?![A-Za-z0-9_.%％])")
    for path in canonical:
        lines = read(path).splitlines()
        in_meta = bool(lines and lines[0] == "---")
        for number, line in enumerate(lines, 1):
            if in_meta:
                if number > 1 and line == "---":
                    in_meta = False
                continue
            line = re.sub(r"\]\([^)]*\)", "]", line)
            line = re.sub(r"(?<![A-Za-z0-9_])[A-Z]+-\d{3,}(?:-\d+)*(?![A-Za-z0-9_])|\b\d{4}-\d{2}-\d{2}(?:T\S+)?|\bv?\d+(?:\.\d+){1,}\b(?![%％])", "", line)
            for match in number_pattern.finditer(line):
                value = match.group().replace(",", "").replace("％", "%")
                if not value.endswith("%") and len(value) < limits["numeric_digits"]:
                    continue
                key = str(Decimal(value[:-1]).normalize()) + "%" if value.endswith("%") else str(int(value))
                facts.setdefault(key, {}).setdefault(path, number)
    for value, occurrences in sorted(facts.items()):
        if len(occurrences) > 1:
            files = "、".join(path.relative_to(root).as_posix() for path in occurrences)
            for path, line in occurrences.items():
                warn(path, line, "L5", f"数字型事实 {value} 重复于 {files}")

    path = docs / "STATUS.md"
    in_section = in_block = False
    manual: list[int] = []
    for number, line in enumerate(read(path).splitlines(), 1):
        if line.startswith("## "):
            in_section = line.strip() == "## 最近验证"
        if line.strip() == f"<!-- planning:{RECENT_MARKER}:start -->":
            in_block = True
        elif line.strip() == f"<!-- planning:{RECENT_MARKER}:end -->":
            in_block = False
        elif in_section and not in_block and re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", line):
            manual.append(number)
    if len(manual) > limits["manual_verifications"]:
        warn(path, manual[limits["manual_verifications"]], "L6", f"最近验证标记块外超过 {limits['manual_verifications']} 条手写验证")
    return {"warnings": warnings}
