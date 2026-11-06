#!/usr/bin/env python3
"""Stop hook: copy every task worktree's unpushed work to GitHub after each turn (2026-11-06).

A claimed task's work lived only on the disk that ran it. A laptop session that died with uncommitted files or unpushed
commits kept its claim until the 6 h lease ran out, then a VPS routine rebuilt the task from the claim commit.

After each turn, for each worktree of the session's repo that is on a task branch of a guarded repo (POLICY `branch`), this
pushes one snapshot commit to refs/wip/<branch>: parent = the worktree's HEAD (so its unpushed commits go too), tree = HEAD
plus every tracked and untracked change (.gitignore honoured; .env*, keys and files over 10 MB left out). refs/wip/ is
outside refs/heads: no CI run, no branch-list entry, no pre-push suite (--no-verify), and the claim and PR checks never
read it. A clean worktree whose HEAD is already on origin deletes its snapshot. claim-branch --resume refuses while a
snapshot is under LIVE_HOURS old (the session is still working); `wip-save.py --restore`, run by `tracker claim` in the
new worktree, merges the dead session's commits and lays its uncommitted files back as changes.

  wip-save.py            hook mode: reads the Stop hook JSON on stdin, never blocks, always exits 0
  wip-save.py --save     same, for the current directory, printing what it did
  wip-save.py --restore  in a clean worktree of a task branch: exit 0 restored or nothing to restore, 2 refused
Installed beside token-guard.py (/opt/agent-rules/hooks on the VPS, ~/.claude/hooks on the laptop). Test: test-wip-save.sh.
"""
import json, os, re, shutil, socket, subprocess, sys, tempfile, time

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOOK_DIR)
sys.dont_write_bytecode = True
MAX_BYTES = 10 * 1024 * 1024
SECRETS = (":(exclude,glob)**/.env*", ":(exclude,glob)**/*.pem", ":(exclude,glob)**/*.key", ":(exclude,glob)**/id_rsa*")


def git(d, *a, env=None, timeout=40):
    return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, timeout=timeout, env=env)


def out(d, *a, **k):
    return git(d, *a, **k).stdout.strip()


def branch_rule(d):
    """The task-branch regex of d's origin when it is a guarded repo, else ''."""
    import token_guard
    token_guard.HOOK_DIR = HOOK_DIR
    from token_guard.policy import CLAIMED, slug
    return CLAIMED.get(slug(out(d, "remote", "get-url", "origin")), "")


def guarded_branch(d, rule=None):
    """The task branch d has checked out when its origin is a guarded repo, else ''."""
    rule = branch_rule(d) if rule is None else rule
    b = out(d, "branch", "--show-current") if rule else ""
    return b if b and re.match(rule, b) else ""


def task_worktrees(d):
    """[(path, branch)] for every worktree of d's repo on a guarded task branch: a routine's session runs in the clone,
    its task in .claude/worktrees/<branch>."""
    rule = branch_rule(d)   # one origin for all worktrees: an unguarded repo costs two local git calls, no network
    found, path = [], None
    for ln in out(d, "worktree", "list", "--porcelain").splitlines() if rule else []:
        if ln.startswith("worktree "):
            path = ln[9:]
        elif ln.startswith("branch ") and path and os.path.isdir(path):
            b = guarded_branch(path, rule)
            if b:
                found.append((path, b))
    return found


def snapshot_tree(d):
    """Tree of d's worktree as `git add -A` would stage it, built in a scratch index so the real one is untouched."""
    with tempfile.TemporaryDirectory() as t:
        idx = os.path.join(t, "index")
        real = os.path.join(d, out(d, "rev-parse", "--git-path", "index"))
        if os.path.isfile(real):
            shutil.copyfile(real, idx)
        big = [f for f in out(d, "ls-files", "-o", "--exclude-standard", "-z").split("\0")
               if f and os.path.isfile(os.path.join(d, f)) and os.path.getsize(os.path.join(d, f)) > MAX_BYTES]
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        r = git(d, "add", "-A", "--", ":/", *SECRETS, *[":(exclude,literal)" + f for f in big], env=env)
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        return out(d, "write-tree", env=env)


def work_time(d):
    """Unix time of the newest change in d: its HEAD commit or the latest mtime of a changed file."""
    t = [int(out(d, "log", "-1", "--format=%ct") or 0)]
    for e in out(d, "status", "--porcelain", "-z", "--untracked-files=all").split("\0"):
        f = os.path.join(d, e[3:]) if len(e) > 3 else ""
        if f and os.path.isfile(f):
            t.append(int(os.path.getmtime(f)))
    return max(t)


def save(d, b):
    head, tree = out(d, "rev-parse", "HEAD"), snapshot_tree(d)
    ref = "refs/wip/" + b
    remote = dict(reversed(ln.split("\t")) for ln in out(d, "ls-remote", "origin", "refs/heads/" + b, ref).splitlines() if "\t" in ln)
    if tree == out(d, "rev-parse", "HEAD^{tree}") and remote.get("refs/heads/" + b) == head:
        if ref not in remote:
            return "clean"
        r = git(d, "push", "-q", "--no-verify", "origin", "--delete", ref)
        return "deleted " + ref if r.returncode == 0 else "delete failed: " + r.stderr.strip()
    old = remote.get(ref, "")
    if old and out(d, "rev-parse", "-q", "--verify", old + "^{tree}") == tree and out(d, "rev-parse", "-q", "--verify", old + "^") == head:
        return "unchanged"
    # dated by the last real work, not by this push: claim-branch reads the date as "still working", and the hook also
    # snapshots worktrees no session has touched for days
    when = work_time(d)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
    msg = f"wip: {b} on {socket.gethostname().split('.')[0]}, last change {stamp}"
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{when} +0000", "GIT_COMMITTER_DATE": f"{when} +0000"}
    sha = out(d, "commit-tree", tree, "-p", head, "-m", msg, env=env)
    r = git(d, "push", "-q", "--no-verify", "origin", f"+{sha}:{ref}", timeout=60)
    return f"saved {ref} {sha[:9]}" if r.returncode == 0 else "push failed: " + r.stderr.strip()


def save_all(d):
    return [f"{p}: {save(p, b)}" for p, b in task_worktrees(d)]


def restore(d):
    b = guarded_branch(d)
    if not b:
        return 2, "not on a task branch of a guarded repo"
    ref = "refs/wip/" + b
    if not out(d, "ls-remote", "origin", ref):
        return 0, "no WIP snapshot for " + b
    if out(d, "status", "--porcelain"):
        return 2, "this worktree has uncommitted files: restoring would mix two sessions' work. Commit or move them first."
    f = git(d, "fetch", "-q", "--no-tags", "origin", f"+{ref}:{ref}")
    if f.returncode:
        return 2, "fetch failed: " + f.stderr.strip()
    parent = out(d, "rev-parse", ref + "^")
    m = git(d, "merge", "-q", "--no-edit", parent)   # the dead session's unpushed commits; a no-op when HEAD has them
    if m.returncode:
        git(d, "merge", "--abort")
        return 2, f"merging the snapshot's commits ({parent[:9]}) conflicts with {b}; inspect with `git log {ref}`"
    if out(d, "rev-parse", ref + "^{tree}") != out(d, "rev-parse", parent + "^{tree}"):
        c = git(d, "cherry-pick", "--no-commit", ref)
        if c.returncode:
            return 2, f"the snapshot's uncommitted files conflict with {b}: resolve them, they are in the worktree now"
        git(d, "reset", "-q")   # back to uncommitted changes, as the dead session left them
    n = len(out(d, "status", "--porcelain").splitlines())
    return 0, f"restored {ref} ({out(d, 'log', '-1', '--format=%s', ref)}): {n} uncommitted files"


def main():
    if sys.argv[1:] == ["--restore"]:
        rc, msg = restore(os.getcwd())
        print(msg)
        sys.exit(rc)
    if sys.argv[1:] == ["--save"]:
        print("\n".join(save_all(os.getcwd())) or "no task worktrees")
        return
    try:
        d = (json.load(sys.stdin) or {}).get("cwd") or os.getcwd()
        if os.path.isdir(d):
            save_all(d)
    except Exception:
        pass  # allow-failopen: a Stop hook must never block a turn; the next turn's snapshot retries


if __name__ == "__main__":
    main()
