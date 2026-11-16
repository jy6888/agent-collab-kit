from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_planning_v4 import SCRIPTS, complete_acceptance, write
from planning.cli import build_parser
from planning.endpoints import bridge_endpoints
from planning.hooks import run_hook
from planning.model import read_document, save_json, write_document
from planning.records import lint_records
from planning.service import PlanningService


class WorkflowV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write(self.root / "main.py", "print('初始')\n")
        self.service = PlanningService(self.root)

    def start(self, session: str = "s1", title: str = "测试任务") -> str:
        return self.service.start_task(title, session_id=session)["task_id"]

    def add_block(self) -> None:
        path = self.service.paths.status
        meta, body = read_document(path)
        body += "\n## 最近验证\n\n<!-- planning:recent-verifications:start -->\n- 尚未验证。\n<!-- planning:recent-verifications:end -->\n"
        write_document(path, meta, body)
        self.service.acknowledge_document_drift("test fixture", dry_run=False)

    def finding(self, title: str = "离线验证通过") -> str:
        rev = read_document(self.service.paths.docs_root / "FINDINGS.md")[0]["revision"]
        return self.service.record_finding(title, "evidence", "tooling", "verified",
                                           expected_revision=rev, dry_run=False)["finding_id"]

    def finish(self, task_id: str, finding_id: str | None = None) -> tuple[int, dict]:
        complete_acceptance(self.service, task_id)
        return self.service.complete_task(task_id, None, finding_id=finding_id,
                                          project_impact="none", reconciliation_note="fixture verified",
                                          next_action="review next task")

    def cli(self, *args: str) -> dict:
        result = subprocess.run([sys.executable, "-s", str(SCRIPTS / "planning_state.py"),
                                 *args, "--root", str(self.root)], capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_events_replace_all_mechanical_progress_appends(self) -> None:
        task = self.start()
        progress = self.service._task_progress_path(task)
        before = progress.read_bytes()
        _, body = read_document(progress)
        self.assertEqual([line for line in body.splitlines() if line.startswith("## ")],
                         ["## 当前状态", "## 未决问题", "## 最近验证", "## 自修记录"])
        self.assertLessEqual(len(progress.read_text(encoding="utf-8").splitlines()), 40)
        self.service.bind_task(task, "s2", takeover=True)
        write(self.root / "main.py", "print('变更')\n")
        self.service.sync_task(task_id=task, session_id="s2", turn_id="turn")
        self.service.set_task_status(task, "ready_for_review", "tested")
        code, result = self.finish(task)
        self.assertEqual(code, 0, result)
        self.assertEqual(before, progress.read_bytes())
        events = [json.loads(line) for line in progress.with_name("events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({row["event_type"] for row in events},
                         {"takeover", "code-sync", "status-change", "reconciliation"})
        for row in events:
            self.assertEqual(set(row), {"time", "event_type", "summary", "fingerprint"})
            self.assertEqual(len(row["fingerprint"]), 12)

    def test_sync_deduplicates_events_and_keeps_legacy_progress_readable(self) -> None:
        task = self.start()
        progress = self.service._task_progress_path(task)
        meta, _ = read_document(progress)
        write_document(progress, meta, "# Legacy\n\n## 会话日志\n- 旧记录保留。\n")
        before = progress.read_bytes()
        self.service.sync_task(task_id=task, note="one sync")
        event_path = progress.with_name("events.jsonl")
        events = event_path.read_bytes()
        self.assertFalse(self.service.sync_task(task_id=task)["changed"])
        self.assertEqual(events, event_path.read_bytes())
        code, validation = self.service.validate(strict=True)
        self.assertEqual(code, 0, validation)
        self.assertTrue(validation["strict_ok"])
        self.assertTrue(any(isinstance(w, dict) and w["rule"] == "L1" for w in validation["warnings"]))
        self.assertEqual(before, progress.read_bytes())

    def test_complete_requires_existing_finding_before_mutation(self) -> None:
        self.service.init_project("测试", "confirmed fixture")
        task = self.start()
        self.add_block()
        complete_acceptance(self.service, task)
        before = self.service._task_plan_path(task).read_bytes(), self.service.paths.status.read_bytes()
        for finding_id in (None, "F-999", "D-001"):
            code, result = self.service.complete_task(task, None, finding_id=finding_id,
                                                      project_impact="none", reconciliation_note="fixture",
                                                      next_action="review")
            self.assertNotEqual(code, 0, result)
            self.assertFalse(result["completed"])
            self.assertIsNone(self.service.load_index().get("pending_transaction_id"))
            self.assertEqual(before, (self.service._task_plan_path(task).read_bytes(), self.service.paths.status.read_bytes()))

    def test_status_placeholder_missing_block_and_dry_run_are_preserved(self) -> None:
        self.service.init_project("测试", "confirmed fixture")
        self.assertFalse(self.service.render_status(apply=True)["applied"])
        self.add_block()
        before = self.service.paths.status.read_bytes()
        self.assertFalse(self.service.render_status(apply=True)["applied"])
        self.assertEqual(before, self.service.paths.status.read_bytes())
        task = self.start()
        code, result = self.finish(task, self.finding())
        self.assertEqual(code, 0, result)
        path = self.service.paths.status
        meta, body = read_document(path)
        write_document(path, meta, body.replace("离线验证通过", "stale display"))
        self.service.acknowledge_document_drift("fixture", dry_run=False)
        before = path.read_bytes()
        preview = self.cli("render-status")
        self.assertTrue(preview["changed"])
        self.assertEqual(before, path.read_bytes())
        self.assertTrue(self.cli("render-status", "--apply")["applied"])
        self.assertFalse(self.cli("render-status", "--apply")["applied"])
        self.assertEqual(self.service.validate(strict=True)[0], 0)

    def test_recent_status_contains_last_three_completed_tasks_only(self) -> None:
        self.service.init_project("测试", "confirmed fixture")
        self.add_block()
        tasks = []
        for index in range(4):
            task = self.start(f"s{index}", f"任务 {index}")
            finding = self.finding(f"结论 {index}")
            code, result = self.finish(task, finding)
            self.assertEqual(code, 0, result)
            tasks.append((task, finding))
        active = self.start("open-task")
        lines = self.service.render_status()["lines"]
        self.assertEqual(len(lines), 3)
        for line, (task, finding) in zip(lines, reversed(tasks[1:])):
            self.assertIn(task, line)
            self.assertIn(finding, line)
        self.assertNotIn(tasks[0][0], "\n".join(lines))
        self.assertNotIn(active, "\n".join(lines))

    def test_lint_all_rules_and_boundary_counterexamples(self) -> None:
        docs = self.root / "docs/project"
        cases = [
            ("L1", docs / "tasks/T-001/PROGRESS.md", "line\n" * 41, "line\n" * 40),
            ("L1", docs / "tasks/T-001/PROGRESS.md", "## 会话日志\n", "## 当前状态\n"),
            ("L2", docs / "DECISIONS.md", "## D-001 — " + "长" * 61 + "\n", "## D-001 — " + "短" * 60 + "\n"),
            ("L3", docs / "FINDINGS.md", "## F-001 — 发现\n" + "长" * 201, "## F-001 — 发现\n" + "短" * 200),
            ("L3", docs / "tasks/T-001/FINDINGS.md", "## 局部事实\n- " + "长" * 201, "## 局部事实\n- " + "短" * 200),
            ("L4", self.root / "PROJECT_EVOLUTION.md", "## E001 — 演进\n" + "line\n" * 25,
             "## E001 — 演进\n" + "line\n" * 24),
            ("L6", docs / "STATUS.md", "## 最近验证\n" + "- 手写\n" * 4,
             "## 最近验证\n" + "- 手写\n" * 3 + "<!-- planning:recent-verifications:start -->\n" + "- 生成\n" * 5 + "<!-- planning:recent-verifications:end -->\n"),
        ]
        for rule, path, bad, good in cases:
            with self.subTest(rule=rule, bad=True):
                write(path, bad)
                before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
                warnings = lint_records(self.root, docs)["warnings"]
                self.assertTrue(any(w["rule"] == rule for w in warnings), warnings)
                self.assertTrue(all(w["line"] > 0 and w["file"] and w["message"] for w in warnings))
                self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
            with self.subTest(rule=rule, bad=False):
                write(path, good)
                self.assertFalse(any(w["rule"] == rule for w in lint_records(self.root, docs)["warnings"]))

    def test_lint_numeric_normalization_ids_and_one_file_duplicates(self) -> None:
        docs = self.root / "docs/project"
        for value1, value2 in (("12,345", "12345"), ("98.50%", "98.5％")):
            with self.subTest(value=value1):
                write(docs / "STATUS.md", f"共{value1}条。\n" if not value1.endswith("%") else f"结果 {value1}\n")
                write(docs / "FINDINGS.md", f"共{value2}条。\n" if not value2.endswith("％") else f"结果 {value2}\n")
                warnings = lint_records(self.root, docs)["warnings"]
                self.assertEqual(len([w for w in warnings if w["rule"] == "L5"]), 2, warnings)
        shared = "T-20261113-001 v12345.6 2026-11-13 [ref](tasks/12345/PLAN.md)\n"
        write(docs / "STATUS.md", shared + "12345 12345\n")
        write(docs / "FINDINGS.md", shared + "1234\n")
        self.assertFalse(lint_records(self.root, docs)["warnings"])

    def test_lint_skips_records_written_before_workflow_v2(self) -> None:
        docs = self.root / "docs/project"
        old_plan = '---\ncreated_at: "2026-11-12T23:59:00+11:00"\n---\n'
        new_plan = '---\ncreated_at: "2026-11-13T00:00:00+11:00"\n---\n'
        for task, plan in (("T-20261112-001", old_plan), ("T-20261113-001", new_plan)):
            write(docs / f"tasks/{task}/PLAN.md", plan)
            write(docs / f"tasks/{task}/PROGRESS.md", "## 会话日志\n")
            write(docs / f"tasks/{task}/FINDINGS.md", "## 局部事实\n- " + "长" * 201)
        write(docs / "DECISIONS.md", "## D-20261112-001 — " + "长" * 61 + "\n\n## D-20261113-001 — " + "长" * 61 + "\n")
        write(docs / "FINDINGS.md", "## F-20261112-001 — 旧\n" + "长" * 201 + "\n\n## F-20261113-001 — 新\n" + "长" * 201 + "\n")
        self.assertIn("D-20261112-001", json.dumps(lint_records(self.root, docs)["warnings"], ensure_ascii=False))
        warnings = lint_records(self.root, docs, {"lint_since": "2026-11-13"})["warnings"]
        joined = json.dumps(warnings, ensure_ascii=False)
        self.assertNotIn("T-20261112-001", joined)
        self.assertNotIn("D-20261112-001", joined)
        self.assertNotIn("F-20261112-001", joined)
        self.assertIn("tasks/T-20261113-001/PROGRESS.md", joined)
        self.assertIn("tasks/T-20261113-001/FINDINGS.md", joined)
        self.assertIn("D-20261113-001", joined)
        self.assertIn("F-20261113-001", joined)

    def test_l7_flags_decisions_of_closed_tasks_until_archived_or_kept(self) -> None:
        docs = self.root / "docs/project"
        for task, status in (("T-20261113-001", "completed"), ("T-20261113-002", "active"),
                             ("T-20261113-003", "cancelled")):
            write(docs / f"tasks/{task}/PLAN.md", f'---\nstatus: "{status}"\ncreated_at: "2026-11-13T00:00:00+11:00"\n---\n')
        write(docs / "DECISIONS.md", "\n\n".join([
            "## D-20261113-001 — 已结项任务的立项\n\n- Reason: T-20261113-001 立项，T-20261113-003 取消",
            "## D-20261113-002 — 任务还在做\n\n- Reason: T-20261113-001 与 T-20261113-002",
            "## D-20261113-003 — 仍约束后续\n\n- Keep: 后续任务沿用\n- Reason: T-20261113-001",
            "## D-20261113-004 — 不提任务\n\n- Reason: 协作规则",
            "## D-20261113-005 — 当天最新一条留着\n\n- Reason: T-20261113-001",
        ]) + "\n")
        hits = [w for w in lint_records(self.root, docs)["warnings"] if w["rule"] == "L7"]
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("D-20261113-001", hits[0]["message"])
        self.assertEqual(hits[0]["line"], 1)
        # A PLAN that cites the decision links it too; an open citing task holds it back.
        plan = docs / "tasks/T-20261113-003/PLAN.md"
        write(plan, plan.read_text(encoding="utf-8") + "关联决定：D-20261113-004\n")
        hits = [w for w in lint_records(self.root, docs)["warnings"] if w["rule"] == "L7"]
        self.assertEqual(len(hits), 2, hits)
        self.assertIn("D-20261113-004 关联任务 T-20261113-003", hits[1]["message"])
        plan = docs / "tasks/T-20261113-002/PLAN.md"
        write(plan, plan.read_text(encoding="utf-8") + "关联决定：D-20261113-004\n")
        self.assertEqual(len([w for w in lint_records(self.root, docs)["warnings"] if w["rule"] == "L7"]), 1)

    def test_l8_warns_when_canonical_entries_pile_up(self) -> None:
        docs = self.root / "docs/project"
        for name, prefix, limit in (("DECISIONS.md", "D", 70), ("FINDINGS.md", "F", 60)):
            for count, expected in ((limit, 0), (limit + 1, 1)):
                write(docs / name, "".join(f"## {prefix}-20261101-{n:03d} — 条目\n\n" for n in range(1, count + 1)))
                hits = [w for w in lint_records(self.root, docs)["warnings"]
                        if w["rule"] == "L8" and w["file"].endswith(name)]
                self.assertEqual(len(hits), expected, hits)

    def test_complete_reports_archive_reminders_for_the_closed_task(self) -> None:
        self.service.init_project("测试", "confirmed fixture")
        task = self.start()
        path = self.service.paths.docs_root / "DECISIONS.md"
        meta, body = read_document(path)
        body += f"\n## D-20261113-001 — 立项\n\n- Reason: {task} 立项\n\n## D-20261113-002 — 最新\n\n- Reason: 无\n"
        write_document(path, meta, body)
        self.service.acknowledge_document_drift("test fixture", dry_run=False)
        code, result = self.finish(task)
        self.assertEqual(code, 0, result)
        self.assertEqual([w["rule"] for w in result["archive_reminders"]], ["L7"])
        self.assertIn("D-20261113-001", result["archive_reminders"][0]["message"])

    def test_lint_reads_design_codex_readme_for_duplicates(self) -> None:
        docs = self.root / "docs/project"
        write(docs / "STATUS.md", "共 54321 条。\n")
        write(self.root / "docs/design/codex/README.md", "共 54321 条。\n")
        warnings = [w for w in lint_records(self.root, docs)["warnings"] if w["rule"] == "L5"]
        self.assertEqual({w["file"] for w in warnings}, {"docs/project/STATUS.md", "docs/design/codex/README.md"})

    def test_l3_generated_metadata_does_not_consume_free_text_budget(self) -> None:
        self.service.init_project("测试", "confirmed fixture")
        findings = self.service.paths.docs_root / "FINDINGS.md"
        ids = []
        for evidence_length in (160, 161):
            revision = read_document(findings)[0]["revision"]
            result = self.service.record_finding(
                "自由文本边界", "证" * evidence_length, "影" * 40, "verified",
                task_id="T-20261113-001", expected_revision=revision, dry_run=False)
            ids.append(result["finding_id"])
        warnings = [w for w in self.service.lint_records()["warnings"] if w["rule"] == "L3"]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(ids[1], warnings[0]["message"])
        self.assertNotIn(ids[0], warnings[0]["message"])

    def test_endpoint_cli_opt_in_preserves_other_addresses_and_cleans_terminal_tasks(self) -> None:
        task = self.start()
        registry = self.root / ".runtime/bridge-endpoints.json"
        save_json(registry, {"schema": 1, "planner": {"session_id": "planner"}, "executors": {"other": "peer"}, "note": "保留"})
        self.cli("bind-task", "--task-id", task, "--session-id", "s1", "--bridge-thread-id", "thread")
        before = registry.read_bytes()
        self.cli("bind-task", "--task-id", task, "--session-id", "s1")
        context = self.cli("resume-context", "--session-id", "s1")
        self.assertEqual(context["bridge_endpoints"]["executors"][task], "thread")
        self.assertEqual(before, registry.read_bytes())
        self.cli("takeover-task", "--task-id", task, "--session-id", "s2", "--bridge-thread-id", "new-thread")
        self.cli("cancel-task", "--task-id", task, "--reason", "fixture")
        data = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(data["executors"], {"other": "peer"})
        self.assertEqual(data["planner"], {"session_id": "planner"})
        self.assertEqual(data["note"], "保留")

    def test_resume_creates_missing_registry_but_does_not_replace_corrupt_registry(self) -> None:
        self.start()
        self.assertEqual(self.service.resume_context("s1")["bridge_endpoints"],
                         {"schema": 1, "planner": None, "executors": {}})
        registry = self.root / ".runtime/bridge-endpoints.json"
        write(registry, "invalid-json")
        before = registry.read_bytes()
        with self.assertRaises(ValueError):
            bridge_endpoints(self.root, "task", "thread")
        self.assertEqual(before, registry.read_bytes())

    def test_cli_full_lifecycle_produces_events_and_generated_status(self) -> None:
        self.service.init_project("测试", "confirmed fixture")
        self.add_block()
        task = self.cli("start-task", "--title", "端到端", "--session-id", "cli-session")["task_id"]
        self.cli("bind-task", "--task-id", task, "--session-id", "cli-session", "--bridge-thread-id", "cli-thread")
        progress = self.service._task_progress_path(task)
        before = progress.read_bytes()
        write(self.root / "main.py", "print('changed')\n")
        self.assertTrue(self.cli("sync", "--task-id", task, "--note", "smoke")["changed"])
        complete_acceptance(self.service, task)
        self.cli("ready-for-review", "--task-id", task, "--reason", "tested")
        finding_id = self.finding("模拟流程通过")
        done = self.cli("complete", "--task-id", task, "--finding-id", finding_id,
                        "--project-impact", "none", "--reconciliation-note", "test evidence",
                        "--next-action", "review")
        self.assertTrue(done["completed"])
        self.assertEqual(before, progress.read_bytes())
        self.assertTrue(progress.with_name("events.jsonl").is_file())
        self.assertIn("模拟流程通过", self.service.paths.status.read_text(encoding="utf-8"))
        self.assertNotIn(task, bridge_endpoints(self.root)["executors"])
        self.assertTrue(self.cli("validate", "--strict")["strict_ok"])
        self.assertEqual(self.cli("lint-records"), self.service.lint_records())

    def test_stop_warns_only_for_new_long_subjects_and_never_blocks(self) -> None:
        def git(*args: str) -> None:
            result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True,
                                    text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Fixture")
        git("add", "main.py")
        git("commit", "-qm", "old " + "长" * 80)
        self.start()

        def stop() -> dict:
            with patch.object(sys, "stdin", io.StringIO(json.dumps({"cwd": str(self.root), "session_id": "s1"}))), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(run_hook("stop"), 0)
            return json.loads(out.getvalue())
        self.assertNotIn("72 字", stop()["systemMessage"])
        git("commit", "--allow-empty", "-qm", "界" * 72)
        self.assertNotIn("72 字", stop()["systemMessage"])
        git("commit", "--allow-empty", "-qm", "新" * 73)
        output = stop()
        self.assertIn("73 字", output["systemMessage"])
        self.assertNotIn("continue", output)
        self.assertNotIn("decision", output)
        task = self.service.resolve_task(session_id="s1")[0]
        self.service.set_task_status(task, "cancelled", "test terminal cleanup")
        self.assertIn("73 字", stop()["systemMessage"])

    def test_cli_exposes_optional_flags(self) -> None:
        parser = build_parser()
        self.assertIsNone(parser.parse_args(["bind-task", "--task-id", "T-001", "--session-id", "s"]).bridge_thread_id)
        self.assertIsNone(parser.parse_args(["complete"]).finding_id)


if __name__ == "__main__":
    unittest.main()
