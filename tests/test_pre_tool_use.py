import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vibevideo_mcp.cli import install
from vibevideo_mcp.PreToolUse import hook
from vibevideo_mcp.server import Project
from test_release import PLAN


class PreToolUseTests(unittest.TestCase):
    def test_clients_install_run_and_remove_only_their_hook(self):
        other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}
        for client, rel in (("claude", ".claude/settings.json"), ("codex", ".codex/hooks.json")):
            with self.subTest(client=client), tempfile.TemporaryDirectory(prefix="video project ") as tmp:
                project = Path(tmp)
                config = project / rel
                config.parent.mkdir()
                config.write_text(json.dumps({"hooks": {"PreToolUse": [other]}}))
                subprocess.run(
                    [sys.executable, "-I", "-m", "vibevideo_mcp", "install", client,
                     "--project", str(project), "--profile", "PreToolUse"], check=True, capture_output=True,
                )
                install(client, project, "PreToolUse")
                rules = json.loads(config.read_text())["hooks"]["PreToolUse"]
                self.assertEqual(rules[:-1], [other])
                command = shlex.split(rules[-1]["hooks"][0]["command"])

                def run(name, args):
                    event = {"session_id": "thread-1", "tool_name": name, "tool_input": args, "cwd": tmp}
                    return subprocess.run(
                        command, input=json.dumps(event), text=True, capture_output=True, check=True
                    ).stdout

                self.assertEqual(
                    json.loads(run("Bash", {"command": "python render.py"}))["hookSpecificOutput"][
                        "permissionDecision"
                    ],
                    "deny",
                )
                self.assertEqual(run("mcp__vibevideo__plan_production", PLAN), "")
                task = Project(project).call("plan_production", PLAN)["task_id"]
                self.assertEqual(run("mcp__vibevideo__status", {"task_id": task}), "")
                self.assertEqual(run("Bash", {"command": "python render.py"}), "")
                self.assertEqual(run("mcp__vibevideo__inspect_segment", {"task_id": task}), "")
                install(client, project, "standard")
                self.assertEqual(json.loads(config.read_text())["hooks"]["PreToolUse"], [other])

    def test_session_and_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "prompt.md").write_text("Brief")
            (p / "media.dat").write_bytes(b"\x89PNG")
            (p / "inputs").mkdir()

            def check(tool, args, session="one"):
                return hook(p, {"session_id": session, "tool_name": tool, "tool_input": args, "cwd": tmp})

            self.assertIsNone(check("Bash", {"command": "sed -n '1,40p' prompt.md"}))
            self.assertIsNone(check("Skill", {"skill": "vibevideo"}))
            self.assertTrue(check("Read", {"file_path": "media.dat"}))
            self.assertTrue(check("Bash", {"command": "python render.py"}))
            task = Project(p).call("plan_production", PLAN)["task_id"]
            self.assertTrue(check("mcp__vibevideo__status", {"task_id": "a" * 32}))
            self.assertIsNone(check("mcp__vibevideo__status", {"task_id": task}))
            self.assertTrue(check("Bash", {"command": "python render.py"}, "two"))
            self.assertTrue(check("mcp__vibevideo__probe_media", {"task_id": "a" * 32}))
            for name, args in [
                ("Bash", {"command": "cat media.dat"}),
                ("Write", {"file_path": "inputs/clip.mp4"}),
                ("Bash", {"command": "echo GEMINI_API_KEY"}),
                ("view_image", {}),
                ("mcp__another__inspect", {}),
                ("Agent", {}),
                ("spawn_agent", {}),
                (
                    "apply_patch",
                    {"command": "*** Begin Patch\n*** Update File: .codex/hooks.json\n*** End Patch"},
                ),
            ]:
                self.assertTrue(check(name, args), name)
            self.assertIsNone(
                check(
                    "apply_patch", {"command": "*** Begin Patch\n*** Update File: render.py\n*** End Patch"}
                )
            )
            self.assertIsNone(check("mcp__vibevideo__plan_production", {}))
            self.assertTrue(check("exec_command", {"cmd": "python render.py"}))
            log = (p / ".vibevideo/hooks/audit.jsonl").read_text()
            self.assertNotIn("echo GEMINI_API_KEY", log)
            self.assertNotIn("media.dat", log)
