---
name: work-the-tracker
description: Use when told "work the tracker" (phone, laptop or a scheduled routine), or asked to pick up the next task from a project's task tracker. Runs `tracker next`, claims one task, builds it to a reviewed PR, and records progress in the tracker so no two sessions work the same task. Also use when he gives an idea to queue ("idea: ... for BH", "add task: ... to oplify", "later: ...", "remember to build ..."): `tracker add` puts it in that project's tracker. And when he answers a blocked task ("T-160: take it over", "T-143: restore boosts and credits"): `tracker unblock`.
---

# Work the tracker

The tracker (a CSV in its own repo, driven by the `tracker` tool) is the only record of who works on what. The tool enforces the rules: it refuses a claim over a held row and over a leftover, so do not decide either yourself. Pick the project with `--project NAME` on every command (or `TRACKER_PROJECT=NAME`); inside a clone, the conf whose `CODE_REPO` matches it is used. Repo specifics (test commands, PR format, merge target) live in the project's task file; read it before building.

1. `tracker next`. Output starts `NEXT LEFTOVER|NEW T-12`, or `NEXT NONE` (nothing to do: say so and stop).
   Leftovers (stale work nobody holds) always come before new tasks. HELD rows are someone else's; skip them.
2. `tracker claim T-12`. It makes the branch and worktree and prints `CLAIMED T-12 branch <b> claim <sha> worktree=… holder=…`, then the task card: every filled column (acceptance criteria, endpoints, tables, rules, dependencies), Next step and Blocker, and earlier merged PRs with what they left. Work only in that worktree.
   - Build from the card, not from the title. `tracker show T-12` prints it again at any time (read-only; `--json` for scripts).
   - The card is the spec. If it does not say what done looks like, block the task (step 5) instead of guessing.
   - exit 3: someone holds it. Run `tracker next` again and take what it names.
   - exit 4: a leftover comes first. Claim the leftover the message names instead.
   - exit 2: config or API error, or the row is Done, Blocked or not ready. Read the message.
   A row is not ready when its dependencies are open, it has no acceptance criteria, or its criteria wait on a decision ("needs the founder's ok", "spec first", "TBD") with no `Answer <date>:` line after it. `tracker next` lists each as `NOT READY T-12 | <reason>`.
3. Build in the worktree following the task file. Failing test first, smallest fix, push, open a PR whose body starts `Closes T-12` (or `Part of T-12` plus `Left: <what remains>`).
4. While waiting (CI, a review, a long build), keep the claim alive: `tracker beat T-12 --status "In review" --pr 34`. Statuses: Building, In review, Waiting. A row not touched for the lease (6 h by default) becomes a leftover anyone may take.
5. Blocked on a decision or something only a person can do: `tracker block T-12 "<what is needed, in one sentence>" --needs founder` (or `--needs stakeholder`). It marks the row and sends the notification at once. Then run `tracker next` for other work.
6. After the PR is reviewed and merged: `tracker done T-12 --pr 34`.
7. Abandoning the task: `tracker release T-12`. An untouched claim goes back to the queue; a branch that carries work stays as a leftover.

Exit 5 from any command, or a push refused with "taken over": another session took the row. Stop, push nothing, report it.

The tracker is the only queue: never take work from GitHub issues or `agent:*` labels. A row that waits on another project names it in `Depends on` as `<project>:<id>` (`oplify-api:T-344`). The PR body says `Closes T-12`.

Examples use `T-12` and PR 34; the real ids come from `tracker next`.

## Writing a task card

Use this whenever you file work: an audit's findings, a bug you root-caused, a follow-up you leave behind. A builder who starts cold, months later, must be able to build from the card alone. A title-only card gets lost or built wrong.

Every card has:
1. **Problem**, with a measured number and its date ("0 of 312 rows sent, prod query 2026-11-22"), not an adjective.
2. **Files**: paths and function names, each checked to exist when you write the card.
3. **Repro**: one read-only command or query that shows the problem today. The builder re-runs it to see if the card is still true.
4. **Done when**: measured on the real path (UI, API, bot, the deployed service), with a target number. "Tests pass" is not done.
5. **Test loop**: the failing test to write first, the gate command, and the before/after numbers the PR must show.
6. **Depends on**: other card ids, and the build order when several cards land together.
7. **Decisions**: anything only the founder can decide, named as a question. The card stays Blocked until it has an `Answer <date>:` line.

Before filing:
- Measure the funnel stage by stage before blaming one stage. Fix the stage that loses the most, and file it first.
- Search for what already exists: the repo, open PRs, the brain, official APIs and their terms, and how others solve it. Put what you found and rejected on the card, with the reason.
- One problem per card. Split an audit into cards and add one ordering card that lists them.
- A public repo gets no personal data, no company or customer names and no private URLs. Describe the case ("an employer on platform X") and keep the specifics in the private tracker or the brain.

Then re-read each card as the cold builder: could you start without asking anything? If not, add what is missing.

## Adding work from the phone

Triggers: "idea: ...", "add task ...", "later: ...", "remember to build ...". Queue it; do not build it now.

1. Pick the project from his words (BH is `bh-morning`; oplify has an api and an app conf). List them with `ls /etc/claude-routines/*.conf`. Ask one question only if two fit or none does, then wait.
2. `tracker add --conf /etc/claude-routines/<name>.conf "<title>" --notes "<the rest of what he said>"`. The title is his words in under 80 characters; drop `--notes` if there is nothing more.
3. Reply in one line with the id and the link it printed: `Added T-404: <title> <link>`.

A task with nothing about what done looks like is filed Blocked, and so is one whose notes wait on a decision. The reply then says `(Blocked, not built until answered: <reason>)`: pass that on so he answers it. Exit 2 "no notes column": run it again with everything in the title. Never edit the CSV by hand. Add `--dry-run` to see what would be written.

## Answering a blocked task

Triggers: he replies to a "Needs you" item with a task id and an answer ("T-160: take it over", "T-303: park it", "T-143: restore boosts and credits").

1. `tracker unblock --conf /etc/claude-routines/<name>.conf T-160 --note "<his answer>"`. The answer lands in the notes column as `Answer <date>: …` for the next builder. A row with a branch becomes a leftover, so the next build resumes that branch first.
2. Reply in one line with what it printed.

A block whose reason is plainly gone (the work it waited on is merged or saved) may be unblocked without asking; say so in the reply.
