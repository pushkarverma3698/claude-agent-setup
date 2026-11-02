#!/usr/bin/env python3
"""PreToolUse + SessionStart hook for claude-agent's sessions on the VPS (and, with --pr-only, the laptop).

Installed to /opt/agent-rules/hooks on the VPS and ~/.claude/hooks on the laptop, together with the token_guard/
directory beside it (the rules, one module per concern; this file only routes each event to them). Every line a tool
prints is resent on every later call, and every byte the model writes into a tool call is output tokens (about 9K =
1% of a 5-hour window). Two incidents this enforces:
  2026-10-14  whole-repo `cat` loops, compacted 3x, then re-read the same files.
  2026-10-15  10 docs re-typed into Drive create_file: 159K output tokens to send one email.
Heavy checks (lint/test/build/install) are no longer queued: 2026-10-29 the single lock
serialized five builders for hours. Memory is bounded by claude.slice (MemoryMax 4.5G).

Deny = exit 2, the reason on stderr goes back to Claude. `--pr-only` (the laptop hook) runs only the PR guards.
`--check-body <repo> [<pr>]` (PR body on stdin) is the VPS merge wrapper's body check.
"""
import json, os, sys

HOOK_FILE = os.path.abspath(__file__)
HOOK_DIR = os.path.dirname(HOOK_FILE)
sys.path.insert(0, HOOK_DIR)
sys.dont_write_bytecode = True  # no __pycache__ in the hook dir; the single-file hook was compiled every call too
try:
    import token_guard
    token_guard.HOOK_DIR = HOOK_DIR  # routine-policy.json is read next to this file, before any rule module loads
    from token_guard.dispatch import check_dispatch
    from token_guard.merge import check_api_merge, check_local_ci_post, check_merge_now, guard_merge
    from token_guard.policy import body_missing, task_format
    from token_guard.pr import check_pr_create
    from token_guard.push import check_task_push
    from token_guard.reads import check_cat, check_connector, check_ranges, check_read
except ImportError as e:  # a copy of this file without token_guard/: refuse loudly rather than guard nothing
    sys.stderr.write(f"BLOCKED by token-guard: its rules are missing ({e}). Copy the token_guard/ directory from "
                     f"claude-agent-setup/claude-remote-hooks next to {HOOK_FILE}.\n")
    sys.exit(2)

# Bash rules in the order they run; the first refusal wins, so the order is part of the behaviour.
LAPTOP_BASH = (check_pr_create, check_api_merge, check_merge_now, check_task_push)
VPS_BASH = (check_cat, check_ranges, check_pr_create, check_task_push, check_api_merge,
            lambda cmd, cwd: check_local_ci_post(cmd))
DISPATCH_TOOLS = ("Agent", "Task")
COMPACT_CONTEXT = ("This session just auto-compacted. Keep going: check the branch and next step with git status, "
                   "do not re-read files you already summarised, and finish the task end to end.")


def check_body_cli():
    """The VPS merge wrapper: --check-body <repo> <pr>, PR body on stdin. Exit 1 with the refusal on stdout."""
    missing = body_missing(sys.argv[2], sys.stdin.read())
    n = sys.argv[3] if len(sys.argv) > 3 else "(current branch)"
    if missing:
        print(f"token-guard: merge refused: PR {n} body lacks: {'; '.join(missing)}. "
              "Fix it with gh pr edit --body-file <file> first." + task_format(sys.argv[2]))
        sys.exit(1)


def laptop(data):
    """--pr-only: the same PR, push and dispatch rules, none of the VPS token limits."""
    if data.get("tool_name") == "Bash":
        cmd, cwd = (data.get("tool_input") or {}).get("command", ""), data.get("cwd") or os.getcwd()
        for rule in LAPTOP_BASH:
            rule(cmd, cwd)
    if data.get("tool_name") in DISPATCH_TOOLS:
        check_dispatch((data.get("tool_input") or {}).get("prompt", ""))


def session_start(data):
    if data.get("source") == "compact":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": COMPACT_CONTEXT}}))


def vps(data):
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()
    if tool in DISPATCH_TOOLS:
        check_dispatch(ti.get("prompt", ""))
    elif tool.startswith("mcp__"):
        check_connector(tool, ti)
    elif tool == "Read":
        check_read(ti, cwd)
    elif tool == "Bash":
        cmd = ti.get("command", "")
        for rule in VPS_BASH:
            rule(cmd, cwd)
        guarded = guard_merge(cmd, cwd, HOOK_FILE)
        if guarded:
            new = dict(ti); new["command"] = guarded
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                "permissionDecision": "allow", "updatedInput": new,
                "permissionDecisionReason": "token-guard: merge runs only if the PR targets its repo's merge base"}}))


def main():
    if sys.argv[1:2] == ["--check-body"]:
        check_body_cli()
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # allow-failopen: a malformed event must never block the session
    event = data.get("hook_event_name")
    if "--pr-only" in sys.argv:
        laptop(data)
    elif event == "SessionStart":
        session_start(data)
    else:
        vps(data)


if __name__ == "__main__":
    main()
