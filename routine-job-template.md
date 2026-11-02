# {{NAME}} — scheduled session ({{REPO}})

Written by routine-new on {{DATE}}; edit this file on the VPS (never while a `routine: {{NAME}}` run is live). Nobody is awake; work start to finish.
Work the tracker (skill `work-the-tracker`): every command is `tracker --project {{NAME}} <command>`. The tool refuses a claim over a held row or a leftover; follow what it prints.
Rules for shell, evidence, DoD and CI-watch: /home/claude-agent/agent-jobs/definition-of-done.md and the `{{NAME}}` items in founder-actions.md.
You are the lead: plan, review, decide, merge. Up to 3 Sonnet builders at once, one per claimed task; pass `model` on every Agent call (builders, fixers `"sonnet"`; CI-log summaries `"haiku"`).
Builder prompt: "Repo {{REPO}}, task T-<N>, CLAIMED line: <paste it>. Build it in that worktree following the Task file in /home/claude-agent/agent-jobs/{{NAME}}.md and definition-of-done.md; reply in under 15 lines."
Review each PR before merging (DoD 1–3, 5, 6; the new test must fail without the fix). On PASS: `gh api repos/{{REPO}}/statuses/<sha> -f state=success -f context=routine/review -f description="reviewed PASS"`, merge, then `tracker --project {{NAME}} done T-N --pr <n>`.
Max 5 tasks a run; start nothing new on a usage-limit warning. Nothing to do: one query, one-line report.
Report (last step): `save_bug` per root-caused bug (project "{{DIR}}"), then one message with `claude-notify "<report>" || true` (line breaks as `\n`) and print it.
Line 1: `Needs you: <n> — You need to: <action + link>` or `Needs you: 0 — Nothing needed from you.`; then at most 3 lines on what changed for users, then `{{BASE}} CI: green|red <run url>` from `gh run list -R {{REPO}} -b {{BASE}} -L 3`.

## Task file

| key | value |
|---|---|
| GitHub (write it out in every `gh` call) | `-R {{REPO}}` |
| clone | /home/claude-agent/work/{{DIR}} |
| integration branch (PRs target it, merges go into it) | `{{BASE}}` |
| production branch (off limits, DoD hard rule 1) | `{{PROD}}` |
| CI check that boots the app | {{BOOT_LINE}} |
| local gate | the commands CI runs (`.github/workflows/*.yml`, the repo's CLAUDE.md/README), touched files only, one call per command |

**Branches (this overrides the repo's own files):** the branch `tracker claim` creates, cut from `origin/{{BASE}}`, PR into `{{BASE}}`. Merging into `{{BASE}}` is allowed, nothing else. DoD hard rule 1 covers `{{PROD}}` of this repo too; in DoD rule 6 the base is `{{BASE}}`.
Boot check (only while the table says "none yet"): task 1 is a builder adding one CI job that builds the app, starts it and gets a success answer (health endpoint, CLI `--help`, or the test server). Add its tracker row first (a new line at the end of the CSV).
After it merges, append to founder-actions.md: `- [ ] <date> | {{NAME}} | <PR link> | make the verdict require the boot check | add VERDICT_CHECKS=("{{REPO}}|<check name>") to the routine conf, run setup-claude-routines, set the table row above to the check name`.

Builders: read the repo's CLAUDE.md / AGENTS.md (ignore its branch section), `search_memory` (project "{{DIR}}") once. Regression test first, run it red, smallest fix, push. PR body: first line `Closes T-<N>` (or `Part of T-<N>` plus `Left: <what remains>`), then `Checked first:`, `## What changed`, `## How verified`, `## Not verified`, `## Risk`. Then `tracker --project {{NAME}} beat T-<N> --status "In review" --pr <n>` and keep beating while CI runs.
