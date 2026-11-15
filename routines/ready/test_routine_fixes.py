#!/usr/bin/env python3
"""Tests for the 2026-10-29 routine audit fixes: python3 -m unittest test_routine_fixes
  - claude-routine probe_verdict / probe_ok: what a one-word haiku answer means (bash 4+, GNU date: run on the VPS)
  - claude-routine rotate: the account list starts after the last run's account (round-robin, 2026-11-10)
  - claude-routine live_run: a working run with a record under 45 min old must not be stopped
  - worktree-holds --clean: a squash-merged worktree is clean; a routine worktree under work/wt/ holds nothing
  - routine-verdict: three facts per group (finished? merged + proof, needs you), idle prints idle, an all-PAUSED group is
    skipped; "Needs you" is one numbered list (PRs, founder ledger items, Blocked tracker rows; deduped, oldest first, 10 max)
    (bash 4+, GNU date, jq: run on the VPS or in CI; gh, systemctl, tracker, claude, claude-notify and the ledger are fakes)
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
RWT = find("routine-worktree")
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

    def test_past_due_subscription_is_billing_and_does_not_retry(self):
        # 2026-11-09: alt2 answered this every hour; as "retry" it cost 30 s a run and the founder was never told
        msg = "Claude Code can't use your Claude subscription, usually because a payment is past due · Pay or renew"
        self.assertEqual(self.verdict(msg), "billing")
        self.assertEqual(self.probe([msg]), (1, "billing", 1))


@unittest.skipUnless(gnu_bash(), "needs bash 4+ (the VPS)")
class ProfileRotation(unittest.TestCase):
    """2026-11-10: 20 of 21 routine runs in 4 days started on alt; each run now starts after the last run's account."""
    def rotate(self, *args):
        r = subprocess.run(["bash", "-c", "set -uo pipefail\n" + extract(ROUTINE, "rotate") + '\nrotate "$@"', "x", *args],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_starts_after_the_last_account_and_wraps(self):
        self.assertEqual(self.rotate("alt", "alt", "alt2", "main"), "alt2 main alt")
        self.assertEqual(self.rotate("alt2", "alt", "alt2", "main"), "main alt alt2")
        self.assertEqual(self.rotate("main", "alt", "alt2", "main"), "alt alt2 main")
        self.assertEqual(self.rotate("alt", "alt2", "alt", "main"), "main alt2 alt")

    def test_unknown_or_no_last_keeps_the_conf_order(self):
        self.assertEqual(self.rotate("", "alt", "alt2", "main"), "alt alt2 main")
        self.assertEqual(self.rotate("main", "alt", "alt2"), "alt alt2")

    def test_wiring(self):
        with open(ROUTINE) as f:
            src = f.read()
        self.assertIn('read -ra order <<<"$(rotate "$(cat "$L/last-profile" 2>/dev/null || true)" "${plist[@]}")"', src)
        self.assertIn('for p in "${order[@]}"; do', src)
        self.assertIn('[ ${#plist[@]} -lt 2 ] || echo "$profile" >"$L/last-profile"', src)


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
            funcs = extract(ROUTINE, "newest_record", "live_run")
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


class OnceToday(unittest.TestCase):
    """claude-routine once_today: a failure the hourly sweep repeats sends one Telegram a day (2026-11-07: 13 in a day)."""
    def test_first_call_per_kind_and_day_only(self):
        d = tempfile.mkdtemp()
        try:
            write(f"{d}/demo.notstarted-2000-01-01", "")
            funcs = extract(ROUTINE, "once_today")
            body = 'once_today notstarted; echo $?; once_today notstarted; echo $?; once_today auth-main; echo $?; ls "$L"'
            r = subprocess.run(["bash", "-c", f'set -uo pipefail\nL={d} name=demo\n{funcs}\n{body}'],
                               capture_output=True, text=True)
            out = r.stdout.split()
            self.assertEqual(out[:3], ["0", "1", "0"], r.stderr)
            self.assertNotIn("demo.notstarted-2000-01-01", out, "yesterday's marker is removed")
        finally:
            shutil.rmtree(d)


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

    def test_detached_worktree_whose_commit_github_holds_is_clean(self):
        # 2026-11-06: four kept-* routine copies (1.4 GB) stayed for days: detached, their branch squash-merged and deleted,
        # so no remote branch and no PR-head lookup (detached has no branch) found the commits; GitHub still had them
        wt = f"{self.d}/work/wt/kept-x-t5"
        self.add_wt(wt, "feat/t5-x")
        sha = self.g("rev-parse", "HEAD", cwd=wt)
        self.g("push", "-q", "origin", f"{sha}:refs/pull/5/head", cwd=wt)   # what a PR leaves on GitHub
        self.g("checkout", "-q", "--detach", cwd=wt)
        self.g("branch", "-D", "feat/t5-x", cwd=self.clone)
        self.assertEqual(self.g("rev-list", "--count", "HEAD", "--not", "--remotes", cwd=wt), "1")
        r = self.clean(wt)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_detached_worktree_github_lacks_is_not_clean(self):
        wt = f"{self.d}/work/wt/kept-x-t6"
        self.add_wt(wt, "feat/t6-x")
        self.g("checkout", "-q", "--detach", cwd=wt)
        r = self.clean(wt)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)

    def sweep(self, path):
        if not shutil.which("timeout"):   # macOS: CI's Ubuntu has coreutils timeout
            write(f"{self.fakebin}/timeout", '#!/bin/sh\nshift; exec "$@"\n', 0o755)
        env = dict(os.environ, PATH=self.fakebin + ":" + os.environ["PATH"])
        return subprocess.run(["bash", RWT, "sweep", "0", path], capture_output=True, text=True, env=env)

    def test_sweep_keeps_a_commit_only_this_disk_has(self):
        # 2026-11-06: unsaved() fetched the SHA inside the worktree, where git exits 0 for a commit it already has,
        # so sweep and clean dropped worktrees whose commits existed nowhere else
        wt = f"{self.d}/work/wt/r-t7"
        self.add_wt(wt, "feat/t7-x")
        self.g("checkout", "-q", "--detach", cwd=wt)
        r = self.sweep(wt)
        self.assertIn("keep", r.stdout, r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(wt))

    def test_sweep_removes_a_commit_github_holds_in_a_pr_ref(self):
        wt = f"{self.d}/work/wt/r-t8"
        self.add_wt(wt, "feat/t8-x")
        self.g("push", "-q", "origin", "HEAD:refs/pull/8/head", cwd=wt)
        self.g("checkout", "-q", "--detach", cwd=wt)
        r = self.sweep(wt)
        self.assertIn("removed", r.stdout, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(wt))

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
                   ROUTINE_VERDICT_PROFILE_ENV=f"{self.fake}/profile-env", ROUTINE_VERDICT_LEDGER=f"{self.d}/ledger.md")
        if dry:
            env["DRY_RUN"] = "1"
        r = subprocess.run([VERDICT, *args], capture_output=True, text=True, env=env)
        sent = f"{self.fake}/sent.txt"
        return r, (open(sent).read() if os.path.exists(sent) else "")

    def day(self, ago):
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ago)).strftime("%Y-%m-%d")

    def ledger(self, *items):
        """items are (days_ago, project, ref, what) open items; a string is written as given (e.g. a done item)."""
        lines = [i if isinstance(i, str) else f"- [ ] {self.day(i[0])} | {i[1]} | {i[2]} | {i[3]} | 1. do it 2. reply | agent carries on"
                 for i in items]
        write(f"{self.d}/ledger.md", "# Founder actions\n\nFormat and rules: elsewhere.\n\n" + "\n".join(lines) + "\n")

    def needs(self, sent):
        """The 'Needs you' part of the first message sent."""
        return sent.split("\n----")[0].partition("3. Needs you")[2]

    def blocked(self, *rows):
        write(f"{self.fake}/tracker.json", json.dumps({"next": {"kind": "NONE"}, "changes": [], "blocked": [
            {"task": t, "title": ti, "status": "Blocked", "pr": pr, "blocker": b, "updated": ""} for t, ti, pr, b in rows]}))

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

    def test_stopped_session_is_paused_not_dead(self):
        self.conf()
        self.set_session("blocked")
        _, sent = self.run_verdict()
        self.assertIn("1. Finished: no, paused on the Claude usage limit", sent)

    def test_unreadable_tracker_is_a_needs_you_item_not_silence(self):
        self.conf("TRACKER_REPO=acme/tracker\n")
        write(f"{self.fake}/tracker.fail", "")
        _, sent = self.run_verdict()
        self.assertRegex(sent, r"3\. Needs you: [1-9]")
        self.assertIn("tracker", sent.lower())

    def test_ledger_items_are_listed_for_their_own_group_only(self):
        self.conf()
        write(f"{self.defs}/other.conf", self.CONF.replace("VERDICT=demo", "VERDICT=other"))
        write(f"{self.defs}/routines.env", "AUTHOR=bot\nPROJ_demo=Demo\nPROJ_other=\"Other Co\"\n")
        self.ledger((14, "demo", "https://github.com/acme/app/issues/3", "Pick the demo provider"),
                    (3, "other-co", "https://github.com/acme/other/issues/9", "Sign the other contract"))
        _, sent = self.run_verdict("demo")
        self.assertIn("3. Needs you: 1.", sent)
        self.assertIn("1) Pick the demo provider (14d) https://github.com/acme/app/issues/3", sent)
        self.assertNotIn("other contract", sent)
        os.remove(f"{self.fake}/sent.txt")
        _, sent = self.run_verdict("other")  # "Other Co" in routines.env matches the ledger's other-co: case and punctuation do not count
        self.assertIn("1) Sign the other contract (3d) https://github.com/acme/other/issues/9", sent)
        self.assertNotIn("demo provider", sent)

    def test_ledger_project_in_the_conf_wins_over_the_display_name(self):
        self.conf("LEDGER_PROJECT=acme-shop\n")
        self.ledger((2, "acme-shop", "app#1", "Approve the shop copy"), (2, "demo", "app#2", "Not mine"))
        _, sent = self.run_verdict()
        self.assertIn("1) Approve the shop copy (2d)", sent)
        self.assertNotIn("Not mine", sent)

    def test_done_ledger_items_are_skipped(self):
        self.conf()
        self.ledger(f"- [x] {self.day(9)} | demo | app#4 | (done by agent) Old ask | 1. x | y", (5, "demo", "app#5", "Open ask"),
                    f"- [X] {self.day(9)} | demo | app#6 | Capital X is not an open item | 1. x | y")
        _, sent = self.run_verdict()
        self.assertIn("3. Needs you: 1.", sent)
        self.assertIn("1) Open ask (5d)", sent)
        self.assertNotIn("Old ask", sent)
        self.assertNotIn("Capital X", sent)

    def test_missing_or_unreadable_ledger_is_no_items_and_no_error(self):
        self.conf()
        r, sent = self.run_verdict()  # no ledger file written
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("3. Needs you: nothing.", sent)
        os.remove(f"{self.fake}/sent.txt")
        os.makedirs(f"{self.d}/ledger.md")  # a directory cannot be read as a file
        r, sent = self.run_verdict()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("3. Needs you: nothing.", sent)

    def test_ledger_item_and_tracker_row_for_the_same_thing_are_listed_once(self):
        self.conf("TRACKER_REPO=acme/tracker\nCODE_REPO=acme/app\n")
        self.blocked(("T-002", "Pick a provider", "", "needs founder: choose a provider"),     # named by task id
                     ("#39", "Unknown app on the webhook", "", "issue label agent:blocked"),   # named as app#39 (the code repo ends in app)
                     ("T-010", "Ship the thing", "#7", "needs founder: look at the PR"),       # named by its PR URL
                     ("#40", "Another issue", "", "issue label agent:blocked"),                # app#40 is not named: other#40 is another repo
                     ("T-003", "Not in the ledger", "", "needs founder: sign off"))
        self.ledger((8, "demo", "T-002 (PR #50)", "Choose the provider"),
                    (7, "demo", "app#39", "Decide on the unknown webhook app"),
                    (6, "demo", "https://github.com/acme/app/pull/7", "Review the ship PR"),
                    (5, "demo", "other#40", "A different repo's issue 40"))
        _, sent = self.run_verdict()
        n = self.needs(sent)
        self.assertIn("3. Needs you: 6.", sent)
        self.assertEqual(n.count("choose a provider"), 0)  # the tracker row's blocker text: the ledger line won
        self.assertEqual(n.count("Choose the provider"), 1)
        self.assertEqual(n.count("Decide on the unknown webhook app"), 1)
        self.assertNotIn("Unknown app on the webhook", n)
        self.assertEqual(n.count("Review the ship PR"), 1)
        self.assertNotIn("Ship the thing", n)
        self.assertIn("Another issue", n)       # the row stays: only a different repo's #40 is in the ledger
        self.assertIn("A different repo's issue 40", n)
        self.assertIn("T-003 Not in the ledger: needs founder: sign off", n)
        # the ledger line without a URL of its own takes the tracker row's link; one with a URL keeps it
        self.assertIn("Decide on the unknown webhook app (7d) https://github.com/acme/app/issues/39", n)
        self.assertIn("Review the ship PR (6d) https://github.com/acme/app/pull/7", n)
        self.assertEqual(n.count("https://github.com/acme/app/pull/7"), 1)

    def test_tracker_row_with_a_pr_links_to_it_and_lists_oldest_first(self):
        self.conf("TRACKER_REPO=acme/tracker\nCODE_REPO=acme/app\n")
        self.blocked(("T-010", "Ship the thing", "#7", "needs founder: look at the PR"))
        self.ledger((20, "demo", "x", "Oldest ledger ask"), (1, "demo", "x", "Newest ledger ask"))
        _, sent = self.run_verdict()
        lines = [l for l in self.needs(sent).splitlines() if l[:2] in ("1)", "2)", "3)")]
        self.assertEqual(len(lines), 3, sent)
        self.assertTrue(lines[0].startswith("1) Oldest ledger ask (20d)"), lines)
        self.assertTrue(lines[1].startswith("2) Newest ledger ask (1d)"), lines)
        self.assertEqual(lines[2], "3) T-010 Ship the thing: needs founder: look at the PR https://github.com/acme/app/pull/7")  # no age: not dated

    def test_failing_pr_and_ledger_item_share_one_numbered_list(self):
        self.conf()
        draft = self.pr(8, "feat: add gadget", state="OPEN")
        draft["isDraft"] = True
        self.set_prs([draft])
        self.ledger((4, "demo", "x", "Send the keys"))
        _, sent = self.run_verdict()
        self.assertIn("3. Needs you: 2.", sent)
        self.assertIn("1) add gadget: it is still a draft https://github.com/acme/app/pull/8", sent)
        self.assertIn("2) Send the keys (4d)", sent)

    def test_at_most_ten_lines_then_plus_n_more(self):
        self.conf()
        self.ledger(*[(30 - i, "demo", "x", f"Ask number {i:02d}") for i in range(12)])
        _, sent = self.run_verdict()
        n = self.needs(sent)
        self.assertIn("3. Needs you: 12.", sent)
        numbered = [l for l in n.splitlines() if l[:1].isdigit() and ") " in l[:4]]
        self.assertEqual(len(numbered), 10, n)
        self.assertTrue(numbered[0].startswith("1) Ask number 00 (30d)"), numbered)  # oldest first
        self.assertTrue(numbered[9].startswith("10) Ask number 09 (21d)"), numbered)
        self.assertNotIn("Ask number 10", n)
        self.assertEqual(n.strip().splitlines()[-1], "+2 more")

    def test_long_text_is_cut_and_a_non_url_ref_adds_no_link(self):
        self.conf()
        self.ledger((1, "demo", "api#9", "Long " + "word " * 40))
        _, sent = self.run_verdict()
        line = [l for l in sent.splitlines() if l.startswith("1) Long")][0]
        self.assertLessEqual(len(line), 3 + 90 + len(" (1d)"), line)
        self.assertTrue(line.endswith("… (1d)"), line)
        self.assertNotIn("http", line)

    def test_dry_run_prints_instead_of_sending(self):
        self.conf()
        r, sent = self.run_verdict(dry=True)
        self.assertEqual(sent, "")
        self.assertIn("1. Finished", r.stdout)


if __name__ == "__main__":
    unittest.main()
