"""What every rule shares: the refusal, line counting, and which repo / branch a shell command acts on."""
import os, re, subprocess, sys

from .policy import slug

MAX_LINES = 300          # biggest file / range a session may print in one go
MAX_TOOL_CHARS = 5000    # biggest connector (mcp__) payload the model may write
READ_HELP = ("Find the spot with `grep -n PATTERN FILE`, then print only that range with "
             f"`sed -n 'A,Bp' FILE` (at most {MAX_LINES} lines), or Read with offset/limit. "
             "Whole-repo questions: `graphify update <repo>` then `graphify query \"<q>\" --budget 1500`, "
             "or hand the reading to a subagent and keep only its findings.")


def deny(msg):
    sys.stderr.write("BLOCKED by token-guard: " + msg + "\n")
    sys.exit(2)


def count_lines(path, cwd):
    p = path if os.path.isabs(path) else os.path.join(cwd, os.path.expanduser(path))
    try:
        if not os.path.isfile(p):
            return None
        if os.path.getsize(p) > 2_000_000:
            return 10**9
        with open(p, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def repo_flag(cmd):
    """The literal owner/repo of `-R`/`--repo`, reading `$R` from an `R=owner/repo` earlier in cmd; None if absent
    or not a literal (`R=$(git remote get-url origin)`: the caller then reads the cd dir)."""
    m = re.search(r"(?<![\w-])(?:-R|--repo)[ =]['\"]?(\$\{?(\w+)\}?|[\w.-]+/[\w.-]+)", cmd)
    if not m:
        return None
    if not m.group(2):
        return m.group(1)
    v = re.findall(r"(?:^|[\s;&|(])" + m.group(2) + r"=['\"]?([\w.-]+/[\w.-]+)['\"]?(?=[\s;&|)]|$)", cmd)
    return v[-1] if v else None


def remote_of(cmd, cwd):
    """The repo cmd acts on: its `-R` value, else the origin URL of the dir it cds into (or cwd); "" if unknown."""
    url = repo_flag(cmd)
    if not url:
        c = re.search(r"\bcd\s+(['\"]?)([^\s;&|'\"]+)\1", cmd)
        d = os.path.join(cwd, os.path.expanduser(c.group(2))) if c else cwd
        try:
            url = subprocess.run(["git", "-C", d, "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=2).stdout
        except Exception:
            url = ""  # allow-failopen: not a git dir means not a guarded repo
    return url.strip()


def repo_of(cmd, cwd):
    return slug(remote_of(cmd, cwd))


def head_of(cmd, cwd):
    """The PR's head branch: `--head`/`-H`, else the branch checked out in the dir cmd cds into (or cwd); "" if unknown."""
    m = re.search(r"(?<![\w-])(?:--head|-H)[ =]['\"]?([^\s'\"]+)", cmd)
    if m:
        return m.group(1).split(":")[-1]
    c = re.search(r"\bcd\s+(['\"]?)([^\s;&|'\"]+)\1", cmd)
    d = os.path.join(cwd, os.path.expanduser(c.group(2))) if c else cwd
    try:
        return subprocess.run(["git", "-C", d, "symbolic-ref", "--short", "-q", "HEAD"],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return ""  # allow-failopen: an unknown head is refused below (fail closed)
