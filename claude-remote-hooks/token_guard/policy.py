"""Per-repo rules (2026-10-30). One entry per guarded repo, read from routine-policy.json next to the hook, which
routine-policy writes from the GUARD_REPOS lines of the routine confs (a repo gets these rules by being in a
conf, not by an edit here). routine-policy.defaults.json (same writer) is the rule set when that file is missing or
unreadable, so a broken install keeps protecting these repos instead of dropping them; the main file can add repos
or change an entry, never remove a default one.
  bases    PR targets; the first is the only one a session may merge into (2026-10-29: a phone PR into an
           integration repo's `beta` branch was merged there). Keys are exact owner/repo (2026-10-29: `-R $R` was not
           read, the hook fell back to the cwd's repo and refused another repo's PR with the wrong rules).
  status   commit status that must be success on the PR head before a merge (a local CI runner posts it while GitHub
           Actions cannot start: 2026-10-29 billing, PRs merged with every check red). A GitHub Actions
           check needs no entry: branch protection (routine-new sets it) already requires it.
  runner   the command that posts `status` (`runner <pr>`); a session may not post that status by hand.
  task     "T-NNN": the PR says which task it finishes, `Closes T-NNN` or `Part of T-NNN` with `Left:`, and carries
           the task format (criteria checklist, Spec refs, Risk), at create and at merge (2026-10-29: phone PRs
           merged without it and the queue rebuilt finished tasks; a PR opened on github.com skips the create check).
  format   where that task format is written down, quoted in refusals.
  branch   task branch regex, group 1 = the task id: a push needs the branch claimed on GitHub first, and a builder
           prompt `task <owner/repo>#<id>` (or the repo's `builder` phrase) needs `branch <b> claim <sha>`.
  from     the branch a claim is cut from (default: bases[0]).
  closes   "issue": a PR body that closes, fixes or resolves #N must come from a head branch matching `branch` with
           task id N (2026-10-30: an ad-hoc session could build issue N on any branch; the queue and the claim never
           saw it and the nightly scheduler rebuilt the issue).
"""
import json, os, re

from . import HOOK_DIR as _HOOK_DIR

HOOK_DIR = _HOOK_DIR or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULTS_FILE = os.environ.get("TOKEN_GUARD_DEFAULTS") or os.path.join(HOOK_DIR, "routine-policy.defaults.json")
POLICY_FILE = os.environ.get("TOKEN_GUARD_POLICY") or os.path.join(HOOK_DIR, "routine-policy.json")


NAME_RE = r"[\w./-]+"   # bases and status contexts go into the merge wrapper's shell text: no quotes or spaces


def entry_ok(r, p):
    """A file entry the guard can use: names safe in shell text, regexes that compile with a task-id group."""
    try:
        return (re.fullmatch(r"[\w.-]+/[\w.-]+", r) and p["bases"]
                and all(re.fullmatch(NAME_RE, b) for b in p["bases"] + [p.get(k) or "x" for k in ("status", "from", "runner")])
                and all(re.compile(p[k]).groups >= 1 for k in ("branch", "builder") if p.get(k)))
    except Exception:
        return False  # allow-failopen: a bad entry is dropped, the repo keeps its default rules if it has any


def read_repos(path):
    try:
        with open(path, encoding="utf-8") as fh:
            repos = json.load(fh)["repos"]
        return {r: dict(p) for r, p in repos.items() if entry_ok(r, p)}
    except Exception:
        return {}  # allow-failopen: no file adds no rules; the defaults file is the fail-closed rule set


def load_policy(path=POLICY_FILE, defaults=DEFAULTS_FILE):
    pol = read_repos(defaults)
    pol.update(read_repos(path))
    return pol


POLICY = load_policy()
PR_BASES = {r: tuple(p["bases"]) for r, p in POLICY.items()}
STATUS = {r: p["status"] for r, p in POLICY.items() if p.get("status")}
TASK_LINE_REPOS = tuple(r for r, p in POLICY.items() if p.get("task") == "T-NNN")
CLAIMED = {r: p["branch"] for r, p in POLICY.items() if p.get("branch")}
CLOSES_ISSUE = tuple(r for r, p in POLICY.items() if p.get("closes") == "issue" and p.get("branch"))
RUNNER = {p["status"]: p["runner"] for p in POLICY.values() if p.get("status") and p.get("runner")}
TASK_LINE_HELP = "a line `Closes T-NNN` (whole task) or `Part of T-NNN` plus `Left: <criteria>` (a slice)"


def status_q(ctx):
    return f'[.statuses[]|select(.context=="{ctx}")][0].state'


def status_help(repo, n):
    ctx = STATUS[repo]
    run = POLICY[repo].get("runner")
    return f"run {run} {n} first (GitHub CI cannot run)" if run else f"wait for {ctx} on the head"
_LN = r"(?im)(?:^|['\"])\s*(?:[-*]\s*)?"


def task_line_ok(body):
    return bool(re.search(_LN + r"closes\s+T-\d+", body)) or bool(
        re.search(_LN + r"part of\s+T-\d+", body) and re.search(_LN + r"left:[ \t]*\S", body))


SECTIONS = (("What changed", r"what changed"), ("How it was verified", r"how (it was )?verified"),
            ("Not verified", r"not verified"))   # the same patterns routine-verdict checks (rule c)
# Task-line repos' PRs carry the whole task format, from every session (2026-10-29 audit: phone PRs
# #77-#81 had no criteria checklist, Spec refs or Risk; the routine's had all three).
TASK_SECTIONS = (("a criteria checklist (`- [x] <criterion>: <evidence>`)", r"\[[ x]\]"), ("Spec refs", r"spec ref"),
               ("Risk", r"risk\b"))


def task_format(repo):
    """The refusal's pointer to the repo's task format; "" for repos without a task line."""
    if repo not in TASK_LINE_REPOS:
        return ""
    return f" Format: {POLICY[repo]['format']}." if POLICY[repo].get("format") else ""
_SEC = r"(^|\n|['\"])[#*_> \t-]*"   # a line start; an inline `--body '## What changed…` puts a quote first


def body_missing(repo, body):
    """The parts a PR body for repo lacks; [] when it is complete."""
    want = SECTIONS + (TASK_SECTIONS if repo in TASK_LINE_REPOS else ())
    missing = [name for name, rx in want if not re.search(_SEC + rx, body, re.I)]
    if repo in TASK_LINE_REPOS and not task_line_ok(body):
        missing.insert(0, TASK_LINE_HELP)
    return missing


def slug(url):
    """The guarded repo key (exact owner/repo, any case) a remote URL or `-R` value names; None if not guarded."""
    m = re.search(r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", (url or "").strip())
    s = f"{m.group(1)}/{m.group(2)}".lower() if m else ""
    return next((r for r in PR_BASES if r.lower() == s), None)


def claim_cmd(repo, branch):
    p = POLICY[repo]
    return f"claim-branch {repo} {branch} {p.get('from') or p['bases'][0]}"
