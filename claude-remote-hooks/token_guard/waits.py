"""No waiting inside a session (2026-11-04 audit: sessions sat in foreground sleeps and CI watches, then stopped to
report). A background command wakes the session when it exits, so nothing needs the foreground. Runs on both machines.
A wait hidden in `bash -c '...'` or `ssh host '...'` is not seen: quoted text is skipped so a commit message that
mentions `gh run watch` is not refused."""
import re

from .common import deny

MAX_SLEEP = 60   # seconds of foreground sleep one command may hold
WAKE_HELP = ("Run it with the Bash tool's run_in_background: true, which wakes this session when it exits, e.g. "
             "`gh pr checks <n> --watch --fail-fast > /tmp/checks-<n>.log 2>&1`. Meanwhile do other work or end the turn.")
UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def code_only(cmd):
    """cmd without heredoc bodies and quoted strings."""
    cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n\s*\1\s*(?:\n|$)", "\n", cmd, flags=re.S)
    return re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "''", cmd)


def check_wait(ti):
    if ti.get("run_in_background"):
        return
    code = code_only(ti.get("command", ""))
    if re.search(r"(?<!&)&\s*$", code):     # the whole command is detached with a trailing &
        return
    if re.search(r"\bgh\b[^|;&\n]*\bpr\s+checks\b[^|;&\n]*--watch\b|\bgh\b[^|;&\n]*\brun\s+watch\b", code):
        deny("watching CI in the foreground holds the session. " + WAKE_HELP)
    sleeps = re.findall(r"(?<![\w-])sleep\s+(\d+(?:\.\d+)?)([smhd]?)(?![\w.])", code)
    if not sleeps:
        return
    if re.search(r"\b(?:while|until|for)\b", code) and re.search(r"\bgh\s", code):
        deny("a sleep loop around gh polls CI in the foreground. " + WAKE_HELP)
    total = sum(float(n) * UNIT[u] for n, u in sleeps)
    if total >= MAX_SLEEP:
        deny(f"a foreground sleep of {total:.0f}s holds the session. " + WAKE_HELP)
