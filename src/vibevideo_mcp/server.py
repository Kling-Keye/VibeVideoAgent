"""One project-scoped stdio server. Each production has its own durable state."""

import asyncio
import base64
import fcntl
import json
import os
import uuid
from pathlib import Path
from typing import Annotated

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import Field, create_model

from .io import read_json, write_json
from .media_tools import TOOLS
from .schema import DeliverySpec, Model
from .workflow import Plan, PlannedCheck, Text, WorkflowService

TaskId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class Start(Plan):
    task_id: TaskId | None = Field(
        default=None, description="Omit for a new task. Supply to replay its plan."
    )
    prompt: Text = Field(description="The user's original video brief, including exact quoted requirements.")
    delivery: DeliverySpec = Field(
        default_factory=DeliverySpec, description="Only explicitly requested output constraints."
    )


class Status(Model):
    task_id: TaskId | None = Field(default=None, description="Omit to list productions in this project.")


class Register(Model):
    video: Text
    source_files: list[Text] = Field(
        min_length=1,
        max_length=200,
        description="Editable sources and dependencies to snapshot, relative to the project.",
    )
    render_command: list[Text] = Field(
        min_length=1, description="Replayable argv to record with the sources."
    )


class Submit(Model):
    artifact_id: str | None = None
    notes: str = ""


OPERATIONS = {
    **TOOLS,
    "check_requirement": (
        PlannedCheck,
        "Test a planned requirement on a preview, assembly or final file. Cite only observed evidence.",
    ),
    "register_artifact": (Register, "Snapshot a rendered video, editable files and replay command."),
    "submit_delivery": (
        Submit,
        "Verify the registered bytes and explicit specifications. Report sampled evidence separately.",
    ),
}
REGISTRY = {
    name: (create_model(schema.__name__ + "ForTask", __base__=schema, task_id=(TaskId, ...)), desc)
    for name, (schema, desc) in OPERATIONS.items()
}
REGISTRY.update(
    plan_production=(
        Start,
        "Start a production from the user's brief and ordered beats. Returns task_id for subsequent calls.",
    ),
    status=(Status, "List task IDs, or resume a specific production with its plan, findings and delivery."),
)


def initial_state(task_id, prompt, delivery):
    return {
        "task": {"id": task_id, "prompt": prompt, "delivery": delivery},
        "artifacts": {},
        "budget": {"calls": 0, "frames": 0, "audio_seconds": 0},
        "config": {
            "max_observation_calls": 40,
            "max_frames": 8192,
            "max_audio_seconds": 7200,
            "frames_per_call": 256,
            "max_fps": 16,
            "max_snapshot_bytes": 64_000_000,
            "max_video_bytes": 2_000_000_000,
        },
    }


class Project:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be an existing directory")
        self.runs = self.workspace / ".vibevideo" / "runs"
        if self.runs.resolve() != self.runs:
            raise ValueError("Project state must be a local directory, not a symlink")

    def call(self, name, arguments):
        if name not in REGISTRY:
            raise ValueError("Unknown production tool")
        args = REGISTRY[name][0].model_validate(arguments).model_dump()
        task_id = args.pop("task_id")
        if name == "status" and task_id is None:
            tasks = sorted(p.parent.name for p in self.runs.glob("*/state.json") if not p.parent.is_symlink())
            return {"tasks": tasks}
        new = task_id is None
        task_id = task_id or uuid.uuid4().hex
        root = self.runs / task_id
        if new:
            root.mkdir(parents=True, mode=0o700)
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Unknown task_id. Call status to list saved productions.")
        with (root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = root / "state.json"
            if name == "plan_production":
                prompt, delivery = args.pop("prompt"), args.pop("delivery")
            if new:
                state = initial_state(task_id, prompt, delivery)
            else:
                if not path.is_file():
                    raise ValueError("No saved production for this task_id")
                state = read_json(path)
                if name == "plan_production" and (
                    state["task"]["prompt"] != prompt or state["task"]["delivery"] != delivery
                ):
                    raise ValueError("Brief or delivery spec differs from saved task")
            state["config"]["perception_model"] = os.getenv("VIBEVIDEO_GEMINI_MODEL", "")
            service = WorkflowService(self.workspace, root, state)
            try:
                result = getattr(service, name)(**args)
            except Exception:
                if new:
                    (root / ".lock").unlink()
                    root.rmdir()
                elif name != "status":
                    write_json(path, state)
                raise
            if name != "status":
                write_json(path, state)
            return {"task_id": task_id, **result}


def build_server(workspace):
    project = Project(workspace)
    server = Server("vibevideo")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(name=n, description=d, inputSchema=s.model_json_schema())
            for n, (s, d) in sorted(REGISTRY.items())
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            result = await anyio.to_thread.run_sync(project.call, name, arguments or {})
            content = [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            for frame in result.get("frames", []):
                path = Path(frame["path"]).resolve()
                if (
                    not path.is_relative_to(project.runs / result["task_id"] / "perception")
                    or path.stat().st_size > 8_000_000
                ):
                    raise ValueError("Invalid evidence image")
                content.append(
                    types.TextContent(type="text", text=f"Source time {frame['time']:.6f} seconds")
                )
                content.append(
                    types.ImageContent(
                        type="image", mimeType="image/jpeg", data=base64.b64encode(path.read_bytes()).decode()
                    )
                )
            return types.CallToolResult(content=content, isError=False)
        except Exception as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc)[:1600])], isError=True
            )

    return server


def serve(workspace):
    server = build_server(workspace)

    async def run():
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(run())
