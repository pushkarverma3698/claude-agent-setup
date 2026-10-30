# claude-agent-setup

Hooks, routines and helper scripts for running Claude Code (and other coding agents) on a laptop plus one VPS.

## What is here

- `token-guard.py`: PreToolUse/SessionStart hook. On the VPS it runs in full mode; on a laptop it runs with `--pr-only`, where it checks PR bases, PR bodies and merge targets. It reads per-repo rules from `routine-policy.defaults.json`, overlaid by `routine-policy.json`. Tests: `./test-token-guard.sh`.
- `routine-policy`: builds those rules from the `GUARD_REPOS` lines in your routine confs. `--install` writes them to the laptop and the VPS.
- `setup-claude-routines`, `routine-new`, `routine-verdict`, `routines/`: daily VPS routines driven by systemd timers, with a verdict message afterwards.
- `session-brief`: SessionStart hook that prints repo state, open PRs and, optionally, recent brain memories.
- `claim-branch`, `worktree-holds` (in `routines/bin/`): claim a task branch on GitHub before working on it, and tell whether a worktree still holds unpushed work.
- `agy`, `agy-guard`, `agent-dispatch`: dispatch work to Google Antigravity, and refuse to start when another agent is writing.
- `pr-brain`, `branch-sweep`, `claude-notify`: PR review sweeper, stale-branch cleanup, Telegram notifications.

## Setup

1. Copy `examples/agent-setup.env` to `~/.config/claude-agent-setup/env` and fill it in. `AGENT_RULES_HOST` is required.
2. Keep your private routine confs in an overlay directory (`AGENT_OVERLAY_DIR`). `examples/claude-routines/` shows the layout.
3. Run `./routine-policy --install`, then `./setup-claude-routines`.

## Tests

```bash
./test-token-guard.sh
python3 routines/bin/test_claim_branch.py
python3 routines/ready/test_routine_fixes.py   # some cases need GNU tools (run on Linux)
```

## License

MIT
