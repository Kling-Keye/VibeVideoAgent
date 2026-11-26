"""Install the same stdio tools and production skill into either client."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import shlex
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

import tomlkit

ENV_NAMES = ["GEMINI_API_KEY", "VIBEVIDEO_GEMINI_MODEL", "VIBEVIDEO_GEMINI_BASE_URL"]


def replace_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".vibevideo-install-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def profile_settings(path, project, profile):
    old = path.read_text("utf-8") if path.exists() else None
    data = json.loads(old) if old is not None else {}
    if profile == "PreToolUse" and data.get("disableAllHooks"):
        raise ValueError(f"Hooks are disabled in {path}")
    hooks = data.get("hooks", {})
    entries = hooks.get("PreToolUse", [])
    command = shlex.join([sys.executable, "-I", "-m", "vibevideo_mcp.PreToolUse", str(project)])
    rule = {"matcher": ".*", "hooks": [{"type": "command", "command": command, "timeout": 10}]}
    updated = list(entries)
    if profile == "PreToolUse" and rule not in entries:
        updated.append(rule)
    if profile == "standard":
        updated = [row for row in entries if row != rule]
    if updated == entries:
        return None
    data.setdefault("hooks", {})["PreToolUse"] = updated
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def install(client, project, profile=None):
    project = project.resolve(strict=True)
    claude = client == "claude"
    config = project / (".mcp.json" if claude else ".codex/config.toml")
    skill = project / (".claude" if claude else ".agents") / "skills/vibevideo/SKILL.md"
    hook_path = project / (".claude/settings.json" if claude else ".codex/hooks.json")
    guide = skill.parent / "references/rendering.md"
    if any(path.resolve() != path for path in (config, skill, guide, hook_path)):
        raise ValueError("Install paths must be regular project paths. Resolve existing symlinks first.")
    description = files("vibevideo_mcp").joinpath("skills/vibevideo/SKILL.md").read_text("utf-8")
    rendering = files("vibevideo_mcp").joinpath("skills/vibevideo/references/rendering.md").read_text("utf-8")
    args = ["-I", "-m", "vibevideo_mcp", "serve", "--workspace", str(project)]
    entry = {"command": sys.executable, "args": args}
    if claude:
        entry["type"] = "stdio"
        # Expansion happens in Claude Code at startup. The config holds no secret values.
        entry["env"] = {key: "${" + key + ":-}" for key in ENV_NAMES}
    else:
        entry.update(env_vars=ENV_NAMES, tool_timeout_sec=600)
    fd = os.open(project, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = config.read_text("utf-8") if config.exists() else ""
        if claude:
            data = json.loads(existing) if config.exists() else {}
            section = data.setdefault("mcpServers", {})
        else:
            data = tomlkit.parse(existing)
            section = data.setdefault("mcp_servers", tomlkit.table())
        old = section.get("vibevideo")
        if old is not None and dict(old) != entry:
            raise ValueError(
                f"Existing vibevideo entry differs in {config}. Rename or reconcile it before installing."
            )
        old_skill = skill.read_text("utf-8") if skill.exists() else None
        bundled_skill_digests = {
            "913efe9363d3c17dd0abed0d33a0eefb12c8b73eddc5bf4f106c711f6d0844ee",  # 0.1.0
            "364475cf291ce8df5fbf6afe73559496259e238bd2b76a3e27371a43e52f54bb",  # 0.2.0
        }
        if (
            old_skill is not None
            and old_skill != description
            and hashlib.sha256(old_skill.encode()).hexdigest() not in bundled_skill_digests
        ):
            raise ValueError(f"Existing skill differs at {skill}. Move it aside or merge your copy.")
        if guide.exists() and guide.read_text("utf-8") != rendering:
            raise ValueError(f"Existing rendering guide differs at {guide}")
        if (
            profile == "PreToolUse"
            and not claude
            and any(data.get("features", {}).get(k) is False for k in ("hooks", "codex_hooks"))
        ):
            raise ValueError(f"Hooks are explicitly disabled in {config}")
        updated_hooks = profile_settings(hook_path, project, profile) if profile else None
        if old is None:
            section["vibevideo"] = entry
            encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n" if claude else tomlkit.dumps(data)
            replace_text(config, encoded)
        if old_skill != description:
            replace_text(skill, description)
        if not guide.exists():
            replace_text(guide, rendering)
        if updated_hooks is not None:
            replace_text(hook_path, updated_hooks)
    finally:
        os.close(fd)
    print(f"Project MCP config: {config}\nProduction skill: {skill}")


def doctor():
    for name in ("ffmpeg", "ffprobe"):
        print(f"{name}: {shutil.which(name) or 'missing'}")
    for name in ENV_NAMES[:2]:
        print(f"{name}: {'set' if os.getenv(name) else 'missing (semantic perception unavailable)'}")
    print("Provider connectivity: unchecked (offline diagnostic)")
    return int(not all(shutil.which(name) for name in ("ffmpeg", "ffprobe")))


def main():
    parser = argparse.ArgumentParser(prog="vibevideo")
    subs = parser.add_subparsers(dest="command", required=True)
    run = subs.add_parser("serve", help="Start the project MCP server over stdio")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    setup = subs.add_parser("install", help="Register the MCP server and install its workflow skill")
    setup.add_argument("client", choices=["claude", "codex"])
    setup.add_argument(
        "--profile",
        choices=["standard", "PreToolUse"],
        help="Enable the PreToolUse hook or remove it with standard. Omit to preserve the current setting.",
    )
    setup.add_argument("--project", type=Path, default=Path.cwd())
    subs.add_parser("doctor", help="Check local tools and perception settings without network access")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from .server import serve

            serve(args.workspace)
        elif args.command == "install":
            install(args.client, args.project, args.profile)
        else:
            return doctor()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        parser.exit(1, f"vibevideo: {exc}\n")
    return 0
