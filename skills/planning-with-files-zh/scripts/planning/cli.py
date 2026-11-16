from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .hooks import run_hook
from .model import LockBusy
from .service import PlanningService, probe, resolve_root


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _root(value: str | None) -> Path:
    return resolve_root(Path(value or os.getcwd()))


def _session(value: str | None) -> str | None:
    return value or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Planning with Files v4 project and task state")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("probe", "status", "resume-context", "check-staleness", "recover-transactions"):
        item = sub.add_parser(name)
        item.add_argument("--root")
        if name in {"status", "resume-context"}:
            item.add_argument("--session-id")
            item.add_argument("--workstream")
        if name == "recover-transactions":
            item.add_argument("--rollback", action="store_true")

    acknowledge_drift = sub.add_parser("acknowledge-document-drift")
    acknowledge_drift.add_argument("--root")
    acknowledge_drift.add_argument("--note", required=True)
    acknowledge_drift.add_argument("--apply", action="store_true")

    validate = sub.add_parser("validate")
    validate.add_argument("--root")
    validate.add_argument("--strict", action="store_true")
    validate.add_argument("--mode", choices=["resume", "strict"])

    lint = sub.add_parser("lint-records")
    lint.add_argument("--root")
    render = sub.add_parser("render-status")
    render.add_argument("--root")
    render.add_argument("--apply", action="store_true")

    init_project = sub.add_parser("init-project")
    init_project.add_argument("--root")
    init_project.add_argument("--title", required=True)
    init_project.add_argument("--vision", required=True)
    init_project.add_argument("--force", action="store_true")

    for name in ("init", "start-task"):
        item = sub.add_parser(name)
        item.add_argument("--root")
        item.add_argument("--title", required=True)
        item.add_argument("--completion-policy", choices=["auto", "user-review"], default="auto")
        item.add_argument("--workstream", default="")
        item.add_argument("--session-id")
        item.add_argument("--force", action="store_true")

    bind = sub.add_parser("bind-task")
    bind.add_argument("--root")
    bind.add_argument("--task-id", required=True)
    bind.add_argument("--session-id", required=True)
    bind.add_argument("--takeover", action="store_true")
    bind.add_argument("--bridge-thread-id")

    takeover = sub.add_parser("takeover-task")
    takeover.add_argument("--root")
    takeover.add_argument("--task-id", required=True)
    takeover.add_argument("--session-id", required=True)
    takeover.add_argument("--bridge-thread-id")

    for name in ("sync", "sync-task"):
        sync = sub.add_parser(name)
        sync.add_argument("--root")
        sync.add_argument("--task-id")
        sync.add_argument("--session-id")
        sync.add_argument("--note")

    for name in ("ready-for-review", "pause-task", "block-task", "cancel-task", "reopen-task"):
        item = sub.add_parser(name)
        item.add_argument("--root")
        item.add_argument("--task-id", required=True)
        item.add_argument("--reason", required=True)

    for name in ("complete", "complete-task"):
        complete = sub.add_parser(name)
        complete.add_argument("--root")
        complete.add_argument("--task-id")
        complete.add_argument("--session-id")
        complete.add_argument("--review-approved", action="store_true")
        complete.add_argument("--project-impact", choices=["none", "updated"])
        complete.add_argument("--reconciliation-note")
        complete.add_argument("--next-action")
        complete.add_argument("--findings-reviewed", action="store_true")
        complete.add_argument("--findings-note")
        complete.add_argument("--finding-id")

    finding = sub.add_parser("record-finding")
    finding.add_argument("--root")
    finding.add_argument("--title", required=True)
    finding.add_argument("--evidence", required=True)
    finding.add_argument("--impact", required=True)
    finding.add_argument("--status", choices=["observed", "verified", "disputed", "invalidated", "superseded"], default="observed")
    finding.add_argument("--task-id")
    finding.add_argument("--expected-revision", type=int, required=True)
    finding.add_argument("--apply", action="store_true")

    decision = sub.add_parser("record-decision")
    decision.add_argument("--root")
    decision.add_argument("--title", required=True)
    decision.add_argument("--status", choices=["proposed", "accepted", "rejected", "superseded"], required=True)
    decision.add_argument("--authority", choices=["user", "agent"], required=True)
    decision.add_argument("--reason", required=True)
    decision.add_argument("--supersedes", default="none")
    decision.add_argument("--expected-revision", type=int, required=True)
    decision.add_argument("--apply", action="store_true")

    goal = sub.add_parser("add-goal")
    goal.add_argument("--root")
    goal.add_argument("--title", required=True)
    goal.add_argument("--acceptance", required=True)
    goal.add_argument("--status", choices=["proposed", "accepted"], default="proposed")
    goal.add_argument("--authority", choices=["user", "agent"], default="agent")
    goal.add_argument("--expected-revision", type=int, required=True)
    goal.add_argument("--apply", action="store_true")

    milestone = sub.add_parser("add-milestone")
    milestone.add_argument("--root")
    milestone.add_argument("--title", required=True)
    milestone.add_argument("--acceptance", required=True)
    milestone.add_argument("--status", choices=["proposed", "accepted"], default="proposed")
    milestone.add_argument("--authority", choices=["user", "agent"], default="agent")
    milestone.add_argument("--depends-on", default="none")
    milestone.add_argument("--expected-revision", type=int, required=True)
    milestone.add_argument("--apply", action="store_true")

    roadmap = sub.add_parser("update-roadmap")
    roadmap.add_argument("--root")
    roadmap.add_argument("--item-id", required=True)
    roadmap.add_argument("--status", choices=["accepted", "active", "blocked", "completed", "abandoned", "superseded"], required=True)
    roadmap.add_argument("--authority", choices=["user", "agent"], required=True)
    roadmap.add_argument("--reason", required=True)
    roadmap.add_argument("--evidence", default="")
    roadmap.add_argument("--replacement-id")
    roadmap.add_argument("--expected-revision", type=int, required=True)
    roadmap.add_argument("--apply", action="store_true")

    update_finding = sub.add_parser("update-finding")
    update_finding.add_argument("--root")
    update_finding.add_argument("--finding-id", required=True)
    update_finding.add_argument("--status", choices=["verified", "disputed", "invalidated", "superseded"], required=True)
    update_finding.add_argument("--reason", required=True)
    update_finding.add_argument("--evidence", default="")
    update_finding.add_argument("--expected-revision", type=int, required=True)
    update_finding.add_argument("--apply", action="store_true")

    update_decision = sub.add_parser("update-decision")
    update_decision.add_argument("--root")
    update_decision.add_argument("--decision-id", required=True)
    update_decision.add_argument("--status", choices=["accepted", "rejected", "superseded"], required=True)
    update_decision.add_argument("--authority", choices=["user", "agent"], required=True)
    update_decision.add_argument("--reason", required=True)
    update_decision.add_argument("--supersedes", default="none")
    update_decision.add_argument("--expected-revision", type=int, required=True)
    update_decision.add_argument("--apply", action="store_true")

    migrate = sub.add_parser("migrate-v3")
    migrate.add_argument("--root")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--session-id")

    rollback_migration = sub.add_parser("rollback-v3-migration")
    rollback_migration.add_argument("--root")
    rollback_migration.add_argument("--migration-id", required=True)
    rollback_migration.add_argument("--force", action="store_true")

    export = sub.add_parser("export-handoff")
    export.add_argument("--root")
    export.add_argument("--output")
    export.add_argument("--force", action="store_true")

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--root")
    snapshot.add_argument("--kind", choices=["decision", "milestone", "risk", "migration", "release"], required=True)
    snapshot.add_argument("--reason", required=True)

    import_handoff = sub.add_parser("import-handoff")
    import_handoff.add_argument("--root")
    import_handoff.add_argument("--input", required=True)
    import_handoff.add_argument("--apply", action="store_true")

    resolve_import = sub.add_parser("resolve-handoff-import")
    resolve_import.add_argument("--root")
    resolve_import.add_argument("--import-id", required=True)
    resolve_import.add_argument("--disposition", choices=["accept-for-reconciliation", "reject"], required=True)
    resolve_import.add_argument("--authority", choices=["user", "agent"], required=True)
    resolve_import.add_argument("--note", required=True)

    hook = sub.add_parser("hook")
    hook.add_argument("event", choices=["session_start", "user_prompt_submit", "pre_tool", "post_tool", "stop"])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "hook":
        return run_hook(args.event)
    root = _root(getattr(args, "root", None))
    try:
        service = PlanningService(root)
        if args.command == "probe":
            _print(probe(root))
            return 0
        if args.command == "status":
            _print(service.status(session_id=_session(args.session_id), workstream=args.workstream))
            return 0
        if args.command == "resume-context":
            _print(service.resume_context(session_id=_session(args.session_id), workstream=args.workstream))
            return 0
        if args.command == "check-staleness":
            _print(service.check_staleness())
            return 0
        if args.command == "acknowledge-document-drift":
            _print(service.acknowledge_document_drift(args.note, dry_run=not args.apply))
            return 0
        if args.command == "recover-transactions":
            _print(service.recover_transactions(rollback=args.rollback))
            return 0
        if args.command == "validate":
            code, result = service.validate(strict=args.strict or args.mode == "strict")
            _print(result)
            return code
        if args.command == "lint-records":
            _print(service.lint_records())
            return 0
        if args.command == "render-status":
            _print(service.render_status(apply=args.apply))
            return 0
        if args.command == "init-project":
            _print(service.init_project(args.title, args.vision, force=args.force))
            return 0
        if args.command in {"init", "start-task"}:
            _print(service.start_task(args.title, args.completion_policy, args.workstream, _session(args.session_id), force=args.force))
            return 0
        if args.command == "bind-task":
            _print(service.bind_task(args.task_id, args.session_id, takeover=args.takeover, bridge_thread_id=args.bridge_thread_id))
            return 0
        if args.command == "takeover-task":
            _print(service.bind_task(args.task_id, args.session_id, takeover=True, bridge_thread_id=args.bridge_thread_id))
            return 0
        if args.command in {"sync", "sync-task"}:
            _print(service.sync_task(task_id=args.task_id, session_id=_session(args.session_id), note=args.note))
            return 0
        state_map = {
            "ready-for-review": "ready_for_review",
            "pause-task": "paused",
            "block-task": "blocked",
            "cancel-task": "cancelled",
            "reopen-task": "active",
        }
        if args.command in state_map:
            _print(service.set_task_status(args.task_id, state_map[args.command], args.reason))
            return 0
        if args.command in {"complete", "complete-task"}:
            code, result = service.complete_task(
                args.task_id,
                _session(args.session_id),
                review_approved=args.review_approved,
                project_impact=args.project_impact,
                reconciliation_note=args.reconciliation_note,
                next_action=args.next_action,
                findings_reviewed=args.findings_reviewed,
                findings_note=args.findings_note,
                finding_id=args.finding_id,
            )
            _print(result)
            return code
        if args.command == "record-finding":
            _print(service.record_finding(args.title, args.evidence, args.impact, args.status, args.task_id, args.expected_revision, dry_run=not args.apply))
            return 0
        if args.command == "record-decision":
            _print(service.record_decision(args.title, args.status, args.authority, args.reason, args.supersedes, args.expected_revision, dry_run=not args.apply))
            return 0
        if args.command == "add-goal":
            _print(service.add_goal(args.title, args.acceptance, args.status, args.authority, args.expected_revision, dry_run=not args.apply))
            return 0
        if args.command == "add-milestone":
            _print(service.add_milestone(args.title, args.acceptance, args.status, args.authority, args.depends_on, args.expected_revision, dry_run=not args.apply))
            return 0
        if args.command == "update-roadmap":
            _print(service.update_roadmap_item(args.item_id, args.status, args.authority, args.reason, args.evidence, args.replacement_id, args.expected_revision, dry_run=not args.apply))
            return 0
        if args.command == "update-finding":
            _print(service.update_finding(args.finding_id, args.status, args.reason, args.evidence, args.expected_revision, dry_run=not args.apply))
            return 0
        if args.command == "update-decision":
            _print(service.update_decision(args.decision_id, args.status, args.authority, args.reason, args.supersedes, args.expected_revision, dry_run=not args.apply))
            return 0
        if args.command == "migrate-v3":
            _print(service.migrate_v3(dry_run=args.dry_run, session_id=_session(args.session_id)))
            return 0
        if args.command == "rollback-v3-migration":
            _print(service.rollback_v3_migration(args.migration_id, force=args.force))
            return 0
        if args.command == "export-handoff":
            output = Path(args.output) if args.output else None
            _print(service.export_handoff(output, force=args.force))
            return 0
        if args.command == "snapshot":
            _print(service.snapshot(args.kind, args.reason))
            return 0
        if args.command == "import-handoff":
            _print(service.import_handoff(Path(args.input), apply=args.apply))
            return 0
        if args.command == "resolve-handoff-import":
            _print(service.resolve_handoff_import(args.import_id, args.disposition, args.authority, args.note))
            return 0
    except (ValueError, OSError, LockBusy) as exc:
        _print({"ok": False, "error": str(exc), "command": args.command, "root": str(root)})
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
