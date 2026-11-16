"""End-to-end checks for the stdio planning MCP server and the kit's own additions."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parent.parent
SERVER = KIT / "mcp" / "planning_mcp.py"
sys.path.insert(0, str(KIT / "skills" / "planning-with-files-zh" / "scripts"))

from planning import hooks  # noqa: E402


class Session:
    def __init__(self) -> None:
        self.process = subprocess.Popen([sys.executable, "-s", str(SERVER)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, encoding="utf-8")
        self.next_id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        if notify:
            return None
        response = json.loads(self.process.stdout.readline())
        assert response["id"] == self.next_id, response
        return response

    def call(self, name: str, arguments: dict) -> tuple[bool, str]:
        result = self.send("tools/call", {"name": name, "arguments": arguments})["result"]
        return not result["isError"], result["content"][0]["text"]

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=10)
        self.process.stdout.close()


class PlanningMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "中文 项目"
        self.root.mkdir()
        (self.root / "main.py").write_text("print('x')\n", encoding="utf-8")
        self.session = Session()
        self.addCleanup(self.session.close)

    def test_handshake_and_tool_list(self) -> None:
        init = self.session.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "test", "version": "0"}})["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        self.assertIsNone(self.session.send("notifications/initialized", notify=True))
        self.assertEqual(self.session.send("ping")["result"], {})
        tools = self.session.send("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools],
                         ["planning_resume_context", "planning_validate", "planning_lint_records", "planning_run"])
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIn("root", tool["inputSchema"]["required"])
        self.assertEqual(self.session.send("no/such")["error"]["code"], -32601)

    def test_task_lifecycle_through_tools(self) -> None:
        root = str(self.root)
        ok, text = self.session.call("planning_run", {"root": root, "command": "start-task",
                                                      "args": ["--title", "测试任务", "--session-id", "s1"]})
        self.assertTrue(ok, text)
        task_id = json.loads(text)["task_id"]
        self.assertTrue((self.root / "docs/project/tasks" / task_id / "PLAN.md").is_file())
        ok, text = self.session.call("planning_resume_context", {"root": root, "session_id": "s1"})
        self.assertTrue(ok, text)
        self.assertIn(task_id, text)
        self.assertIn("测试任务", text)
        ok, text = self.session.call("planning_lint_records", {"root": root})
        self.assertTrue(ok, text)
        self.assertEqual(json.loads(text), {"warnings": []})
        ok, text = self.session.call("planning_validate", {"root": root})
        self.assertIn("resume_usable", text)
        # A refusal from the CLI comes back as an error result, not as a protocol failure.
        ok, text = self.session.call("planning_run", {"root": root, "command": "complete",
                                                      "args": ["--task-id", task_id]})
        self.assertFalse(ok)
        self.assertIn("[exit", text)

    def test_guards(self) -> None:
        root = str(self.root)
        for arguments, expected in (
            ({"root": root, "command": "hook", "args": ["stop"]}, "hook runner"),
            ({"root": root, "command": "status", "args": ["--root", "C:/"]}, "not as --root"),
            ({"root": "relative/path", "command": "status"}, "absolute"),
            ({"root": root, "command": "status", "args": "oops"}, "list of strings"),
            ({"root": root}, "'command' is required"),
        ):
            ok, text = self.session.call("planning_run", arguments)
            self.assertFalse(ok, text)
            self.assertIn(expected, text)
        ok, text = self.session.call("planning_run", {"root": root, "command": "help"})
        self.assertTrue(ok, text)
        self.assertIn("resume-context", text)


class SharedInstallHookTests(unittest.TestCase):
    def test_project_scope_makes_the_shared_copy_do_the_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            policy = root / "tools/codex/project-policy.json"
            policy.parent.mkdir(parents=True)
            policy.write_text('{"enforce_executor_role": true}', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PLANNING_HOOK_SCOPE", None)
                self.assertTrue(hooks._delegates_to_project(root))
                os.environ["PLANNING_HOOK_SCOPE"] = "project"
                self.assertFalse(hooks._delegates_to_project(root))


class PackageHygieneTests(unittest.TestCase):
    def test_files_carry_no_machine_or_personal_specifics(self) -> None:
        # Home-directory paths, drive-letter project paths, mail addresses and live session ids
        # belong to one machine or one person; a reusable package must not ship any of them.
        patterns = {
            "home path": re.compile(r"[\\/]Users[\\/]+(?!<)[\w.-]+", re.IGNORECASE),
            "drive path": re.compile(r"\b[D-Z]:[\\/]+\w"),
            "mail address": re.compile(r"[\w.+-]+@[\w-]+\.(?:com|cn|net|org)\b(?<!noreply\.github\.com)"),
            "session id": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
        }
        offenders = []
        for path in KIT.rglob("*"):
            relative = path.relative_to(KIT).as_posix()
            if (not path.is_file() or relative.startswith(".git/") or "__pycache__" in relative
                    or path.suffix in {".png", ".svg", ".ico"}):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            offenders += [f"{relative}: {label}: {match.group(0)}" for label, pattern in patterns.items()
                          for match in pattern.finditer(text)]
        self.assertEqual(offenders, [])

    def test_manifests_are_valid_and_point_at_existing_files(self) -> None:
        for name in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", ".codex-plugin/plugin.json",
                     ".mcp.json", "templates/project-policy.json", "templates/codex-hooks.json",
                     "templates/bridge-endpoints.json", "templates/planning-config.json"):
            json.loads((KIT / name).read_text(encoding="utf-8"))
        servers = json.loads((KIT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        for server in servers.values():
            target = server["args"][0].replace("${CLAUDE_PLUGIN_ROOT}/", "")
            self.assertTrue((KIT / target).is_file(), target)
        for skill in ("planning-with-files-zh", "agent-collab", "claude-bridge"):
            head = (KIT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
            self.assertIn(f"name: {skill}", head)
            self.assertIn("description:", head)
        self.assertTrue((KIT / "vendor/codex-claude-desktop-bridge/LICENSE").is_file())


if __name__ == "__main__":
    unittest.main()
