---
name: work-the-tracker
description: Use when told "work the tracker" (phone, laptop or a scheduled routine), or asked to pick up the next task from a project's task tracker. Runs `tracker next`, claims one task, builds it to a reviewed PR, and records progress in the tracker so no two sessions work the same task.
---

# Work the tracker

The tracker (a CSV in its own repo, driven by the `tracker` tool) is the only record of who works on what. The tool enforces the rules: it refuses a claim over a held row and over a leftover, so do not decide either yourself. Pick the project with `--project NAME` on every command (or `TRACKER_PROJECT=NAME`); inside a clone, the conf whose `CODE_REPO` matches it is used. Repo specifics (test commands, PR format, merge target) live in the project's task file; read it before building.

1. `tracker next`. Output starts `NEXT LEFTOVER|NEW T-12`, or `NEXT NONE` (nothing to do: say so and stop).
   Leftovers (stale work nobody holds) always come before new tasks. HELD rows are someone else's; skip them.
2. `tracker claim T-12`. It makes the branch and worktree and prints `CLAIMED T-12 branch <b> claim <sha> worktree=… holder=…`. Work only in that worktree.
   - exit 3: someone holds it. Run `tracker next` again and take what it names.
   - exit 4: a leftover comes first. Claim the leftover the message names instead.
   - exit 2: config or API error, or the row is Done, Blocked or not ready. Read the message.
3. Build in the worktree following the task file. Failing test first, smallest fix, push, open a PR whose body starts `Closes T-12` (or `Part of T-12` plus `Left: <what remains>`).
4. While waiting (CI, a review, a long build), keep the claim alive: `tracker beat T-12 --status "In review" --pr 34`. Statuses: Building, In review, Waiting. A row not touched for the lease (6 h by default) becomes a leftover anyone may take.
5. Blocked on a decision or something only a person can do: `tracker block T-12 "<what is needed, in one sentence>" --needs founder` (or `--needs stakeholder`). It marks the row and sends the notification at once. Then run `tracker next` for other work.
6. After the PR is reviewed and merged: `tracker done T-12 --pr 34`.
7. Abandoning the task: `tracker release T-12`. An untouched claim goes back to the queue; a branch that carries work stays as a leftover.

Exit 5 from any command, or a push refused with "taken over": another session took the row. Stop, push nothing, report it.

Examples use `T-12` and PR 34; the real ids come from `tracker next`.
