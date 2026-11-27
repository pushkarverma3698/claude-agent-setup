"""gh pr create: the repo's base, the body sections (and task format), and for closes=task repos the head branch."""
import os, re, shlex

from .common import deny, head_of, repo_of
from .policy import CLAIMED, CLOSES_TASK, PR_BASES, body_missing, claim_cmd, task_format


def pr_create_args(cmd):
    """The text of the `gh pr create` invocation alone: its flags up to the next ; && || | or line break.
    2026-11-27: the flags were searched in the whole command, so `git checkout -B chore/promote-x && gh pr create
    --base main` read the git -B as the PR base and refused the promotion PR."""
    rest = re.split(r"\bgh\s+pr\s+create\b", cmd, maxsplit=1)[1].replace("\\\n", " ")
    lex = shlex.shlex(rest, posix=True, punctuation_chars=";&|")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    out = []
    try:
        for tok in lex:
            if tok == "\n" or (tok and set(tok) <= set(";&|")):
                break
            out.append(shlex.quote(tok))
    except ValueError:  # unbalanced quote: fall back to the first line
        return rest.split("\n", 1)[0]
    return " ".join(out)


def check_pr_create(cmd, cwd):
    if not re.search(r"\bgh\s+pr\s+create\b", cmd):
        return
    repo = repo_of(cmd, cwd)
    if not repo:
        return
    args = pr_create_args(cmd)
    m = re.search(r"(?<![\w-])(?:--base|-B)[ =]['\"]?([^\s'\"]+)", args)
    if not m or m.group(1) not in PR_BASES[repo]:
        deny(f"PRs in {repo} target `{PR_BASES[repo][0]}`: add `--base {PR_BASES[repo][0]}`.")
    if m.group(1) == "production":
        return  # the nightly promotion PR (beta -> production) is a checklist, never merged by an agent
    body = cmd
    f = re.search(r"(?<![\w-])(?:--body-file|-F)[ =]['\"]?([^\s'\"]+)", args)
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
    if repo in CLOSES_TASK:
        check_task_head(cmd, cwd, repo, body, args)


CLOSES_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+T-(\d+)\b")


def check_task_head(cmd, cwd, repo, body, args=None):
    ids = sorted(set(CLOSES_RE.findall(body)), key=int)
    if not ids:
        return
    head = head_of(cmd, cwd, args)
    m = re.search(CLAIMED[repo], head)
    want = re.sub(r"\(\\d\+\)", ids[0], CLAIMED[repo]).lstrip("^").rstrip("-") + "-<slug>"
    if not head:
        deny(f"this PR closes T-{ids[0]} in {repo} but its head branch is unknown: add `--head {want}`.")
    if not m or any(i != m.group(1) for i in ids):
        deny(f"this PR closes T-{', T-'.join(ids)} in {repo} from `{head}`; a task PR comes from its claimed task "
             f"branch `{want}` (the repo task file), or the nightly queue rebuilds it. Claim it with "
             f"`{claim_cmd(repo, want)}` (exit 3: another session holds it), push there, then open the PR.")
