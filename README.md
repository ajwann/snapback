# snapback

Claude Code can rewind the edits Claude makes with its file tools, but not what a shell command does.
snapback covers that gap: just before Claude runs a Bash command that can destroy uncommitted work,
it saves a snapshot of the working tree, so the files can be brought back.

Commands that trigger a snapshot:
- `git reset --hard`
- `git checkout -- <paths>`, `git checkout .`, `git checkout --force`
- `git restore` (unless `--staged` only), `git switch --discard-changes`
- `git clean -f`, `git rm --force`
- `git stash drop` and `git stash clear` (the discarded stash entries are kept instead)
- `rm`, `unlink`, `shred`, `truncate`, `find -delete`, `xargs rm`

These are also recognised when the command is wrapped in a nested shell call. The snapshot is taken in the
repository the command acts on, so `cd ../other && git reset --hard` and `git -C ../other reset --hard`
protect `../other`, and the restore hint Claude sees names that repository.

`rm` is ignored when every target is outside a repository, git-ignored or absent, so clearing a scratch
directory or `node_modules` takes no snapshot. A snapshot is also skipped when nothing changed since the
previous one. The command itself is never blocked or delayed for approval.

A snapshot holds tracked and untracked files, honouring `.gitignore`. It is stored as a commit under
`refs/snapback/` in the repository, built with a throwaway copy of the index, so your staging area,
stash, branches and `git status` are untouched.

## Getting files back

```
snapback list                  # snapshots, newest first, with the command each one preceded
snapback show 7                # what restoring snapshot 7 would change
snapback restore 7             # put every file back
snapback restore 7 src/app.py  # or only some paths
snapback clear                 # delete every snapshot of this repository
```

Or ask Claude to undo it: the bundled `/snapback:undo` skill walks through the same steps.

- A restore first snapshots the current state, so a restore can itself be undone.
- Files created after the snapshot are left alone.
- A kept stash entry is restored onto the stash list; `git stash pop` applies it.
- With several worktrees, each one lists and restores its own snapshots.

**Hooks:** `PreToolUse` on Bash. **Config:** `{"enabled": true, "keep": 50, "max_age_days": 14, "patterns": [], "include_ignored": [], "announce": true}`.
Git repositories only.

## Install

From Anthropic's plugin directory, once listed: search for `snapback` in `/plugin directory` in Claude Code, or under **Customize > Plugins** on claude.ai.

To try it from a clone for one session: `claude --plugin-dir /path/to/snapback`.

This plugin is for Claude Code: it is built on hooks, which claude.ai chat does not load.

## Configuration

Settings are read from `~/.claude/snapback.json` and then `<project>/.claude/snapback.json`. The project file wins, objects merge by key, and a broken file is skipped and logged.

- `patterns`: extra regular expressions. A command matching one is snapshotted first, for example `["\\bmake\\s+distclean\\b"]`.
- `include_ignored`: globs of git-ignored files to capture as well, for example `[".env*"]`. Off by default,
  because it writes those files, secrets included, into the repository's local object store. They stay
  local unless you push `refs/snapback/*` yourself (`git push --mirror` does).
- `keep` and `max_age_days`: older snapshots are dropped and their refs deleted.
- `announce`: set to `false` to hide the one-line banner shown when a snapshot is taken.

## Limits

- It only sees commands Claude runs through the Bash tool. Commands you type in another terminal, and
  deletions made from inside a script or build tool, take no snapshot.
- It covers deleting and discarding, not overwriting: `mv`, `cp`, `> file` and `sed -i` take no snapshot
  unless you add them to `patterns`.
- `rm -rf .git`, or `rm -rf` of the whole repository, cannot be undone: the snapshots live in `.git`.
  snapback says so instead of taking one.
- A snapshot copies untracked files into the repository's object store, so a large untracked file that
  is not git-ignored makes `.git` grow by about its size until the snapshot expires.
- A file that was staged and then deleted from disk is not in the snapshot.
- Git-ignored files are not captured unless `include_ignored` matches them.
- Files inside a submodule or a nested repository are not captured by the outer repository's snapshot.
- Outside a git repository it does nothing.

## What it runs and stores

It is Python 3.9+ standard library only, makes no network requests, and sends nothing anywhere. Every hook fails open: an internal error is logged to `~/.claude/snapback/errors.log` and the hook exits 0.

- Runs `git` in the project to write the snapshot commit and its ref under `refs/snapback/`.
- Stores the list of snapshots for each repository in `~/.claude/snapback/`.
- To remove everything it stored in a repository: `snapback clear`, then `git gc --prune=now` to free the space.

## Development

```
python3 -m pytest -q
claude plugin validate --strict .
```

`docs/HOOK_SCHEMA.md` records the hook payloads this plugin depends on.

## License

MIT. See `LICENSE`.
