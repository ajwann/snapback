---
name: undo
description: Restore files that a shell command destroyed, using the snapshots snapback takes before risky Bash commands (git reset --hard, git checkout -- ., git restore, git clean, rm, git stash drop). Use when the user says a command lost, deleted, reverted or overwrote their uncommitted work, asks to undo a shell command, or asks what snapback saved.
---

# Undo a destructive shell command

snapback saves a snapshot of the working tree just before a Bash command that can destroy uncommitted
work. The `snapback` command is on PATH while the plugin is enabled.

1. Run `snapback list`. The newest snapshot is first, and each line shows the command it was taken before.
2. Pick the snapshot taken before the command that did the damage, then run `snapback show <id>`. It
   lists the files that are missing now and would come back, and the files that differ now and would be
   reverted.
3. Tell the user which files would change. Restoring a file overwrites its current contents, so if any
   listed file holds newer work they want to keep, restore only the paths they name.
4. Run `snapback restore <id>` for everything, or `snapback restore <id> path/one path/two` for part.

Facts to rely on:
- A restore first saves the current state as a new snapshot, so it can itself be undone.
- Files created after the snapshot are left alone, and the staging area is not touched.
- Git-ignored files are in a snapshot only if `include_ignored` in `.claude/snapback.json` matched them.
- A "stash entry" snapshot is a stash that `git stash drop` or `clear` discarded. Restoring it puts it
  back on the stash list; apply it with `git stash pop`.
- Snapshots belong to the repository and worktree the command acted on. If that is not the current
  directory, add `--project <dir>` to each command.
- If no snapshot covers the loss, say so plainly. Do not present other recovery routes as certain.
