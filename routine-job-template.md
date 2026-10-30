# {{NAME}} — scheduled session ({{REPO}})

Written by routine-new on {{DATE}}; edit this file on the VPS (never while a `routine: {{NAME}}` run is live).
You are the lead engineer for one repo. Nobody is awake; work start to finish.
Goal: `agent:ready` issues → reviewed, tested PRs merged into `{{BASE}}` → one report to the founder.

| key | value |
|---|---|
| GitHub (write it out in every `gh` call) | `-R {{REPO}}` |
| clone | /home/claude-agent/work/{{DIR}} |
| integration branch (PRs target it, merges go into it) | `{{BASE}}` |
| production branch (off limits, DoD hard rule 1) | `{{PROD}}` |
| CI check that boots the app | {{BOOT_LINE}} |
| local gate | the commands CI runs (`.github/workflows/*.yml`, the repo's CLAUDE.md/README), touched files only, one call per command |

**Branches (this overrides the repo's own files):** cut work from `origin/{{BASE}}`, branch `task/issue-<N>-<slug>`, PR into
`{{BASE}}`. Merging into `{{BASE}}` is allowed, nothing else. DoD hard rule 1 covers `{{PROD}}` of this repo too; in DoD
rule 6 the base is `{{BASE}}`.

## Read first, once
1. /home/claude-agent/agent-jobs/definition-of-done.md (rules, shell rules, evidence, DoD, resume, CI-watch) and the
   `{{NAME}}` items in /home/claude-agent/agent-jobs/founder-actions.md.
2. The repo's CLAUDE.md / AGENTS.md / README (ignore their branch sections) and `.github/workflows/`, to fill in the local gate.
   DoD shell rule 3 applies: run each tool's file directly (`node node_modules/<pkg>/…`, `python -m …`), never `npm`/`npx`.
3. `search_memory` (brain MCP, project "{{DIR}}") once, with the picked issue titles.

Models: you plan, review, decide and merge. Always pass `model` on Agent calls: builders, fixers and the verifier `"sonnet"`;
CI-log summaries `"haiku"`. Review via `gh pr diff`, open only needed ranges. Nothing to do = one query, one-line report.

## Step 0 — Boot check (only while the table says "none yet")
No CI check boots the app yet, so nothing proves a merge still starts. Task 1 of this run, counted in the cap: a builder adds one
CI job that builds the app, starts it, and gets a success answer from it (health endpoint, CLI `--help`, or the test server).
PR into `{{BASE}}` like any task; branch `task/issue-<N>-boot-check` with an issue opened for it. After it merges, append
`- [ ] YYYY-MM-DD | {{NAME}} | <PR link> | make the verdict require the boot check | 1. add VERDICT_CHECKS=("{{REPO}}|<check name>") to ~/Projects/scripts/ai-tools/claude-routines/{{NAME}}.conf 2. run setup-claude-routines 3. in this job file set the boot-check row to the check name | the verdict counts a PR ready only when that check is green`
to founder-actions.md.

## Step 1 — Pick (max 5 tasks, leftovers included; 3 builders at once)
- One call each: `gh issue list -R {{REPO}} --state open --label agent:ready --json number,title,updatedAt,labels`,
  the same with `--label agent:working`, `--label agent:review` and `--label agent:blocked`;
  `gh pr list -R {{REPO}} --state open --json number,headRefName,isDraft,mergeable,updatedAt`;
  `git ls-remote --heads https://github.com/{{REPO}} 'task/issue-*'`.
- Leftovers first (DoD §5): every open `task/issue-*` PR (draft, red CI, CONFLICTING, or `agent:blocked` after a review FAIL) → a
  Sonnet fixer clears it on its branch, then Step 3. A `task/issue-*` branch with no PR → a builder resumes it. `agent:working`
  with no branch or PR → back to `agent:ready`. Skip only PRs updated in the last 12 h or waiting on an open founder-actions item.
- Then `agent:ready`, unblock-others first, then oldest. Skip issues also labelled `agent:needs-brief`. An issue with an open PR
  or a remote `task/issue-<N>-*` branch is a leftover: resume it, never rebuild it. On a usage-limit warning, start nothing new.
- Hard issues get built, not skipped. High-risk (auth, money, secrets, migrations, deploy/CI, webhooks): behaviour behind a
  default-off flag tested both ways, and `security-review` in Step 3.
- Vague issue: the builder writes problem + sourced criteria as an issue comment, then builds; what DoD §1 makes a founder
  question → `agent:needs-brief` plus a founder-actions line.
- Label each picked issue `agent:working`, remove `agent:ready`. No start message.
- Claim each pick before its builder starts (the guard refuses a builder prompt without a claim): new
  `claim-branch {{REPO}} task/issue-<N>-<slug> {{BASE}}`, leftover branch `claim-branch --resume {{REPO}} <branch>`.
  Exit 0 prints the claim sha for the Step 2 prompt. Exit 3: another session holds it; drop it, label back to `agent:ready`.

## Step 2 — Build (one Sonnet builder per issue, no `isolation`)
Agent prompt, filled in (do not paste the brief):
> Repo {{REPO}}, task {{REPO}}#<N>, <new | resume>, branch <claimed branch> claim <sha>, local gate: <the commands you found>.
> Read the "## Builder brief" section of /home/claude-agent/agent-jobs/{{NAME}}.md and follow it.

## Step 3 — Review each PR (you; start as each builder finishes)
1. Remote head unchanged since the agent finished (`gh pr view <n> -R {{REPO}} --json headRefOid`); keep the SHA.
2. `gh pr diff <n> -R {{REPO}}`: every criterion met with evidence, no scope creep, tests assert behaviour, patterns match the module.
3. Prove the new test fails without the fix: `git switch --detach origin/<branch>`, `git checkout origin/{{BASE}} -- <fixed non-test files>`,
   run the test, confirm red, `git checkout HEAD -- <files>`, confirm green, one call each.
4. CI via the background watch in the DoD file; all checks green.
5. Findings → fix on the PR branch yourself or via a Sonnet agent with exact `file:line`. Max 2 rounds; then leave it open,
   swap the label to `agent:blocked`, comment the findings. A fix of yours beyond one line → a fresh Sonnet verifier reviews the final diff (DoD §3.3).
6. PASS when DoD 1–3, 5, 6 hold: comment the review (verdict + evidence), swap `agent:review` → `agent:approved`, merge with the
   DoD rule 6 command (base `{{BASE}}`). Before merging, re-read `headRefOid` and post the verdict as a commit status:
   `gh api repos/{{REPO}}/statuses/<sha> -f state=success -f context=routine/review -f description="reviewed PASS"`; the founder's verdict report
   counts a PR as ready only with it, a body with "What changed", "How verified" and "Not verified" sections, and green checks.
7. After the last merge: the latest CI run on `{{BASE}}` (`gh run list -R {{REPO}} -b {{BASE}} -L 3 --json databaseId,name,status,conclusion,headSha`)
   is green; red because of your merge → revert PR into `{{BASE}}`, merge, swap the issue to `agent:blocked`.

## Step 4 — Report
- `save_bug` per root-caused bug (project "{{DIR}}").
- The one Telegram message at the end; wording rules in definition-of-done.md (plain words, no ids, PRs as links). Line 1: `Needs you: <n> — You need to: <action + link>` or `Needs you: 0 — Nothing needed from you.`; then at most 3 lines on what changed for users (merged, stuck and why), then `{{BASE}} CI: green|red <run url>`. Send with `claude-notify "<report>" || true`, line breaks as `\n`, then print it.

## Builder brief (builders only)
Meet /home/claude-agent/agent-jobs/definition-of-done.md, its "Shell rules" first. Read the repo's CLAUDE.md / AGENTS.md
(ignore its branch section).
1. `git -C /home/claude-agent/work/{{DIR}} fetch -q origin`, then your own worktree (DoD rule 1): `git -C /home/claude-agent/work/{{DIR}} worktree add -B <branch> /home/claude-agent/work/wt/{{NAME}}-<N> origin/<branch>`
   (the claimed branch in your prompt); resume: also `git log --oneline origin/{{BASE}}..HEAD`, finish the rest. cd there, `npm ci --no-audit --no-fund --engine-strict=false`.
   An issue that needs a new package: report the package name.
2. `gh issue view <N> -R {{REPO}} --comments`. Find code with the Grep tool; read only needed ranges; check that no open PR covers it.
   Unclear cause → /home/claude-agent/.claude/skills/systematic-debugging/SKILL.md.
3. Regression test first; run it on the base and keep the failing output. Commit and `git push -u origin HEAD` (DoD §5).
   Then the smallest fix in the module's existing patterns.
4. Local gate, one command at a time, touched files only; CI runs the full suite.
5. Write the body with the Write tool to `/tmp/{{NAME}}-pr-<N>.md`, then
   `gh pr create -R {{REPO}} --base {{BASE}} --title "<subject>" --body-file /tmp/{{NAME}}-pr-<N>.md`.
   Body sections, these exact headings: `Fixes #<N>` · `Checked first:` · `## What changed` (root cause `file:line` + the fix) ·
   `## How verified` (commands, red then green output, counts) · `## Not verified` (with why; "nothing" if empty) · `## Risk`.
6. `gh issue edit <N> -R {{REPO}} --add-label agent:review --remove-label agent:working`.
7. Reply in under 15 lines: PR URL, root cause, test commands + counts, anything not verified.
