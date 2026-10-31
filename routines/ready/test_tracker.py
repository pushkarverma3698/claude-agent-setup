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
    raw = open(csvp, "rb").read()
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
  --resume) echo "CLAIM_HOURS=${CLAIM_HOURS:-12}" >> "$FAKE_GH_DIR/cb.log"   # claim-branch refuses a claim commit younger than CLAIM_HOURS
            [ -f "$FAKE_GH_DIR/tip_age" ] && [ "$(cat "$FAKE_GH_DIR/tip_age")" -lt "${CLAIM_HOURS:-12}" ] && { echo "claim-branch: claimed $(cat "$FAKE_GH_DIR/tip_age")h ago, someone holds the task" >&2; exit 3; }
            echo claimsha ;;
  *) git -C "$FAKE_ORIGIN" branch "$2" main && echo claimsha ;;
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
        for k in ("CLAUDE_CODE_HOST_SESSION_ID", "AGENT_OVERLAY_DIR", "TRACKER_PROJECT", "AGENT_SETUP_ENV"):
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

    def test_04_leftover_first_then_resume(self):
        self.branch_on_remote("feat/t101-thing", 9)
        self.put([row("T-101", status="Building", Holder="x:y:z", Branch="feat/t101-thing", Updated=ago(9)), row("T-102", "Next one")])
        c = self.tracker("claim", "T-102")
        self.assertEqual(c.returncode, 4, c.stderr)
        self.assertIn("leftover first: T-101", c.stderr)
        r = self.tracker("claim", "T-101")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--resume acme/app feat/t101-thing", self.log("cb.log"))
        got = self.rows()["T-101"]
        self.assertEqual((got["Status"], got["Branch"], got["Environment"], got["Run"]), ("Claimed", "feat/t101-thing", "laptop", "runA"))
        self.assertRegex(got["Holder"], r"^laptop:runA:[0-9a-f]{6}$")
        wt = os.path.join(self.clones["a"], ".claude", "worktrees", "feat-t101-thing")
        claim = json.loads(readf(os.path.join(sh("git", "rev-parse", "--absolute-git-dir", cwd=wt), "tracker-claim")))
        self.assertEqual((claim["task"], claim["holder"], claim["project"]), ("T-101", got["Holder"], "proj-a"))
        self.assertEqual(self.tracker("next", who="B").stdout.splitlines()[0], "NEXT NEW T-102")

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


if __name__ == "__main__":
    unittest.main()
