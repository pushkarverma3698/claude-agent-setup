#!/usr/bin/env python3
"""Tests for claim-branch --resume refusing while another worktree has unpushed work: python3 -m unittest test_claim_branch
(local git only; a fake gh stands in for the GitHub API)."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# the repo's copy, else the installed one (on the VPS the tests sit in ~/work, claim-branch in /usr/local/bin)
CLAIM = next(p for p in (os.path.join(HERE, "..", "bin", "claim-branch"), shutil.which("claim-branch") or "") if os.path.exists(p))
FAKE_GH = """#!/usr/bin/env bash
case "$*" in
  *"git/ref/heads/"*) echo 1111111111111111111111111111111111111111 ;;
  *"PATCH"*) ;;
  *"--jq .tree.sha"*) echo 2222222222222222222222222222222222222222 ;;
  *"git/commits -f"*) echo CLAIMSHA ;;
  *"git/commits/"*) echo "feat(x):_work 2026-01-01T00:00:00Z" ;;
  *) echo "fake gh: unexpected $*" >&2; exit 1 ;;
esac
"""


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class ResumeRespectsWorktrees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        self.g("init", "-q", "--bare", os.path.join(d, "owner", "repo.git"))
        self.g("clone", "-q", os.path.join(d, "owner", "repo.git"), os.path.join(d, "work", "repo"))
        self.clone = os.path.join(d, "work", "repo")
        write(os.path.join(self.clone, "a.txt"), "1")
        self.g("add", ".", cwd=self.clone)
        self.g("commit", "-qm", "c1", cwd=self.clone)
        self.g("push", "-q", "-u", "origin", "HEAD:main", cwd=self.clone)
        self.wt = os.path.join(self.clone, ".claude", "worktrees", "t1-x")
        self.g("worktree", "add", "-q", "-b", "feat/t1-x", self.wt, cwd=self.clone)
        self.g("push", "-q", "-u", "origin", "feat/t1-x", cwd=self.wt)
        os.makedirs(os.path.join(d, "bin"))
        gh = os.path.join(d, "bin", "gh")
        write(gh, FAKE_GH)
        os.chmod(gh, 0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def g(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd or self.d,
                              check=True, capture_output=True)

    def resume(self, cwd):
        env = dict(os.environ, HOME=self.d, PATH=os.path.join(self.d, "bin") + os.pathsep + os.environ["PATH"])
        env.pop("CLAIM_REPO_DIR", None)
        return subprocess.run([CLAIM, "--resume", "owner/repo", "feat/t1-x"], cwd=cwd, env=env,
                              capture_output=True, text=True)

    def test_refused_while_another_worktree_is_dirty(self):
        write(os.path.join(self.wt, "b.txt"), "2")
        r = self.resume(self.d)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn(".claude/worktrees/t1-x: 0 commit(s) ahead, 1 uncommitted file(s)", r.stderr)

    def test_refused_while_another_worktree_has_unpushed_commits(self):
        self.g("commit", "--allow-empty", "-qm", "c2", cwd=self.wt)
        r = self.resume(self.clone)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("1 commit(s) ahead", r.stderr)

    def test_allowed_when_clean(self):
        r = self.resume(self.d)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "CLAIMSHA"), r.stderr)

    def test_allowed_from_its_own_worktree(self):
        write(os.path.join(self.wt, "b.txt"), "2")
        r = self.resume(self.wt)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "CLAIMSHA"), r.stderr)


NEW_GH = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
case "$*" in
  *"git/ref/heads/task/issue-42-login "*) exit 1 ;;
  *"git/ref/heads/production "*) echo PRODSHA ;;
  *"git/ref/heads/beta "*) echo BETASHA ;;
  *"git/matching-refs/"*) echo "refs/heads/task/issue-42-old=OLDSHA" ;;
  *"compare/"*) echo 0 ;;
  *"git/commits/OLDSHA "*) echo "feat: merged work" ;;
  *"--jq .tree.sha"*) echo TREE ;;
  *"git/commits -f"*) echo CLAIMSHA ;;
  *"git/refs -f"*) ;;
  *) echo "fake gh: unexpected $*" >&2; exit 1 ;;
esac
"""


RELEASE_GH = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$*" in
  "pr list"*) echo "$FAKE_PRS" ;;
  *"-X DELETE"*) ;;
  *"git/ref/heads/"*) [ "$FAKE_NOBRANCH" = 1 ] && exit 1; echo 1111111111111111111111111111111111111111 ;;
  *"git/commits/"*) echo "$FAKE_SUBJECT" ;;
  *) echo "fake gh: unexpected $*" >&2; exit 1 ;;
esac
"""


class NewClaimAnyRepo(unittest.TestCase):
    """2026-10-30: the same-task check was limited to one repo (feat/t<id>- prefix, its integration branch)."""
    def run_claim(self, policy):
        d = self.tmp.name
        write(os.path.join(d, "bin", "gh"), NEW_GH)
        os.chmod(os.path.join(d, "bin", "gh"), 0o755)
        write(os.path.join(d, "policy.json"), policy)
        env = dict(os.environ, HOME=d, GH_LOG=os.path.join(d, "gh.log"), ROUTINE_POLICY=os.path.join(d, "policy.json"),
                   PATH=os.path.join(d, "bin") + os.pathsep + os.environ["PATH"])
        env.pop("INTEGRATION", None)
        r = subprocess.run([CLAIM, "o/api", "task/issue-42-login", "production"], cwd=d, env=env,
                           capture_output=True, text=True)
        with open(os.path.join(d, "gh.log")) as f:
            return r, f.read()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "bin"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_issue_branch_is_checked_for_same_task_holders_against_the_policy_merge_base(self):
        r, log = self.run_claim('{"repos":{"o/api":{"bases":["beta","production"]}}}')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "CLAIMSHA"), r.stderr)
        self.assertIn("git/matching-refs/heads/task/issue-42- ", log)
        self.assertIn("compare/BETASHA...OLDSHA", log)

    def test_repo_without_a_policy_entry_uses_the_claim_base(self):
        r, log = self.run_claim('{"repos":{}}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("compare/PRODSHA...OLDSHA", log)


class Release(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "bin"))
        gh = os.path.join(self.tmp.name, "bin", "gh")
        write(gh, RELEASE_GH)
        os.chmod(gh, 0o755)
        self.log = os.path.join(self.tmp.name, "calls.log")

    def tearDown(self):
        self.tmp.cleanup()

    def release(self, subject="chore(claim): reserve b for one session", prs="0", nobranch="0"):
        env = dict(os.environ, PATH=os.path.join(self.tmp.name, "bin") + os.pathsep + os.environ["PATH"],
                   FAKE_SUBJECT=subject, FAKE_PRS=prs, FAKE_NOBRANCH=nobranch, FAKE_LOG=self.log)
        r = subprocess.run([CLAIM, "--release", "owner/repo", "feat/t1-x"], env=env, capture_output=True, text=True)
        calls = open(self.log).read() if os.path.exists(self.log) else ""
        return r, "-X DELETE" in calls

    def test_claim_only_branch_without_pr_is_deleted(self):
        r, deleted = self.release()
        self.assertEqual((r.returncode, r.stdout.strip(), deleted), (0, "released", True), r.stderr)

    def test_branch_with_work_is_kept(self):
        r, deleted = self.release(subject="feat(x): work (T-1)")
        self.assertEqual((r.returncode, deleted), (3, False), r.stderr)

    def test_branch_with_a_pr_is_kept(self):
        r, deleted = self.release(prs="1")
        self.assertEqual((r.returncode, deleted), (3, False), r.stderr)

    def test_missing_branch_is_fine(self):
        r, deleted = self.release(nobranch="1")
        self.assertEqual((r.returncode, deleted), (0, False), r.stderr)

    def test_usage(self):
        r = subprocess.run([CLAIM, "--release", "owner/repo"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
