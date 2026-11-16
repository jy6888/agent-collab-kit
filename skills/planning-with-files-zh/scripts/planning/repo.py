from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any


CODE_MARKERS = {
    "pyproject.toml", "requirements.txt", "environment.yml", "environment.yaml",
    "package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "Gemfile", "mix.exs", "CMakeLists.txt", "Makefile",
}
CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".go", ".rs", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".cs", ".fs", ".fsx", ".rb", ".php", ".swift", ".scala",
    ".vue", ".svelte",
}
CODE_DIRS = {"src", "app", "apps", "lib", "libs", "packages", "tests", "test"}
SKIP_DIRS = {".git", ".codex", ".agents", ".claude", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def git_root(start: Path) -> Path | None:
    result = run_git(start, "rev-parse", "--show-toplevel")
    if result and result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return None


def code_evidence(root: Path) -> list[str]:
    evidence: list[str] = []
    for marker in sorted(CODE_MARKERS):
        if (root / marker).is_file():
            evidence.append(marker)
    for dirname in sorted(CODE_DIRS):
        if (root / dirname).is_dir():
            evidence.append(dirname + "/")
    if evidence:
        return evidence[:8]
    try:
        for child in root.iterdir():
            if child.name in SKIP_DIRS:
                continue
            if child.is_file() and child.suffix.lower() in CODE_SUFFIXES:
                evidence.append(child.name)
            elif child.is_dir():
                try:
                    for nested in child.iterdir():
                        if nested.is_file() and nested.suffix.lower() in CODE_SUFFIXES:
                            evidence.append(str(nested.relative_to(root)).replace("\\", "/"))
                            break
                except OSError:
                    continue
            if len(evidence) >= 8:
                break
    except OSError:
        pass
    return evidence


def _ignored(relative: str, docs_root: str) -> bool:
    normalized = relative.replace("\\", "/").lstrip('"')
    return normalized == ".codex/planning-config.json" or normalized.startswith(".codex/planning/") or normalized.startswith(docs_root.rstrip("/") + "/")


def _filtered_status(text: str, docs_root: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        path = line[3:].strip() if len(line) > 3 and not line.startswith("##") else line
        if _ignored(path, docs_root):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _changed_paths(status: str) -> tuple[list[str], list[str]]:
    changed: list[str] = []
    untracked: list[str] = []
    for line in status.splitlines():
        if not line or line.startswith("##") or len(line) < 4:
            continue
        marker = line[:2]
        relative = line[3:].strip()
        if " -> " in relative:
            relative = relative.rsplit(" -> ", 1)[1]
        relative = relative.strip('"').replace("\\", "/")
        if not relative:
            continue
        changed.append(relative)
        if marker == "??":
            untracked.append(relative)
    return sorted(set(changed)), sorted(set(untracked))


def _untracked_signatures(root: Path, docs_root: str) -> str:
    result = run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if not result or result.returncode != 0:
        return ""
    signatures: list[str] = []
    for relative in sorted(part for part in result.stdout.split("\0") if part):
        if _ignored(relative, docs_root):
            continue
        path = root / relative
        try:
            stat = path.stat()
            if not path.is_file():
                continue
            if stat.st_size <= 2 * 1024 * 1024:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                digest = f"large:{stat.st_size}:{stat.st_mtime_ns}"
            signatures.append(f"{relative.replace('\\', '/')}\0{digest}")
        except OSError:
            continue
        if len(signatures) >= 200:
            break
    return "\n".join(signatures)


def _git_snapshot(root: Path, docs_root: str) -> dict[str, Any] | None:
    inside = run_git(root, "rev-parse", "--is-inside-work-tree")
    if not inside or inside.returncode != 0:
        return None
    status_result = run_git(root, "status", "--short", "--branch", "--untracked-files=all")
    status = _filtered_status(status_result.stdout if status_result else "", docs_root)
    head_result = run_git(root, "rev-parse", "HEAD")
    branch_result = run_git(root, "branch", "--show-current")
    excludes = (":(exclude).codex/planning/**", ":(exclude).codex/planning-config.json", f":(exclude){docs_root}/**")
    diff_result = run_git(root, "diff", "--no-ext-diff", "--", ".", *excludes)
    staged_result = run_git(root, "diff", "--cached", "--no-ext-diff", "--", ".", *excludes)
    stat_result = run_git(root, "diff", "--stat", "--", ".", *excludes)
    staged_stat_result = run_git(root, "diff", "--cached", "--stat", "--", ".", *excludes)
    raw = "\n".join([
        status,
        diff_result.stdout if diff_result else "",
        staged_result.stdout if staged_result else "",
        _untracked_signatures(root, docs_root),
    ])
    fingerprint = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
    summary_parts = [status]
    if stat_result and stat_result.stdout.strip():
        summary_parts.append("unstaged: " + " | ".join(stat_result.stdout.strip().splitlines()))
    if staged_stat_result and staged_stat_result.stdout.strip():
        summary_parts.append("staged: " + " | ".join(staged_stat_result.stdout.strip().splitlines()))
    summary = " | ".join(part for part in summary_parts if part).strip() or "working tree clean"
    changed_paths, untracked_paths = _changed_paths(status)
    return {
        "mode": "git",
        "fingerprint": fingerprint,
        "summary": summary[:3000],
        "head": head_result.stdout.strip() if head_result and head_result.returncode == 0 else None,
        "branch": branch_result.stdout.strip() if branch_result and branch_result.returncode == 0 else None,
        "changed_paths": changed_paths,
        "untracked_paths": untracked_paths,
    }


def _non_git_snapshot(root: Path, docs_root: str) -> dict[str, Any]:
    signatures: list[str] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue and len(signatures) < 500:
        directory, depth = queue.pop(0)
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        for child in children:
            try:
                relative = str(child.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if _ignored(relative, docs_root) or child.name in SKIP_DIRS:
                continue
            if child.is_dir() and depth < 2:
                queue.append((child, depth + 1))
            elif child.is_file() and (child.suffix.lower() in CODE_SUFFIXES or child.name in CODE_MARKERS):
                try:
                    stat = child.stat()
                    signatures.append(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}")
                except OSError:
                    continue
            if len(signatures) >= 500:
                break
    raw = "\n".join(signatures)
    return {
        "mode": "event-path",
        "fingerprint": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "summary": f"non-git event-path snapshot: {len(signatures)} code files",
        "head": None,
        "branch": None,
        "changed_paths": [],
        "untracked_paths": [],
    }


def repo_snapshot(root: Path, docs_root: str) -> dict[str, Any]:
    return _git_snapshot(root, docs_root) or _non_git_snapshot(root, docs_root)
