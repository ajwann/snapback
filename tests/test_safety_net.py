import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT, run_hook
from safety_net.core import DEFAULTS, GONE, find, handle, load, restore, risk, take


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def commit(cwd, msg):
    sh(cwd, "add", "-A")
    sh(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)


@pytest.fixture
def repo(project):
    sh(project, "init", "-q")
    (project / "app.py").write_text("v1\n")
    (project / "keep.txt").write_text("keep\n")
    (project / ".gitignore").write_text("node_modules/\n.env\n")
    commit(project, "init")
    (project / "app.py").write_text("v1\nuncommitted edit\n")      # dirty tracked file
    (project / "notes.md").write_text("untracked notes\n")          # untracked file
    return project


def pre(project, command, **kw):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(project), "session_id": "sess-1",
            "tool_input": {"command": command}, **kw}


def cli(project, *args, home):
    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "safety-net"), *args], cwd=str(project),
                       capture_output=True, text=True, env=env, timeout=30)
    return r.returncode, r.stdout


@pytest.mark.parametrize("command, expected", [
    ("git reset --hard", "git reset --hard"),
    ("git reset --hard HEAD~1", "git reset --hard"),
    ("git -C . reset --hard origin/main", "git reset --hard"),
    ("git reset --soft HEAD~1", None),
    ("git reset app.py", None),
    ("git checkout -- app.py", "git checkout -- <paths>"),
    ("git checkout .", "git checkout <paths>"),
    ("git checkout app.py", "git checkout <paths>"),
    ("git checkout HEAD~2 -- app.py", "git checkout -- <paths>"),
    ("git checkout -f main", "git checkout --force"),
    ("git checkout main", None),
    ("git checkout -b feature", None),
    ("git restore app.py", "git restore"),
    ("git restore --staged app.py", None),
    ("git restore --staged --worktree app.py", "git restore"),
    ("git clean -fd", "git clean"),
    ("git clean -fdx", "git clean"),
    ("git clean -n", None),
    ("git clean -nfd", None),
    ("git switch --discard-changes main", "git switch --discard-changes"),
    ("git switch main", None),
    ("git stash drop", "git stash drop"),
    ("git stash clear", "git stash clear"),
    ("git stash", None),
    ("git stash pop", None),
    ("git rm -f app.py", "git rm --force"),
    ("git rm --cached app.py", None),
    ("rm app.py", "rm"),
    ("rm -rf .", GONE),
    ("rm -rf .git", GONE),
    ("cd sub && rm -rf ../app.py", "rm"),
    ("rm -rf *.py", "rm"),
    ("rm -rf $BUILD_DIR", "rm"),
    ("rm -rf /tmp/scratch-xyz", None),
    ("rm -rf node_modules", None),
    ("rm -rf node_modules app.py", "rm"),
    ("rm .env", None),
    ("find . -name '*.orig' -delete", "find -delete"),
    ("find . -name '*.pyc' -exec rm {} +", "find -delete"),
    ("find . -name '*.py'", None),
    ("git ls-files | xargs rm", "rm"),
    ("truncate -s 0 app.py", "truncate"),
    ("git status && git diff", None),
    ("git commit -m 'rm -rf everything'", None),
    ("echo 'git reset --hard'", None),
    ("ls -la", None),
    ("git reset \\\n  --hard HEAD~1", "git reset --hard"),
    ("rm -rf \\\n  app.py", "rm"),
    ("bash -c 'git reset --hard'", "git reset --hard"),
    ("sh -lc \"cd sub && rm ../app.py\"", "rm"),
    ("eval 'git clean -fd'", "git clean"),
    ("bash scripts/build.sh", None),
    ("sudo rm -rf app.py", "rm"),
])
def test_risk(repo, command, expected):
    (repo / "sub").mkdir(exist_ok=True)
    found = risk(command, repo)
    assert (found[0] if found else None) == expected
    assert found is None or found[1].resolve() == repo.resolve()


@pytest.fixture
def other(tmp_path):
    """A second repository next to the one the session runs in."""
    d = tmp_path / "other"
    d.mkdir()
    sh(d, "init", "-q")
    (d / "lib.py").write_text("v1\n")
    commit(d, "init")
    (d / "lib.py").write_text("v1\nuncommitted in other\n")
    return d


@pytest.mark.parametrize("command", [
    "git -C {other} reset --hard",
    "cd {other} && git reset --hard",
    "cd {other} && rm lib.py",
    "rm -f {other}/lib.py",
    "bash -c 'cd {other}; git checkout -- .'",
])
def test_snapshot_goes_to_the_repository_the_command_acts_on(repo, other, command):
    out = handle(pre(repo, command.format(other=other)))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "saved snapshot 1" in ctx and f"--project {other.resolve()}" in ctx
    assert load(repo) == [] and len(load(other)) == 1
    assert "lib.py" in sh(other, "ls-tree", "-r", "--name-only", "refs/safety-net/1")


def test_session_outside_any_repository_still_covers_one_it_enters(repo):
    out = handle(pre(repo.parent, f"cd {repo.name} && git reset --hard"))
    assert "saved snapshot 1" in out["hookSpecificOutput"]["additionalContext"]


def test_removing_the_git_directory_is_reported_not_snapshotted(repo):
    for command in ("rm -rf .", "rm -rf .git", f"rm -rf {repo}"):
        out = handle(pre(repo, command))
        assert "no snapshot taken" in out["hookSpecificOutput"]["additionalContext"]
        assert out["systemMessage"] == "safety-net: no snapshot taken."
    assert load(repo) == []


def test_worktree_snapshots_stay_with_their_worktree(repo, isolated):
    commit(repo, "wip")
    wt = repo.parent / "wt"
    sh(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    (wt / "only-wt.txt").write_text("worktree work\n")
    assert "saved snapshot 1" in handle(pre(wt, "git clean -fd"))["hookSpecificOutput"]["additionalContext"]
    code, text = cli(repo, "list", home=isolated)
    assert code == 0 and "no snapshots for this working tree" in text and f"--project {wt.resolve()}" in text
    code, text = cli(repo, "restore", "1", home=isolated)
    assert code == 1 and "another working tree" in text and not (repo / "only-wt.txt").exists()
    (wt / "only-wt.txt").unlink()
    code, _ = cli(wt, "restore", home=isolated)
    assert code == 0 and (wt / "only-wt.txt").read_text() == "worktree work\n"
    (repo / "app.py").write_text("same tree as nothing before\n")
    assert handle(pre(repo, "git checkout -- .")) is not None          # main worktree gets its own snapshot


def test_nested_repository_and_odd_names(repo, isolated):
    nested = repo / "vendor"
    nested.mkdir()
    sh(nested, "init", "-q")
    (nested / "n.txt").write_text("n\n")
    commit(nested, "n")
    (repo / "sp ace é.txt").write_text("odd\n")
    handle(pre(repo, "git clean -fd"))
    (repo / "sp ace é.txt").unlink()
    code, text = cli(repo, "show", home=isolated)
    assert "would come back: sp ace é.txt" in text and "would come back: vendor" not in text
    assert "not in the snapshot (nested repository or submodule): vendor" in text
    code, _ = cli(repo, "restore", "1", "sp ace é.txt", home=isolated)
    assert code == 0 and (repo / "sp ace é.txt").read_text() == "odd\n"


def test_parallel_hooks_get_distinct_ids(repo, isolated):
    import concurrent.futures
    def one(i):
        (repo / f"p{i}.txt").write_text(f"{i}\n")
        return run_hook(pre(repo, f"rm p{i}.txt"), isolated)
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        list(ex.map(one, range(6)))
    rows = load(repo)
    refs = sh(repo, "for-each-ref", "--format=%(refname)", "refs/safety-net/").split()
    assert len({r["id"] for r in rows}) == len(rows) == len(refs) >= 1


def test_snapshot_then_restore_after_reset_hard_and_clean(repo, isolated):
    before = sh(repo, "status", "--porcelain")
    out = handle(pre(repo, "git reset --hard && git clean -fd"))
    assert sh(repo, "status", "--porcelain") == before and sh(repo, "diff", "--cached", "--name-only") == ""
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PreToolUse" and "permissionDecision" not in ctx
    assert "saved snapshot 1" in ctx["additionalContext"] and "safety-net restore 1" in ctx["additionalContext"]
    assert out["systemMessage"].startswith("safety-net: saved snapshot 1")
    assert sh(repo, "for-each-ref", "--format=%(refname)", "refs/safety-net/").split() == ["refs/safety-net/1"]

    sh(repo, "reset", "--hard", "-q")
    sh(repo, "clean", "-fdq")
    assert (repo / "app.py").read_text() == "v1\n" and not (repo / "notes.md").exists()

    code, text = cli(repo, "show", home=isolated)
    assert code == 0 and "differs now, would be reverted: app.py" in text
    assert "missing now, would come back: notes.md" in text
    code, text = cli(repo, "restore", "1", home=isolated)
    assert code == 0 and "restored 2 file(s) from snapshot 1" in text
    assert (repo / "app.py").read_text() == "v1\nuncommitted edit\n"
    assert (repo / "notes.md").read_text() == "untracked notes\n"
    assert sh(repo, "diff", "--cached", "--name-only") == ""
    assert sh(repo, "status", "--porcelain") == before


def test_restore_is_undoable_keeps_newer_files_and_takes_paths(repo):
    row = take(repo, DEFAULTS, "rm app.py notes.md", "rm")
    (repo / "app.py").unlink()
    (repo / "notes.md").write_text("rewritten after the snapshot\n")
    (repo / "later.py").write_text("created after the snapshot\n")
    lines = restore(repo, row, DEFAULTS, ["app.py"], repo)
    assert "saved the current state as snapshot 2" in lines[0]
    assert (repo / "app.py").read_text() == "v1\nuncommitted edit\n"
    assert (repo / "notes.md").read_text() == "rewritten after the snapshot\n"   # not asked for
    restore(repo, row, DEFAULTS)
    assert (repo / "notes.md").read_text() == "untracked notes\n"
    assert (repo / "later.py").exists()                                           # newer file left alone
    restore(repo, find(repo, "2"), DEFAULTS, ["notes.md"], repo)                  # undo the restore
    assert (repo / "notes.md").read_text() == "rewritten after the snapshot\n"


def test_unchanged_tree_is_not_snapshotted_twice(repo):
    assert handle(pre(repo, "rm notes.md")) is not None
    assert handle(pre(repo, "git checkout -- app.py")) is None
    assert len(load(repo)) == 1
    (repo / "app.py").write_text("v1\nanother edit\n")
    assert "saved snapshot 2" in handle(pre(repo, "git checkout -- app.py"))["hookSpecificOutput"]["additionalContext"]


def test_safe_commands_non_repos_and_disabled_are_silent(repo, project, tmp_path):
    assert handle(pre(repo, "git status")) is None
    assert handle(pre(repo, "rm -rf /tmp/elsewhere")) is None
    assert load(repo) == []
    plain = tmp_path / "plain"
    plain.mkdir()
    assert handle(pre(plain, "rm -rf .")) is None
    (repo / ".claude").mkdir()
    (repo / ".claude" / "safety-net.json").write_text('{"enabled": false}')
    assert handle(pre(repo, "git reset --hard")) is None


def test_config_patterns_announce_and_include_ignored(repo):
    (repo / ".env").write_text("SECRET=1\n")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "safety-net.json").write_text(
        json.dumps({"patterns": [r"\bmake\s+distclean\b"], "announce": False, "include_ignored": [".env*"]}))
    out = handle(pre(repo, "make distclean"))
    assert "systemMessage" not in out and "saved snapshot 1" in out["hookSpecificOutput"]["additionalContext"]
    assert ".env" in sh(repo, "ls-tree", "-r", "--name-only", "refs/safety-net/1").split()
    (repo / ".env").unlink()
    cfg = dict(DEFAULTS, include_ignored=[".env*"])
    restore(repo, find(repo, "1"), cfg)
    assert (repo / ".env").read_text() == "SECRET=1\n"


def test_ignored_files_are_left_out_by_default(repo):
    (repo / ".env").write_text("SECRET=1\n")
    handle(pre(repo, "git clean -fdx"))
    assert ".env" not in sh(repo, "ls-tree", "-r", "--name-only", "refs/safety-net/1").split()


def test_prune_by_count_and_age(repo):
    cfg = dict(DEFAULTS, keep=3)
    for i in range(5):
        (repo / "app.py").write_text(f"edit {i}\n")
        take(repo, cfg, f"rm {i}", "rm")
    assert [r["id"] for r in load(repo)] == [3, 4, 5]
    refs = sh(repo, "for-each-ref", "--format=%(refname)", "refs/safety-net/").split()
    assert sorted(refs) == ["refs/safety-net/3", "refs/safety-net/4", "refs/safety-net/5"]
    rows = load(repo)
    rows[0]["t"] = int(time.time()) - 30 * 86400
    from safety_net.core import _save
    _save(repo, rows)
    (repo / "app.py").write_text("edit 9\n")
    take(repo, DEFAULTS, "rm 9", "rm")
    assert [r["id"] for r in load(repo)] == [4, 5, 6]


def test_dropped_stash_is_kept_and_restorable(repo):
    sh(repo, "-c", "user.email=t@t", "-c", "user.name=t", "stash", "push", "-qm", "my wip")
    out = handle(pre(repo, "git stash drop"))
    assert "kept the stash entry" in out["hookSpecificOutput"]["additionalContext"]
    sh(repo, "stash", "drop", "-q")
    assert sh(repo, "stash", "list") == ""
    lines = restore(repo, find(repo, "last"), DEFAULTS)
    assert "stash list" in lines[0] and "my wip" in sh(repo, "stash", "list")
    assert handle(pre(repo, "git stash clear")) is None            # same commit is already pinned


def test_repo_without_commits(project):
    sh(project, "init", "-q")
    (project / "a.txt").write_text("first draft\n")
    assert "saved snapshot 1" in handle(pre(project, "rm a.txt"))["hookSpecificOutput"]["additionalContext"]
    (project / "a.txt").unlink()
    restore(project, find(project, "1"), DEFAULTS)
    assert (project / "a.txt").read_text() == "first draft\n"


def test_cli_list_and_errors(repo, isolated, tmp_path):
    code, text = cli(repo, home=isolated)
    assert code == 0 and "no snapshots" in text
    run_hook(pre(repo, "git checkout -- ."), isolated)
    code, text = cli(repo, "list", home=isolated)
    assert code == 0 and "working tree" in text and "git checkout -- ." in text
    code, text = cli(repo, "restore", "99", home=isolated)
    assert code == 1 and "no snapshot 99" in text
    plain = tmp_path / "plain"
    plain.mkdir()
    code, text = cli(plain, "list", home=isolated)
    assert code == 1 and "not inside a git repository" in text


def test_script_end_to_end_from_subdirectory(repo, isolated):
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.txt").write_text("deep\n")
    out = run_hook(pre(repo / "sub", "rm deep.txt"), isolated)
    assert "saved snapshot 1" in out["hookSpecificOutput"]["additionalContext"]
    (repo / "sub" / "deep.txt").unlink()
    code, _ = cli(repo / "sub", "restore", "1", "deep.txt", home=isolated)
    assert code == 0 and (repo / "sub" / "deep.txt").read_text() == "deep\n"
