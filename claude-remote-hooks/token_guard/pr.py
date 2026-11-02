"""gh pr create: the repo's base, the body sections (and task format), and for closes=issue repos the head branch."""
import os, re

from .common import deny, head_of, repo_of
from .policy import CLAIMED, CLOSES_ISSUE, PR_BASES, body_missing, claim_cmd, task_format


def check_pr_create(cmd, cwd):
    if not re.search(r"\bgh\s+pr\s+create\b", cmd):
        return
    repo = repo_of(cmd, cwd)
    if not repo:
        return
    m = re.search(r"(?:--base|-B)[ =]['\"]?([^\s'\"]+)", cmd)
    if not m or m.group(1) not in PR_BASES[repo]:
        deny(f"PRs in {repo} target `{PR_BASES[repo][0]}`: add `--base {PR_BASES[repo][0]}`.")
    if m.group(1) == "production":
        return  # the nightly promotion PR (beta -> production) is a checklist, never merged by an agent
    body = cmd
    f = re.search(r"(?:--body-file|-F)[ =]['\"]?([^\s'\"]+)", cmd)
    if f:
        try:
            with open(os.path.join(cwd, os.path.expanduser(f.group(1))), encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            return  # allow-failopen: the file is written by an earlier step of the same command
    missing = body_missing(repo, body)
    if missing:
        deny(f"the PR body for {repo} needs: {'; '.join(missing)}. Write it to a file, then `--body-file`."
             + task_format(repo))
    if repo in CLOSES_ISSUE:
        check_issue_head(cmd, cwd, repo, body)


ISSUE_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b")


def check_issue_head(cmd, cwd, repo, body):
    issues = sorted(set(ISSUE_RE.findall(body)), key=int)
    if not issues:
        return
    head = head_of(cmd, cwd)
    m = re.search(CLAIMED[repo], head)
    want = re.sub(r"\(\\d\+\)", issues[0], CLAIMED[repo]).lstrip("^").rstrip("-") + "-<slug>"
    if not head:
        deny(f"this PR closes #{issues[0]} in {repo} but its head branch is unknown: add `--head {want}`.")
    if not m or any(i != m.group(1) for i in issues):
        deny(f"this PR closes #{', #'.join(issues)} in {repo} from `{head}`; an issue PR comes from its claimed task "
             f"branch `{want}` (the repo task file), or the nightly queue rebuilds it. Claim it with "
             f"`{claim_cmd(repo, want)}` (exit 3: another session holds it), push there, then open the PR.")
