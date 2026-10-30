#!/usr/bin/env python3
"""Tests for the 2026-10-29 routine audit fixes: python3 -m unittest test_routine_fixes
  - claude-routine probe_verdict / probe_ok: what a one-word haiku answer means (bash 4+, GNU date: run on the VPS)
  - claude-routine live_run: a working run with a record under 45 min old must not be stopped
  - worktree-holds --clean: a squash-merged worktree is clean; a routine worktree under work/wt/ holds nothing
"""
import datetime
import importlib.machinery
import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def find(name):
    for p in (os.path.join(HERE, "..", "bin", name), shutil.which(name) or ""):
        if p and os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError(name)


ROUTINE = find("claude-routine")
HOLDS = find("worktree-holds")


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


def extract(script, *names):
    """The text of the top-level bash functions `name() {` … `^}` from the script."""
    out, grab = [], False
    for line in open(script).read().splitlines():
        if any(line.startswith(n + "() {") for n in names):
            grab = True
        if grab:
            out.append(line)
            if line == "}":
                grab = False
    return "\n".join(out)


def gnu_bash():
    r = subprocess.run(["bash", "-c", 'echo "${BASH_VERSINFO[0]}"; date -d "2026-01-01T00:00:00Z" +%s'],
                       capture_output=True, text=True)
    lines = r.stdout.split()
    return r.returncode == 0 and len(lines) == 2 and int(lines[0]) >= 4


@unittest.skipUnless(gnu_bash(), "needs bash 4+ and GNU date (the VPS)")
class ProbeAnswers(unittest.TestCase):
    def run_bash(self, body, **env):
        funcs = extract(ROUTINE, "probe_verdict", "probe_ok")
        shim = tempfile.mkdtemp()
        if not shutil.which("timeout"):
            write(os.path.join(shim, "timeout"), '#!/bin/sh\nshift\nexec "$@"\n', 0o755)
        e = dict(os.environ, PATH=shim + ":" + os.environ["PATH"], PROBE_RETRY_SLEEP="0", **env)
        try:
            return subprocess.run(["bash", "-c", "set -uo pipefail\n" + funcs + "\n" + body],
                                  capture_output=True, text=True, env=e)
        finally:
            shutil.rmtree(shim)

    def verdict(self, ans):
        r = self.run_bash('probe_verdict "$ANS"', ANS=ans)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_only_the_word_ok_is_usable(self):
        for a in ("ok", "OK.", " ok \n", "Ok"):
            self.assertEqual(self.verdict(a), "ok", repr(a))
        # 2026-10-29: "token" contains "ok", so an expired login passed
        for a in ("okay", "Okay, ok", "not ok", "OAuth token has expired"):
            self.assertNotEqual(self.verdict(a), "ok", repr(a))

    def test_auth_errors(self):
        for a in ("OAuth token has expired. Please run /login", "Invalid API key · Please run /login",
                  "API Error: 401 authentication_error"):
            self.assertEqual(self.verdict(a), "auth", a)

    def test_limit_text_only_is_out_of_usage(self):
        for a in ("You've hit your session limit · resets 11am", "5-hour limit reached ∙ resets 3pm",
                  "Claude usage limit reached."):
            self.assertEqual(self.verdict(a), "limit", a)

    def test_empty_overloaded_and_timeout_are_not_out_of_usage(self):
        for a in ("", "\n", "API Error: 529 overloaded_error", "Request timed out"):
            self.assertEqual(self.verdict(a), "retry", repr(a))

    def probe(self, answers):
        """Run probe_ok against a fake claude that prints answers[i] on call i; returns (rc, why, calls)."""
        d = tempfile.mkdtemp()
        try:
            for i, a in enumerate(answers):
                write(os.path.join(d, f"a{i}"), a)
            fake = os.path.join(d, "claude")
            write(fake, f'#!/bin/bash\nn=$(cat {d}/n 2>/dev/null || echo 0)\necho $((n+1)) >{d}/n\n'
                        f'cat {d}/a$n 2>/dev/null\n', 0o755)
            r = self.run_bash(f'claude={fake}; probe_why=; probe_ok; rc=$?; echo "$rc $probe_why"')
            calls = int(open(os.path.join(d, "n")).read())
            rc, _, why = r.stdout.strip().partition(" ")
            return int(rc), why, calls
        finally:
            shutil.rmtree(d)

    def test_probe_ok(self):
        self.assertEqual(self.probe(["ok"]), (0, "", 1))

    def test_empty_answer_retries_once_then_succeeds(self):
        # 10:51:58 UTC 10-29: alt2 and alt answered empty right after a global OOM; both had usage left
        self.assertEqual(self.probe(["", "ok"]), (0, "", 2))

    def test_empty_twice_is_unknown_not_limit(self):
        self.assertEqual(self.probe(["", ""]), (1, "unknown", 2))

    def test_limit_and_auth_do_not_retry(self):
        self.assertEqual(self.probe(["You've hit your session limit"]), (1, "limit", 1))
        self.assertEqual(self.probe(["OAuth token has expired. Please run /login"]), (1, "auth", 1))


@unittest.skipUnless(gnu_bash(), "needs bash 4+ and GNU date (the VPS)")
class LiveRunGuard(unittest.TestCase):
    def check(self, state, lead_age_min, sub_age_min=None):
        d = tempfile.mkdtemp()
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            ts = lambda m: (now - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
            write(f"{d}/state.json", '{"state": "%s"}' % state)
            write(f"{d}/t.jsonl", '{"type":"assistant","timestamp":"%s"}\n' % ts(lead_age_min))
            if sub_age_min is not None:
                write(f"{d}/t/subagents/a.jsonl", '{"type":"assistant","timestamp":"%s"}\n' % ts(sub_age_min))
            funcs = extract(ROUTINE, "newest_record", "live_run", "stalled")
            r = subprocess.run(["bash", "-c", f'set -uo pipefail\n{funcs}\nlive_run {d}/state.json {d}/t.jsonl; echo $?'],
                               capture_output=True, text=True)
            return r.stdout.strip()
        finally:
            shutil.rmtree(d)

    def test_recent_record_is_live(self):
        self.assertEqual(self.check("working", 10), "0")

    def test_recent_subagent_record_is_live(self):
        self.assertEqual(self.check("working", 120, 5), "0")

    def test_old_record_is_not_live(self):
        self.assertEqual(self.check("working", 60), "1")

    def test_stopped_job_is_not_live(self):
        self.assertEqual(self.check("stopped", 1), "1")


def load_holds():
    loader = importlib.machinery.SourceFileLoader("worktree_holds_t", HOLDS)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class WorktreeHolds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        self.g("init", "-q", "--bare", f"{d}/origin.git")
        self.clone = f"{d}/work/task-api"
        self.g("clone", "-q", f"{d}/origin.git", self.clone)
        write(f"{self.clone}/a.txt", "1")
        self.g("add", ".", cwd=self.clone)
        self.g("commit", "-qm", "c1", cwd=self.clone)
        self.g("push", "-q", "-u", "origin", "HEAD:main", cwd=self.clone)
        self.fakebin = f"{d}/bin"
        self.set_gh("")

    def tearDown(self):
        self.tmp.cleanup()

    def g(self, *a, cwd=None):
        r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def set_gh(self, sha):
        write(f"{self.fakebin}/gh", f"#!/bin/sh\necho '{sha}'\n", 0o755)

    def clean(self, path):
        env = dict(os.environ, PATH=self.fakebin + ":" + os.environ["PATH"])
        return subprocess.run(["python3", HOLDS, "--clean", path], capture_output=True, text=True, env=env)

    def add_wt(self, path, branch, commit=True):
        self.g("worktree", "add", "-q", "-b", branch, path, "origin/main", cwd=self.clone)
        if commit:
            write(f"{path}/b.txt", branch)
            self.g("add", "b.txt", cwd=path)
            self.g("commit", "-qm", "work", cwd=path)

    def test_squash_merged_worktree_is_clean(self):
        # the builder pushed, the PR was squash-merged and its branch deleted: no remote ref holds the commit
        # any more, but the PR head on GitHub (what gh reports) does
        wt = f"{self.d}/work/wt/x-t1"
        self.add_wt(wt, "feat/t1-x")
        self.g("push", "-q", "origin", "feat/t1-x", cwd=wt)
        sha = self.g("rev-parse", "HEAD", cwd=wt)
        self.g("push", "-q", "origin", ":feat/t1-x", cwd=wt)
        self.g("fetch", "-q", "--prune", cwd=wt)
        self.assertEqual(self.g("rev-list", "--count", "HEAD", "--not", "--remotes", cwd=wt), "1")  # the old check
        self.set_gh(sha)
        r = self.clean(wt)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_unpushed_worktree_is_not_clean(self):
        wt = f"{self.d}/work/wt/x-t2"
        self.add_wt(wt, "feat/t2-x")
        self.set_gh("")  # no PR
        r = self.clean(wt)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("1 commit", r.stdout)

    def test_dirty_worktree_is_not_clean_even_when_pushed(self):
        wt = f"{self.d}/work/wt/x-t3"
        self.add_wt(wt, "feat/t3-x", commit=False)
        write(f"{wt}/new.txt", "x")
        r = self.clean(wt)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)

    def test_gh_failure_keeps_the_worktree(self):
        wt = f"{self.d}/work/wt/x-t4"
        self.add_wt(wt, "feat/t4-x")
        write(f"{self.fakebin}/gh", "#!/bin/sh\nexit 1\n", 0o755)
        self.assertEqual(self.clean(wt).returncode, 3)

    def test_routine_worktree_holds_nothing_but_phone_worktree_does(self):
        # 10-29: the ready script skipped T-145 for 72 h because of a dirty routine-builder worktree under work/wt/
        mod = load_holds()
        routine = f"{self.d}/work/wt/x-push-t145"
        phone = f"{self.d}/work/task-api/.claude/worktrees/t146-y"
        self.add_wt(routine, "feat/t145-x", commit=False)
        write(f"{routine}/dirty.txt", "x")
        self.add_wt(phone, "feat/t146-y", commit=False)
        write(f"{phone}/dirty.txt", "x")
        holds = mod.worktree_holds(self.clone)
        self.assertNotIn("feat/t145-x", holds)
        self.assertIn("feat/t146-y", holds)


if __name__ == "__main__":
    unittest.main()
