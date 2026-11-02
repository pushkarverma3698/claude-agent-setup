"""Claim before dispatch (2026-10-29: the push check fires only after a builder has done the work). A builder
prompt names its task with the marker `task <owner/repo>#<id>` (or the repo's older `builder` phrase, e.g.
"Task API, task T-<id>"), the branch and the sha claim-branch printed for it, and that claim commit must
be on the branch on GitHub: the lead claimed it, and exit 3 leaves no sha to give."""
import re, subprocess

from .common import deny
from .policy import CLAIMED, POLICY, claim_cmd


def builder_task(prompt):
    """(repo, task id) of the first claimed repo whose marker or builder phrase is in prompt; None if none is."""
    for repo, p in POLICY.items():
        if repo not in CLAIMED:
            continue
        for rx in [r"\btask\s+`?" + re.escape(repo) + r"#T?-?(\d+)\b"] + ([p["builder"]] if p.get("builder") else []):
            m = re.search(rx, prompt, re.I)
            if m:
                return repo, m.group(1)
    return None


def check_dispatch(prompt):
    hit = builder_task(prompt or "")
    if not hit:
        return
    repo, tid = hit
    eg = re.sub(r"\(\\d\+\)", tid, CLAIMED[repo]).lstrip("^") + "<slug>"   # ^feat/t(\d+)- -> feat/t205-<slug>
    how = (f"Claim it first: new `{claim_cmd(repo, eg)}`, leftover `claim-branch --resume {repo} <branch>`. Exit 0 "
           "prints the claim sha: put `branch <branch> claim <sha>` in the prompt. Exit 3: another session holds the "
           "task, do not start a builder for it.")
    names = [n for n in re.findall(r"\bbranch\s+`?([\w.-]+/[\w./-]+?)`?(?=\.?(?:[\s,;)]|$))", prompt)
             if re.match(CLAIMED[repo], n)]
    c = re.search(r"\bclaim\s+`?([0-9a-f]{7,40})\b", prompt)
    if not names or not c:
        deny(f"a builder for {repo} task {tid} needs `branch {eg} claim <sha>` in its prompt. " + how)
    branch, sha = names[0], c.group(1)
    if int(re.match(CLAIMED[repo], branch).group(1)) != int(tid):
        deny(f"branch {branch} is not task {tid}'s. " + how)
    try:
        r = subprocess.run(["gh", "api", f"repos/{repo}/compare/{sha}...{branch}", "--jq",
                            '.status + " " + (.base_commit.commit.message | split("\\n")[0])'],
                           capture_output=True, text=True, timeout=4)
        status, _, subject = r.stdout.strip().partition(" ")
    except Exception:
        status = subject = ""  # allow-failopen: no answer is handled below as a refusal (fail closed)
    if status not in ("identical", "ahead") or subject != f"chore(claim): reserve {branch} for one session":
        deny(f"claim {sha} is not a claim commit on {branch} on GitHub (or GitHub did not answer: retry once). " + how)
