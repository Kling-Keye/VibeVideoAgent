# VibeVideoAgent

VibeVideoAgent gives a coding agent an audiovisual feedback loop while it makes a video. The agent plans from your brief, renders a small preview, checks what a viewer can actually see or hear, edits its source, then verifies the encoded delivery. Claude Code and Codex use the same eleven MCP tools and the same production skill.

## Install from this checkout

Use Python 3.11+ on Linux or macOS. Install FFmpeg and ffprobe on PATH with H.264, AAC and Opus encoding support. The coding agent uses your project's renderer; install any renderer-specific dependencies in that project.

From this repository, create a virtual environment in a permanent location and install the package.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
vibevideo doctor
```

Choose the client you use and point the installer at the directory where you will create videos. You can install both into the same project.

```sh
vibevideo install claude --project /path/to/my-video
vibevideo install codex --project /path/to/my-video
```

The installer registers its absolute Python interpreter and workspace. Keep the environment at that location. Claude Code reads the project’s `.mcp.json` and `.claude/skills/vibevideo/SKILL.md`. Codex reads `.codex/config.toml` and `.agents/skills/vibevideo/SKILL.md`. Reinstalling identical settings is safe. If the project or interpreter moves, remove the old `vibevideo` server entry and run the installer again. Other servers can share the same config file. [Claude Code MCP](https://code.claude.com/docs/en/mcp) · [Codex MCP](https://developers.openai.com/codex/mcp/)

## Paper profile with PreToolUse

For a production with the experimental tool restrictions, opt in per client and per video project:

```sh
vibevideo install claude --project /path/to/my-video --profile paper
vibevideo install codex --project /path/to/my-video --profile paper
```

Restart the client in the trusted project. Review the installed hook in `/hooks`. In Codex, trust the exact **PreToolUse** definition before the first run and review it again after its command changes. Claude Code reads `.claude/settings.json`; Codex reads `.codex/hooks.json`. Verify that **PreToolUse** is enabled before collecting a run. The tool service and the profile use the same project directory. [Claude Code hooks](https://code.claude.com/docs/en/hooks) · [Codex hooks](https://developers.openai.com/codex/hooks/)

Start with `plan_production`, then call `status` with the returned task ID. This binds the validated plan to the current client session. A fresh session can resume by calling `status` with that ID. Before activation, the hook allows brief and skill reads and input inventory, and gates editing, rendering, and media operations. During production it blocks known direct media reads, image-viewing tools, subagent calls, other MCP servers, edits to protected paths through file tools, and explicit model API commands. Shell commands remain available for rendering. The audit records tool names and decisions in `.vibevideo/hooks/audit.jsonl` without logging command bodies, media, or credentials. This profile enforces tool-entry rules; filesystem isolation, resource limits and network controls depend on the host. To turn off the installed rule, rerun the installer with `--profile standard` and restart the client.

## Rendering environment

Install FFmpeg and the renderer your project will use before launching the client. The [rendering guide](src/vibevideo_mcp/skills/vibevideo/references/rendering.md) includes commands for FFmpeg, fonts, browser/Three.js animation, Python drawing, optional Edge TTS, and a local H.264/AAC smoke test. The installer also places the guide beside the skill in each video project.

## Make a first video

In the terminal that will launch your client, supply your Gemini API key via your secret manager or enter it with the following Bash commands. Choose a native Gemini API model your account can use for video, audio, and structured output.

```bash
read -rsp 'Gemini API key: ' GEMINI_API_KEY
export GEMINI_API_KEY
printf '\n'
read -rp 'Gemini model ID: ' VIBEVIDEO_GEMINI_MODEL
export VIBEVIDEO_GEMINI_MODEL
cd /path/to/my-video
claude  # or codex
```

Open a new client session in that project, trust the project and enable the project MCP server when prompted. In either client, try this brief:

> Use the vibevideo skill to make a six-second title card. A blue circle grows into view, pauses, and fades out. Play a short chime at the start of the pause. Give me the final MP4 and the editable source.

The host account supplies the coding model. Gemini receives selected excerpts and the associated inspection question when the agent invokes a semantic tool. The default endpoint is Google's `v1beta` API. To use a compatible gateway, set `VIBEVIDEO_GEMINI_BASE_URL` to its versioned base URL. It must support native `generateContent`, `x-goog-api-key`, inline media, and a structured response. The key is read from the environment at runtime. Planning, local measurements, artifact snapshots and technical delivery checks run offline. [Gemini generateContent](https://ai.google.dev/api/generate-content)

## Tools

| Tool | Use during production |
| --- | --- |
| `plan_production` | Start a task with the original brief, explicit requirements, ordered beats and a representative preview. Returns a task ID. |
| `describe_media` | Index the sampled scenes, layout and sounds of unfamiliar inputs. |
| `locate_event` | Search for an event and optionally refine up to two candidate windows. |
| `inspect_segment` | Inspect a selected interval or pixel crop and optionally return actual evidence frames. |
| `transcribe_speech` | Extract audio and identify spoken lines with excerpt-local and source times. |
| `check_requirement` | Check one quoted requirement against a produced preview, assembly or final encode. |
| `compare_media` | Compare to a reference, or compare a new render to the saved excerpt behind a prior finding. |
| `probe_media` | Inspect streams, duration, full decode, frame repeats, endpoint differences, loudness and silence locally. |
| `register_artifact` | Snapshot an encoded output, selected editable files and the replay command. |
| `submit_delivery` | Decode the snapshot, enforce requested specifications and report evidence tied to its hash. |
| `status` | List project tasks, or pass a task ID to recover the plan, evidence and latest delivery. |

Media paths are relative to the workspace. The crop input uses source pixels `[x,y,width,height]`; returned evidence boxes use normalized excerpt coordinates `[left,top,right,bottom]`. Source times refer to the input file. Set `timeline_offset` when the edit maps source time zero to a different timeline time. Sparse observations have limited coverage. A successful submission means the encoded file met the declared technical constraints. Its verification record separately lists the evidence for each creative requirement.

## Project files and resource use

Each task owns a directory under `.vibevideo/runs/<task_id>/`. It contains a saved plan, sampled excerpts, findings, revision links and registered artifacts. Each registered artifact includes the actual encoded bytes, a snapshot of its explicitly listed project files and its render argv. Include all files needed to reproduce the render when registering. Keep `.vibevideo/` in your video project's ignore file if you use Git. To resume, call `status`, then pass the returned ID. Each task serializes concurrent tool calls; distinct task IDs have independent state and budgets.

The default task budget allows 40 semantic calls, 8,192 sampled frames and 7,200 seconds of audio across calls. Each excerpt samples at most 256 frames at up to 16 fps. Cache hits reuse matching evidence. A two-input comparison must fit the inline upload limit; narrow both intervals if needed. There is no task-wide wall-clock deadline. The installer does not provision a renderer or choose a coding model.

## Other MCP clients and offline tests

Configure a stdio client to run `vibevideo serve --workspace /absolute/path/to/my-video` and forward the Gemini environment variables listed above. The workflow text is bundled at `src/vibevideo_mcp/skills/vibevideo/SKILL.md`.

After installation, run `python -m unittest discover -s tests -v`. The suite generates small synthetic clips with FFmpeg and exercises the MCP protocol against a loopback-only Gemini fixture. It needs no API key or paid model session.

## License

MIT. See [LICENSE](LICENSE).
