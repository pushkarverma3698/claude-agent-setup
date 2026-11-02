"""git push: the tracker owner check (any repo) and claim-before-push (task branches of claimed repos)."""
import os, re, shlex, shutil, subprocess

from .common import deny
from .policy import CLAIMED, claim_cmd, slug


def pushes(cmd, cwd):
    """(dir, args) for each `git [-C dir] push args` in cmd."""
    out = []
    for seg in re.split(r"\|\||&&|;|\n|\|", cmd):
        m = re.search(r"\bgit\s+(?:-C\s+(['\"]?)([^\s'\"]+)\1\s+)?push\b(.*)", seg)
        if not m:
            continue
        c = re.search(r"\bcd\s+(['\"]?)([^\s;&|'\"]+)\1", cmd)
        d = m.group(2) or (c.group(2) if c else ".")
        try:
            args = shlex.split(m.group(3))
        except ValueError:
            args = m.group(3).split()
        out.append((os.path.join(cwd, os.path.expanduser(d)), args))
    return out


# Owner check before push (2026-10-31: a session woken after a takeover pushed over the new owner's branch).
# `tracker check` exits 0 when the worktree has no tracker-claim file, so it is safe in every repo; 5 = taken over,
# 2 = config or API error. Both refuse: a push nobody can vouch for is the failure this stops.
def check_owner(d):
    if not shutil.which("tracker") or not os.path.isdir(d):
        return  # allow-failopen: a laptop without tracker, or a dir git would reject anyway, has nothing to check
    try:
        r = subprocess.run(["tracker", "check"], cwd=d, capture_output=True, text=True,
                           timeout=float(os.environ.get("TOKEN_GUARD_TRACKER_TIMEOUT", "20")))
    except subprocess.TimeoutExpired:
        deny("`tracker check` timed out, so this session's claim cannot be confirmed: not pushing. Retry the push; "
             "if it keeps timing out, stop and report it.")
    except OSError:
        return  # allow-failopen: tracker vanished between which() and run(): same as not installed
    if r.returncode:
        deny((r.stderr.strip() or f"tracker check exited {r.returncode}") +
             ("" if r.returncode == 5 else " (the claim cannot be confirmed: do not push)"))


# Claim before push (2026-10-29: feat/t247-disputes and feat/t247-ownership-disputes were both built; the second
# lead never saw the first's unpushed worktree). A task branch (POLICY `branch`) must already exist on GitHub, made
# by claim-branch, which refuses when another branch holds the same task id. Plain --force could wipe a claim.
def check_task_push(cmd, cwd):
    for d, args in pushes(cmd, cwd):
        if "--delete" in args or "-d" in args:
            continue
        check_owner(d)
        git = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, timeout=10).stdout.strip()
        try:
            url = git("remote", "get-url", "origin")
        except Exception:
            url = ""  # allow-failopen: not a git dir means not a guarded repo
        repo = slug(url)
        if repo not in CLAIMED:
            continue
        pos = [a for a in args if not a.startswith("-")]
        refs = [r.split(":")[-1].removeprefix("+") for r in pos[1:]] or ["HEAD"]
        for ref in refs:
            if ref == "HEAD":
                try:
                    ref = git("branch", "--show-current")
                except Exception:
                    ref = ""  # allow-failopen: a detached head is not a task branch
            ref = ref.removeprefix("refs/heads/")
            if not re.match(CLAIMED[repo], ref):
                continue
            if "--force" in args or "-f" in args or any(p.startswith("+") for p in pos[1:]):
                deny(f"plain --force on {ref} can wipe another session's claim. Use `--force-with-lease`.")
            try:
                held = git("ls-remote", "--heads", "origin", "refs/heads/" + ref)
            except Exception:
                held = ""  # allow-failopen: no answer is handled below as a refusal (fail closed)
            if not any(ln.endswith("\trefs/heads/" + ref) for ln in held.splitlines()):
                deny(f"{ref} is not claimed on GitHub. Run `{claim_cmd(repo, ref)}` first; exit 3 means another "
                     "session holds the task: stop. Then `git fetch`, rebase your commits onto origin/" + ref + " and push.")
