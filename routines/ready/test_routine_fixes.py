#!/usr/bin/env python3
"""Tests for the 2026-10-29 routine audit fixes: python3 -m unittest test_routine_fixes
  - claude-routine probe_verdict / probe_ok: what a one-word haiku answer means (bash 4+, GNU date: run on the VPS)
  - claude-routine live_run: a working run with a record under 45 min old must not be stopped
  - worktree-holds --clean: a squash-merged worktree is clean; a routine worktree under work/wt/ holds nothing
  - routine-verdict: three facts per group (finished? merged + proof, needs you), idle prints idle, an all-PAUSED group is
    skipped (bash 4+, GNU date, jq: run on the VPS or in CI; gh, systemctl, tracker, claude and claude-notify are fakes)
"""
import datetime
import json
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
VERDICT = os.path.abspath(os.path.join(HERE, "..", "..", "routine-verdict"))


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


@unittest.skipUnless(gnu_bash() and shutil.which("jq"), "needs bash 4+, GNU date and jq (the VPS, CI)")
class VerdictThreeFacts(unittest.TestCase):
    CONF = ("VERDICT=demo\nVERDICT_AT=17:30:00\nREPO=app\nPROFILE=p1\n"
            "VERDICT_REPOS=(\"app|acme/app|main\")\nVERDICT_CHECKS=(\"acme/app|ci\")\nVERDICT_QUEUE='echo \"READY (0)\"'\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        self.defs, self.fake = f"{d}/defs", f"{d}/fake"
        write(f"{self.defs}/routines.env", "AUTHOR=bot\nPROJ_demo=Demo\n")
        iso = lambda m: (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.iso = iso
        write(f"{self.fake}/gh", "#!/bin/sh\ncase \"$1 $2\" in\n 'pr list') cat \"$FAKE/prs.json\" ;;\n 'issue list') echo 0 ;;\n *) exit 1 ;;\nesac\n", 0o755)
        write(f"{self.fake}/systemctl", "#!/bin/sh\ncase \"$*\" in\n *claude-routine@demo.service*) cat \"$FAKE/service.txt\" ;;\nesac\n", 0o755)
        write(f"{self.fake}/notify", "#!/bin/sh\nprintf '%s\\n----\\n' \"$1\" >> \"$FAKE/sent.txt\"\necho sent\n", 0o755)
        write(f"{self.fake}/tracker", "#!/bin/sh\necho \"$*\" >> \"$FAKE/tracker.log\"\n[ -f \"$FAKE/tracker.fail\" ] && exit 2\ncat \"$FAKE/tracker.json\"\n", 0o755)
        write(f"{self.fake}/claude", "#!/bin/sh\ncat \"$FAKE/agents.json\"\n", 0o755)
        write(f"{self.fake}/profile-env", f"claude={self.fake}/claude\nCLAUDE_CONFIG_DIR={self.fake}/cfg\n")
        os.makedirs(f"{d}/work/app")
        self.set_prs([])
        self.set_service(ok=True)
        self.set_session("done")
        write(f"{self.fake}/tracker.json", json.dumps({"next": {"kind": "NONE"}, "changes": [], "blocked": []}))

    def tearDown(self):
        self.tmp.cleanup()

    def conf(self, extra="", name="demo"):
        write(f"{self.defs}/{name}.conf", self.CONF + extra)

    def set_prs(self, prs):
        write(f"{self.fake}/prs.json", json.dumps(prs))

    def set_service(self, ok=True, minutes_ago=60):
        t = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)).strftime("%a %Y-%m-%d %H:%M:%S UTC")
        write(f"{self.fake}/service.txt", f"Result={'success' if ok else 'exit-code'}\nExecMainStatus={0 if ok else 1}\n"
              f"ExecMainExitTimestamp={t}\nActiveState={'inactive' if ok else 'failed'}\n")

    def set_session(self, state):
        started = int((datetime.datetime.now().timestamp() - 4 * 3600) * 1000)
        write(f"{self.fake}/agents.json", json.dumps([{"kind": "background", "name": "routine: demo 11-02", "state": state, "startedAt": started,
                                                       "id": "abc", "sessionId": "sess", "cwd": "/w"}]))
        write(f"{self.fake}/cfg/projects/-w/sess.jsonl",
              json.dumps({"type": "assistant", "timestamp": self.iso(30), "message": {"content": []}}) + "\n")

    def pr(self, number, title, state="MERGED", conclusion="SUCCESS"):
        run = {"__typename": "CheckRun", "name": "ci", "workflowName": "test", "status": "COMPLETED", "conclusion": conclusion,
               "startedAt": self.iso(200), "completedAt": self.iso(190)}
        review = {"__typename": "StatusContext", "context": "routine/review", "state": "SUCCESS", "startedAt": self.iso(150)}
        return {"number": number, "title": title, "url": f"https://github.com/acme/app/pull/{number}", "state": state,
                "createdAt": self.iso(300), "mergedAt": self.iso(120) if state == "MERGED" else None, "isDraft": False,
                "headRefName": f"feat/t{number}-x", "baseRefName": "main", "mergeStateStatus": "CLEAN", "headRefOid": "a" * 40,
                "body": "## What changed\nx\n## How verified\ny\n## Not verified\nz\n", "statusCheckRollup": [run, review]}

    def run_verdict(self, *args, dry=False):
        env = dict(os.environ, PATH=self.fake + ":" + os.environ["PATH"], FAKE=self.fake, ROUTINE_VERDICT_DEFS=self.defs,
                   ROUTINE_VERDICT_STATE=f"{self.d}/state", ROUTINE_VERDICT_NOTIFY=f"{self.fake}/notify",
                   ROUTINE_VERDICT_TRACKER=f"{self.fake}/tracker", ROUTINE_VERDICT_WORK=f"{self.d}/work",
                   ROUTINE_VERDICT_PROFILE_ENV=f"{self.fake}/profile-env")
        if dry:
            env["DRY_RUN"] = "1"
        r = subprocess.run([VERDICT, *args], capture_output=True, text=True, env=env)
        sent = f"{self.fake}/sent.txt"
        return r, (open(sent).read() if os.path.exists(sent) else "")

    def test_finished_merged_and_blocked(self):
        self.conf("TRACKER_REPO=acme/tracker\nVERDICT_NOTE='Tests green and merged, not deployed.'\n")
        self.set_prs([self.pr(7, "feat: add widget")])
        write(f"{self.fake}/tracker.json", json.dumps({"next": {"kind": "NONE"}, "changes": [], "blocked": [
            {"task": "T-002", "title": "Pick a provider", "status": "Blocked", "blocker": "needs founder: choose a provider"}]}))
        r, sent = self.run_verdict()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(sent, r"1\. Finished: yes, ended \d\d:\d\d UTC")
        self.assertIn("2. Merged: 1", sent)
        self.assertIn("add widget: ci success", sent)  # the "feat:" prefix is dropped
        self.assertIn("ci success", sent)
        self.assertIn("https://github.com/acme/app/pull/7", sent)
        self.assertIn("Tests green and merged, not deployed.", sent)
        self.assertIn("3. Needs you: 1", sent)
        self.assertIn("T-002", sent)
        self.assertIn("choose a provider", sent)
        self.assertNotRegex(sent, r"\bNO\b")
        self.assertIn("next --json", open(f"{self.fake}/tracker.log").read())
        self.assertIn("--conf", open(f"{self.fake}/tracker.log").read())

    def test_idle_group_prints_one_idle_line(self):
        self.conf()
        self.set_service(ok=True, minutes_ago=60 * 40)  # last run is older than the window
        r, sent = self.run_verdict()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        body = sent.split("\n----")[0].strip()
        self.assertEqual(len(body.splitlines()), 1, body)
        self.assertIn("idle", body.lower())
        self.assertNotRegex(body, r"\bNO\b")

    def test_all_paused_group_is_skipped(self):
        self.conf("PAUSED=1\n")
        r, sent = self.run_verdict()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sent, "")
        self.assertIn("paused", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(f"{self.d}/state/demo"))

    def test_note_is_absent_without_the_field_or_without_merges(self):
        self.conf()
        self.set_prs([self.pr(7, "feat: add widget")])
        _, sent = self.run_verdict()
        self.assertIn("2. Merged: 1", sent)
        self.assertNotIn("not deployed", sent)

    def test_crashed_run_says_not_finished_and_asks_for_help(self):
        self.conf()
        self.set_service(ok=False)
        _, sent = self.run_verdict()
        self.assertRegex(sent, r"1\. Finished: no")
        self.assertNotIn("Finished: yes", sent)
        self.assertRegex(sent, r"3\. Needs you: [1-9]")

    def test_unreadable_tracker_is_a_needs_you_item_not_silence(self):
        self.conf("TRACKER_REPO=acme/tracker\n")
        write(f"{self.fake}/tracker.fail", "")
        _, sent = self.run_verdict()
        self.assertRegex(sent, r"3\. Needs you: [1-9]")
        self.assertIn("tracker", sent.lower())

    def test_dry_run_prints_instead_of_sending(self):
        self.conf()
        r, sent = self.run_verdict(dry=True)
        self.assertEqual(sent, "")
        self.assertIn("1. Finished", r.stdout)


if __name__ == "__main__":
    unittest.main()
