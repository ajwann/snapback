# Hook payloads toolbelt depends on

Captured on 2026-10-09 from Claude Code **2.1.295**, using a logging plugin loaded with `--plugin-dir` in a
scratch git repo. Common fields such as `session_id`, `transcript_path`, `cwd`, `permission_mode` and
`prompt_id` follow skillwire's `docs/HOOK_SCHEMA.md`. This file covers the payloads skillwire didn't capture.
When Claude Code changes, update this file first, then `common/hookio.py`.

## Bash, success → `PostToolUse`

```json
{"hook_event_name": "PostToolUse", "tool_name": "Bash",
 "tool_input": {"command": "echo out; echo err 1>&2", "description": "..."},
 "tool_response": {"stdout": "out\nerr", "stderr": "", "interrupted": false,
                   "isImage": false, "noOutputExpected": false},
 "duration_ms": 1004}
```

- stderr arrives merged into `stdout`, and `stderr` was empty here.
- There is **no exit-code field**, because success means exit 0.
- A background run (`"run_in_background": true` in `tool_input`) returns immediately with
  `"backgroundTaskId": "ba6u1zo2s"` and a tiny `duration_ms`. cmd-clock ignores these runs.
- `timeout` appears in `tool_input` only when Claude passes it. The default is 120000 ms (max 600000),
  and `BASH_DEFAULT_TIMEOUT_MS` overrides the default.

## Bash, non-zero exit → `PostToolUseFailure` (not PostToolUse)

```json
{"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
 "tool_input": {"command": "ls /nonexistent_zz", "description": "..."},
 "error": "Exit code 1\nls: /nonexistent_zz: No such file or directory",
 "is_interrupt": false, "duration_ms": 24}
```

There is no `tool_response`. The exit code and output are both in `error`, in the form
`Exit code N\n<output>`. `hookio.BashRun` parses both shapes.

## Edit → `PostToolUse`

`tool_input`: `file_path`, `old_string`, `new_string`, `replace_all`. The `tool_response` adds
`structuredPatch` and `originalFile`.

## Stop

```json
{"hook_event_name": "Stop", "stop_hook_active": false,
 "last_assistant_message": "...", "background_tasks": [], "session_crons": []}
```

These output shapes come from the docs:
- To block, return `{"decision": "block", "reason": "..."}`. Claude continues with the reason as feedback.
- `hookSpecificOutput.additionalContext` also continues the conversation.
- `systemMessage` is a banner shown to the user.

leftovers blocks only when `stop_hook_active` is false. portwatch uses `systemMessage` by default, so
it doesn't force Claude to keep working.

## SessionStart / SessionEnd

SessionStart has `source` (`startup`, `resume`, `clear` or `compact`). SessionEnd has `reason`. leftovers and
portwatch keep their baselines across `resume` and `compact`.

## Background-task notification

A finished background command arrives as a **UserPromptSubmit** whose `prompt` is a
`<task-notification>` XML block. No plugin here uses it.

## Plugin MCP servers

`.mcp.json` at the plugin root: `{"mcpServers": {"name": {"command": ..., "args": ["${CLAUDE_PLUGIN_ROOT}/..."]}}}`.
Stdio servers receive `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` but **not** `CLAUDE_PROJECT_DIR`.
installed-api uses its working directory and accepts an explicit `project_dir` argument.

## Claude Code's Bash shell

Claude Code runs Bash commands in a snapshot of the user's shell (`~/.claude/shell-snapshots/snapshot-*.sh`).
The snapshot keeps the user's aliases and functions, and it adds its own shims: `grep`/`find` become an
embedded ugrep/bfs, and there is a guarded `pkill`. A program name in a command is therefore not always the
binary on PATH. flagcheck reads the newest snapshot and skips any name it shadows.
