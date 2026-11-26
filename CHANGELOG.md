# Changelog

## 0.2.1 — first public release

- Eleven MCP tools for Claude Code, Codex and other stdio clients: `plan_production`, seven perception primitives (`describe_media`, `locate_event`, `inspect_segment`, `transcribe_speech`, `check_requirement`, `compare_media`, `probe_media`), `register_artifact`, `submit_delivery` and `status`.
- `vibevideo install claude|codex` registers the project MCP server and the bundled production skill, plus the rendering setup guide.
- Optional `--profile PreToolUse` hook enforces the perception boundary used in the paper experiments and writes a privacy-preserving audit log.
- `vibevideo doctor` offline environment check.
- Fix: `probe_media` silence measurement no longer fails on containers without a format-level duration.
- Offline test suite (FFmpeg-generated clips, real MCP stdio transport, loopback Gemini fixture).
