# claude-agent-setup

Hooks, routines and helper scripts for running Claude Code (and other coding agents) on a laptop plus one VPS.

## What is here

- `token-guard.py`: PreToolUse/SessionStart hook. On the VPS it runs in full mode; on a laptop it runs with `--pr-only`, where it checks PR bases, PR bodies and merge targets. Before every non-delete `git push`, in any repo, it runs `tracker check` in the push dir and refuses the push when the session was taken over (exit 5) or the check fails or times out (fail closed); it skips this when `tracker` is not installed. It reads per-repo rules from `routine-policy.defaults.json`, overlaid by `routine-policy.json`. The rules live in `token_guard/` (one module per concern); `token-guard.py` only routes each event to them, so install copies both: `token-guard.py` and the `token_guard/` directory go side by side, and the hook refuses every call (exit 2) if the directory is missing. Tests: `claude-remote-hooks/test-token-guard.sh`, and `claude-remote-hooks/test-token-guard-golden.sh`, which replays every call that suite makes plus a few direct ones and requires the exit code, stdout and stderr to match `testdata/token-guard.golden` byte for byte.
- `wip-save.py` (in `claude-remote-hooks/`): Stop, StopFailure and SessionEnd hook, plus a 10-minute sweep (`claude-wip-sweep.timer` on the VPS, `com.turicks.wip-sweep` launchd agent on the laptop, installed by the overlay's `sync-agent-rules`). It pushes each worktree's unpushed commits and uncommitted files to `refs/wip/<branch>` on GitHub, so work never lives only on one disk. See "Unpushed work and duplicates" below.
- `guard-bash.sh` (in `claude-remote-hooks/`): laptop PreToolUse(Bash) hook that blocks a recursive force-delete and a direct `git push` to `main`/`master`. Needs `jq`.
- `routine-policy`: builds those rules from the `GUARD_REPOS` lines in your routine confs. `--install` writes them to the laptop and the VPS.
- `setup-claude-routines`, `routine-new`, `routine-verdict`, `routines/`: daily VPS routines driven by systemd timers, with a verdict message afterwards.
- `session-brief`: SessionStart hook that prints repo state, open PRs and, optionally, recent brain memories.
- `claim-branch`, `worktree-holds` (in `routines/bin/`): claim a task branch on GitHub before working on it, and tell whether a worktree still holds unpushed work.
- `tracker` (in `routines/bin/`): one CSV task tracker per project, kept in its own GitHub repo, as the single source of truth for who works on which task. See "Tracker" below.
- `agy`, `agy-guard`, `agent-dispatch`: dispatch work to Google Antigravity, and refuse to start when another agent is writing.
- `pr-brain`, `branch-sweep`, `claude-notify`: PR review sweeper, stale-branch cleanup, Telegram notifications.

## Laptop install: hooks and skills

Needs `git`, `python3`, `jq` and `gh`. No VPS needed. Run from the clone:

```bash
git clone https://github.com/pushkarverma3698/claude-agent-setup && cd claude-agent-setup
mkdir -p ~/.claude/hooks ~/.claude/skills
install -m 755 claude-remote-hooks/token-guard.py claude-remote-hooks/guard-bash.sh claude-remote-hooks/verdict-stop.py claude-remote-hooks/wip-save.py ~/.claude/hooks/
[ ! -d ~/.claude/hooks/token_guard ] || rm -r ~/.claude/hooks/token_guard
cp -R claude-remote-hooks/token_guard ~/.claude/hooks/   # must sit next to token-guard.py
cp -R skills/work-the-tracker ~/.claude/skills/
mkdir -p ~/.local/bin && for f in tracker claim-branch worktree-holds; do ln -sf "$PWD/routines/bin/$f" ~/.local/bin/; done
```

Register the hooks in `~/.claude/settings.json`. With no settings file yet, this writes one (the clone path is filled in for `session-brief`); with an existing file, merge its `hooks` key by hand:

```bash
[ -e ~/.claude/settings.json ] && echo "settings.json exists: merge the hooks key by hand" || cat > ~/.claude/settings.json <<JSON
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [
        {"type": "command", "command": "\$HOME/.claude/hooks/guard-bash.sh", "timeout": 5},
        {"type": "command", "command": "\$HOME/.claude/hooks/token-guard.py --pr-only", "timeout": 30}]},
      {"matcher": "Agent|Task", "hooks": [
        {"type": "command", "command": "\$HOME/.claude/hooks/token-guard.py --pr-only", "timeout": 30}]}
    ],
    "SessionStart": [
      {"matcher": "startup|clear", "hooks": [
        {"type": "command", "command": "$PWD/session-brief", "timeout": 8}]}
    ],
    "Stop": [
      {"hooks": [
        {"type": "command", "command": "\$HOME/.claude/hooks/verdict-stop.py", "timeout": 10},
        {"type": "command", "command": "\$HOME/.claude/hooks/wip-save.py", "async": true, "timeout": 120}]}
    ],
    "StopFailure": [
      {"hooks": [{"type": "command", "command": "\$HOME/.claude/hooks/wip-save.py", "async": true, "timeout": 120}]}
    ],
    "SessionEnd": [
      {"hooks": [{"type": "command", "command": "\$HOME/.claude/hooks/wip-save.py", "timeout": 60}]}
    ]
  }
}
JSON
```

Check it works (the second line must print BLOCKED and exit 2):

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | ~/.claude/hooks/token-guard.py --pr-only; echo "exit=$?"
echo '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}' | ~/.claude/hooks/guard-bash.sh; echo "exit=$?"
```

Per-repo PR rules come from `~/.claude/hooks/routine-policy.json`. Without it the guard has no repo rules and lets PR calls through. To write it on the laptop only, from your routine confs (format in `examples/claude-routines/`):

```bash
ROUTINE_CONF_DIR=<your confs> ./routine-policy > ~/.claude/hooks/routine-policy.json
cp ~/.claude/hooks/routine-policy.json ~/.claude/hooks/routine-policy.defaults.json
```

`tracker`, `claim-branch` and the `work-the-tracker` skill need `routines/bin` on your `PATH` and a tracker conf (see "Tracker").

## VPS setup: routines

1. Copy `examples/agent-setup.env` to `~/.config/claude-agent-setup/env` and fill it in. `AGENT_RULES_HOST` is required.
2. Keep your private routine confs in an overlay directory (`AGENT_OVERLAY_DIR`). `examples/claude-routines/` shows the layout.
3. Run `./routine-policy --install` (writes the guard policy to the laptop and over ssh to `AGENT_RULES_HOST`; it fails without a reachable host), then `./setup-claude-routines`.

## Tests

```bash
./claude-remote-hooks/test-token-guard.sh
./claude-remote-hooks/test-token-guard-golden.sh
./claude-remote-hooks/test-verdict-stop.sh
./claude-remote-hooks/test-wip-save.sh
python3 routines/ready/test_claim_branch.py
python3 routines/ready/test_tracker.py
python3 routines/ready/test_routine_fixes.py   # some cases need GNU tools (run on Linux)
```

## Onboarding a repo: routine-new

```bash
./routine-new <owner/repo> <integration-branch> "HH:MM [HH:MM…]" [--tz <zone>] [--boot-check <ci-check>] --dry-run
./routine-new <owner/repo> <integration-branch> "HH:MM [HH:MM…]" [--tz <zone>] [--boot-check <ci-check>]
```

`--dry-run` runs every read-only check, prints each write it would make (GitHub, VPS, conf folder) and the conf it would write, and exits 1 when a real run would stop. A real run then, each step skipping what exists: checks write access here and for `claude-agent` on the VPS, creates the integration branch, creates and seeds the private tracker repo, protects the branch, clones on the VPS, writes the conf (routine, tracker and `GUARD_REPOS` keys) and the VPS job file, runs `setup-claude-routines`, and checks that the guard policy on the laptop and on the VPS lists the repo. `--no-deploy` skips the timers but still installs the policy. `--paused` writes `PAUSED=1` in the conf, so the timer is installed but disabled until you delete that line and re-run `setup-claude-routines` (use it to write the job contract before the first run).

It stops, instead of carrying on unguarded, when GitHub refuses branch protection (a private repo on a free plan; `--allow-unprotected` goes on and lists it as a step left) or when the policy does not list the repo. `routine-policy` itself refuses a conf with `CODE_REPO` and no `GUARD_REPOS` entry for that repo in any conf.

It ends with the steps left for you, normally two: write the job contract (the local gate, services and PR body in the VPS job file) and replace the example row `T-1` in the tracker.

Task ids must be `T-<number>`: the tracker's default `ID_RE`, the `feat/t<N>-` branch regex `routine-new` writes, and the guard's PR-body task line (`task=T-NNN` in `GUARD_REPOS`; `token-guard.py` matches `Closes T-<N>` / `Part of T-<N>` only) all assume it. A repo with another id scheme is not covered by this setup.

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
MAX_BUILDERS=3                 # new claims stop while this many rows are held; 0 = no cap
NOTIFY_CMD=claude-notify       # gets the message on stdin
ISSUE_IMPORT=1                 # optional: `tracker sync` mirrors GitHub issues (rows `#N`); see ISSUE_REPO, ISSUE_STATUS below
```

The tool adds eight columns (Holder, Environment, Run, Branch, PR, Blocker, Next step, Updated); `tracker migrate` appends them in one commit, and write commands refuse to run until they exist.

With `ISSUE_IMPORT=1`, `sync` also imports the open issues of `ISSUE_REPO` (default `CODE_REPO`) in the same write: a new issue becomes a row `#N` with a status from `ISSUE_STATUS` (default `agent:blocked=Blocked,agent:ready=Not started,agent:working=Not started`, first matching label in map order wins; an issue with no mapped label is not imported, add `*=<status>` to import it); a closed issue makes its row Done; labels only change rows with no holder in Not started or Blocked; the dependency column (`DEPS_COL`) and `BLOCK_COL` (default `Blocked by`, needs that column) hold only the still-open blockers from the issue's "Blocked by" line (same-repo as `#N`, other repos as `owner/repo#N`). `next` skips a row whose `BLOCK_COL` is non-empty and says why. Blockers GitHub does not list (pull requests, over 1000 issues) stay until removed from the issue.

Commands: `sync`, `next`, `add "title" [--notes TXT] [--dry-run]`, `claim T-NNN`, `beat T-NNN`, `block T-NNN "text" --needs founder|stakeholder`, `done T-NNN --pr N`, `release T-NNN`, `check`, `migrate`. `add` queues new work: a CSV row with the next free id (same prefix and zero-padding as the file), status Not started, the `ROW_FILTER` value, and the `ORDER_COLS` values of the row that sorts last, so it is picked after the planned work; `--notes` goes in `NOTES_COL` (default: the first of Notes, Description, Details, Acceptance criteria that the CSV has). With `ISSUE_IMPORT=1` it opens an issue in `ISSUE_REPO` instead, labelled with the first `ISSUE_STATUS` label that maps to Not started (`agent:ready` if listed), and leaves the CSV to the next `sync`. `--dry-run` prints the row or issue and writes nothing. A row is held while it is active, has a holder and was updated less than `LEASE_HOURS` ago. An active row past that, or with no holder, is a leftover and must be resumed before new work. Exit codes: 0 ok, 2 usage/config/API error, 3 held by someone else, 4 leftover first, 5 not the holder, 6 at builder cap. `claim` of a new task exits 6 while `MAX_BUILDERS` rows are held (default 3); leftovers are never capped, and `next` then prints `NEXT NONE` plus an `AT CAP` line. `check` is for push hooks: it exits 5 when another holder took the row over.

### Unpushed work and duplicates

GitHub is the only place a claim or a piece of work counts. Five pieces keep it that way, on the laptop, on the phone (Remote Control on the VPS) and in routines alike:

1. **One branch per task.** `claim-branch` (and `tracker claim`, which calls it) refuses while another branch carries the same task id, a claim is under `CLAIM_HOURS` old, or a real push is under `LIVE_HOURS` old. `tracker next` turns a branch made by `claim-branch` alone into a Claimed row, so the CSV still shows it.
2. **No push without a claim.** `token-guard.py` refuses a push of a task branch (the guarded repo's `branch` regex) that is not on GitHub yet, plain `--force` on one, and any push after `tracker check` says the row was taken over.
3. **Work never stays on one disk.** `wip-save.py` runs in the background after every turn, when a turn ends on an API error, when a session exits, and every 10 minutes from a timer, which catches a session killed with no hook (SIGKILL, OOM, reboot, force quit). It covers a guarded repo's task branches and, in any other repo of an `OWNERS` account (pushkarverma3698, oplifyterminal, OplifyMessage), every branch except the bases and origin's default, so a new repo needs no setup. Each such worktree gets one snapshot commit at `refs/wip/<branch>`: parent = its HEAD, so unpushed commits go too; tree = HEAD plus tracked and untracked changes (`.gitignore` honoured; `.env*`, `*.pem`, `*.key` and files over 10 MB left out). The ref is outside `refs/heads`: no CI run, no branch-list entry, no pre-push suite. A clean worktree whose HEAD is on origin deletes its snapshot.
4. **A live snapshot holds the task; an old one is resumed.** `claim-branch --resume` refuses while the snapshot is under `LIVE_HOURS` old. Past that, `tracker claim` of the leftover runs `wip-save.py --restore` in the new worktree: the dead session's commits are merged and its uncommitted files come back as changes. On the same machine the dead run's own worktree is adopted instead, uncommitted files and all.
5. **Nothing unsaved is deleted.** `worktree-holds` treats a worktree as clean only when every commit is on GitHub (an empty-repo fetch of the sha, PR heads included); `routine-worktree` moves an unsaved worktree aside instead of removing it, and `claude-clutter-clean` removes kept copies only once `worktree-holds --clean` passes.

## License

MIT
