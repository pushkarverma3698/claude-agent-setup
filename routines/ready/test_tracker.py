#!/usr/bin/env python3
"""Tests for routines/bin/tracker: python3 routines/ready/test_tracker.py
No network: a fake `gh` serves the tracker CSV from a local file (real sha1, 409 on a stale sha, one injectable race), a fake
`claim-branch` creates the branch in a local bare repo, and the code repo is a temp git repo, so worktrees are real."""
import argparse
import csv
import datetime as dt
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TRACKER = os.path.join(HERE, "..", "bin", "tracker")
HEADER = ["Task ID", "Title", "Milestone", "Owner", "Status", "Depends on", "Open Qs",
          "Holder", "Environment", "Run", "Branch", "PR", "Blocker", "Next step", "Updated"]

FAKE_GH = r'''#!/usr/bin/env python3
import base64, hashlib, json, os, sys
D, a = os.environ["FAKE_GH_DIR"], sys.argv[1:]
open(D + "/gh.log", "a").write(" ".join(a) + "\n")
csvp = D + "/tracker.csv"
def sha(b): return hashlib.sha1(b).hexdigest()
if a[:2] == ["pr", "list"]:
    sys.stdout.write(open(D + "/prs.json").read() if os.path.exists(D + "/prs.json") else "[]")
elif a[0] == "api" and "PUT" in a:
    body = json.load(sys.stdin)
    race = os.environ.get("FAKE_GH_RACE")  # another writer lands first, once
    if race and not os.path.exists(D + "/race.done"):
        open(D + "/race.done", "w").close()
        os.replace(race, csvp)
        sys.stderr.write("gh: tracker.csv does not match (HTTP 409)\n"); sys.exit(1)
    cur = open(csvp, "rb").read()
    if body["sha"] != sha(cur):
        sys.stderr.write("gh: sha does not match (HTTP 409)\n"); sys.exit(1)
    open(csvp, "wb").write(base64.b64decode(body["content"]))
    print(json.dumps({"content": {"sha": sha(open(csvp, "rb").read())}}))
elif a[0] == "api" and "/contents/" in " ".join(a):
    raw = open(D + "/other.csv" if "other.csv" in " ".join(a) else csvp, "rb").read()
    b64 = base64.encodebytes(raw).decode()
    print(json.dumps({"sha": sha(raw), "encoding": "base64", "content": b64}))
elif a[0] == "api" and "matching-refs" in " ".join(a):
    prefix = "refs/heads/" + a[-1].split("/heads/", 1)[1]
    refs = json.load(open(D + "/refs.json")) if os.path.exists(D + "/refs.json") else []
    print(json.dumps([r for r in refs if r["ref"].startswith(prefix)]))
elif a[0] == "api" and "/git/commits/" in a[-1]:
    print(json.dumps({"committer": {"date": json.load(open(D + "/commits.json"))[a[-1].rsplit("/", 1)[1]]}}))
else:
    sys.stderr.write("fake gh: unexpected " + " ".join(a) + "\n"); sys.exit(1)
'''
FAKE_CB = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_GH_DIR/cb.log"
[ -n "${FAKE_CB_EXIT:-}" ] && { echo "claim-branch: held" >&2; exit "$FAKE_CB_EXIT"; }
case "$1" in
  --release) [ -f "$FAKE_GH_DIR/keep" ] && exit 3; git -C "$FAKE_ORIGIN" branch -qD "$3" 2>/dev/null; echo released ;;
  --resume) echo "CLAIM_HOURS=${CLAIM_HOURS:-12} cwd=$PWD" >> "$FAKE_GH_DIR/cb.log"   # claim-branch refuses a claim commit younger than CLAIM_HOURS
            [ -f "$FAKE_GH_DIR/tip_age" ] && [ "$(cat "$FAKE_GH_DIR/tip_age")" -lt "${CLAIM_HOURS:-12}" ] && { echo "claim-branch: claimed $(cat "$FAKE_GH_DIR/tip_age")h ago, someone holds the task" >&2; exit 3; }
            echo "noise line"; echo 2222222222222222222222222222222222222222 ;;
  *) git -C "$FAKE_ORIGIN" branch "$2" main && echo 1111111111111111111111111111111111111111 ;;
esac
'''
FAKE_NOTIFY = '#!/usr/bin/env bash\n[ -f "$FAKE_GH_DIR/notify.fail" ] && exit 1\ncat >> "$FAKE_GH_DIR/notify.log"; echo "---" >> "$FAKE_GH_DIR/notify.log"\n'


def load_module():
    loader = importlib.machinery.SourceFileLoader("tracker_mod", TRACKER)
    spec = importlib.util.spec_from_loader("tracker_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def ago(hours):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%MZ")


def row(task, title="Thing", status="Not started", **kw):
    r = dict.fromkeys(HEADER, "")
    r.update({"Task ID": task, "Title": title, "Status": status, "Milestone": "M1", "Owner": "Eng"})
    r.update(kw)
    return r


def render(rows, header=HEADER, eol="\n"):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=eol)
    w.writerow(header)
    for r in rows:
        w.writerow([r.get(h, "") for h in header])
    return buf.getvalue()


def readf(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def writef(path, text, mode="w"):
    with open(path, mode, newline="" if "b" not in mode else None) as f:
        f.write(text)


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        self.fake = os.path.join(d, "fake")
        self.bin = os.path.join(d, "bin")
        for p in (self.fake, self.bin, os.path.join(d, "cwd"), os.path.join(d, "conf")):
            os.makedirs(p)
        for name, text in (("gh", FAKE_GH), ("claim-branch", FAKE_CB), ("notify", FAKE_NOTIFY)):
            writef(os.path.join(self.bin, name), text)
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.origin = os.path.join(d, "acme", "app.git")
        os.makedirs(os.path.dirname(self.origin))
        sh("git", "init", "-q", "--bare", "-b", "main", self.origin)
        self.clones = {}
        for who in ("a", "b"):
            c = self.clones[who] = os.path.join(d, "clone-" + who)
            sh("git", "clone", "-q", self.origin, c)
            sh("git", "config", "user.email", "t@example.invalid", cwd=c)
            sh("git", "config", "user.name", "t", cwd=c)
            if who == "a":
                writef(os.path.join(c, "f.txt"), "1")
                sh("git", "add", "f.txt", cwd=c)
                sh("git", "commit", "-qm", "init", cwd=c)
                sh("git", "push", "-q", "origin", "HEAD:main", cwd=c)
            else:
                sh("git", "fetch", "-q", "origin", cwd=c)
        self.conf_extra = ""
        self.write_conf()
        self.env = {**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"], "HOME": d, "FAKE_GH_DIR": self.fake,
                    "FAKE_ORIGIN": self.origin, "TRACKER_CLAIM_BRANCH": os.path.join(self.bin, "claim-branch"),
                    "TRACKER_RETRY_SCALE": "0", "TRACKER_CONF_DIR": os.path.join(d, "conf")}
        for k in ("CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "AGENT_OVERLAY_DIR", "TRACKER_PROJECT", "AGENT_SETUP_ENV"):
            self.env.pop(k, None)
        self.put([])

    def tearDown(self):
        self.tmp.cleanup()

    def write_conf(self):
        for who, clone in self.clones.items():
            writef(os.path.join(self.d, "conf", f"proj-{who}.conf"), (f"TRACKER_REPO=acme/tracker\nTRACKER_PATH=backlog.csv\nCODE_REPO=acme/app\nREPO_DIR={clone}\nWORK_BASE=main\n"
                        f"NOTIFY_CMD={self.bin}/notify\n{self.conf_extra}"))

    def put(self, rows, **kw):
        writef(os.path.join(self.fake, "tracker.csv"), render(rows, **kw))

    def rows(self):
        return {r["Task ID"]: r for r in csv.DictReader(io.StringIO(readf(os.path.join(self.fake, "tracker.csv")), newline=""))}

    def log(self, name):
        p = os.path.join(self.fake, name)
        return readf(p) if os.path.exists(p) else ""

    def tracker(self, *args, who="A", cwd=None, env=None):
        e = {**self.env, "TRACKER_ENV": "laptop" if who == "A" else "vps", "TRACKER_RUN": "run" + who, **(env or {})}
        conf = os.path.join(self.d, "conf", f"proj-{who.lower()}.conf")
        return subprocess.run([sys.executable, TRACKER, *args, "--conf", conf], cwd=cwd or os.path.join(self.d, "cwd"),
                              env=e, capture_output=True, text=True)

    def branch_on_remote(self, name, hours_old):
        """A branch pushed to origin and listed by the fake GitHub, with its tip commit dated hours_old ago."""
        sha = sh("git", "rev-parse", "main", cwd=self.origin)
        if subprocess.run(["git", "-C", self.origin, "rev-parse", "-q", "--verify", "refs/heads/" + name], capture_output=True).returncode:
            sh("git", "branch", name, "main", cwd=self.origin)
        for fn, init, upd in (("refs.json", [], lambda v: v + [{"ref": "refs/heads/" + name, "object": {"sha": sha}}]),
                              ("commits.json", {}, lambda v: {**v, sha: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_old)).strftime("%Y-%m-%dT%H:%M:%SZ")})):
            p = os.path.join(self.fake, fn)
            cur = json.loads(readf(p)) if os.path.exists(p) else init
            writef(p, json.dumps(upd(cur)))


class RoundTrip(Sandbox):
    def cfg(self):
        mod = load_module()
        return mod, mod.load_conf(argparse.Namespace(conf=os.path.join(self.d, "conf", "proj-a.conf"), project=None))

    def test_01_roundtrip_is_byte_identical(self):
        mod, cfg = self.cfg()
        base = [row("T-101", 'Needs "quotes", commas', Next_step=""), row("T-102", "multi\nline\r\ntitle é—日本 ✓")]
        for kw in ({}, {"eol": "\r\n"}):
            text = render([{**r, "Next step": "a, b"} for r in base], **kw)
            for variant in (text, text.rstrip("\r\n"), "﻿" + text):
                self.assertEqual(mod.Table(variant, cfg).dump(), variant)
        # through the CLI: a read-only command writes nothing, a write changes only its own row's line
        self.put([row("T-101", "x, y"), row("T-102", "é\nz")])
        before = readf(os.path.join(self.fake, "tracker.csv"), "rb")
        self.assertEqual(self.tracker("next").returncode, 0)
        self.assertNotIn("PUT", self.log("gh.log"))
        self.assertEqual(readf(os.path.join(self.fake, "tracker.csv"), "rb"), before)

    def test_01b_a_write_touches_only_its_row(self):
        self.put([row("T-101", "keep, this"), row("T-102", "é\nz")])
        mod, cfg = self.cfg()
        t = mod.Table(readf(os.path.join(self.fake, "tracker.csv")), cfg)
        original = t.dump()
        t.set(t.find("T-102"), "Holder", "x:y:z")
        out = t.dump()
        self.assertNotEqual(out, original)
        self.assertTrue(out.startswith(original.split("\n")[0] + "\n" + "T-101,\"keep, this\""))


class Behaviour(Sandbox):
    def test_02_routine_holds_task_second_holder_skips_it(self):
        self.put([row("T-101", status="Building", Holder="routine:r1:abc123", Environment="routine", Run="r1", Branch="feat/t101-thing", Updated=ago(1)),
                  row("T-102", "Other")])
        r = self.tracker("next", who="B")
        self.assertEqual(r.stdout.splitlines()[0], "NEXT NEW T-102", r.stdout + r.stderr)
        self.assertIn("HELD T-101 | routine:r1:abc123 | routine | r1 | updated 1h ago | skip: held by", r.stdout)
        c = self.tracker("claim", "T-101", who="B")
        self.assertEqual(c.returncode, 3, c.stderr)
        self.assertIn("routine:r1:abc123", c.stderr)
        self.assertEqual(self.log("cb.log"), "")

    def test_03_lease_three_hours_held_seven_hours_leftover(self):
        for hours, kind in ((3, "HELD"), (7, "LEFTOVER")):
            self.put([row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(hours))])
            out = self.tracker("next").stdout
            self.assertIn(f"{kind} T-101", out)
        self.assertEqual(out.splitlines()[0], "NEXT LEFTOVER T-101")

    def assert_hook_parses(self, line, branch, sha):
        """The CLAIMED line must satisfy token-guard's dispatch regexes, or every builder prompt built from it is refused."""
        m = re.search(r"\bbranch\s+`?([\w.-]+/[\w./-]+?)`?(?=\.?(?:[\s,;)]|$))", line)
        self.assertTrue(m and m.group(1) == branch, line)
        m = re.search(r"\bclaim\s+`?([0-9a-f]{7,40})\b", line)
        self.assertTrue(m and m.group(1) == sha, line)

    def test_04_leftover_first_then_resume(self):
        self.branch_on_remote("feat/t101-thing", 9)
        self.put([row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(9)), row("T-102", "Next one")])
        c = self.tracker("claim", "T-102")
        self.assertEqual(c.returncode, 4, c.stderr)
        self.assertIn("leftover first: T-101", c.stderr)
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--resume acme/app feat/t101-thing", self.log("cb.log"))
        self.assert_hook_parses(r.stdout, "feat/t101-thing", "2" * 40)
        got = self.rows()["T-101"]
        self.assertEqual((got["Status"], got["Branch"], got["Environment"], got["Run"]), ("Claimed", "feat/t101-thing", "laptop", "runA"))
        self.assertRegex(got["Holder"], r"^laptop:runA:[0-9a-f]{6}$")
        wt = os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t101-thing")
        claim = json.loads(readf(os.path.join(sh("git", "rev-parse", "--absolute-git-dir", cwd=wt), "tracker-claim")))
        self.assertEqual((claim["task"], claim["holder"], claim["project"]), ("T-101", got["Holder"], "proj-a"))
        self.assertEqual(self.tracker("next", who="B").stdout.splitlines()[0], "NEXT NEW T-102")

    def test_04a_leftover_resumes_its_branch_from_an_older_branch_format(self):
        # 2026-11-27: Oplify moved task/issue-N-* to feat/tN-*; an in-review row on the old branch must resume it, not start over
        self.branch_on_remote("task/issue-101-thing", 9)
        self.put([row("T-101", status="In review", Holder="x:y:z", Branch="task/issue-101-thing", Updated=ago(9))])
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--resume acme/app task/issue-101-thing", self.log("cb.log"))
        self.assertEqual(self.rows()["T-101"]["Branch"], "task/issue-101-thing")

    def test_04b_leftover_between_lease_and_claim_hours_resumes(self):
        # 2026-10: a 7h-old leftover was offered by `next`, but claim-branch's 12h CLAIM_HOURS refused the resume
        self.branch_on_remote("feat/t101-thing", 7)
        writef(os.path.join(self.fake, "tip_age"), "7")  # the branch tip is a claim commit 7h old
        self.put([row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(7)), row("T-102", "Next one")])
        self.assertEqual(self.tracker("next").stdout.splitlines()[0], "NEXT LEFTOVER T-101")
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CLAIM_HOURS=6", self.log("cb.log"))
        writef(os.path.join(self.fake, "tip_age"), "3")  # a claim commit younger than the lease still refuses
        self.put([row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(7))])
        self.assertEqual(self.tracker("claim", "T-101").returncode, 3)

    def test_04c_leftover_reuses_worktree_at_another_path(self):
        self.branch_on_remote("feat/t101-thing", 9)
        old = os.path.join(self.d, "elsewhere", "old-run")
        sh("git", "fetch", "-q", "origin", cwd=self.clones["a"])
        sh("git", "worktree", "add", "--track", "-B", "feat/t101-thing", old, "origin/feat/t101-thing", cwd=self.clones["a"])
        self.put([row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(9))])
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.path.realpath(re.search(r"worktree=(\S+)", r.stdout).group(1)), os.path.realpath(old))
        gd = sh("git", "rev-parse", "--absolute-git-dir", cwd=old)
        self.assertEqual(json.loads(readf(os.path.join(gd, "tracker-claim")))["task"], "T-101")
        self.assertFalse(os.path.exists(os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t101-thing")))

    def test_04c2_leftover_adopts_the_dead_runs_dirty_worktree(self):
        # 2026-11-06: T-137/142/143 were leftovers whose dead run left uncommitted files: claim-branch --resume ran from the
        # clone, so worktree-holds counted that very worktree as someone else's unsaved work and refused (exit 3) for 72h
        self.branch_on_remote("feat/t101-thing", 9)
        old = os.path.join(self.d, "elsewhere", "old-run")
        sh("git", "fetch", "-q", "origin", cwd=self.clones["a"])
        sh("git", "worktree", "add", "--track", "-B", "feat/t101-thing", old, "origin/feat/t101-thing", cwd=self.clones["a"])
        writef(os.path.join(old, "half-done.txt"), "wip")
        tree = sh("git", "rev-parse", "feat/t101-thing^{tree}", cwd=self.origin)
        claim = sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit-tree", tree, "-p", "feat/t101-thing", "-m", "claim", cwd=self.origin)
        sh("git", "update-ref", "refs/heads/feat/t101-thing", claim, cwd=self.origin)   # what claim-branch --resume pushes
        self.put([row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(9))])
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        cwd = re.search(r"cwd=(\S+)", self.log("cb.log")).group(1)
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(old))
        self.assertEqual(readf(os.path.join(old, "half-done.txt")), "wip")
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=old), claim)

    def test_04c3_leftover_from_another_machine_restores_its_wip_snapshot(self):
        # 2026-11-06: a laptop run died with an unpushed commit and uncommitted files; its wip-save Stop hook had pushed them
        # to refs/wip/<branch>. The VPS claim of the leftover starts a fresh worktree and must start from that work.
        self.branch_on_remote("feat/t101-thing", 9)
        wip = os.path.abspath(os.path.join(HERE, "..", "..", "claude-remote-hooks", "wip-save.py"))
        pol = os.path.join(self.d, "policy.json")
        writef(pol, json.dumps({"repos": {"acme/app": {"bases": ["main"], "branch": r"^feat/t(\d+)-"}}}))
        env = {"WIP_SAVE": wip, "TOKEN_GUARD_POLICY": pol, "TOKEN_GUARD_DEFAULTS": os.path.join(self.d, "none.json")}
        b = self.clones["b"]
        sh("git", "fetch", "-q", "origin", cwd=b)
        sh("git", "checkout", "-q", "-b", "feat/t101-thing", "origin/feat/t101-thing", cwd=b)
        writef(os.path.join(b, "done.txt"), "committed")
        sh("git", "add", "done.txt", cwd=b)
        sh("git", "commit", "-qm", "feat: half", cwd=b)
        writef(os.path.join(b, "half-done.txt"), "wip")
        subprocess.run([sys.executable, wip, "--save"], cwd=b, env={**self.env, **env}, check=True, capture_output=True)
        self.assertTrue(sh("git", "rev-parse", "refs/wip/feat/t101-thing", cwd=self.origin))
        self.put([row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(9))])
        r = self.tracker("claim", "T-101", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        wt = re.search(r"worktree=(\S+)", r.stdout).group(1)
        self.assertEqual(readf(os.path.join(wt, "half-done.txt")), "wip")
        self.assertEqual(readf(os.path.join(wt, "done.txt")), "committed")
        self.assertIn("?? half-done.txt", sh("git", "status", "--porcelain", cwd=wt))
        self.assertIn("restored refs/wip/feat/t101-thing", r.stderr)

    def test_04d_leftover_whose_derived_branch_is_a_merged_pr_head_gets_a_suffix(self):
        # a "Part of T-101" PR merged from feat/t101-thing and the branch was kept: claiming the leftover must not reuse that name
        self.branch_on_remote("feat/t101-thing", 30)
        writef(os.path.join(self.fake, "prs.json"), json.dumps([{"number": 13, "headRefName": "feat/t101-thing", "mergedAt": "2026-10-30T11:00:00Z",
                                                                  "body": "Part of T-101\nLeft: wire up the webhook\n"}]))
        self.put([row("T-101", status="Building", Updated=ago(9), **{"Next step": "wire up the webhook"})])
        self.assertIn("LEFTOVER T-101", self.tracker("next").stdout)
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CLAIMED T-101 branch feat/t101-thing-2 claim 1111111111111111111111111111111111111111 worktree=", r.stdout)
        self.assert_hook_parses(r.stdout, "feat/t101-thing-2", "1" * 40)
        self.assertIn("acme/app feat/t101-thing-2 main", self.log("cb.log"))
        self.assertEqual(self.rows()["T-101"]["Branch"], "feat/t101-thing-2")

    def test_04e_new_row_whose_derived_branch_is_taken_skips_to_the_next_free_suffix(self):
        self.branch_on_remote("feat/t101-thing", 30)
        self.branch_on_remote("feat/t101-thing-2", 30)
        self.put([row("T-101")])
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.rows()["T-101"]["Branch"], "feat/t101-thing-3")

    def test_05_takeover_stops_old_holder(self):
        self.put([row("T-101", "Alpha")])
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        wt = os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t101-alpha")
        self.assertEqual(self.tracker("check", cwd=wt).returncode, 0)
        rows = self.rows()
        rows["T-101"]["Updated"] = ago(7)  # A went silent
        self.put(list(rows.values()))
        self.branch_on_remote("feat/t101-alpha", 7)
        b = self.tracker("claim", "T-101", who="B")
        self.assertEqual(b.returncode, 0, b.stderr)
        chk = self.tracker("check", cwd=wt)
        self.assertEqual(chk.returncode, 5)
        self.assertIn("taken over by vps:runB:", chk.stderr)
        self.assertIn("(vps, runB)", chk.stderr)
        beat = self.tracker("beat", "T-101", "--next", "still here", cwd=wt)
        self.assertEqual(beat.returncode, 5, beat.stderr)
        self.assertIn("not your row: held by vps:runB:", beat.stderr)
        self.assertNotEqual(self.rows()["T-101"]["Next step"], "still here")

    def test_06_crash_between_branch_and_row(self):
        self.branch_on_remote("feat/t102-other", 10)
        self.put([row("T-101", "First"), row("T-102", "Other")])
        r = self.tracker("next")
        self.assertEqual(r.stdout.splitlines()[0], "NEXT LEFTOVER T-102", r.stdout + r.stderr)
        got = self.rows()["T-102"]
        self.assertEqual((got["Status"], got["Holder"], got["Branch"]), ("Claimed", "", "feat/t102-other"))
        self.assertIn("LEFTOVER T-102", r.stdout)
        # a fresh branch tip: the claimer may still be about to write its row, so it stays held until the lease passes
        self.branch_on_remote("feat/t101-first", 1)
        self.assertIn("HELD T-101 | unknown", self.tracker("next").stdout)

    def test_07_two_writers_409_retry_keeps_both(self):
        self.put([row("T-101", "A"), row("T-102", "B")])
        self.assertEqual(self.tracker("claim", "T-101").returncode, 0)
        self.assertEqual(self.tracker("claim", "T-102", who="B").returncode, 0)
        rows = self.rows()
        rows["T-102"]["Next step"] = "written by B while A was mid-write"
        race = os.path.join(self.d, "race.csv")
        writef(race, render(list(rows.values())))
        wt = os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t101-a")
        r = self.tracker("beat", "T-101", "--next", "from A", cwd=wt, env={"FAKE_GH_RACE": race})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sha conflict, retry 1/5", r.stderr)
        final = self.rows()
        self.assertEqual(final["T-101"]["Next step"], "from A")
        self.assertEqual(final["T-102"]["Next step"], "written by B while A was mid-write")

    def test_08_409_where_other_writer_took_the_row(self):
        self.put([row("T-101", "A")])
        rows = self.rows()
        rows["T-101"].update(Status="Claimed", Holder="other:runX:aaaaaa", Environment="other", Run="runX", Branch="feat/t101-a", Updated=ago(0))
        race = os.path.join(self.d, "race.csv")
        writef(race, render(list(rows.values())))
        r = self.tracker("claim", "T-101", env={"FAKE_GH_RACE": race})
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.rows()["T-101"]["Holder"], "other:runX:aaaaaa")

    def test_09_merged_prs_win(self):
        self.put([row("T-101"), row("T-102"), row("T-103"), row("T-104")])
        prs = [{"number": 12, "headRefName": "feat/t101-thing", "mergedAt": "2026-10-30T10:15:42Z", "body": "## Summary\nx\n- Closes T-101\n"},
               {"number": 13, "headRefName": "feat/t102-thing", "mergedAt": "2026-10-30T11:00:00Z", "body": "Part of T-102\nLeft: wire up the webhook\n"},
               {"number": 14, "headRefName": "feat/t103-thing", "mergedAt": "2026-10-30T12:00:00Z", "body": "**Closes** T-103, T-104\n"}]
        writef(os.path.join(self.fake, "prs.json"), json.dumps(prs))
        r = self.tracker("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("T-101 Not started -> Done (#12 Closes)", r.stdout)
        rows = self.rows()
        self.assertEqual((rows["T-101"]["Status"], rows["T-101"]["PR"], rows["T-101"]["Updated"]), ("Done", "#12", "2026-10-30T10:15Z"))
        self.assertEqual((rows["T-103"]["Status"], rows["T-104"]["Status"]), ("Done", "Done"))
        self.assertEqual((rows["T-102"]["Status"], rows["T-102"]["Holder"], rows["T-102"]["Next step"]), ("Building", "", "wire up the webhook"))
        self.assertEqual(self.log("gh.log").count("-X PUT"), 1)
        out = self.tracker("next").stdout
        self.assertIn("NEXT LEFTOVER T-102", out)
        self.assertIn("LEFTOVER T-102 | Thing |  | #13 | wire up the webhook |", out)

    def test_10_block_notifies_once_and_failed_notify_is_loud(self):
        self.put([row("T-101", "Alpha"), row("T-102", "Beta")])
        self.assertEqual(self.tracker("claim", "T-101").returncode, 0)
        wt = os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t101-alpha")
        r = self.tracker("block", "T-101", "need the DNS login; add it to the vault", "--needs", "founder", cwd=wt)
        self.assertEqual(r.returncode, 0, r.stderr)
        msg = self.log("notify.log")
        for needle in ("proj-a", "T-101", "Alpha", "laptop", "runA", "feat/t101-alpha", "need the DNS login; add it to the vault"):
            self.assertIn(needle, msg)
        self.assertEqual(msg.count("need the DNS login; add it to the vault"), 1)  # the text is printed once, not as blocker and action
        self.assertEqual(self.rows()["T-101"]["Blocker"], "needs founder: need the DNS login; add it to the vault")
        again = self.tracker("block", "T-101", "need the DNS login; add it to the vault", "--needs", "founder", cwd=wt)
        self.assertEqual(again.returncode, 0)
        self.assertIn("already sent", again.stdout)
        self.assertEqual(self.log("notify.log").count("---"), 1)
        # a failed notify: non-zero, the row stays written, and the same command sends again once notify works
        self.assertEqual(self.tracker("claim", "T-102").returncode, 0)
        wt2 = os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t102-beta")
        writef(os.path.join(self.fake, "notify.fail"), "")
        bad = self.tracker("block", "T-102", "need access", "--needs", "stakeholder", cwd=wt2)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("notify failed", bad.stderr)
        self.assertEqual(self.rows()["T-102"]["Status"], "Blocked")
        os.remove(os.path.join(self.fake, "notify.fail"))
        self.assertEqual(self.tracker("block", "T-102", "need access", "--needs", "stakeholder", cwd=wt2).returncode, 0)
        self.assertEqual(self.log("notify.log").count("---"), 2)

    def test_11_row_filter_block_re_and_deps(self):
        self.conf_extra = "ROW_FILTER=owner=eng\nBLOCK_COL='Open Qs'\nBLOCK_RE='Q-[A-Za-z0-9]+'\n"
        self.write_conf()
        self.put([row("T-201", "Free", Milestone="M2"), row("T-202", "Not ours", Owner="Design"), row("T-203", "Asks", **{"Open Qs": "see Q-7"}),
                  row("T-204", "Needs 201", **{"Depends on": "T-201"}), row("T-205", "Dep done", **{"Depends on": "T-206"}),
                  row("T-206", "Finished", status="Done (PR #3)"), row("T-207", "Legacy", status="In progress, someone", Updated=ago(1), Holder="h")])
        out = self.tracker("next").stdout.splitlines()
        self.assertEqual(out[0], "NEXT NEW T-205")  # M1 sorts before M2
        self.assertEqual([l for l in out if l.startswith("NEW")], ["NEW T-205 | Dep done", "NEW T-201 | Free"])
        self.assertIn("NOT READY T-203 | blocked by Q-7", out)
        self.assertIn("NOT READY T-204 | deps not done: T-201", out)
        self.assertFalse([l for l in out if "T-202" in l])
        self.assertTrue([l for l in out if l.startswith("HELD T-207")])
        self.assertEqual(self.tracker("claim", "T-202").returncode, 2)
        self.assertEqual(self.tracker("claim", "T-203").returncode, 2)
        js = json.loads(self.tracker("next", "--json").stdout)
        self.assertEqual(js["next"], {"kind": "NEW", "task": "T-205"})

    def test_12_missing_columns_refuse_then_migrate(self):
        legacy = [h for h in HEADER if h not in ("Holder", "Environment", "Run", "Branch", "PR", "Blocker", "Next step", "Updated")]
        writef(os.path.join(self.fake, "tracker.csv"), render([row("T-101", "Alpha, beta")], header=legacy))
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 2)
        self.assertIn("tracker migrate", r.stderr)
        self.assertEqual(self.tracker("migrate").returncode, 0)
        text = readf(os.path.join(self.fake, "tracker.csv"))
        self.assertEqual(text.splitlines()[0], ",".join(legacy + ["Holder", "Environment", "Run", "Branch", "PR", "Blocker", "Next step", "Updated"]))
        self.assertIn('"Alpha, beta"', text)
        self.assertIn("nothing to migrate", self.tracker("migrate").stdout)
        self.assertEqual(self.tracker("claim", "T-101").returncode, 0)

    def test_13_check_without_claim_file_is_silent_and_offline(self):
        r = self.tracker("check", cwd=self.clones["a"])
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.log("gh.log"), "")

    def test_14b_run_id_falls_back_to_claude_code_session_id(self):
        self.put([row("T-101")])
        e = {k: v for k, v in self.env.items() if k != "TRACKER_RUN"}
        e.update(TRACKER_ENV="laptop", CLAUDE_CODE_SESSION_ID="abc")
        conf = os.path.join(self.d, "conf", "proj-a.conf")
        r = subprocess.run([sys.executable, TRACKER, "claim", "T-101", "--conf", conf], cwd=os.path.join(self.d, "cwd"), env=e,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.rows()["T-101"]
        self.assertEqual(got["Run"], "abc")
        self.assertRegex(got["Holder"], r"^laptop:abc:[0-9a-f]{6}$")

    def test_14_conf_found_by_origin_when_no_project_given(self):
        self.put([row("T-101")])
        e = {**self.env, "TRACKER_ENV": "laptop", "TRACKER_RUN": "runA"}
        os.remove(os.path.join(self.d, "conf", "proj-b.conf"))  # two confs for one repo would be ambiguous
        r = subprocess.run([sys.executable, TRACKER, "next"], cwd=self.clones["a"], env=e, capture_output=True, text=True)
        self.assertEqual(r.stdout.splitlines()[0], "NEXT NEW T-101", r.stderr)

    def test_15_release_gives_row_back_or_keeps_leftover(self):
        self.put([row("T-101", "Alpha"), row("T-102", "Beta")])
        self.tracker("claim", "T-101")
        self.tracker("claim", "T-102")
        wt1 = os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t101-alpha")
        wt2 = os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t102-beta")
        self.assertEqual(self.tracker("release", "T-101", cwd=wt1).returncode, 0)
        got = self.rows()["T-101"]
        self.assertEqual((got["Status"], got["Holder"], got["Branch"], got["Updated"]), ("Not started", "", "", ""))
        self.assertFalse(os.path.exists(os.path.join(sh("git", "rev-parse", "--absolute-git-dir", cwd=wt1), "tracker-claim")))
        writef(os.path.join(self.fake, "keep"), "")  # claim-branch says the branch carries work
        self.assertEqual(self.tracker("release", "T-102", cwd=wt2).returncode, 0)
        got = self.rows()["T-102"]
        self.assertEqual((got["Status"], got["Holder"], got["Branch"], got["Updated"]), ("Building", "", "feat/t102-beta", ""))
        self.assertIn("LEFTOVER T-102", self.tracker("next", who="B").stdout)

    def held_rows(self, n):
        return [row(f"T-{200 + i}", status="Building", Holder="x:y:z", Branch=f"feat/t{200 + i}-held", Updated=ago(1)) for i in range(n)]

    def test_16_builder_cap_refuses_new_claim_with_exit_6(self):
        # 2026-10: up to five builders on one project, the memory reaper killed live eslint/tsc/vitest jobs 6-27 times a day
        self.conf_extra = "MAX_BUILDERS=2\n"
        self.write_conf()
        self.put(self.held_rows(2) + [row("T-101", "Fresh")])
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertIn("T-200", r.stderr)
        self.assertIn("T-201", r.stderr)
        self.assertEqual(self.log("cb.log"), "")
        self.put(self.held_rows(1) + [row("T-101", "Fresh")])  # one below the cap: allowed
        self.assertEqual(self.tracker("claim", "T-101").returncode, 0)

    def test_16b_default_cap_is_three(self):
        self.put(self.held_rows(3) + [row("T-101", "Fresh")])
        self.assertEqual(self.tracker("claim", "T-101").returncode, 6)

    def test_16c_leftover_is_claimable_at_cap(self):
        self.conf_extra = "MAX_BUILDERS=1\n"
        self.write_conf()
        self.branch_on_remote("feat/t101-thing", 9)
        self.put(self.held_rows(1) + [row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(9))])
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_16d_zero_means_no_cap(self):
        self.conf_extra = "MAX_BUILDERS=0\n"
        self.write_conf()
        self.put(self.held_rows(6) + [row("T-101", "Fresh")])
        self.assertEqual(self.tracker("claim", "T-101").returncode, 0)

    def test_16e_next_at_cap_offers_no_new_task_but_still_offers_leftover(self):
        self.conf_extra = "MAX_BUILDERS=2\n"
        self.write_conf()
        self.put(self.held_rows(2) + [row("T-101", "Fresh")])
        r = self.tracker("next")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "NEXT NONE")
        self.assertIn("AT CAP 2 of 2 builders: T-200, T-201", r.stdout)
        js = json.loads(self.tracker("next", "--json").stdout)
        self.assertEqual(js["next"], {"kind": "NONE", "reason": "builder cap", "cap": 2, "held": ["T-200", "T-201"]})
        self.branch_on_remote("feat/t102-old", 9)
        self.put(self.held_rows(2) + [row("T-101", "Fresh"), row("T-102", status="Building", Holder="x:y:z", Branch="feat/t102-old", Updated=ago(9))])
        self.assertEqual(self.tracker("next").stdout.splitlines()[0], "NEXT LEFTOVER T-102")

    def test_17_closes_clears_next_step_and_blocker(self):
        # a Done row kept the "Left: ..." text of an earlier Part-of PR and still read like work was left
        writef(os.path.join(self.fake, "prs.json"), json.dumps([{"number": 12, "headRefName": "feat/t101-thing", "mergedAt": "2026-10-30T10:15:42Z",
                                                                  "body": "Closes T-101\n"}]))
        self.put([row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(1), PR="#5",
                      **{"Next step": "SV1-SV4", "Blocker": "waiting on X"})])
        self.assertEqual(self.tracker("sync").returncode, 0)
        got = self.rows()["T-101"]
        self.assertEqual((got["Status"], got["PR"], got["Next step"], got["Blocker"]), ("Done", "#12", "", ""))


class Dependencies(Sandbox):
    """What keeps a Not started row out of `next`: a dependency in this file, a `<project>:<id>` row of another project's tracker
    (2026-11-27: replaces the GitHub-issue blockers of ISSUE_IMPORT), or a BLOCK_COL match."""

    def other(self, rows):
        writef(os.path.join(self.d, "conf", "other.conf"), "TRACKER_REPO=acme/tracker\nTRACKER_PATH=other.csv\nCODE_REPO=acme/api\nWORK_BASE=main\n")
        writef(os.path.join(self.fake, "other.csv"), render(rows))

    def test_25_block_col_match_is_not_pickable_and_says_why(self):
        self.conf_extra = "ID_RE='#\\d+'\nBLOCK_COL='Blocked by'\nBLOCK_RE='\\S.*'\n"
        self.write_conf()
        self.put([row("#7", "Waits on api", **{"Blocked by": "acme/api#245"}), row("#8", "Free")], header=HEADER + ["Blocked by"])
        out = self.tracker("next").stdout.splitlines()
        self.assertEqual(out[0], "NEXT NEW #8")
        self.assertIn("NOT READY #7 | blocked by acme/api#245", out)
        self.assertEqual(self.tracker("claim", "#7").returncode, 2)

    def test_26_other_project_dep_waits_until_that_row_is_done(self):
        self.other([row("T-344", status="In review")])
        self.put([row("T-5", "Needs the api", **{"Depends on": "other:T-344"}), row("T-344", status="Done")])  # a local T-344 is not the other one
        out = self.tracker("next").stdout.splitlines()
        self.assertIn("NOT READY T-5 | deps not done: other:T-344", out)
        self.other([row("T-344", status="Done")])
        self.assertEqual(self.tracker("next").stdout.splitlines()[0], "NEXT NEW T-5")

    def test_27_local_and_other_project_deps_mix(self):
        self.other([row("T-9", status="Done")])
        self.put([row("T-1"), row("T-5", **{"Depends on": "T-1, other:T-9"})])
        self.assertIn("NOT READY T-5 | deps not done: T-1", self.tracker("next").stdout.splitlines())

    def test_28_unreadable_dep_project_fails_closed(self):
        self.put([row("T-5", **{"Depends on": "nosuch:T-1"}), row("T-6")])
        r = self.tracker("next")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "NEXT NEW T-6")
        self.assertIn("NOT READY T-5 | deps not done: nosuch:T-1", r.stdout.splitlines())
        self.assertIn("dependency project nosuch unreadable", r.stderr)

    def test_29_a_conf_still_on_issue_import_is_refused(self):
        self.conf_extra = "ISSUE_IMPORT=1\n"
        self.write_conf()
        r = self.tracker("next")
        self.assertEqual(r.returncode, 2)
        self.assertIn("ISSUE_IMPORT was removed", r.stderr)


class AddTask(Sandbox):
    """`tracker add`: a CSV row through the same compare-and-swap write."""
    NOTES = HEADER + ["Notes"]
    LINK = "https://github.com/acme/tracker/blob/main/backlog.csv"

    def csv_bytes(self):
        return readf(os.path.join(self.fake, "tracker.csv"), "rb")

    def created(self):
        p = os.path.join(self.fake, "created.json")
        return [json.loads(l) for l in readf(p).splitlines()] if os.path.exists(p) else []

    def test_30_adds_next_id_not_started_notes_and_sorts_after_the_planned_work(self):
        self.put([row("T-101", "First", Milestone="M1"), row("T-102", "Later", Milestone="M3"), row("T-110", "Finished", status="Done", Milestone="M2")],
                 header=self.NOTES)
        before = self.csv_bytes()
        r = self.tracker("add", "  A new   idea ", "--notes", "line one, with comma\nline \"two\"")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["added T-111: A new idea", self.LINK])
        new = self.rows()["T-111"]
        self.assertEqual((new["Title"], new["Status"], new["Notes"], new["Holder"], new["Milestone"]),
                         ("A new idea", "Not started", 'line one, with comma\nline "two"', "", "M3"))
        self.assertEqual(self.log("gh.log").count("-X PUT"), 1)
        self.assertTrue(self.csv_bytes().startswith(before))  # every other row is untouched
        nxt = self.tracker("next").stdout.splitlines()
        self.assertEqual(nxt[0], "NEXT NEW T-101")
        self.assertEqual([l for l in nxt if l.startswith("NEW")], ["NEW T-101 | First", "NEW T-102 | Later", "NEW T-111 | A new idea"])

    def test_31_id_keeps_the_files_own_format(self):
        self.put([row("T-007"), row("T-009", status="Done")])
        self.assertEqual(self.tracker("add", "x").stdout.splitlines()[0], "added T-010: x")
        self.put([row("T-099")])
        self.assertEqual(self.tracker("add", "y").stdout.splitlines()[0], "added T-100: y")
        self.put([row("T-8"), row("T-12")])
        self.assertEqual(self.tracker("add", "z").stdout.splitlines()[0], "added T-13: z")
        self.put([])
        self.assertEqual(self.tracker("add", "first").stdout.splitlines()[0], "added T-1: first")

    def test_32_row_filter_value_is_set_so_next_sees_the_row(self):
        self.conf_extra = "ROW_FILTER=owner=eng\n"
        self.write_conf()
        self.put([row("T-300", "Not ours", Owner="Design"), row("T-200", "Ours")])
        r = self.tracker("add", "Idea")
        self.assertEqual(r.stdout.splitlines()[0], "added T-301: Idea", r.stderr)  # ids are shared with the rows the filter hides
        self.assertEqual(self.rows()["T-301"]["Owner"], "eng")
        self.assertIn("NEW T-301 | Idea", self.tracker("next").stdout.splitlines())

    def test_33_notes_need_a_column_and_the_conf_can_name_it(self):
        self.put([row("T-101")])
        before = self.csv_bytes()
        r = self.tracker("add", "Idea", "--notes", "details")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no notes column", r.stderr)
        self.assertEqual(self.csv_bytes(), before)
        self.assertEqual(self.tracker("add", "Idea").returncode, 0)  # no notes: no column needed
        self.conf_extra = "NOTES_COL='Open Qs'\n"
        self.write_conf()
        self.assertEqual(self.tracker("add", "Second", "--notes", "details").returncode, 0)
        self.assertEqual(self.rows()["T-103"]["Open Qs"], "details")

    def test_34_crlf_file_without_final_newline_is_kept(self):
        text = render([row("T-101", "Keep, this")], eol="\r\n").rstrip("\r\n")
        writef(os.path.join(self.fake, "tracker.csv"), text, "w")
        self.assertEqual(self.tracker("add", "Idea").returncode, 0)
        out = self.csv_bytes().decode()
        self.assertTrue(out.startswith(text + "\r\n"))
        self.assertTrue(out.endswith("\r\n"))
        self.assertEqual(self.rows()["T-102"]["Title"], "Idea")

    def test_35_dry_run_writes_nothing(self):
        self.put([row("T-101")], header=self.NOTES)
        before = self.csv_bytes()
        r = self.tracker("add", "Idea", "--notes", "why", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "dry run: would add T-102: Idea")
        self.assertIn("Notes=why", r.stdout)
        self.assertIn("Status=Not started", r.stdout)
        self.assertEqual(self.csv_bytes(), before)
        self.assertNotIn("PUT", self.log("gh.log"))

    def test_36_empty_title_is_refused(self):
        self.put([row("T-101")])
        r = self.tracker("add", "   ")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("PUT", self.log("gh.log"))

    def test_37_409_retry_picks_the_next_free_id_again(self):
        self.put([row("T-101")])
        race = os.path.join(self.d, "race.csv")
        writef(race, render([row("T-101"), row("T-102", "Added by someone else")]))
        r = self.tracker("add", "Mine", env={"FAKE_GH_RACE": race})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sha conflict, retry 1/5", r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "added T-103: Mine")
        self.assertEqual({k: v["Title"] for k, v in self.rows().items()}, {"T-101": "Thing", "T-102": "Added by someone else", "T-103": "Mine"})

class Unblock(Sandbox):
    """`tracker unblock`: his answer to a Blocked row (or a block that no longer holds) puts it back in the queue, whoever holds it.
    2026-11-07: six BH rows sat Blocked on "an earlier session left uncommitted work" after that work was saved, and only a hand edit
    of the CSV could clear them, so `next` found nothing to build."""
    NOTES = HEADER + ["Notes"]

    def blocked(self, task, **kw):
        return row(task, status="Blocked", Holder="old:run:abc", Environment="vps", Run="run9", Updated=ago(30),
                   Blocker="needs founder: resume that session, or take the work over?", **kw)

    def test_50_row_with_a_branch_becomes_a_leftover_and_keeps_the_answer(self):
        self.put([self.blocked("T-101", Branch="feat/t101-thing", PR="34", Notes="spec")], header=self.NOTES)
        r = self.tracker("unblock", "T-101", "--note", "take it  over", who="B")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "unblocked T-101: leftover, `next` resumes feat/t101-thing first")
        got = self.rows()["T-101"]
        self.assertEqual((got["Status"], got["Holder"], got["Environment"], got["Run"], got["Blocker"], got["Updated"]),
                         ("Building", "", "", "", "", ""))
        self.assertEqual((got["Branch"], got["PR"]), ("feat/t101-thing", "34"))
        self.assertRegex(got["Notes"], r"^spec\nAnswer \d{4}-\d\d-\d\d: take it over$")
        self.assertIn("LEFTOVER T-101", self.tracker("next").stdout)

    def test_51_row_without_a_branch_goes_back_to_not_started(self):
        self.put([self.blocked("T-102")])
        r = self.tracker("unblock", "T-102")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "unblocked T-102: Not started, ready for the next build")
        got = self.rows()["T-102"]
        self.assertEqual((got["Status"], got["Holder"], got["Blocker"], got["Updated"]), ("Not started", "", "", ""))
        self.assertEqual(self.tracker("next").stdout.splitlines()[0], "NEXT NEW T-102")

    def test_52_refuses_a_row_that_is_not_blocked_or_a_note_with_nowhere_to_go(self):
        self.put([row("T-103"), self.blocked("T-104")])
        before = readf(os.path.join(self.fake, "tracker.csv"), "rb")
        r = self.tracker("unblock", "T-103")
        self.assertEqual(r.returncode, 2)
        self.assertIn("T-103 is Not started, not Blocked", r.stderr)
        r = self.tracker("unblock", "T-104", "--note", "answer")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no notes column", r.stderr)
        self.assertEqual(readf(os.path.join(self.fake, "tracker.csv"), "rb"), before)
        self.assertNotIn("-X PUT", self.log("gh.log"))

def iso_now(minutes=0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskContext(Sandbox):
    """2026-11-15 audit: builders started cold. A Part-of PR on a claimed row lost its Left, vague ad-hoc rows were built from a title,
    and the claim printed one line, so each builder re-read the CSV (or guessed). Now: Part-of lands on active rows, a row needs
    criteria to be ready, and claim/show print the task card."""
    AC = HEADER + ["Acceptance criteria", "Tables"]

    def prs(self, *prs):
        writef(os.path.join(self.fake, "prs.json"), json.dumps(list(prs)))

    def test_60_part_of_on_a_claimed_row_keeps_its_pr_and_left(self):
        self.put([row("T-102", status="Claimed", Holder="laptop:runA:abc", Environment="laptop", Run="runA",
                      Branch="feat/t102-thing", Updated=ago(1))])
        self.prs({"number": 13, "headRefName": "feat/t102-thing", "mergedAt": iso_now(), "body": "Part of T-102\nLeft: wire up the webhook\n"})
        r = self.tracker("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "T-102 Claimed: #13 Part of merged, left: wire up the webhook")
        got = self.rows()["T-102"]
        self.assertEqual((got["Status"], got["Holder"], got["PR"], got["Next step"]), ("Claimed", "laptop:runA:abc", "#13", "wire up the webhook"))
        self.assertEqual(self.tracker("sync").stdout.strip(), "nothing to sync")  # idempotent: no write every sync
        self.assertEqual(self.log("gh.log").count("-X PUT"), 1)
        # a beat after the merge is newer than the PR's Left: the next sync must not put the old text back
        self.put([row("T-102", status="Building", Holder="laptop:runA:abc", Branch="feat/t102-thing", PR="#13",
                      **{"Next step": "newer plan", "Updated": iso_now(2)[:16] + "Z"})])
        self.assertEqual(self.tracker("sync").stdout.strip(), "nothing to sync")
        self.assertEqual(self.rows()["T-102"]["Next step"], "newer plan")

    def test_60b_part_of_from_a_side_branch_leaves_the_rows_own_pr(self):
        # 2026-11-22 T-411: side fix #182 (fix/matching-test-listing-expiry, "Part of T-411") merged while the task's own PR #181
        # on feat/t411-* was open. Sync set PR=#182 and Next step to #182's Left, so the card sent the next builder to a merged PR.
        self.put([row("T-102", status="In review", Holder="laptop:runA:abc", Branch="feat/t102-thing", PR="#12",
                      **{"Next step": "review the webhook", "Updated": ago(1)})])
        self.prs({"number": 13, "headRefName": "fix/unrelated-flaky-test", "mergedAt": iso_now(),
                  "body": "Part of T-102\nLeft: the T-102 work itself (PR 12)\n"})
        self.assertEqual(self.tracker("sync").stdout.strip(), "nothing to sync")
        got = self.rows()["T-102"]
        self.assertEqual((got["PR"], got["Next step"]), ("#12", "review the webhook"))
        # a row with no branch yet still takes the PR, as before
        self.put([row("T-102", status="Claimed", Holder="laptop:runA:abc", Updated=ago(1))])
        self.tracker("sync")
        self.assertEqual(self.rows()["T-102"]["PR"], "#13")

    def test_61_add_files_a_vague_task_as_blocked_with_the_reason(self):
        self.put([row("T-101", **{"Acceptance criteria": "returns 200"})], header=self.AC)
        r = self.tracker("add", "Idea")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], "added T-102: Idea (Blocked, not built until answered: no acceptance criteria)")
        got = self.rows()["T-102"]
        self.assertEqual(got["Status"], "Blocked")
        self.assertTrue(got["Blocker"].startswith("needs founder: no acceptance criteria; answer with tracker unblock T-102"), got["Blocker"])
        r = self.tracker("add", "Specd", "--notes", "GET /x returns 404 for an unknown id")
        self.assertEqual(r.stdout.splitlines()[0], "added T-103: Specd")
        self.assertEqual(self.rows()["T-103"]["Status"], "Not started")
        r = self.tracker("add", "Gated", "--notes", "Refund flow; needs the founder's ok on fees")
        self.assertIn("waits on a decision", r.stdout.splitlines()[0])
        self.assertEqual(self.rows()["T-104"]["Status"], "Blocked")
        # his answer makes it ready: unblock --note writes it into the criteria
        self.assertEqual(self.tracker("unblock", "T-104", "--note", "flat 2% fee").returncode, 0)
        self.assertEqual(self.rows()["T-104"]["Status"], "Not started")
        self.assertIn("NEW T-104 | Gated", self.tracker("next").stdout.splitlines())

    def test_62_next_and_claim_hold_a_vague_row_as_not_ready(self):
        self.put([row("T-101"), row("T-102", **{"Acceptance criteria": "needs the founder's ok\nAnswer 2026-11-01: yes, go"}),
                  row("T-103", **{"Acceptance criteria": "Spec first: which plans get it"})], header=self.AC)
        out = self.tracker("next").stdout.splitlines()
        self.assertEqual(out[0], "NEXT NEW T-102")
        self.assertIn("NOT READY T-101 | no acceptance criteria", out)
        self.assertIn("NOT READY T-103 | waits on a decision ('Spec first')", out)
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 2)
        self.assertIn("T-101 is not ready: no acceptance criteria", r.stderr)

    def test_63_claim_prints_the_card_after_the_claimed_line_and_show_reprints_it(self):
        self.put([row("T-100", status="Done"), row("T-101", **{"Acceptance criteria": "returns 200\nwrites an audit row", "Tables": "users",
                                                                "Depends on": "T-100"})], header=self.AC)
        self.prs({"number": 9, "headRefName": "feat/t101-first", "mergedAt": "2026-10-30T10:00:00Z", "body": "Part of T-101\nLeft: the webhook\n"})
        self.assertEqual(self.tracker("sync").returncode, 0)
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].startswith("CLAIMED T-101 branch feat/t101-thing claim 1111"), lines[0])
        card = lines[1:]
        self.assertEqual(card[0], "TASK CARD T-101 | Thing")
        for want in ("Acceptance criteria:", "  returns 200", "  writes an audit row", "Tables: users", "Depends on: T-100",
                     "Next step: the webhook", "Earlier PRs:", "  #9 Part of (merged 2026-10-30) from feat/t101-first: left: the webhook"):
            self.assertIn(want, card)
        self.assertFalse(any(re.search(r"\bbranch\s", l) for l in card), card)  # token-guard takes the first `branch <name>` of a prompt
        puts = self.log("gh.log").count("-X PUT")
        s = self.tracker("show", "T-101")
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertIn("TASK CARD T-101 | Thing", s.stdout)
        self.assertIn("Branch: feat/t101-thing", s.stdout)
        self.assertEqual(self.log("gh.log").count("-X PUT"), puts)  # read-only
        d = json.loads(self.tracker("show", "T-101", "--json").stdout)
        self.assertEqual(d["prs"][0]["left"], "the webhook")


class SetParkReopen(Sandbox):
    """2026-11-27 audit: with no command to change a field, park or cancel a task, agents rewrote the CSV with python and the auto-mode
    classifier refused it (13 denials in 5 phone sessions); 3 of 26 Blocked rows were parked or cancelled tasks kept as Blocked."""
    NOTES = HEADER + ["Notes"]

    def puts(self):
        return self.log("gh.log").count("-X PUT")

    def test_70_set_changes_one_cell_and_refuses_the_columns_other_commands_own(self):
        self.put([row("T-101", "keep, this", **{"Next step": "old"}), row("T-102", "é\nz", Updated="2026-11-01T10:00Z")])
        before = readf(os.path.join(self.fake, "tracker.csv"))
        r = self.tracker("set", "T-102", "--field", "next step", "--value", "wire the webhook, then test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "set T-102 Next step")
        got = self.rows()["T-102"]
        self.assertEqual((got["Next step"], got["Updated"], got["Status"]), ("wire the webhook, then test", "2026-11-01T10:00Z", "Not started"))
        after = readf(os.path.join(self.fake, "tracker.csv"))
        self.assertEqual(after.split("\nT-102")[0], before.split("\nT-102")[0])  # header and T-101 byte-identical
        n = self.puts()
        r = self.tracker("set", "T-102", "--field", "Next step", "--value", "wire the webhook, then test")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "T-102 Next step unchanged"))
        self.assertEqual(self.puts(), n)
        for field, want in (("Nope", "no 'Nope' column"), ("Status", "tracker park"), ("Holder", "claim and release"),
                            ("Updated", "claim and release"), ("Task ID", "the task id")):
            r = self.tracker("set", "T-102", "--field", field, "--value", "x")
            self.assertEqual(r.returncode, 2, field)
            self.assertIn(want, r.stderr, field)
        self.assertEqual(self.tracker("set", "T-999", "--field", "PR", "--value", "#1").returncode, 2)
        self.assertEqual(self.puts(), n)

    def test_71_park_and_cancel_leave_the_blocked_list_and_stop_the_holder(self):
        self.put([row("T-101", status="Blocked", Blocker="needs founder: Parked by founder 10 Nov", Holder="old:run:abc", Updated=ago(30)),
                  row("T-102", "Alpha"), row("T-103", status="Done", PR="#4"), row("T-104")])
        r = self.tracker("claim", "T-102")
        self.assertEqual(r.returncode, 0, r.stderr)
        wt = os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t102-alpha")
        p = self.tracker("park", "T-101", "--note", "after the launch", who="B")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "parked T-101"), p.stderr)
        c = self.tracker("cancel", "T-102", "--note", "superseded by T-104", who="B")
        self.assertEqual((c.returncode, c.stdout.strip()), (0, "cancelled T-102"), c.stderr)
        rows = self.rows()
        self.assertEqual((rows["T-101"]["Status"], rows["T-101"]["Holder"]), ("Parked", ""))
        self.assertRegex(rows["T-101"]["Blocker"], r"^parked \d{4}-\d\d-\d\d: after the launch$")
        self.assertEqual((rows["T-102"]["Status"], rows["T-102"]["Holder"], rows["T-102"]["Branch"]), ("Cancelled", "", "feat/t102-alpha"))
        self.assertRegex(rows["T-102"]["Blocker"], r"^cancelled \d{4}-\d\d-\d\d: superseded by T-104$")
        out = self.tracker("next").stdout
        self.assertEqual(out.splitlines()[0], "NEXT NEW T-104", out)
        self.assertNotIn("T-101", out)
        self.assertNotIn("T-102", out)
        self.assertEqual(self.tracker("check", cwd=wt).returncode, 5)  # the builder of a cancelled task is told to stop
        self.assertIn("is Parked", self.tracker("claim", "T-101").stderr)
        r = self.tracker("park", "T-103", "--note", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("T-103 is Done", r.stderr)
        self.assertEqual(self.tracker("park", "T-104").returncode, 2)  # a reason is required

    def test_72_reopen_puts_done_parked_or_cancelled_rows_back(self):
        self.put([row("T-101", status="Parked", Branch="feat/t101-thing", PR="#5", Blocker="parked 2026-11-10: later"),
                  row("T-102", status="Cancelled", Blocker="cancelled 2026-11-10: no"),
                  row("T-103", status="Done", Branch="feat/t103-thing", PR="#7", Notes="spec"), row("T-104"),
                  row("T-105", status="canceled")], header=self.NOTES)
        r = self.tracker("reopen", "T-101")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "reopened T-101: leftover, `next` resumes feat/t101-thing first"), r.stderr)
        got = self.rows()["T-101"]
        self.assertEqual((got["Status"], got["Holder"], got["Blocker"], got["Branch"], got["PR"]), ("Building", "", "", "feat/t101-thing", "#5"))
        r = self.tracker("reopen", "T-102")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "reopened T-102: Not started, ready for the next build"), r.stderr)
        self.assertEqual((self.rows()["T-102"]["Status"], self.rows()["T-102"]["Blocker"]), ("Not started", ""))
        r = self.tracker("reopen", "T-103", "--note", "the export  broke again")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.rows()["T-103"]
        self.assertEqual((got["Status"], got["Branch"], got["PR"]), ("Not started", "", ""))  # a merged branch is not resumed
        self.assertRegex(got["Notes"], r"^spec\nReopened \d{4}-\d\d-\d\d: the export broke again$")
        self.assertEqual(self.tracker("reopen", "T-105").returncode, 0)  # the US spelling counts as Cancelled
        r = self.tracker("reopen", "T-104")
        self.assertEqual(r.returncode, 2)
        self.assertIn("T-104 is Not started, not Done, Parked or Cancelled", r.stderr)


if __name__ == "__main__":
    unittest.main()
