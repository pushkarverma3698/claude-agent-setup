# claude-agent-setup

Hooks, routines and helper scripts for running Claude Code (and other coding agents) on a laptop plus one VPS.

## What is here

- `token-guard.py`: PreToolUse/SessionStart hook. On the VPS it runs in full mode; on a laptop it runs with `--pr-only`, where it checks PR bases, PR bodies and merge targets. It reads per-repo rules from `routine-policy.defaults.json`, overlaid by `routine-policy.json`. Tests: `./test-token-guard.sh`.
- `routine-policy`: builds those rules from the `GUARD_REPOS` lines in your routine confs. `--install` writes them to the laptop and the VPS.
- `setup-claude-routines`, `routine-new`, `routine-verdict`, `routines/`: daily VPS routines driven by systemd timers, with a verdict message afterwards.
- `session-brief`: SessionStart hook that prints repo state, open PRs and, optionally, recent brain memories.
- `claim-branch`, `worktree-holds` (in `routines/bin/`): claim a task branch on GitHub before working on it, and tell whether a worktree still holds unpushed work.
- `tracker` (in `routines/bin/`): one CSV task tracker per project, kept in its own GitHub repo, as the single source of truth for who works on which task. See "Tracker" below.
- `agy`, `agy-guard`, `agent-dispatch`: dispatch work to Google Antigravity, and refuse to start when another agent is writing.
- `pr-brain`, `branch-sweep`, `claude-notify`: PR review sweeper, stale-branch cleanup, Telegram notifications.

## Setup

1. Copy `examples/agent-setup.env` to `~/.config/claude-agent-setup/env` and fill it in. `AGENT_RULES_HOST` is required.
2. Keep your private routine confs in an overlay directory (`AGENT_OVERLAY_DIR`). `examples/claude-routines/` shows the layout.
3. Run `./routine-policy --install`, then `./setup-claude-routines`.

## Tests

```bash
./test-token-guard.sh
python3 routines/ready/test_claim_branch.py
python3 routines/ready/test_tracker.py
python3 routines/ready/test_routine_fixes.py   # some cases need GNU tools (run on Linux)
```

## Tracker

`routines/bin/tracker` keeps task state in a CSV inside a GitHub repo. Only this tool writes the file, through `gh api` (the contents API), and every write is a compare-and-swap on the file sha: on a conflict the same change is re-applied to the fresh file and re-validated, so a claim on a row someone took meanwhile fails with exit 3 instead of overwriting.

Config is `<project>.conf`, a bash fragment, found in `$TRACKER_CONF_DIR`, `/etc/claude-routines` or `$AGENT_OVERLAY_DIR/claude-routines` (or `--conf PATH`). Select it with `--project NAME` or `$TRACKER_PROJECT`; with neither, the conf whose `CODE_REPO` matches the current clone's origin is used.

```bash
TRACKER_REPO=acme/tracker      # repo holding the CSV
TRACKER_PATH=backlog.csv
CODE_REPO=acme/app             # repo the work branches live in
WORK_BASE=main                 # branch new work is cut from
BRANCH_FMT='feat/t{num}-{slug}'
ID_RE='T-\d+'
LEASE_HOURS=6
NOTIFY_CMD=claude-notify       # gets the message on stdin
```

The tool adds eight columns (Holder, Environment, Run, Branch, PR, Blocker, Next step, Updated); `tracker migrate` appends them in one commit, and write commands refuse to run until they exist.

Commands: `sync`, `next`, `claim T-NNN`, `beat T-NNN`, `block T-NNN "text" --needs founder|stakeholder`, `done T-NNN --pr N`, `release T-NNN`, `check`, `migrate`. A row is held while it is active, has a holder and was updated less than `LEASE_HOURS` ago. An active row past that, or with no holder, is a leftover and must be resumed before new work. Exit codes: 0 ok, 2 usage/config/API error, 3 held by someone else, 4 leftover first, 5 not the holder. `check` is for push hooks: it exits 5 when another holder took the row over.

## License

MIT
