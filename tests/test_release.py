"""Exercise the installed wheel, real MCP transport, FFmpeg and a local provider fixture."""

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from vibevideo_mcp.cli import install
from vibevideo_mcp.server import Project
from unittest.mock import patch

PLAN = {
    "prompt": "A blue circle pauses.",
    "goal": "Animate the circle",
    "deliverables": ["video"],
    "requirements": [
        {
            "id": "r1",
            "prompt_quote": "blue circle",
            "observable": "The circle is blue",
            "evidence": ["visual"],
        }
    ],
    "beats": [{"action": "Draw circle", "requirements": ["r1"]}],
    "first_requirement": "r1",
    "preview": "A short color test",
    "risk": "Color after encoding",
    "delivery": {"width": 64, "audio_required": True},
}


class Fixture(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        parts = body["contents"][0]["parts"]
        media = [p["inlineData"] for p in parts if "inlineData" in p]
        self.server.calls.append((self.path, self.headers.get("x-goog-api-key"), media))
        verdicts = body["generationConfig"]["responseSchema"]["properties"]["verdict"]["enum"]
        verdict = verdicts[0]
        if self.server.fail:
            self.send_error(503)
            return
        value = {
            "summary": "Fixture observation",
            "verdict": verdict,
            "evidence": [
                {
                    "media_index": i,
                    "modality": "audio" if m["mimeType"] == "audio/ogg" else "visual",
                    "start": 0,
                    "end": 0,
                    "box": [],
                    "text": "Sampled observation",
                }
                for i, m in enumerate(media)
            ],
        }
        data = json.dumps(
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(value)}]}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def clip(workspace, color):
    command = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color={color}:s=64x64:r=8:d=1",
        "-f",
        "lavfi",
        "-i",
        "sine=duration=1",
        "-threads",
        "1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "draft.mp4",
    ]
    subprocess.run(command, cwd=workspace, check=True)
    (workspace / "color.txt").write_text(color)
    return command


class ReleaseTests(unittest.TestCase):
    def test_project_installs(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".codex").mkdir()
            (project / ".codex/config.toml").write_text(
                '# retain this comment\n[mcp_servers.other]\ncommand = "other"\n'
            )
            (project / ".mcp.json").write_text('{"mcpServers": {"other": {"command":"other"}}}')
            for client in ("claude", "codex"):
                install(client, project)
            config = project / ".codex/config.toml"
            original = config.read_bytes()
            install("codex", project)
            self.assertEqual(config.read_bytes(), original)
            self.assertIn(b"# retain this comment", original)
            self.assertEqual(tomllib.loads(config.read_text())["mcp_servers"]["other"]["command"], "other")
            servers = json.loads((project / ".mcp.json").read_text())["mcpServers"]
            self.assertEqual(servers["other"], {"command": "other"})
            self.assertEqual(servers["vibevideo"]["env"]["GEMINI_API_KEY"], "${GEMINI_API_KEY:-}")
            self.assertEqual(
                (project / ".claude/skills/vibevideo/SKILL.md").read_bytes(),
                (project / ".agents/skills/vibevideo/SKILL.md").read_bytes(),
            )
            (project / ".mcp.json").write_text('{"mcpServers":{"vibevideo":{"command":"custom"}}}')
            with self.assertRaises(ValueError):
                install("claude", project)
            self.assertIn("custom", (project / ".mcp.json").read_text())

    def test_offline_delivery(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            workspace = Path(folder)
            project = Project(workspace)
            replay = clip(workspace, "blue")
            task = project.call("plan_production", PLAN)["task_id"]
            with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
                project.call("describe_media", {"task_id": task, "media": {"path": "draft.mp4"}})
            reg = {
                "task_id": task,
                "video": "draft.mp4",
                "source_files": ["color.txt"],
                "render_command": replay,
            }
            project.call("register_artifact", reg)
            self.assertFalse(project.call("submit_delivery", {"task_id": task})["accepted"])
            result = project.call(
                "submit_delivery", {"task_id": task, "notes": "No semantic provider configured"}
            )
            self.assertTrue(result["accepted"])
            self.assertEqual(result["workflow"]["final_evidence"], {"r1": {}})
            self.assertEqual(project.call("status", {"task_id": task})["budget"]["calls"], 0)
            wrong = project.call("plan_production", {**PLAN, "delivery": {"width": 128}})["task_id"]
            project.call("register_artifact", {**reg, "task_id": wrong})
            rejected = project.call("submit_delivery", {"task_id": wrong, "notes": "Offline"})
            self.assertFalse(rejected["spec_checks"]["width"])
            self.assertFalse(rejected["accepted"])

    def test_stdio_production(self):
        with tempfile.TemporaryDirectory() as folder, ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as http:
            http.calls, http.fail = [], False
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            try:
                asyncio.run(self.production(Path(folder), http))
            finally:
                http.shutdown()
                worker.join()

    async def production(self, workspace, http):
        clip(workspace, "red")
        env = {
            "GEMINI_API_KEY": "loopback-test",
            "VIBEVIDEO_GEMINI_MODEL": "fixture",
            "VIBEVIDEO_GEMINI_BASE_URL": f"http://127.0.0.1:{http.server_port}/v1beta",
            "NO_PROXY": "127.0.0.1",
            "PATH": os.environ["PATH"],
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=["-I", "-m", "vibevideo_mcp", "serve", "--workspace", str(workspace)],
            env=env,
        )

        @asynccontextmanager
        async def session():
            async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
                await client.initialize()
                yield client

        async def call(tool, **args):
            reply = await client.call_tool(tool, args)
            self.assertFalse(reply.isError, reply.content[0].text)
            return json.loads(reply.content[0].text)

        async with session() as client:
            listed = await client.list_tools()
            self.assertEqual(len(listed.tools), 11)
            bad = await client.call_tool("plan_production", {**PLAN, "prompt": "Other brief"})
            self.assertTrue(bad.isError)
            self.assertEqual((await call("status"))["tasks"], [])
            task = (await call("plan_production", **PLAN))["task_id"]
            another = (await call("plan_production", **PLAN))["task_id"]
            media = {"path": "draft.mp4", "source_files": ["color.txt"]}
            common = {"media": media, "task_id": task}
            self.assertTrue(
                (await client.call_tool("probe_media", {"path": "../outside", "task_id": task})).isError
            )
            probed = await call(
                "probe_media",
                task_id=task,
                path="draft.mp4",
                measurements=["decode", "motion", "silence", "loudness"],
            )
            self.assertEqual(probed["motion"]["decoded_frame_count"], 8)
            self.assertEqual(probed["decode"], "passed")
            self.assertTrue(probed["loudness"]["measured"])
            await call("describe_media", **common)
            await call("locate_event", query="Find the pause", **common)
            reply = await client.call_tool(
                "inspect_segment", {**common, "question": "What is visible?", "evidence_frames": True}
            )
            self.assertFalse(reply.isError, reply.content[0].text)
            self.assertIn("image", [c.type for c in reply.content])
            await call("transcribe_speech", **common)
            self.assertEqual(base64.b64decode(http.calls[-1][2][0]["data"])[:4], b"OggS")
            check = {**common, "requirement_id": "r1", "stage": "preview"}
            finding = await call("check_requirement", **check)
            before = len(http.calls)
            self.assertTrue((await call("check_requirement", **check))["cache_hit"])
            self.assertEqual(len(http.calls), before)
            replay = clip(workspace, "blue")
            comparison = await call("compare_media", finding_id=finding["finding_id"], **common)
            self.assertEqual(comparison["verdict"], "resolved")
            self.assertTrue(
                (
                    await client.call_tool(
                        "compare_media", {**common, "task_id": another, "finding_id": finding["finding_id"]}
                    )
                ).isError
            )
            await call("check_requirement", **{**check, "stage": "final"})
            art = await call(
                "register_artifact",
                video="draft.mp4",
                source_files=["color.txt"],
                render_command=replay,
                task_id=task,
            )
            delivered = await call("submit_delivery", artifact_id=art["artifact_id"], task_id=task)
            self.assertTrue(delivered["accepted"])
            self.assertTrue(Path(delivered["video"]).is_file())
            self.assertNotIn(finding["finding_id"], delivered["workflow"]["final_evidence"]["r1"])
            self.assertTrue(
                all(c[:2] == ("/v1beta/models/fixture:generateContent", "loopback-test") for c in http.calls)
            )
            http.fail = True
            error = await client.call_tool("inspect_segment", {**common, "question": "Different question"})
            self.assertTrue(error.isError)
            self.assertIn("503", error.content[0].text)
        async with session() as client:
            self.assertCountEqual((await call("status"))["tasks"], [task, another])
            state = await call("status", task_id=task)
            self.assertEqual(state["delivery"]["sha256"], art["sha256"])
            self.assertEqual(
                state["revision_checks"][comparison["finding_id"]]["changed_declared_sources"], ["color.txt"]
            )
            self.assertIsNone((await call("status", task_id=another))["delivery"])
