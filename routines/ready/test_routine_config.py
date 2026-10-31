#!/usr/bin/env python3
"""Tests for the per-project routine config (2026-10-31): python3 routines/ready/test_routine_config.py
  - routine-schedule: SCHEDULE="03:04 15:00" becomes one OnCalendar line per time (and the timer drop-in), a calendar string
    stays one line, garbage is refused
  - claude-routine slot_busy: a timer slot that fires while the lead is working prints one line and skips (bash 4+, GNU date)
  - routine-new on an EMPTY code repo with a fake gh/ssh: first commit, integration branch, private tracker repo seeded,
    protection, conf with routine + tracker keys, no GitHub labels
No network: gh, ssh and systemd-analyze are fakes on PATH.
"""
import base64
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SCHEDULE = os.path.join(ROOT, "routines", "bin", "routine-schedule")
ROUTINE = os.path.join(ROOT, "routines", "bin", "claude-routine")
NEW = os.path.join(ROOT, "routine-new")


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


def extract(script, *names):
    """The text of the top-level bash functions `name() {` … `^}` from the script."""
    out, grab = [], False
    for line in read(script).splitlines():
        if any(line.startswith(n + "() {") for n in names):
            grab = True
        if grab:
            out.append(line)
            if line == "}":
                grab = False
    return "\n".join(out)


def read(path):
    with open(path) as f:
        return f.read()


def gnu_bash():
    r = subprocess.run(["bash", "-c", 'echo "${BASH_VERSINFO[0]}"; date -d "2026-01-01T00:00:00Z" +%s'], capture_output=True, text=True)
    lines = r.stdout.split()
    return r.returncode == 0 and len(lines) == 2 and int(lines[0]) >= 4


def sched(*args, path):
    return subprocess.run([SCHEDULE, *args], capture_output=True, text=True, env=dict(os.environ, PATH=path))


class ScheduleParsing(unittest.TestCase):
    def setUp(self):
        # a PATH with no systemd-analyze (macOS) so the format checks are what is tested
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = self.tmp.name
        for tool in ("bash", "env", "cat", "printf"):
            p = shutil.which(tool)
            if p:
                os.symlink(p, os.path.join(self.bin, tool))

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_time(self):
        r = sched("03:04", path=self.bin)
        self.assertEqual((r.returncode, r.stdout), (0, "*-*-* 03:04:00 UTC\n"), r.stderr)

    def test_two_times_and_zone(self):
        r = sched("03:04 15:00", "Europe/Amsterdam", path=self.bin)
        self.assertEqual((r.returncode, r.stdout), (0, "*-*-* 03:04:00 Europe/Amsterdam\n*-*-* 15:00:00 Europe/Amsterdam\n"), r.stderr)

    def test_calendar_string_stays_one_line(self):
        for legacy in ("*-*-* 02:00:00 UTC", "Mon..Fri *-*-* 02:00:00 UTC", "*-*-* *:00/30:00"):
            r = sched(legacy, "Asia/Kolkata", path=self.bin)  # SCHEDULE_TZ is ignored for it
            self.assertEqual((r.returncode, r.stdout), (0, legacy + "\n"), r.stderr)

    def test_garbage_is_refused(self):
        for bad in ("", "  ", "3:04", "24:00", "03:60", "03:04,15:00", "0304", "noon", "03:04 junk"):
            r = sched(bad, path=self.bin)
            self.assertEqual(r.returncode, 2, f"{bad!r}: {r.stdout} {r.stderr}")
            self.assertEqual(r.stdout, "")
            self.assertIn("routine-schedule:", r.stderr)

    def test_garbage_zone_is_refused(self):
        r = sched("03:04", "UTC; touch x", path=self.bin)
        self.assertEqual(r.returncode, 2)
        self.assertIn("zone", r.stderr)

    def test_systemd_has_the_last_word(self):
        write(f"{self.bin}/systemd-analyze", '#!/bin/sh\n[ "$2" = "*-*-* 03:04:00 Nowhere/Land" ] && exit 1\nexit 0\n', 0o755)
        r = sched("03:04", "Nowhere/Land", path=self.bin)
        self.assertEqual(r.returncode, 2)
        self.assertIn("systemd does not accept", r.stderr)
        self.assertEqual(sched("03:04", "UTC", path=self.bin).returncode, 0)

    def test_dropin_clears_the_template_then_lists_every_time(self):
        r = sched("--dropin", "03:04 15:00", path=self.bin)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "[Timer]\nOnCalendar=\nOnCalendar=*-*-* 03:04:00 UTC\nOnCalendar=*-*-* 15:00:00 UTC\n")

    def test_dropin_for_a_calendar_string_is_one_line(self):
        r = sched("--dropin", "*-*-* 02:00:00 UTC", path=self.bin)
        self.assertEqual(r.stdout, "[Timer]\nOnCalendar=\nOnCalendar=*-*-* 02:00:00 UTC\n")


@unittest.skipUnless(gnu_bash(), "needs bash 4+ and GNU date (the VPS)")
class SlotSkip(unittest.TestCase):
    def run_slot(self, state, age_min):
        d = tempfile.mkdtemp()
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            ts = (now - datetime.timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
            write(f"{d}/state.json", '{"state": "%s"}' % state)
            write(f"{d}/t.jsonl", '{"type":"assistant","timestamp":"%s"}\n' % ts)
            funcs = extract(ROUTINE, "newest_record", "live_run", "slot_busy")
            r = subprocess.run(["bash", "-c", f'set -uo pipefail\n{funcs}\nslot_busy demo alt abc123 {d}/state.json {d}/t.jsonl; echo "rc=$?"'],
                               capture_output=True, text=True)
            return r.stdout.strip().splitlines()
        finally:
            shutil.rmtree(d)

    def test_live_lead_skips_with_one_line_naming_the_run(self):
        out = self.run_slot("working", 10)
        self.assertEqual(len(out), 2, out)
        self.assertRegex(out[0], r"^claude-routine: slot skipped, demo run abc123 \(alt\) is still working \(last record \d+ min ago\)")
        self.assertEqual(out[1], "rc=0")

    def test_stale_or_stopped_lead_does_not_skip(self):
        self.assertEqual(self.run_slot("working", 90), ["rc=1"])
        self.assertEqual(self.run_slot("stopped", 1), ["rc=1"])


class SlotSkipWiring(unittest.TestCase):
    def test_the_start_loop_exits_zero_on_a_busy_slot(self):
        src = read(ROUTINE)
        self.assertIn('slot_busy "$name" "$p" "$id" "$st" "$f"; then exit 0; fi', src)


FAKE_GH = r'''#!/usr/bin/env python3
"""A fake gh with one state file ($FAKE_STATE): repos, branches, files, calls."""
import json, os, sys
SP = os.environ["FAKE_STATE"]
S = json.load(open(SP))
a = sys.argv[1:]
S["calls"].append(" ".join(a))
def save(): json.dump(S, open(SP, "w"))
def flag(name):
    return a[a.index(name) + 1] if name in a else None
def field(prefix):
    return next(x for x in a if x.startswith(prefix))[len(prefix):]
def fail(msg, code=1):
    save(); sys.stderr.write(msg + "\n"); sys.exit(code)
def out(s=""):
    save(); print(s); sys.exit(0)
if a[:2] == ["repo", "view"]:
    r = a[2]
    if r not in S["repos"]: fail("Could not resolve to a Repository")
    out("ADMIN" if "-q" in a else json.dumps({"name": r}))
if a[:2] == ["repo", "create"]:
    S["repos"][a[2]] = {"private": "--private" in a, "default": "main", "branches": {}, "files": {}}
    out()
if a[0] == "api":
    method = flag("-X") or "GET"
    path = next(x for x in a[1:] if x.startswith("repos/"))
    parts = path.split("/")
    repo = "/".join(parts[1:3]); rest = "/".join(parts[3:])
    R = S["repos"].get(repo) or fail("Not Found")
    if rest == "":
        out(R["default"])
    if rest == "branches":
        out(str(len(R["branches"])))
    if rest.startswith("branches/") and rest.endswith("/protection"):
        b = rest[len("branches/"):-len("/protection")]
        if method == "PUT":
            R.setdefault("protection", {})[b] = json.loads(sys.stdin.read()); out()
        if b in R.get("protection", {}): out("{}")
        fail("gh: Branch not protected (HTTP 404)")
    if rest.startswith("branches/"):
        b = rest[len("branches/"):]
        if b in R["branches"]: out()
        fail("Branch not found (HTTP 404)")
    if rest.startswith("git/ref/heads/"):
        b = rest[len("git/ref/heads/"):]
        out(R["branches"][b]) if b in R["branches"] else fail("Not Found")
    if rest == "git/refs" and method == "POST":
        R["branches"][field("ref=refs/heads/")] = field("sha=")
        out()
    if rest.startswith("contents/"):
        f = rest[len("contents/"):]
        if method == "PUT":
            R["files"][f] = field("content=")
            if not R["branches"]: R["branches"][R["default"]] = "0000001"
            out()
        if f in R["files"]: out("fakesha")
        fail("Not Found (HTTP 404)")
fail("fake gh: unhandled " + " ".join(a), 99)
'''

FAKE_SSH = r'''#!/usr/bin/env python3
"""A fake ssh: runs nothing; logs the command and stdin, answers the few questions routine-new asks."""
import os, sys
cmd = sys.argv[-1]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["FAKE_SSH_LOG"], "a") as f:
    f.write("CMD " + cmd + "\n")
if "viewerPermission" in stdin:
    print("ADMIN")
elif "git clone" in stdin:
    print("5. cloned (fake)")
elif "cat >" in cmd and ".md" in cmd:
    open(os.environ["FAKE_JOB_OUT"], "w").write(stdin)
sys.exit(0)
'''

FAKE_TRACKER = r'''#!/usr/bin/env bash
echo "$*" >> "$FAKE_TRACKER_LOG"
case $1 in
  migrate) echo "added columns: Holder, Environment" ;;
  next) echo "NEXT NEW T-1"; echo "NEW T-1 | Replace this example row with your first task" ;;
esac
'''


class RoutineNewEmptyRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        self.bin = f"{d}/bin"
        write(f"{self.bin}/gh", FAKE_GH, 0o755)
        write(f"{self.bin}/ssh", FAKE_SSH, 0o755)
        write(f"{self.bin}/tracker", FAKE_TRACKER, 0o755)
        write(f"{self.bin}/systemd-analyze", "#!/bin/sh\nexit 0\n", 0o755)
        self.state = f"{d}/state.json"
        write(self.state, json.dumps({"calls": [], "repos": {"acme/shop": {"private": True, "default": "main", "branches": {}, "files": {}}}}))
        self.confs = f"{d}/confs"
        os.makedirs(self.confs)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=self.state, FAKE_SSH_LOG=f"{d}/ssh.log",
                        FAKE_JOB_OUT=f"{d}/job.md", FAKE_TRACKER_LOG=f"{d}/tracker.log", TRACKER_BIN=f"{self.bin}/tracker",
                        AGENT_RULES_HOST="vps-test", AGENT_SETUP_ENV=f"{d}/none", ROUTINE_CONF_DIR=self.confs)
        self.env.pop("AGENT_OVERLAY_DIR", None)

    def tearDown(self):
        self.tmp.cleanup()

    def new(self, *args):
        return subprocess.run([NEW, *args], capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL)

    def st(self):
        return json.loads(read(self.state))

    def conf(self, name="shop-daily"):
        return read(f"{self.confs}/{name}.conf")

    def test_empty_repo_two_times_no_labels(self):
        r = self.new("acme/shop", "beta", "03:04 15:00", "--no-deploy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        s = self.st()
        shop, trk = s["repos"]["acme/shop"], s["repos"]["acme/shop-tracker"]
        # the empty repo got a first commit, then the integration branch cut from it
        self.assertIn("README.md", shop["files"])
        self.assertEqual(sorted(shop["branches"]), ["beta", "main"])
        self.assertEqual(shop["branches"]["beta"], shop["branches"]["main"])
        self.assertIn("beta", shop["protection"])
        # the tracker repo is private and seeded with the header and one example row
        self.assertTrue(trk["private"])
        lines = base64.b64decode(trk["files"]["backlog.csv"]).decode().splitlines()
        self.assertEqual(lines[0], "Task ID,Title,Status,Depends on,Milestone")
        self.assertTrue(lines[1].startswith("T-1,"))
        self.assertEqual(len(lines), 2)
        # no labels anywhere
        self.assertNotIn("label", "\n".join(s["calls"]))
        self.assertNotIn("gh label", read(f"{self.d}/job.md"))
        self.assertNotIn("--label", read(f"{self.d}/job.md"))
        # the conf carries routine keys and tracker keys
        c = self.conf()
        for want in ('SCHEDULE="03:04 15:00"', "SCHEDULE_TZ=UTC", "REPO=shop", "BASE=beta", "VERDICT=shop-daily",
                     "VERDICT_AT=21:00:00", "TRACKER_REPO=acme/shop-tracker", "TRACKER_PATH=backlog.csv", "CODE_REPO=acme/shop",
                     "WORK_BASE=beta", r"GUARD_REPOS=('acme/shop|bases=beta|branch=^feat/t(\d+)-')"):
            self.assertIn(want, c)
        chk = subprocess.run(["bash", "-c", 'source "$1"; echo "$TRACKER_REPO $SCHEDULE ${GUARD_REPOS[0]}"', "_", f"{self.confs}/shop-daily.conf"],
                             capture_output=True, text=True)
        self.assertEqual(chk.stdout.strip(), r"acme/shop-tracker 03:04 15:00 acme/shop|bases=beta|branch=^feat/t(\d+)-")
        # migrate ran on the draft conf before anything was written, then next as the last step
        tl = read(f"{self.d}/tracker.log").splitlines()
        self.assertTrue(tl[0].startswith("migrate --project shop-daily --conf "), tl)
        self.assertTrue(tl[-1].startswith(f"next --project shop-daily --conf {self.confs}/shop-daily.conf"), tl)
        self.assertIn("NEXT NEW T-1", r.stdout)
        self.assertIn("Done.", r.stdout)

    def test_second_run_refuses_to_overwrite(self):
        self.assertEqual(self.new("acme/shop", "beta", "03:04", "--no-deploy").returncode, 0)
        r = self.new("acme/shop", "beta", "03:04", "--no-deploy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already exists", r.stderr)

    def test_garbage_schedule_changes_nothing(self):
        r = self.new("acme/shop", "beta", "3:04 noon", "--no-deploy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bad schedule", r.stderr)
        self.assertEqual(self.st()["calls"], [])
        self.assertEqual(os.listdir(self.confs), [])

    def test_tracker_repo_must_differ_from_code_repo(self):
        r = self.new("acme/shop", "beta", "03:04", "--tracker-repo", "acme/shop", "--no-deploy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("different repo", r.stderr)

    def test_failed_migrate_writes_no_conf(self):
        write(f"{self.bin}/tracker", "#!/bin/sh\necho 'no write access' >&2\nexit 2\n", 0o755)
        r = self.new("acme/shop", "beta", "03:04", "--no-deploy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tracker migrate failed", r.stderr)
        self.assertEqual(os.listdir(self.confs), [])

    def test_calendar_string_needs_verdict_time(self):
        r = self.new("acme/shop", "beta", "*-*-* 02:00:00 UTC", "--no-deploy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--verdict-at", r.stderr)
        r = self.new("acme/shop", "beta", "*-*-* 02:00:00 UTC", "--verdict-at", "06:00:00", "--no-deploy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('SCHEDULE="*-*-* 02:00:00 UTC"', self.conf())
        self.assertNotIn("SCHEDULE_TZ", self.conf())

    def test_verdict_time_follows_the_zone(self):
        r = self.new("acme/shop", "beta", "09:00 21:30", "--tz", "Asia/Kolkata", "--name", "shop", "--no-deploy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # 21:30 IST = 16:00 UTC, +6 h = 22:00 UTC
        self.assertIn("VERDICT_AT=22:00:00", self.conf("shop"))


if __name__ == "__main__":
    unittest.main()
