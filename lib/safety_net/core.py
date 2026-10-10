"""Snapshot the working tree before a Bash command that can destroy uncommitted work, and restore it later."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import hookio

PLUGIN = "safety-net"
REF = "refs/safety-net/"

DEFAULTS = {
    "enabled": True,
    "keep": 50,              # snapshots kept per repository
    "max_age_days": 14,
    "patterns": [],          # extra regexes; a command matching one is snapshotted first
    "include_ignored": [],   # globs of git-ignored files to capture too, e.g. [".env*"]
    "announce": True,        # one-line banner to the user when a snapshot is taken
}

# ---------------------------------------------------------------- risk detection

_GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _git_sub(words: list) -> tuple:
    """('checkout', [args...], dir_from_-C) for a git command word list."""
    i, chdir = 1, None
    while i < len(words) and words[i].startswith("-"):
        if words[i] in _GIT_GLOBAL_WITH_VALUE and i + 1 < len(words):
            if words[i] == "-C":
                chdir = words[i + 1]
            i += 2
        else:
            i += 1
    return (words[i] if i < len(words) else ""), words[i + 1:], chdir


def _short_flags(args: list) -> str:
    return "".join(a[1:] for a in args if re.fullmatch(r"-[A-Za-z]+", a))


def _git_risk(words: list, cwd: Path) -> Optional[str]:
    sub, args, chdir = _git_sub(words)
    if chdir:
        cwd = (cwd / chdir) if not os.path.isabs(chdir) else Path(chdir)
    longs = {a.split("=")[0] for a in args if a.startswith("--")}
    short = _short_flags(args)
    plain = [a for a in args if not a.startswith("-")]
    if sub == "reset" and "--hard" in longs:
        return "git reset --hard"
    if sub == "checkout":
        if "--" in args and args.index("--") < len(args) - 1:
            return "git checkout -- <paths>"
        if "f" in short or "--force" in longs:
            return "git checkout --force"
        if any(p in (".", "./") or (cwd / p).exists() for p in plain):
            return "git checkout <paths>"
    if sub == "restore":
        staged_only = ("--staged" in longs or "S" in short) and not ("--worktree" in longs or "W" in short)
        if not staged_only:
            return "git restore"
    if sub == "clean" and ("f" in short or "--force" in longs) and "n" not in short and "--dry-run" not in longs:
        return "git clean"
    if sub == "switch" and ("f" in short or longs & {"--force", "--discard-changes"}):
        return "git switch --discard-changes"
    if sub == "rm" and ("f" in short or "--force" in longs) and "--cached" not in longs:
        return "git rm --force"
    if sub == "stash" and plain[:1] in (["drop"], ["clear"]):
        return "git stash " + plain[0]
    return None


def _targets_matter(targets: list, cwd: Path, root: Path) -> bool:
    """False when every rm target is known to be outside the repository, git-ignored or absent."""
    inside = []
    for t in targets:
        if t.startswith("~"):
            t = os.path.expanduser(t)
        if "$" in t or "`" in t:
            return True  # cannot know where it points
        p = Path(os.path.normpath(t if os.path.isabs(t) else str(cwd / t)))
        try:
            p.resolve().relative_to(root.resolve())
            inside.append(str(p))
        except (ValueError, OSError):
            try:
                root.resolve().relative_to(p.resolve())  # target is a parent of the repo
                return True
            except (ValueError, OSError):
                continue
    if not inside:
        return False
    plain = [p for p in inside if not re.search(r"[*?\[]", p)]
    if len(plain) < len(inside):
        return True  # a glob may match anything
    plain = [p for p in plain if os.path.lexists(p)]  # removing what is not there loses nothing
    if not plain:
        return False
    try:
        r = subprocess.run(["git", "-C", str(root), "check-ignore", "--", *plain],
                           capture_output=True, text=True, timeout=10)
        return len(r.stdout.splitlines()) < len(plain)
    except (OSError, subprocess.TimeoutExpired):
        return True


def risk(command: str, cwd: Path, root: Path, patterns: Optional[list] = None) -> Optional[str]:
    """Short label of the first destructive thing in `command`, or None."""
    for seg in hookio.segments(command):
        w = list(seg)
        if w and w[0] == "xargs":
            w = [x for x in w[1:] if not x.startswith("-")] or w
            if w[0] == "rm":
                return "rm"
        tool = os.path.basename(w[0])
        if tool == "cd" and len(w) > 1:
            cwd = Path(w[1]) if os.path.isabs(w[1]) else cwd / w[1]
        elif tool == "git":
            label = _git_risk(w, cwd)
            if label:
                return label
        elif tool in ("rm", "unlink", "shred", "truncate", "rmdir"):
            targets = [a for a in w[1:] if not a.startswith("-") or a == "-"]
            if tool == "truncate":
                targets = [t for t in targets if not re.fullmatch(r"[+\-<>/%]?\d+[A-Za-z]*", t)]
            if targets and _targets_matter(targets, cwd, root):
                return tool
        elif tool == "find" and ("-delete" in w or ("-exec" in w and "rm" in w[w.index("-exec"):])):
            return "find -delete"
    for p in patterns or []:
        try:
            if re.search(p, command):
                return f"pattern {p}"
        except re.error as e:
            hookio.log_error(PLUGIN, f"bad pattern {p!r}: {e}")
    return None


# ---------------------------------------------------------------- git plumbing

def git(project: Path, *args: str, env: Optional[dict] = None, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True,
                       env=env, timeout=25, errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def repo_root(project: Path) -> Optional[Path]:
    if not shutil.which("git"):
        return None
    try:
        return Path(git(project, "rev-parse", "--show-toplevel").strip())
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None


def snapshot_tree(root: Path, include_ignored: Optional[list] = None) -> str:
    """Tree object of the working tree (tracked + untracked, honouring .gitignore). Touches nothing visible:
    it uses a throwaway index file seeded from the real one so unchanged files are not re-hashed."""
    gitdir = Path(git(root, "rev-parse", "--absolute-git-dir").strip())
    fd, tmp_index = tempfile.mkstemp(prefix="safety-net-index-")
    os.close(fd)
    try:
        real = gitdir / "index"
        if real.is_file():
            shutil.copyfile(real, tmp_index)
        else:
            os.unlink(tmp_index)
        env = dict(os.environ, GIT_INDEX_FILE=tmp_index, GIT_OPTIONAL_LOCKS="0")
        git(root, "add", "-A", "--ignore-errors", ".", env=env, check=False)
        if include_ignored:
            ignored = git(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                          env=env, check=False).split("\0")
            extra = [f for f in ignored if f and any(
                fnmatch.fnmatch(f, g) or fnmatch.fnmatch(os.path.basename(f), g) for g in include_ignored)]
            for i in range(0, len(extra), 200):
                git(root, "add", "-f", "--ignore-errors", "--", *extra[i:i + 200], env=env, check=False)
        return git(root, "write-tree", env=env).strip()
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass


_IDENT = {"GIT_AUTHOR_NAME": "safety-net", "GIT_AUTHOR_EMAIL": "safety-net@localhost",
          "GIT_COMMITTER_NAME": "safety-net", "GIT_COMMITTER_EMAIL": "safety-net@localhost"}


# ---------------------------------------------------------------- snapshot index

def index_file(root: Path) -> Path:
    """One index per repository. Keyed on the common git dir, because refs are shared by all worktrees."""
    common = git(root, "rev-parse", "--git-common-dir").strip()
    common = os.path.realpath(common if os.path.isabs(common) else os.path.join(root, common))
    return hookio.data_dir(PLUGIN) / (hashlib.sha1(common.encode()).hexdigest()[:16] + ".jsonl")


def load(root: Path) -> list:
    p = index_file(root)
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _save(root: Path, rows: list) -> None:
    p = index_file(root)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    os.replace(tmp, p)


def _prune(root: Path, rows: list, cfg: dict) -> list:
    keep = max(1, int(cfg.get("keep") or 50))
    cutoff = time.time() - float(cfg.get("max_age_days") or 14) * 86400
    kept = [r for r in rows[-keep:] if r.get("t", 0) >= cutoff] or rows[-1:]
    alive = {r["id"] for r in kept}
    for r in rows:
        if r["id"] not in alive:
            git(root, "update-ref", "-d", REF + str(r["id"]), check=False)
    return kept


def _add(root: Path, rows: list, cfg: dict, **row) -> dict:
    row = {"id": (rows[-1]["id"] + 1) if rows else 1, "t": int(time.time()), "root": str(root), **row}
    git(root, "update-ref", REF + str(row["id"]), row["commit"])
    _save(root, _prune(root, rows + [row], cfg))
    return row


def take(root: Path, cfg: dict, command: str, why: str, session: str = "") -> Optional[dict]:
    """Snapshot the working tree. Returns the new row, or None when nothing changed since the last one."""
    rows = load(root)
    tree = snapshot_tree(root, cfg.get("include_ignored") or [])
    last_tree = next((r for r in reversed(rows) if r.get("kind") == "tree"), None)
    if last_tree and last_tree.get("tree") == tree:
        return None
    head = git(root, "rev-parse", "-q", "--verify", "HEAD", check=False).strip()
    parents = ["-p", head] if head else []
    commit = git(root, "commit-tree", tree, *parents, "-m", f"safety-net: before {command[:200]}",
                 env=dict(os.environ, **_IDENT)).strip()
    return _add(root, rows, cfg, kind="tree", tree=tree, commit=commit, head=head, why=why,
                cmd=command[:300], session=session)


def pin_stashes(root: Path, cfg: dict, command: str, session: str = "") -> list:
    """Keep the stash entries a `git stash drop|clear` is about to discard reachable."""
    words = next((s for s in hookio.segments(command) if s[:1] == ["git"] and "stash" in s), [])
    _, args, _ = _git_sub(words)
    plain = [a for a in args if not a.startswith("-")]
    if plain[:1] == ["clear"]:
        commits = git(root, "stash", "list", "--format=%H", check=False).split()
    else:
        ref = plain[1] if len(plain) > 1 else "stash@{0}"
        if ref.isdigit():
            ref = f"stash@{{{ref}}}"
        commits = git(root, "rev-parse", "-q", "--verify", ref, check=False).split()
    out = []
    for c in commits:
        rows = load(root)
        if any(r.get("commit") == c for r in rows):
            continue
        subject = git(root, "log", "-1", "--format=%s", c, check=False).strip()
        out.append(_add(root, rows, cfg, kind="stash", commit=c, why="git stash " + plain[0],
                        cmd=command[:300], session=session, subject=subject))
    return out


# ---------------------------------------------------------------- restore (used by bin/safety-net)

def find(root: Path, ident: str) -> dict:
    rows = load(root)
    if not rows:
        raise LookupError("no snapshots for this repository")
    if ident in ("last", "latest", ""):
        return rows[-1]
    for r in rows:
        if str(r["id"]) == ident.lstrip("#"):
            return r
    raise LookupError(f"no snapshot {ident}; `safety-net list` shows the ones that exist")


def differences(root: Path, row: dict, cfg: dict) -> list:
    """[(status, path)] of what restoring `row` would change: M = reverted, A = brought back."""
    now = snapshot_tree(root, cfg.get("include_ignored") or [])
    out = []
    for line in git(root, "diff", "--name-status", "--no-renames", "--no-ext-diff", now, row["tree"]).splitlines():
        status, _, path = line.partition("\t")
        if status[:1] in ("A", "M", "T"):
            out.append(("A" if status[0] == "A" else "M", path))
    return out


def restore(root: Path, row: dict, cfg: dict, paths: Optional[list] = None, cwd: Optional[Path] = None) -> list:
    """Put the snapshot's files back. Returns printable lines. Files created since the snapshot are kept."""
    if row.get("kind") == "stash":
        git(root, "stash", "store", "-m", row.get("subject") or "restored by safety-net", row["commit"])
        return [f"put snapshot {row['id']} back on the stash list as stash@{{0}}: {row.get('subject', '')}"]
    changes = differences(root, row, cfg)
    lines = []
    backup = take(root, cfg, f"safety-net restore {row['id']}", "safety-net restore")
    if backup:
        lines.append(f"saved the current state as snapshot {backup['id']} first, so this restore can be undone")
    where, spec = (cwd or root, paths) if paths else (root, ["."])
    git(where, "restore", "--overlay", f"--source={row['commit']}", "--worktree", "--", *spec)
    if paths:
        lines.append(f"restored {', '.join(paths)} from snapshot {row['id']}")
    else:
        lines.append(f"restored {len(changes)} file(s) from snapshot {row['id']}")
        lines += [f"  {'brought back' if s == 'A' else 'reverted   '} {p}" for s, p in changes[:40]]
        if len(changes) > 40:
            lines.append(f"  …and {len(changes) - 40} more")
    return lines


def describe(row: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row.get("t", 0)))
    what = "stash entry" if row.get("kind") == "stash" else "working tree"
    return f"{row['id']:>4}  {when}  {what:<12}  before `{row.get('cmd', '')[:90]}`"


# ---------------------------------------------------------------- hook

def handle(event: dict) -> Optional[dict]:
    if event.get("hook_event_name") != "PreToolUse" or not hookio.is_bash_event(event):
        return None
    command = (event.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        return None
    cfg = hookio.load_config(PLUGIN, hookio.project_dir(event), DEFAULTS)
    if not cfg.get("enabled", True):
        return None
    cwd = Path(event.get("cwd") or hookio.project_dir(event))
    root = repo_root(cwd)
    if not root:
        return None
    why = risk(command, cwd, root, cfg.get("patterns"))
    if not why:
        return None
    session = event.get("session_id") or ""
    if why.startswith("git stash"):
        rows = pin_stashes(root, cfg, command, session)
        if not rows:
            return None
        ids = ", ".join(str(r["id"]) for r in rows)
        text = (f"safety-net: kept the stash entr{'y' if len(rows) == 1 else 'ies'} this command discards "
                f"(snapshot {ids}). `safety-net restore <id>` puts one back on the stash list.")
    else:
        row = take(root, cfg, command, why, session)
        if not row:
            return None  # identical to the previous snapshot: already covered
        text = (f"safety-net: saved snapshot {row['id']} of the working tree before this command ({why}). "
                f"If it loses work, `safety-net show {row['id']}` lists what differs and "
                f"`safety-net restore {row['id']} [paths]` brings the files back.")
    out = hookio.context("PreToolUse", text)
    if cfg.get("announce", True):
        out = hookio.with_system_message(out, text.split(". ")[0] + ".")
    return out
