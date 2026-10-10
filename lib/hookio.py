"""Hook I/O: read the event from stdin, write the JSON reply. See docs/HOOK_SCHEMA.md."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

MAX_FIELD = 10_000  # documented cap for additionalContext / systemMessage / reasons


def clip(text: str, limit: int = MAX_FIELD) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n…[truncated]"


# ---------------------------------------------------------------- locations

def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def data_dir(plugin: str) -> Path:
    """Persistent per-plugin state in ~/.claude/<plugin>/.

    Deliberately not CLAUDE_PLUGIN_DATA: hooks receive that variable but commands Claude runs
    through the Bash tool (the plugins' bin/ CLIs) do not, so the CLIs could never find the
    hooks' data. A fixed path keeps both sides on the same files.
    """
    d = home() / ".claude" / plugin
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_dir(event: dict) -> Path:
    p = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd()
    return Path(p)


def log_error(plugin: str, msg: str) -> None:
    try:
        with open(data_dir(plugin) / "errors.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- config

def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(plugin: str, project: Path, defaults: dict) -> dict:
    """defaults <- ~/.claude/<plugin>.json <- <project>/.claude/<plugin>.json.

    Objects merge key by key; lists and scalars replace. A broken file is skipped
    and logged, never fatal.
    """
    cfg = dict(defaults)
    for path in (home() / ".claude" / f"{plugin}.json", project / ".claude" / f"{plugin}.json"):
        if not path.is_file():
            continue
        try:
            layer = json.loads(path.read_text())
            if isinstance(layer, dict):
                cfg = _merge(cfg, layer)
            else:
                log_error(plugin, f"config {path}: top level is not an object")
        except (OSError, ValueError) as e:
            log_error(plugin, f"config {path}: {e}")
    return cfg


# ---------------------------------------------------------------- output builders

def context(event_name: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": clip(text)}}


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": clip(reason)}}


def block_stop(reason: str) -> dict:
    """Stop / SubagentStop: keep Claude going with `reason` as feedback."""
    return {"decision": "block", "reason": clip(reason)}


def with_system_message(out: Optional[dict], msg: str) -> dict:
    out = dict(out or {})
    out["systemMessage"] = clip(msg)
    return out


# ---------------------------------------------------------------- Bash helpers

_EXIT_RE = re.compile(r"^Exit code (\d+)")


class BashRun:
    """A finished Bash call, from PostToolUse (success) or PostToolUseFailure."""

    def __init__(self, event: dict):
        ti = event.get("tool_input") or {}
        self.command: str = ti.get("command") or ""
        self.timeout_ms: Optional[int] = ti.get("timeout")
        self.background: bool = bool(ti.get("run_in_background"))
        self.duration_ms: Optional[int] = event.get("duration_ms")
        if event.get("hook_event_name") == "PostToolUseFailure":
            err = event.get("error") or ""
            m = _EXIT_RE.match(err)
            self.exit_code: Optional[int] = int(m.group(1)) if m else None
            self.output: str = err.split("\n", 1)[1] if m and "\n" in err else ("" if m else err)
            self.ok = False
            self.interrupted = bool(event.get("is_interrupt"))
        else:
            tr = event.get("tool_response") or {}
            if not isinstance(tr, dict):
                tr = {"stdout": str(tr)}
            self.exit_code = 0
            self.output = (tr.get("stdout") or "") + (("\n" + tr["stderr"]) if tr.get("stderr") else "")
            self.ok = True
            self.interrupted = bool(tr.get("interrupted"))
            self.background_task_id = tr.get("backgroundTaskId")


def is_bash_event(event: dict) -> bool:
    return event.get("tool_name") == "Bash"


_SEPARATORS = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")", "{", "}"}
_PREFIX_WORDS = {"sudo", "time", "command", "exec", "nohup", "env", "builtin", "then", "do", "else", "!"}
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def segments(command: str) -> list:
    """Split a shell command into simple-command word lists.

    `cd web && FOO=1 sudo npm i -D x | tee log` -> [['cd', 'web'], ['npm', 'i', '-D', 'x'], ['tee', 'log']].
    Leading env assignments and wrapper words (sudo, time, env, nohup, ...) are dropped.
    Best effort: on a quoting error the line is split on whitespace.
    """
    import shlex
    out = []
    for line in command.splitlines():
        line = re.sub(r"\d*>&\d+", " ", line)          # 2>&1: fd duplication, not a separator
        line = re.sub(r"&>>?|\d+>>?", " > ", line)       # &>file, 2>file -> plain redirection
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
            lex.whitespace_split = True
            lex.commenters = "#"
            tokens = list(lex)
        except ValueError:
            tokens = line.split()
        cur: list = []
        skip_target = False
        for tok in tokens:
            if skip_target:
                skip_target = False
                continue
            if tok in (">", ">>", "<", "<<", "<<<"):
                skip_target = True
                continue
            if tok[:1] in (">", "<"):
                continue  # >file / <file glued to its target
            if tok in _SEPARATORS:
                if cur:
                    out.append(cur)
                cur = []
                continue
            if not cur and (_ASSIGN_RE.match(tok) or tok in _PREFIX_WORDS):
                continue
            cur.append(tok)
        if cur:
            out.append(cur)
    return out


# ---------------------------------------------------------------- runner

Handler = Callable[[dict], Optional[dict]]


def run(plugin: str, handler: Handler) -> int:
    """Read one event from stdin, call handler, print its JSON. Always returns 0.

    A hook must never break the session: every exception is logged and swallowed.
    """
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        out = handler(event)
        if out:
            sys.stdout.write(json.dumps(out))
    except BaseException as e:  # noqa: BLE001 - fail open, always
        if isinstance(e, SystemExit) and e.code in (0, None):
            return 0
        log_error(plugin, "".join(traceback.format_exception(type(e), e, e.__traceback__)).strip())
    return 0
