"""Install the same stdio tools and production skill into either client."""

import argparse
import fcntl
import json
import os
import shutil
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


def install(client, project):
    project = project.resolve(strict=True)
    claude = client == "claude"
    config = project / (".mcp.json" if claude else ".codex/config.toml")
    skill = project / (".claude" if claude else ".agents") / "skills/vibevideo/SKILL.md"
    if any(path.resolve() != path for path in (config, skill)):
        raise ValueError("Install paths must be regular project paths. Resolve existing symlinks first.")
    description = files("vibevideo_mcp").joinpath("skills/vibevideo/SKILL.md").read_text("utf-8")
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
        if skill.exists() and skill.read_text("utf-8") != description:
            raise ValueError(f"Existing skill differs at {skill}. Move it aside or merge your copy.")
        if old is None:
            section["vibevideo"] = entry
            encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n" if claude else tomlkit.dumps(data)
            replace_text(config, encoded)
        if not skill.exists():
            replace_text(skill, description)
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
    setup.add_argument("--project", type=Path, default=Path.cwd())
    subs.add_parser("doctor", help="Check local tools and perception settings without network access")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from .server import serve

            serve(args.workspace)
        elif args.command == "install":
            install(args.client, args.project)
        else:
            return doctor()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        parser.exit(1, f"vibevideo: {exc}\n")
    return 0
