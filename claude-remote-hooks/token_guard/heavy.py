"""Heavy checks take a slot on the VPS (routines/bin/heavy-slot explains why). The command itself is unchanged and runs
in the same shell, so `cd` and exit codes survive. Only a command word counts: `grep vitest` and a commit message
that says `npm test` must not wait behind a test run. Hidden in `bash -c '...'` or `ssh host '...'` it is not seen."""
import re

from .waits import code_only

SLOT = "/usr/local/bin/heavy-slot"
HEAVY = re.compile(
    r"(?:^|[;&|(\n]\s*)"                                   # start of a command
    r"(?:\w+=\S*\s+|(?:timeout|nice|time)(?:\s+-?[\w.]+)*?\s+(?=\S))*"   # env assignments, timeout 600, nice -n 10
    r"(?:"
    r"(?:npm|pnpm|yarn|bun)(?:\s+-\S+(?:\s+[^\s-]\S*)?){0,3}\s+(?:run\s+)?"
    r"(?:verify|gate|predeploy|test|test:\S+|lint|lint:\S+|typecheck|check|build|e2e|ci|install|i)\b"
    r"|npx\s+(?:tsc|eslint|vitest|jest|playwright|next\s+build)\b"
    r"|(?:\./)?node_modules/\.bin/(?:tsc|eslint|vitest|jest)\b"
    r"|(?:tsc|eslint|vitest|jest)(?:\s|$)"
    r"|docker\s+(?:compose\s+)?build\b"
    r")")


def wrap_heavy(cmd):
    """The command with a slot taken first, or None when it is not a heavy check."""
    code = code_only(cmd)
    if "heavy-slot" in code or not HEAVY.search(code):
        return None
    return f"[ -r {SLOT} ] && . {SLOT}\n{cmd}"
