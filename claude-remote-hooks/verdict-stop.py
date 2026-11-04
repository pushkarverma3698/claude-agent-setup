#!/usr/bin/env python3
"""Stop hook: a turn that changed files or shipped code must say whether the task is done.

2026-11-04 audit of phone and Mac sessions: turns ended on a summary or an offer, and the founder had to ask what state
the work was in. The closing message of such a turn carries one line:
  Verdict: YES
  Verdict: NO: <blocker>, checked with <command>

Exit 2 sends the reason back to Claude, which then writes the line. Only once per stop (stop_hook_active), so this can
never loop. Read-only turns and questions pass. Subagents fire SubagentStop, so they are not touched.
Installed to /opt/agent-rules/hooks on the VPS and ~/.claude/hooks on the laptop. Test: test-verdict-stop.sh.
"""
import json, os, re, sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHIP = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:commit|push|merge)\b|\bgh\s+pr\s+(?:create|merge|ready)\b")
VERDICT = re.compile(r"^[\s*_>`-]*Verdict:\s*(?:YES\b|NO:\s*\S.*,\s*checked with\s+\S)", re.M)
TAIL_BYTES = 8_000_000
ASK = ("This turn changed files or shipped code, and its closing message has no verdict line. End with exactly one of: "
       "`Verdict: YES` if the outcome works on the real path, or `Verdict: NO: <blocker>, checked with <command>`. "
       "If there is no real blocker, keep working instead of writing NO. Do not repeat your summary.")


def turn_rows(path):
    """Transcript rows of the current turn, newest first: everything after the last prompt that was not a tool result."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TAIL_BYTES))
        lines = f.read().splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("isSidechain"):
            continue
        content = (row.get("message") or {}).get("content")
        if row.get("type") == "user" and not row.get("isMeta"):
            if not (isinstance(content, list) and any(b.get("type") == "tool_result" for b in content if isinstance(b, dict))):
                return
        if row.get("type") == "assistant" and isinstance(content, list):
            yield content


def worked(blocks):
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            if b.get("name") in EDIT_TOOLS:
                return True
            if b.get("name") == "Bash" and SHIP.search(str((b.get("input") or {}).get("command", ""))):
                return True
    return False


def needs_verdict(data):
    text, did_work = data.get("last_assistant_message") or "", False
    for blocks in turn_rows(data["transcript_path"]):
        did_work = did_work or worked(blocks)
        if not text:
            text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    return did_work and not VERDICT.search(text)


def main():
    try:
        data = json.load(sys.stdin)
        if data.get("stop_hook_active") or not needs_verdict(data):
            return
    except Exception:
        return  # allow-failopen: a hook bug must never trap a session at its end
    sys.stderr.write(ASK + "\n")
    sys.exit(2)


if __name__ == "__main__":
    main()
