"""Bounded local IO and durable writes; no directory-wide media scans."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inside(root: Path, name: str, *, exists=True) -> Path:
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("use a workspace-relative path without '..'")
    out = (root / p).resolve()
    if not out.is_relative_to(root.resolve()):
        raise ValueError("path escapes workspace")
    for part in (*p.parts, *out.relative_to(root.resolve()).parts):
        if part in {
            ".vibevideo",
            ".git",
            ".claude",
            ".codex",
            ".secrets",
            ".ssh",
            ".mcp.json",
        } or part.startswith(".env"):
            raise ValueError("Path points to project state or private configuration")
    if exists and not out.is_file():
        raise ValueError(f"not a regular file: {name}")
    return out


def stop_group(process: subprocess.Popen):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Parent may have exited while a child ignores SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(command: list[str], *, timeout: float, cwd=None, env=None):
    argv = command
    if sys.platform == "linux":
        argv = [
            sys.executable,
            "-S",
            str(Path(__file__).with_name("_process_guard.py")),
            str(os.getpid()),
            *command,
        ]
    p = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=max(0.01, timeout))
    except BaseException:
        stop_group(p)
        raise
    if p.returncode:
        raise RuntimeError(f"{Path(command[0]).name} exited {p.returncode}: {err[-1000:]}")
    return out


def probe(path: Path, timeout=30) -> dict:
    obj = json.loads(
        run_command(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            timeout=timeout,
        )
    )
    videos = [
        s
        for s in obj.get("streams", [])
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    if not videos:
        raise ValueError("artifact has no video stream")
    stream = videos[0]
    n, d = stream.get("avg_frame_rate", "0/1").split("/")
    fps = float(n) / float(d) if float(d) else 0
    duration = float(obj.get("format", {}).get("duration") or stream.get("duration") or 0)
    if duration <= 0 or fps <= 0:
        raise ValueError("artifact duration/fps must be positive")
    return {
        "duration": duration,
        "fps": fps,
        "width": stream["width"],
        "height": stream["height"],
        "has_audio": any(s.get("codec_type") == "audio" for s in obj["streams"]),
        "codec": stream.get("codec_name"),
    }
