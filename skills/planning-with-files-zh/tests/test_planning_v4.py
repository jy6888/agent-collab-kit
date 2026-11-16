from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(os.environ.get("PF_SKILL_ROOT") or Path(__file__).resolve().parents[1]).resolve()
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from planning.hooks import run_hook  # noqa: E402
from planning.model import LockBusy, _pid_alive, file_lock, read_document, save_json, write_document  # noqa: E402
from planning.service import PlanningService, probe  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def complete_acceptance(service: PlanningService, task_id: str) -> None:
    path = service._task_plan_path(task_id)
    metadata, body = read_document(path)
    body = body.replace("- [ ] 用可观察、可测试的条件替换本项；没有完整验收条件不得完成任务。", "- [x] 已产生并验证预期产物。")
    write_document(path, metadata, body)


class PlanningV4SmokeTests(unittest.TestCase):
    def test_non_git_task_only_runs_without_project_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('ok')\n")
            service = PlanningService(root)
            result = service.start_task("non-git smoke", session_id="s1")
            self.assertTrue(result["created"])
            self.assertFalse((root / "docs/project/PROJECT.md").exists())
            self.assertTrue((root / "docs/project/STATUS.md").exists())
            code, validation = service.validate()
            self.assertEqual(code, 0)
            self.assertTrue(validation["resume_usable"])
            self.assertEqual(service.check_staleness()["snapshot"]["mode"], "event-path")

    def test_project_upgrade_and_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "package.json", "{}\n")
            service = PlanningService(root)
            service.start_task("project task", session_id="s1")
            service.init_project("Example", "用户确认的长期目标")
            self.assertTrue((root / "docs/project/PROJECT.md").exists())
            decision_revision = read_document(root / "docs/project/DECISIONS.md")[0]["revision"]
            with self.assertRaises(ValueError):
                service.record_decision("Agent cannot accept", "accepted", "agent", "invalid", expected_revision=decision_revision, dry_run=False)
            decision = service.record_decision("Use v4", "accepted", "user", "user confirmed", expected_revision=decision_revision, dry_run=False)
            finding_revision = read_document(root / "docs/project/FINDINGS.md")[0]["revision"]
            finding = service.record_finding("Environment fact", "test output", "affects setup", "verified", expected_revision=finding_revision, dry_run=False)
            self.assertTrue(decision["decision_id"].startswith("D-"))
            self.assertTrue(finding["finding_id"].startswith("F-"))
            code, validation = service.validate(strict=True)
            self.assertEqual(code, 0, validation)

    def test_goal_and_milestone_state_machine_preserves_abandoned_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            project_revision = read_document(root / "docs/project/PROJECT.md")[0]["revision"]
            preview = service.add_goal("Ship client", "release artifact verified", "accepted", "user", project_revision, dry_run=True)
            self.assertTrue(preview["dry_run"])
            self.assertNotIn("G-001", (root / "docs/project/PROJECT.md").read_text(encoding="utf-8"))
            goal = service.add_goal("Ship client", "release artifact verified", "accepted", "user", project_revision, dry_run=False)
            self.assertEqual(goal["goal_id"], "G-001")

            roadmap_revision = read_document(root / "docs/project/ROADMAP.md")[0]["revision"]
            milestone = service.add_milestone("Old direction", "verified", "accepted", "user", "none", roadmap_revision, dry_run=False)
            roadmap_revision = read_document(root / "docs/project/ROADMAP.md")[0]["revision"]
            service.update_roadmap_item(milestone["milestone_id"], "active", "agent", "implementation started", "branch", None, roadmap_revision, dry_run=False)
            roadmap_revision = read_document(root / "docs/project/ROADMAP.md")[0]["revision"]
            abandoned = service.update_roadmap_item(milestone["milestone_id"], "abandoned", "user", "user rejected this direction", "user instruction", None, roadmap_revision, dry_run=False)
            roadmap_text = (root / "docs/project/ROADMAP.md").read_text(encoding="utf-8")
            self.assertIn("- Status: abandoned", roadmap_text)
            self.assertIn("user rejected this direction", roadmap_text)
            self.assertNotIn("继续 M-001", abandoned["next_action"])
            self.assertIn("M-001 abandoned", (root / "docs/project/DECISIONS.md").read_text(encoding="utf-8"))

    def test_semantic_revision_conflict_prevents_stale_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            revision = read_document(root / "docs/project/ROADMAP.md")[0]["revision"]
            service.add_milestone("One", "done", "proposed", "agent", "none", revision, dry_run=False)
            with self.assertRaisesRegex(ValueError, "revision conflict"):
                service.add_milestone("Stale", "done", "proposed", "agent", "none", revision, dry_run=False)

    def test_finding_and_decision_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            finding_revision = read_document(root / "docs/project/FINDINGS.md")[0]["revision"]
            finding = service.record_finding("Missing tool", "probe", "setup", "observed", expected_revision=finding_revision, dry_run=False)
            finding_revision = read_document(root / "docs/project/FINDINGS.md")[0]["revision"]
            service.update_finding(finding["finding_id"], "verified", "rechecked", "command output", finding_revision, dry_run=False)
            finding_revision = read_document(root / "docs/project/FINDINGS.md")[0]["revision"]
            service.update_finding(finding["finding_id"], "invalidated", "environment changed", "new output", finding_revision, dry_run=False)
            self.assertIn("- Status: invalidated", (root / "docs/project/FINDINGS.md").read_text(encoding="utf-8"))

            decision_revision = read_document(root / "docs/project/DECISIONS.md")[0]["revision"]
            decision = service.record_decision("Candidate", "proposed", "agent", "proposal", expected_revision=decision_revision, dry_run=False)
            decision_revision = read_document(root / "docs/project/DECISIONS.md")[0]["revision"]
            service.update_decision(decision["decision_id"], "rejected", "user", "user rejected", "none", decision_revision, dry_run=False)
            self.assertIn("- Status: rejected", (root / "docs/project/DECISIONS.md").read_text(encoding="utf-8"))

    def test_explicit_init_project_works_in_non_code_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = PlanningService(root)
            result = service.init_project("Notes project", "user confirmed this long-term project")
            self.assertEqual(result["project_layer"], "initialized")
            self.assertEqual(probe(root)["mode"], "project")

    def test_auto_and_user_review_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "app.py", "value = 1\n")
            service = PlanningService(root)
            first = service.start_task("auto", session_id="auto")
            complete_acceptance(service, first["task_id"])
            code, result = service.complete_task(first["task_id"], "auto")
            self.assertEqual(code, 0, result)
            self.assertTrue(result["completed"])
            strict_code, strict = service.validate(strict=True)
            self.assertEqual(strict_code, 0, strict)

            second = service.start_task("review", completion_policy="user-review", session_id="review")
            complete_acceptance(service, second["task_id"])
            code, result = service.complete_task(second["task_id"], "review")
            self.assertEqual(code, 0, result)
            self.assertTrue(result["ready_for_review"])
            strict_code, strict = service.validate(strict=True)
            self.assertEqual(strict_code, 0, strict)
            code, result = service.complete_task(second["task_id"], "review", review_approved=True)
            self.assertEqual(code, 0, result)
            self.assertTrue(result["completed"])
            strict_code, strict = service.validate(strict=True)
            self.assertEqual(strict_code, 0, strict)

    def test_start_and_status_transition_leave_index_strictly_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('ok')\n")
            service = PlanningService(root)
            task = service.start_task("fresh ordering", session_id="s1")
            code, result = service.validate(strict=True)
            self.assertEqual(code, 0, result)
            service.set_task_status(task["task_id"], "blocked", "waiting for evidence")
            code, result = service.validate(strict=True)
            self.assertEqual(code, 0, result)

    def test_git_sync_deduplicates_same_turn_and_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            write(root / "main.py", "print('one')\n")
            subprocess.run(["git", "-C", str(root), "add", "main.py"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "v0.1 initial"], check=True)
            service = PlanningService(root)
            task = service.start_task("git smoke", session_id="s1")
            write(root / "main.py", "print('two')\n")
            first = service.sync_task(task_id=task["task_id"], session_id="s1", turn_id="turn-1", tool_name="apply_patch")
            second = service.sync_task(task_id=task["task_id"], session_id="s1", turn_id="turn-1", tool_name="apply_patch")
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertIn(second["reason"], {"duplicate turn fingerprint", "repo state unchanged"})


class PlanningV4IntegrationTests(unittest.TestCase):
    def test_off_config_is_silent_and_does_not_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            write(root / ".codex/planning-config.json", json.dumps({"auto_start": False}))
            self.assertEqual(probe(root)["mode"], "off")
            payload = json.dumps({"cwd": str(root), "session_id": "s", "turn_id": "t"})
            old_stdin = sys.stdin
            try:
                sys.stdin = io.StringIO(payload)
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_hook("session_start")
            finally:
                sys.stdin = old_stdin
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "")
            self.assertFalse((root / "docs/project/STATUS.md").exists())

    def test_off_config_suppresses_stale_legacy_v3_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            write(root / ".codex/planning-config.json", json.dumps({"auto_start": False}))
            write(root / ".codex/planning/current/state.json", json.dumps({"schema_version": 1, "title": "stale legacy", "status": "active"}))
            payload = json.dumps({"cwd": str(root), "session_id": "s", "turn_id": "t"})
            old_stdin = sys.stdin
            try:
                sys.stdin = io.StringIO(payload)
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_hook("session_start")
            finally:
                sys.stdin = old_stdin
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "")

    def test_ambiguous_tasks_are_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            one = service.start_task("one")
            two = service.start_task("two")
            task_id, candidates = service.resolve_task()
            self.assertIsNone(task_id)
            self.assertEqual(set(candidates), {one["task_id"], two["task_id"]})

    def test_workstream_filter_resolves_without_changing_global_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            api = service.start_task("api", workstream="backend")
            web = service.start_task("web", workstream="frontend")
            self.assertIsNone(service.resolve_task()[0])
            self.assertEqual(service.resolve_task(workstream="backend")[0], api["task_id"])
            self.assertEqual(service.resolve_task(workstream="frontend")[0], web["task_id"])
            self.assertEqual(service.load_index()["workstreams"]["backend"], [api["task_id"]])

    def test_completion_requires_explicit_task_finding_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            task = service.start_task("finding reconciliation", session_id="s1")
            complete_acceptance(service, task["task_id"])
            findings_path = service._task_findings_path(task["task_id"])
            metadata, body = read_document(findings_path)
            body = body.replace("- 暂无。任务完成时逐项选择：晋升、保留局部或丢弃。", "- 本机没有 foo 工具，用户明确不安装。")
            write_document(findings_path, metadata, body)
            code, result = service.complete_task(task["task_id"], "s1")
            self.assertEqual(code, 5)
            self.assertIn("classification", result["reason"])
            code, result = service.complete_task(
                task["task_id"],
                "s1",
                findings_reviewed=True,
                findings_note="promoted durable environment constraint to project findings",
            )
            self.assertEqual(code, 0, result)
            plan_meta, _ = read_document(service._task_plan_path(task["task_id"]))
            self.assertTrue(plan_meta["findings_reconciled_at"])

    def test_v3_dry_run_then_migration_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            legacy = root / ".codex/planning/current"
            write(legacy / "state.json", json.dumps({"schema_version": 1, "title": "legacy", "status": "active"}))
            write(legacy / "task_plan.md", "# legacy\n\n## 验收条件\n\n- [ ] legacy item\n")
            write(legacy / "findings.md", "# old findings\n")
            write(legacy / "progress.md", "# old progress\n")
            service = PlanningService(root)
            preview = service.migrate_v3(dry_run=True)
            self.assertTrue(preview["dry_run"])
            self.assertFalse((root / ".codex/planning/migration-backups").exists())
            result = service.migrate_v3(session_id="s1")
            self.assertTrue(result["migrated"])
            self.assertTrue(Path(result["backup"]).is_dir())
            self.assertIn("task_plan.md", result["mapping"])
            self.assertTrue((legacy / "state.json").exists())
            self.assertIn("# legacy", service._task_plan_path(result["task_id"]).read_text(encoding="utf-8"))
            task_meta, _ = read_document(service._task_plan_path(result["task_id"]))
            self.assertEqual(task_meta["status"], "active")

    def test_v3_migration_rollback_restores_pre_migration_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            legacy = root / ".codex/planning/current"
            write(legacy / "state.json", json.dumps({"schema_version": 1, "title": "legacy", "status": "active"}))
            write(legacy / "task_plan.md", "# legacy\n\n## 验收条件\n\n- [ ] item\n")
            write(legacy / "findings.md", "# findings\n")
            write(legacy / "progress.md", "# progress\n")
            service = PlanningService(root)
            result = service.migrate_v3()
            manifest = json.loads((Path(result["backup"]) / "MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "applied")
            self.assertTrue(manifest["created_files"])
            rollback = service.rollback_v3_migration(result["migration_id"])
            self.assertTrue(rollback["rolled_back"])
            self.assertTrue(rollback["legacy_source_preserved"])
            self.assertFalse(service.paths.index.exists())
            self.assertFalse(service._task_plan_path(result["task_id"]).exists())

    def test_v3_rollback_refuses_changed_v4_artifact_until_forced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            legacy = root / ".codex/planning/current"
            write(legacy / "state.json", json.dumps({"schema_version": 1, "title": "legacy", "status": "active"}))
            write(legacy / "task_plan.md", "# legacy\n\n## 验收条件\n\n- [ ] item\n")
            service = PlanningService(root)
            result = service.migrate_v3()
            plan = service._task_plan_path(result["task_id"])
            plan.write_text(plan.read_text(encoding="utf-8") + "\nnew v4 work\n", encoding="utf-8")
            refused = service.rollback_v3_migration(result["migration_id"])
            self.assertFalse(refused["rolled_back"])
            self.assertIn(str(plan.relative_to(root)).replace("\\", "/"), refused["conflicts"])
            forced = service.rollback_v3_migration(result["migration_id"], force=True)
            self.assertTrue(forced["rolled_back"])
            self.assertTrue(Path(forced["archive"]).is_dir())

    def test_v3_completed_status_is_preserved_when_acceptance_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            legacy = root / ".codex/planning/current"
            write(legacy / "state.json", json.dumps({"schema_version": 1, "title": "done", "status": "complete", "created_at": "2025-01-01T00:00:00Z", "updated_at": "2025-01-02T00:00:00Z", "session_id": "old-session"}))
            write(legacy / "task_plan.md", "# done\n\n## 验收条件\n\n- [x] shipped\n")
            write(legacy / "findings.md", "# old findings\n")
            write(legacy / "progress.md", "# old progress\n")
            service = PlanningService(root)
            preview = service.migrate_v3(dry_run=True)
            self.assertEqual(preview["mapped_status"], "completed")
            result = service.migrate_v3(session_id="new-session")
            task_meta, _ = read_document(service._task_plan_path(result["task_id"]))
            self.assertEqual(task_meta["status"], "completed")
            self.assertEqual(task_meta["created_at"], "2025-01-01T00:00:00Z")
            self.assertEqual(task_meta["legacy_session_id"], "old-session")
            self.assertNotIn(result["task_id"], service.load_index()["active_task_ids"])
            code, validation = service.validate(strict=True)
            self.assertEqual(code, 0, validation)

    def test_v3_inconsistent_complete_requires_review_instead_of_false_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            legacy = root / ".codex/planning/current"
            write(legacy / "state.json", json.dumps({"schema_version": 1, "title": "inconsistent", "status": "complete"}))
            write(legacy / "task_plan.md", "# incomplete\n\n## 验收条件\n\n- [ ] missing\n")
            write(legacy / "findings.md", "# findings\n")
            write(legacy / "progress.md", "# progress\n")
            service = PlanningService(root)
            result = service.migrate_v3()
            task_meta, _ = read_document(service._task_plan_path(result["task_id"]))
            self.assertEqual(task_meta["status"], "ready_for_review")
            self.assertTrue(result["warnings"])

    def test_v3_blocked_status_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            legacy = root / ".codex/planning/current"
            write(legacy / "state.json", json.dumps({"schema_version": 1, "title": "blocked", "status": "blocked"}))
            write(legacy / "task_plan.md", "# blocked\n\n## 验收条件\n\n- [ ] waiting\n")
            write(legacy / "findings.md", "# findings\n")
            write(legacy / "progress.md", "# progress\n")
            service = PlanningService(root)
            result = service.migrate_v3()
            task_meta, _ = read_document(service._task_plan_path(result["task_id"]))
            self.assertEqual(task_meta["status"], "blocked")
            self.assertIn(result["task_id"], service.load_index()["active_task_ids"])

    def test_corrupt_index_rebuilds_from_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            task = service.start_task("rebuild")
            write(service.paths.index, "{broken")
            rebuilt = service.load_index()
            self.assertIn(task["task_id"], rebuilt["active_task_ids"])

    def test_explicit_task_in_off_workspace_keeps_auto_start_off_but_hooks_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / ".codex/planning-config.json", json.dumps({"auto_start": False, "reason": "non-code"}))
            service = PlanningService(root)
            task = service.start_task("explicit", session_id="s1", force=True)
            self.assertEqual(probe(root)["mode"], "off")
            self.assertTrue(service._task_plan_path(task["task_id"]).exists())
            payload = json.dumps({"cwd": str(root), "session_id": "s1", "turn_id": "t"})
            old_stdin = sys.stdin
            try:
                sys.stdin = io.StringIO(payload)
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_hook("session_start")
            finally:
                sys.stdin = old_stdin
            self.assertEqual(code, 0)
            self.assertIn(task["task_id"], output.getvalue())

    def test_project_completion_requires_explicit_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            task = service.start_task("project completion", session_id="s1")
            service.init_project("Example", "confirmed vision")
            complete_acceptance(service, task["task_id"])
            code, result = service.complete_task(task["task_id"], "s1")
            self.assertEqual(code, 5)
            self.assertIn("reconciliation", result["reason"])
            code, result = service.complete_task(
                task["task_id"],
                "s1",
                project_impact="none",
                reconciliation_note="checked project files; no durable change",
                next_action="start the next accepted milestone",
            )
            self.assertEqual(code, 0, result)
            self.assertTrue(result["completed"])

    def test_repeated_stop_without_code_change_does_not_append_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            task = service.start_task("stop dedupe", session_id="s1")
            progress = service._task_progress_path(task["task_id"])
            before = progress.read_text(encoding="utf-8")
            old_stdin = sys.stdin
            try:
                for turn in ("t1", "t2"):
                    sys.stdin = io.StringIO(json.dumps({"cwd": str(root), "session_id": "s1", "turn_id": turn}))
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(run_hook("stop"), 0)
            finally:
                sys.stdin = old_stdin
            self.assertEqual(before, progress.read_text(encoding="utf-8"))

    def test_handoff_export_is_derived_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.start_task("handoff")
            result = service.export_handoff(Path("docs/HANDOFF.md"))
            exported = Path(result["path"])
            self.assertIn("derived export", exported.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                service.export_handoff(Path("docs/HANDOFF.md"))

    def test_snapshot_is_explicit_noncanonical_and_manifested(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.start_task("snapshot", session_id="s1")
            service.init_project("Example", "confirmed vision")
            before = service.load_index()
            result = service.snapshot("milestone", "phase one accepted")
            destination = Path(result["path"])
            manifest = json.loads((destination / "MANIFEST.json").read_text(encoding="utf-8"))
            self.assertFalse(result["canonical"])
            self.assertFalse(manifest["canonical"])
            self.assertIn("STATUS.md", manifest["files"])
            self.assertEqual(before["active_task_ids"], service.load_index()["active_task_ids"])

    def test_handoff_import_is_candidate_only_and_requires_user_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            source = root / "incoming-HANDOFF.md"
            write(source, "# Incoming\n\n## Current State\n\n- untrusted state\n\n## Next Actions\n\n- untrusted action\n")
            project_before = (root / "docs/project/PROJECT.md").read_bytes()
            preview = service.import_handoff(source)
            self.assertTrue(preview["dry_run"])
            self.assertFalse((root / ".codex/planning/imports").exists())
            registered = service.import_handoff(source, apply=True)
            self.assertTrue(registered["registered"])
            self.assertFalse(registered["canonical"])
            with self.assertRaisesRegex(ValueError, "user authority"):
                service.resolve_handoff_import(registered["import_id"], "accept-for-reconciliation", "agent", "agent guess")
            resolved = service.resolve_handoff_import(registered["import_id"], "accept-for-reconciliation", "user", "review this candidate")
            self.assertFalse(resolved["canonical_changed"])
            self.assertEqual(project_before, (root / "docs/project/PROJECT.md").read_bytes())

    def test_partial_semantic_transaction_requires_explicit_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            project = root / "docs/project/PROJECT.md"
            before = project.read_text(encoding="utf-8")
            relative = str(project.relative_to(root)).replace("\\", "/")
            state = service._document_states()["PROJECT.md"]
            transaction_id = "TX-semantic-partial"
            save_json(service.paths.transactions / f"{transaction_id}.json", {
                "transaction_id": transaction_id,
                "kind": "semantic-test",
                "status": "prepared",
                "before_contents": {relative: before},
                "before_states": {relative: state},
            })
            index = service.load_index()
            index["pending_transaction_id"] = transaction_id
            save_json(service.paths.index, index)
            write(project, before + "\nexternal partial write\n")
            unresolved = service.recover_transactions()
            self.assertFalse(unresolved["recovered"])
            self.assertTrue(unresolved["unresolved"])
            recovered = service.recover_transactions(rollback=True)
            self.assertTrue(recovered["recovered"])
            self.assertEqual(before, project.read_text(encoding="utf-8"))

    def test_external_document_edit_is_detected_and_acknowledged_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            project = root / "docs/project/PROJECT.md"
            project.write_text(project.read_text(encoding="utf-8") + "\nmanual note\n", encoding="utf-8")
            stale = service.check_staleness()
            self.assertEqual(stale["status"], "semantic-drift")
            preview = service.acknowledge_document_drift("reviewed manual note")
            self.assertTrue(preview["dry_run"])
            applied = service.acknowledge_document_drift("reviewed manual note", dry_run=False)
            self.assertTrue(applied["applied"])
            self.assertEqual(service.check_staleness()["status"], "fresh")

    def test_sync_does_not_acknowledge_manual_project_document_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            task = service.start_task("preserve drift", session_id="s1")
            index = service.load_index()
            recorded_project = dict(index["document_states"]["PROJECT.md"])
            index["document_acknowledgements"] = [{"note": "keep me"}]
            index["semantic_conflicts"] = ["existing-conflict"]
            index["future_extension"] = {"kept": True}
            save_json(service.paths.index, index)
            project = root / "docs/project/PROJECT.md"
            write(project, project.read_text(encoding="utf-8") + "\nmanual edit\n")
            write(root / "main.py", "print('changed')\n")
            result = service.sync_task(task_id=task["task_id"], session_id="s1", turn_id="drift")
            self.assertTrue(result["changed"])
            rebuilt = service.load_index()
            self.assertEqual(rebuilt["document_states"]["PROJECT.md"], recorded_project)
            self.assertEqual(rebuilt["document_acknowledgements"], [{"note": "keep me"}])
            self.assertEqual(rebuilt["future_extension"], {"kept": True})
            self.assertEqual(rebuilt["freshness"], "semantic-drift")
            self.assertIn("PROJECT.md", rebuilt["semantic_conflicts"])
            self.assertEqual(service.check_staleness(persist=False)["status"], "semantic-drift")

    def test_non_persistent_staleness_check_does_not_write_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            before = service.paths.index.read_bytes()
            result = service.check_staleness(persist=False)
            self.assertEqual(result["status"], "fresh")
            self.assertEqual(before, service.paths.index.read_bytes())

    def test_sync_progress_records_path_deltas_instead_of_full_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            write(root / "main.py", "print('one')\n")
            subprocess.run(["git", "-C", str(root), "add", "main.py"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "v0.1 initial"], check=True)
            service = PlanningService(root)
            task = service.start_task("compact sync", session_id="s1")
            write(root / "main.py", "print('two')\n")
            write(root / "new-a.py", "print('a')\n")
            first = service.sync_task(task_id=task["task_id"], session_id="s1", turn_id="one")
            self.assertIn("首次同步仅记录总数", first["summary"])
            self.assertNotIn("main.py", first["summary"])
            write(root / "main.py", "print('one')\n")
            (root / "new-a.py").unlink()
            write(root / "new-b.py", "print('b')\n")
            second = service.sync_task(task_id=task["task_id"], session_id="s1", turn_id="two")
            self.assertIn("`new-b.py`", second["summary"])
            self.assertIn("`main.py`", second["summary"])
            self.assertIn("`new-a.py`", second["summary"])
            progress = service._task_progress_path(task["task_id"]).read_text(encoding="utf-8")
            self.assertNotIn("## main", progress)
            self.assertNotIn("unstaged:", progress)

    def test_docs_root_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / ".codex/planning-config.json", json.dumps({"schema_version": 4, "mode": "task-only", "docs_root": "../outside"}))
            with self.assertRaises(ValueError):
                PlanningService(root)

    def test_task_lock_does_not_last_writer_win(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "task.lock"
            with file_lock(lock, "owner", timeout=0.1):
                with self.assertRaises(LockBusy):
                    with file_lock(lock, "other", timeout=0.1):
                        pass

    def test_pid_liveness_check_does_not_terminate_process(self) -> None:
        with patch("os.kill", side_effect=AssertionError("must not signal self")):
            self.assertTrue(_pid_alive(os.getpid()))
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(_pid_alive(process.pid))
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=10)
        self.assertFalse(_pid_alive(process.pid))

    def test_unfinished_completion_transaction_can_recover(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            task = service.start_task("recover", session_id="s1")
            index = service.load_index()
            index["pending_transaction_id"] = "TX-recover"
            save_json(service.paths.index, index)
            save_json(service.paths.transactions / "TX-recover.json", {"transaction_id": "TX-recover", "kind": "complete-task", "task_id": task["task_id"], "status": "prepared"})
            result = service.recover_transactions()
            self.assertTrue(result["recovered"])
            self.assertEqual(service.load_index()["pending_transaction_id"], None)
            self.assertFalse((service.paths.transactions / "TX-recover.json").exists())


class PlanningV4HookContractTests(unittest.TestCase):
    def _run(self, event: str, root: Path, turn: str = "t", **extra: object) -> str:
        value = {"cwd": str(root), "session_id": "s1", "turn_id": turn, "tool_name": "apply_patch"}
        value.update(extra)
        payload = json.dumps(value)
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(payload)
            output = io.StringIO()
            with redirect_stdout(output), patch("planning.hooks.__file__", str(root / "tools/codex/planning-with-files-zh/scripts/planning/hooks.py")):
                self.assertEqual(run_hook(event), 0)
            return output.getvalue()
        finally:
            sys.stdin = old_stdin

    def test_global_hook_yields_before_probe_and_service(self) -> None:
        for marker in ("tools/codex/project-policy.json", ".codex/hooks.json"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                value = {} if marker.endswith("project-policy.json") else {"hooks": {"PostToolUse": [{"hooks": [{"command": "python planning_state.py hook post_tool"}]}]}}
                write(root / marker, json.dumps(value))
                before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                with patch("planning.hooks.__file__", str(root / "global/hooks.py")), patch("planning.hooks.read_hook_payload", return_value={"cwd": str(root)}), patch("planning.hooks.probe", side_effect=AssertionError("must not probe")):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(run_hook("post_tool"), 0)
                    self.assertEqual(output.getvalue(), "")
                self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_unbound_hook_does_not_sync_only_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            task = service.start_task("only task", session_id="owner")
            progress = service._task_plan_path(task["task_id"]).with_name("PROGRESS.md")
            before = progress.read_bytes(), service.paths.index.read_bytes()
            for event in ("session_start", "pre_tool", "post_tool", "stop"):
                self.assertIn("bind-task", self._run(event, root, session_id="unbound"))
            self.assertEqual(before, (progress.read_bytes(), service.paths.index.read_bytes()))

    def test_executor_task_transition_leaves_status_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            task = service.start_task("executor", session_id="s1")
            write(root / "tools/codex/project-policy.json", '{"enforce_executor_role": true}')
            before = service.paths.status.read_bytes()
            service.set_task_status(task["task_id"], "ready_for_review", "tested")
            self.assertEqual(service.paths.status.read_bytes(), before)

    def test_hook_outputs_are_valid_json_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.start_task("hooks", session_id="s1")
            for event in ("session_start", "user_prompt_submit", "pre_tool", "post_tool", "stop"):
                output = self._run(event, root, turn=event)
                if output:
                    value = json.loads(output)
                    self.assertIn("systemMessage", value)
                    self.assertLessEqual(len(value["systemMessage"]), 4000)

    def test_new_session_recovers_goal_phase_abandoned_task_and_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "build the durable product")
            project_revision = read_document(root / "docs/project/PROJECT.md")[0]["revision"]
            service.add_goal("Ship reliable v1", "release checks pass", "accepted", "user", project_revision, dry_run=False)
            roadmap_revision = read_document(root / "docs/project/ROADMAP.md")[0]["revision"]
            old = service.add_milestone("Old direction", "old done", "accepted", "user", "none", roadmap_revision, dry_run=False)
            roadmap_revision = read_document(root / "docs/project/ROADMAP.md")[0]["revision"]
            service.update_roadmap_item(old["milestone_id"], "abandoned", "user", "user dropped it", "user instruction", None, roadmap_revision, dry_run=False)
            roadmap_revision = read_document(root / "docs/project/ROADMAP.md")[0]["revision"]
            current = service.add_milestone("Current phase", "phase verified", "accepted", "user", "none", roadmap_revision, dry_run=False)
            roadmap_revision = read_document(root / "docs/project/ROADMAP.md")[0]["revision"]
            service.update_roadmap_item(current["milestone_id"], "active", "agent", "work started", "task started", None, roadmap_revision, dry_run=False)
            task = service.start_task("Implement current phase", session_id="s1")
            context = service.resume_context(session_id="s1")
            self.assertEqual(context["goals"][0]["title"], "Ship reliable v1")
            self.assertEqual(context["active_milestones"][0]["title"], "Current phase")
            self.assertEqual(context["inactive_directions"][0]["title"], "Old direction")
            self.assertEqual(context["selected_task_id"], task["task_id"])
            self.assertNotIn(old["milestone_id"], context["next_action"])
            output = json.loads(self._run("session_start", root))["systemMessage"]
            self.assertIn("Ship reliable v1", output)
            self.assertIn("Current phase", output)
            self.assertIn("Old direction", output)
            self.assertIn(task["task_id"], output)

    def test_hooks_never_promote_proposed_project_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            revision = read_document(root / "docs/project/PROJECT.md")[0]["revision"]
            service.add_goal("Candidate goal", "candidate acceptance", "proposed", "agent", revision, dry_run=False)
            service.start_task("hook authority", session_id="s1")
            canonical = [root / "docs/project/PROJECT.md", root / "docs/project/ROADMAP.md", root / "docs/project/DECISIONS.md", root / "docs/project/FINDINGS.md"]
            before = {path.name: path.read_bytes() for path in canonical}
            for event in ("session_start", "user_prompt_submit", "pre_tool", "post_tool", "stop"):
                self._run(event, root, turn=event)
            after = {path.name: path.read_bytes() for path in canonical}
            self.assertEqual(before, after)
            self.assertIn("- Status: proposed", (root / "docs/project/PROJECT.md").read_text(encoding="utf-8"))

    def test_pre_tool_denies_apply_patch_for_planner_owned_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            write(root / "tools/codex/project-policy.json", json.dumps({
                "enforce_executor_role": True,
                "planner_owned_paths": ["docs/project/PROJECT.md", "docs/design/**"],
            }))
            service = PlanningService(root)
            service.init_project("Example", "confirmed vision")
            service.start_task("guard", session_id="s1")
            patch = f"*** Begin Patch\n*** Update File: {root / 'docs/project/PROJECT.md'}\n@@\n-old\n+new\n*** End Patch"
            output = json.loads(self._run("pre_tool", root, tool_input={"command": patch}))
            decision = output["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn("PROJECT.md", decision["permissionDecisionReason"])

    def test_stop_requires_commit_after_declared_experiment_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            write(root / "main.py", "print('code')\n")
            write(root / "tools/codex/project-policy.json", json.dumps({
                "experiment_completion_terms": ["实验完成"],
                "executor_owned_paths": ["scripts/**"],
            }))
            write(root / "scripts/run.py", "print('baseline')\n")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "v0.1 initial"], check=True)
            service = PlanningService(root)
            service.start_task("experiment", session_id="s1")
            write(root / "scripts/run.py", "print('result')\n")
            output = json.loads(self._run("stop", root, last_assistant_message="本次实验完成，结果已验证。"))
            self.assertEqual(output["decision"], "block")
            self.assertIn("scripts/run.py", output["reason"])
            self.assertIn("vX.Y.Z", output["reason"])

    def test_strict_stop_blocks_only_once_per_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "main.py", "print('code')\n")
            write(root / ".codex/planning-config.json", json.dumps({"schema_version": 4, "mode": "task-only", "stop_policy": "strict"}))
            service = PlanningService(root)
            service.start_task("strict", session_id="s1")
            index = service.load_index()
            index["pending_transaction_id"] = "TX-test"
            save_json(service.paths.index, index)
            save_json(service.paths.transactions / "TX-test.json", {"status": "prepared"})
            first = json.loads(self._run("stop", root, turn="same"))
            second = json.loads(self._run("stop", root, turn="same"))
            self.assertFalse(first["continue"])
            self.assertNotIn("continue", second)

    def test_cli_handles_chinese_path_without_utf8_environment_variables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "中文项目"
            root.mkdir()
            write(root / "main.py", "print('code')\n")
            env = dict(os.environ)
            env.pop("PYTHONUTF8", None)
            env.pop("PYTHONIOENCODING", None)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "planning_state.py"), "probe", "--root", str(root)],
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            payload = json.loads(result.stdout.decode("utf-8"))
            self.assertIn("中文项目", payload["root"])

    def test_cli_error_path_returns_json_instead_of_secondary_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / ".codex/planning-config.json", json.dumps({"schema_version": 4, "mode": "task-only", "docs_root": "../escape"}))
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "planning_state.py"), "status", "--root", str(root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stderr + "\nstdout=" + result.stdout)
            self.assertIn("error", json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main(verbosity=2)
