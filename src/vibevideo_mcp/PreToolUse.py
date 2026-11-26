"""PreToolUse for the local Claude Code and Codex PreToolUse profile (standard library only)."""

import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from .io import read_json, write_json

MCP = "mcp__vibevideo__"
MEDIA = set(
    ".png .jpg .jpeg .webp .gif .bmp .tif .tiff .avif .heic .mp4 .mov .webm .mkv .avi .wav .mp3 .ogg .flac .m4a .pdf .ipynb".split()
)
PRIVATE = {".vibevideo", ".agents", ".mcp.json", ".codex", ".claude", ".git", ".secrets"}
DENIED = {
    "Agent",
    "Task",
    "Workflow",
    "NotebookEdit",
    "EnterWorktree",
    "ExitWorktree",
    "CronCreate",
    "ScheduleWakeup",
    "SendMessage",
    "spawn_agent",
    "send_input",
    "spawn_agents_on_csv",
}
EXTERNAL = re.compile(
    r"/v1/(?:messages|chat/completions)|generateContent|api\.anthropic\.com|generativelanguage|GEMINI_API_KEY|ANTHROPIC_(?:AUTH_TOKEN|API_KEY)|\b(?:chafa|viu|imgcat|tesseract)\b|IPython\.display|\b(?:from|import)\s+(?:anthropic|google\.genai)",
    re.I,
)
PLAN_FIRST = (
    "Call plan_production, then status with its returned task_id to activate that saved plan in this session."
)
USE_MEDIA = "Inspect source media and renders through the VibeVideo MCP tools."


def is_media(path):
    if path.suffix.lower() in MEDIA:
        return True
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        head = stream.read(32)
    return head.startswith(
        (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"%PDF", b"OggS", b"fLaC", b"RIFF")
    ) or head[4:8] in {b"ftyp", b"WEBP"}


def private(path, project):
    if not path.is_relative_to(project):
        return True
    parts = path.relative_to(project).parts
    return any(part in PRIVATE or part.startswith(".env") for part in parts)


def readable(path, project):
    # The PreToolUse profile must still be able to load its own project skill.
    for folder in (".claude", ".agents"):
        skill = project / folder / "skills/vibevideo"
        if path in (skill / "SKILL.md", skill / "references/rendering.md"):
            return True
    return not private(path, project) and not is_media(path)


def text_inputs(words):
    if not words:
        return []
    if words[0] == "cat":
        return words[1:]
    if words[0] == "head":
        if len(words) >= 3 and words[1] == "-n" and words[2].isdigit():
            return words[3:]
        return words[1:]
    if (
        words[0] == "sed"
        and len(words) >= 4
        and words[1] == "-n"
        and re.fullmatch(r"\d+(?:,\d+)?p", words[2])
    ):
        return words[3:]
    return []


def before_plan_shell(cmd, cwd, project):
    if re.search(r"[;&|`$<>\\\n]", cmd):
        return False
    words = shlex.split(cmd)
    if words == ["pwd"]:
        return True
    if not words:
        return False
    if words[0] == "ls":
        return all(
            arg in {"-a", "-l", "-la", "-al"}
            or (not arg.startswith("-") and not private((cwd / arg).resolve(), project))
            for arg in words[1:]
        )
    if words[:2] == ["rg", "--files"]:
        return all(
            not arg.startswith("-") and not private((cwd / arg).resolve(), project) for arg in words[2:]
        )
    paths = text_inputs(words)
    return bool(paths) and all(
        not arg.startswith("-") and readable((cwd / arg).resolve(), project) and (cwd / arg).is_file()
        for arg in paths
    )


def edit_paths(name, args):
    if name != "apply_patch":
        return [args.get("file_path", "")]
    patch = args.get("command") or args.get("input") or args.get("patch") or ""
    return re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", patch, re.M)


def decision(project, binding, event):
    name = event["tool_name"]
    args = event.get("tool_input") or {}
    if isinstance(args, str) and name == "apply_patch":
        args = {"input": args}
    cwd = Path(event.get("cwd") or project).resolve()
    if name == MCP + "plan_production":
        binding.unlink(missing_ok=True)
        return None
    task = args.get("task_id")
    if name == MCP + "status":
        if task is None:
            return None
        if not isinstance(task, str) or not re.fullmatch(r"[0-9a-f]{32}", task):
            return "Use a task_id returned by plan_production or status."
        state = project / ".vibevideo/runs" / task / "state.json"
        if state.resolve() != state or not state.is_file() or not read_json(state).get("production_plan"):
            return PLAN_FIRST
        write_json(binding, task)
        return None

    if name == "Skill" and args.get("skill") == "vibevideo":
        return None
    if name in DENIED or name == "Skill" or (name.startswith("mcp__") and not name.startswith(MCP)):
        return "Use the single production agent and the registered VibeVideo tools."
    if re.search(r"screenshot|view_image|image_generation|computer|browser|web_search", name, re.I):
        return USE_MEDIA
    if name == "Read":
        return None if readable((cwd / args["file_path"]).resolve(), project) else USE_MEDIA
    active = read_json(binding) if binding.exists() else None
    if name.startswith(MCP):
        return None if active and active == task else PLAN_FIRST
    if name in {"Write", "Edit", "MultiEdit", "apply_patch"}:
        paths = edit_paths(name, args)
        if not paths:
            return "Supply a patch with explicit project file paths."
        for value in paths:
            path = (cwd / value).resolve()
            if private(path, project) or path.is_relative_to(project / "inputs"):
                return "Edit project sources and keep inputs, configuration and method state intact."
        return None if active else PLAN_FIRST
    if name in {"Bash", "exec_command", "Monitor", "write_stdin"}:
        cmd = args.get("command") or args.get("cmd") or args.get("chars") or ""
        if EXTERNAL.search(cmd):
            return USE_MEDIA
        cwd = (cwd / (args.get("workdir") or ".")).resolve()
        if active:
            # Recognize common direct reader commands. Render scripts remain normal executable code.
            try:
                words = shlex.split(cmd)
            except ValueError:
                words = []
            for value in text_inputs(words):
                path = (cwd / value).resolve()
                if path.is_file() and not readable(path, project):
                    return USE_MEDIA
            return None
        return None if before_plan_shell(cmd, cwd, project) else PLAN_FIRST
    return None


def hook(project, event):
    session = event.get("session_id")
    if not isinstance(session, str) or not session:
        raise ValueError("Missing hook session ID")
    store = project / ".vibevideo/hooks"
    if store.resolve() != store:
        raise ValueError("Hook state must be a project-local directory")
    store.mkdir(parents=True, mode=0o700, exist_ok=True)
    session_hash = hashlib.sha256(session.encode()).hexdigest()
    binding = store / (session_hash + ".json")
    if binding.is_symlink():
        raise ValueError("Invalid session state")
    reason = decision(project, binding, event)
    row = {
        "time": time.time(),
        "session": session_hash,
        "tool": event["tool_name"],
        "decision": "deny" if reason else "continue",
        "reason": reason,
    }
    fd = os.open(store / "audit.jsonl", os.O_APPEND | os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as log:
        log.write(json.dumps(row) + "\n")
    return reason


def main():
    try:
        reason = hook(Path(sys.argv[1]).resolve(strict=True), json.load(sys.stdin))
    except (OSError, IndexError, KeyError, ValueError, TypeError, AttributeError):
        reason = "PreToolUse could not validate this call. Check the installed hook and project state."
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
