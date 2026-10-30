#!/usr/bin/env python3
"""PreToolUse + SessionStart hook for claude-agent's sessions on the VPS (and, with --pr-only, the laptop).

Installed to /opt/agent-rules/hooks on the VPS and ~/.claude/hooks on the laptop. Every line a tool prints is resent on every later
call, and every byte the model writes into a tool call is output tokens (about 9K = 1% of a
5-hour window). Two incidents this enforces:
  2026-10-14  whole-repo `cat` loops, compacted 3x, then re-read the same files.
  2026-10-15  10 docs re-typed into Drive create_file: 159K output tokens to send one email.
Heavy checks (lint/test/build/install) are no longer queued: 2026-10-29 the single lock
serialized five builders for hours. Memory is bounded by claude.slice (MemoryMax 4.5G).

Deny = exit 2, the reason on stderr goes back to Claude. `--pr-only` (the laptop hook) runs only the PR guards.
"""
import json, os, re, shlex, subprocess, sys

MAX_LINES = 300          # biggest file / range a session may print in one go
MAX_TOOL_CHARS = 5000    # biggest connector (mcp__) payload the model may write
READ_HELP = ("Find the spot with `grep -n PATTERN FILE`, then print only that range with "
             f"`sed -n 'A,Bp' FILE` (at most {MAX_LINES} lines), or Read with offset/limit. "
             "Whole-repo questions: `graphify update <repo>` then `graphify query \"<q>\" --budget 1500`, "
             "or hand the reading to a subagent and keep only its findings.")


def deny(msg):
    sys.stderr.write("BLOCKED by token-guard: " + msg + "\n")
    sys.exit(2)


def count_lines(path, cwd):
    p = path if os.path.isabs(path) else os.path.join(cwd, os.path.expanduser(path))
    try:
        if not os.path.isfile(p):
            return None
        if os.path.getsize(p) > 2_000_000:
            return 10**9
        with open(p, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def check_cat(cmd, cwd):
    if re.search(r"\bxargs\b[^|;&]*\bcat\b|-exec\s+cat\b|\bfor\b[^;]*;\s*do\b[^;]*\bcat\b|\bwhile\b.*\bdo\b[^;]*\bcat\b", cmd):
        deny("`cat` in a loop / xargs / -exec prints every file into context. " + READ_HELP)
    for seg in re.split(r"\|\||&&|;|\n", cmd):
        m = re.search(r"(?:^|\s|\()cat\s+(.*)", seg)
        if not m:
            continue
        rest = m.group(1)
        if re.match(r"\s*(<<|>)", rest):          # heredoc / file write
            continue
        piped = "|" in rest
        args_part = re.split(r"[|<>]", rest)[0]
        try:
            args = [a for a in shlex.split(args_part) if not a.startswith("-")]
        except ValueError:
            args = args_part.split()
        if not args or piped:
            continue                              # cat x | grep … is a filter, not a dump
        if any(c in a for a in args for c in "*?["):
            deny(f"`cat {args_part.strip()}` dumps every matching file. " + READ_HELP)
        total = 0
        for a in args:
            n = count_lines(a, cwd)
            if n is None:
                continue
            total += n
        if total > MAX_LINES:
            deny(f"`cat` would print {total} lines. " + READ_HELP)


def check_ranges(cmd, cwd):
    for a, b in re.findall(r"sed\s+-n\s+['\"]?(\d+),(\d+)p", cmd):
        if int(b) - int(a) > MAX_LINES:
            deny(f"sed range {a},{b} is over {MAX_LINES} lines. Narrow it. " + READ_HELP)
    for seg in re.split(r"\|\||&&|;|\n", cmd):
        for m in re.finditer(r"\b(?:head|tail)\s+-n?\s*\+?(\d+)([^|>]*)(.?)", seg):
            if int(m.group(1)) > MAX_LINES and m.group(3) not in ("|", ">"):
                deny(f"head/tail of {m.group(1)} lines is over {MAX_LINES}. " + READ_HELP)


# Per-repo rules (2026-10-30). One entry per guarded repo, read from routine-policy.json next to this file, which
# routine-policy writes from the GUARD_REPOS lines of the routine confs (a repo gets these rules by being in a
# conf, not by an edit here). routine-policy.defaults.json (same writer) is the rule set when that file is missing or
# unreadable, so a broken install keeps protecting these repos instead of dropping them; the main file can add repos
# or change an entry, never remove a default one.
#   bases    PR targets; the first is the only one a session may merge into (2026-10-29: a phone PR into an
#            integration repo's `beta` branch was merged there). Keys are exact owner/repo (2026-10-29: `-R $R` was not
#            read, the hook fell back to the cwd's repo and refused another repo's PR with the wrong rules).
#   status   commit status that must be success on the PR head before a merge (a local CI runner posts it while GitHub
#            Actions cannot start: 2026-10-29 billing, PRs merged with every check red). A GitHub Actions
#            check needs no entry: branch protection (routine-new sets it) already requires it.
#   runner   the command that posts `status` (`runner <pr>`); a session may not post that status by hand.
#   task     "T-NNN": the PR says which task it finishes, `Closes T-NNN` or `Part of T-NNN` with `Left:`, and carries
#            the task format (criteria checklist, Spec refs, Risk), at create and at merge (2026-10-29: phone PRs
#            merged without it and the queue rebuilt finished tasks; a PR opened on github.com skips the create check).
#   format   where that task format is written down, quoted in refusals.
#   branch   task branch regex, group 1 = the task id: a push needs the branch claimed on GitHub first, and a builder
#            prompt `task <owner/repo>#<id>` (or the repo's `builder` phrase) needs `branch <b> claim <sha>`.
#   from     the branch a claim is cut from (default: bases[0]).
HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
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


def repo_flag(cmd):
    """The literal owner/repo of `-R`/`--repo`, reading `$R` from an `R=owner/repo` earlier in cmd; None if absent
    or not a literal (`R=$(git remote get-url origin)`: the caller then reads the cd dir)."""
    m = re.search(r"(?<![\w-])(?:-R|--repo)[ =]['\"]?(\$\{?(\w+)\}?|[\w.-]+/[\w.-]+)", cmd)
    if not m:
        return None
    if not m.group(2):
        return m.group(1)
    v = re.findall(r"(?:^|[\s;&|(])" + m.group(2) + r"=['\"]?([\w.-]+/[\w.-]+)['\"]?(?=[\s;&|)]|$)", cmd)
    return v[-1] if v else None


def remote_of(cmd, cwd):
    """The repo cmd acts on: its `-R` value, else the origin URL of the dir it cds into (or cwd); "" if unknown."""
    url = repo_flag(cmd)
    if not url:
        c = re.search(r"\bcd\s+(['\"]?)([^\s;&|'\"]+)\1", cmd)
        d = os.path.join(cwd, os.path.expanduser(c.group(2))) if c else cwd
        try:
            url = subprocess.run(["git", "-C", d, "remote", "get-url", "origin"],
                                 capture_output=True, text=True, timeout=2).stdout
        except Exception:
            url = ""  # allow-failopen: not a git dir means not a guarded repo
    return url.strip()


def repo_of(cmd, cwd):
    return slug(remote_of(cmd, cwd))


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


# Claim before push (2026-10-29: feat/t247-disputes and feat/t247-ownership-disputes were both built; the second
# lead never saw the first's unpushed worktree). A task branch (POLICY `branch`) must already exist on GitHub, made
# by claim-branch, which refuses when another branch holds the same task id. Plain --force could wipe a claim.
def claim_cmd(repo, branch):
    p = POLICY[repo]
    return f"claim-branch {repo} {branch} {p.get('from') or p['bases'][0]}"


def pushes(cmd, cwd):
    """(dir, args) for each `git [-C dir] push args` in cmd."""
    out = []
    for seg in re.split(r"\|\||&&|;|\n|\|", cmd):
        m = re.search(r"\bgit\s+(?:-C\s+(['\"]?)([^\s'\"]+)\1\s+)?push\b(.*)", seg)
        if not m:
            continue
        c = re.search(r"\bcd\s+(['\"]?)([^\s;&|'\"]+)\1", cmd)
        d = m.group(2) or (c.group(2) if c else ".")
        try:
            args = shlex.split(m.group(3))
        except ValueError:
            args = m.group(3).split()
        out.append((os.path.join(cwd, os.path.expanduser(d)), args))
    return out


def check_task_push(cmd, cwd):
    for d, args in pushes(cmd, cwd):
        if "--delete" in args or "-d" in args:
            continue
        git = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, timeout=10).stdout.strip()
        try:
            url = git("remote", "get-url", "origin")
        except Exception:
            url = ""  # allow-failopen: not a git dir means not a guarded repo
        repo = slug(url)
        if repo not in CLAIMED:
            continue
        pos = [a for a in args if not a.startswith("-")]
        refs = [r.split(":")[-1].removeprefix("+") for r in pos[1:]] or ["HEAD"]
        for ref in refs:
            if ref == "HEAD":
                try:
                    ref = git("branch", "--show-current")
                except Exception:
                    ref = ""  # allow-failopen: a detached head is not a task branch
            ref = ref.removeprefix("refs/heads/")
            if not re.match(CLAIMED[repo], ref):
                continue
            if "--force" in args or "-f" in args or any(p.startswith("+") for p in pos[1:]):
                deny(f"plain --force on {ref} can wipe another session's claim. Use `--force-with-lease`.")
            try:
                held = git("ls-remote", "--heads", "origin", "refs/heads/" + ref)
            except Exception:
                held = ""  # allow-failopen: no answer is handled below as a refusal (fail closed)
            if not any(ln.endswith("\trefs/heads/" + ref) for ln in held.splitlines()):
                deny(f"{ref} is not claimed on GitHub. Run `{claim_cmd(repo, ref)}` first; exit 3 means another "
                     "session holds the task: stop. Then `git fetch`, rebase your commits onto origin/" + ref + " and push.")


# Claim before dispatch (2026-10-29: the push check above fires only after a builder has done the work). A builder
# prompt names its task with the marker `task <owner/repo>#<id>` (or the repo's older `builder` phrase, e.g.
# "Task API, task T-<id>"), the branch and the sha claim-branch printed for it, and that claim commit must
# be on the branch on GitHub: the lead claimed it, and exit 3 leaves no sha to give.
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


# Merging without `gh pr merge` skipped every merge check (2026-10-29 audit: `gh api -X PUT .../pulls/90/merge` and the
# GraphQL mergePullRequest mutation passed with no output, and the integration branch has no GitHub protection, so
# this hook is the only gate). Agents already fall back to `gh api` when a gh subcommand fails.
API_MERGE_GQL = r"\b(?:mergePullRequest|enablePullRequestAutoMerge|enqueuePullRequest)\b"
API_MERGE_HELP = ("; merge with `gh pr merge <n> -R <owner/repo> --squash`, which checks the PR's base, task line and "
                  "required status first")


def api_method(text):
    m = re.search(r"(?<![\w-])(?:-X|--method)(?:\s+|=|(?=[A-Za-z]))['\"]?([A-Za-z]+)", text)
    return m.group(1).upper() if m else "GET"


def api_repo(token, cmd, cwd):
    """The guarded repo a `repos/<token>/...` path names: a literal owner/repo, a `$VAR` set earlier in cmd, or the
    `{owner}/{repo}` placeholders (gh fills them from the cwd's remote); None if the repo is not guarded."""
    t = token.strip("'\"")
    if re.fullmatch(r"[\w.-]+/[\w.-]+", t):
        return slug(t)
    v = re.fullmatch(r"\$\{?(\w+)\}?", t)
    if v:
        a = re.findall(r"(?:^|[\s;&|(])" + v.group(1) + r"=['\"]?([\w.-]+/[\w.-]+)", cmd)
        if a:
            return slug(a[-1])
    return repo_of(cmd, cwd) or next((r for r in PR_BASES if r.lower() in cmd.lower()), None)


def check_local_ci_post(cmd):
    """Only a policy `runner` posts its `status` (e.g. local-ci). Refuse a write to /statuses/ that names it; a read
    that selects it with jq stays allowed (2026-10-30: `--jq '.[] | select(.context=="local-ci")'` was refused)."""
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        ctx = next((c for c in RUNNER if re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", seg)), None)
        if not ctx or not re.search(r"statuses/", seg):
            continue
        m = re.search(r"(?:-X|--method)[ =]+['\"]?([A-Za-z]+)", seg)
        method = m.group(1).upper() if m else None
        if re.search(r"\bgh\s+api\b", seg):
            fields = re.search(r"(?:^|\s)(?:-f|-F|--field|--raw-field)[ =]*['\"]?(?:state|context)=", seg)
            write = method in ("POST", "PUT", "PATCH") or (method is None and fields)
        elif re.search(r"\bcurl\b", seg):
            write = method in ("POST", "PUT", "PATCH") or (
                method is None and re.search(r"(?:^|\s)(?:-d|--data(?:-raw|-binary|-urlencode)?|--json)\b", seg))
        else:
            continue
        if write:
            run = RUNNER[ctx]
            deny(f"the `{ctx}` status is posted only by {run}, which runs CI on the head: run `{run} <pr>`.")


def check_api_merge(cmd, cwd):
    for m in re.finditer(r"\bgh\s+api\b", cmd):
        tail = cmd[m.start():]
        text = tail
        for f in re.findall(r"(?:=@|--input[ =])['\"]?([^\s'\"]+)", tail):  # a query kept in a file
            try:
                with open(os.path.join(cwd, os.path.expanduser(f)), encoding="utf-8", errors="replace") as fh:
                    text += "\n" + fh.read(20000)
            except OSError:
                pass  # allow-failopen: a file an earlier step writes cannot be read yet; the inline forms stay checked
        if re.search(API_MERGE_GQL, text):  # the mutation names a PR node id, never the repo: refuse everywhere
            deny("a GraphQL merge mutation skips the merge checks" + API_MERGE_HELP)
        method = api_method(tail)
        for repo_tok, route in re.findall(r"repos/(\S+?)/(pulls/\d+/merge\b|merges\b|git/refs/heads/[^\s'\"?]+)", tail):
            repo = api_repo(repo_tok, cmd, cwd)
            if repo is None:
                continue
            if route.startswith("pulls/") and method == "PUT":
                deny(f"`gh api` PUT {route} in {repo} skips the merge checks" + API_MERGE_HELP)
            if route == "merges":
                deny(f"`gh api` repos/.../merges in {repo} merges a branch past the merge checks" + API_MERGE_HELP)
            if route.startswith("git/refs/heads/") and method != "GET" and route[len("git/refs/heads/"):] in PR_BASES[repo]:
                deny(f"`gh api` {method} on the {route.split('/', 3)[3]} ref of {repo} moves the merge base past the merge checks"
                     + API_MERGE_HELP)


def merge_targets(cmd):
    """The PR of each `gh pr merge` (number, URL or branch); "" = the current branch's PR."""
    return [("" if n.startswith("-") else n) for n in re.findall(r"\bgh\s+pr\s+merge(?:\s+([^\s;&|]+))?", cmd)]


def check_merge_now(cmd, cwd):
    """The laptop's merge guard (--pr-only): read the PR's base before the command runs. No rewrite, so
    the laptop's permission prompts still see the command. Fails closed in a guarded repo."""
    for n in merge_targets(cmd):
        u = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/\d+", n)
        repo = slug(u.group(1)) if u else repo_of(cmd, cwd)
        if repo not in PR_BASES:
            continue
        args = ["gh", "pr", "view"] + ([n] if n else []) + (["-R", repo] if not u else [])
        run = dict(cwd=cwd if os.path.isdir(cwd) else None, capture_output=True, text=True, timeout=8)
        try:
            out = subprocess.run(args + ["--json", "url,baseRefName,headRefOid", "-q",
                                         '.url+" "+.baseRefName+" "+.headRefOid'], **run).stdout.split()
        except Exception:
            out = []  # allow-failopen: no answer is handled below as a refusal (fail closed)
        base = out[1] if len(out) == 3 else "(unknown: gh could not read the PR)"
        if base != PR_BASES[repo][0]:
            deny(f"merge refused: PR {n or '(current branch)'} in {repo} targets {base}; merge only into "
                 f"`{PR_BASES[repo][0]}`.")
        if repo in TASK_LINE_REPOS:
            try:
                body = subprocess.run(["gh", "pr", "view", out[0], "--json", "body", "-q", ".body"], **run).stdout
            except Exception:
                body = ""  # allow-failopen: no answer is a refusal (fail closed)
            missing = body_missing(repo, body)
            if missing:
                deny(f"merge refused: PR {n or '(current branch)'} body lacks: {'; '.join(missing)}. "
                     f"Fix it with `gh pr edit {out[0]} --body-file <file>` first." + task_format(repo))
        if repo in STATUS:
            rs = re.sub(r"^https://github\.com/|/pull/\d+$", "", out[0])
            try:
                ci = subprocess.run(["gh", "api", f"repos/{rs}/commits/{out[2]}/status", "-q",
                                     status_q(STATUS[repo])], **run).stdout.strip()
            except Exception:
                ci = ""  # allow-failopen: no answer is a refusal (fail closed)
            if ci != "success":
                deny(f"merge refused: PR {n or '(current branch)'} head {out[2][:7]} has {STATUS[repo]}="
                     f"{ci or 'none'}; {status_help(repo, n)}.")


def guard_merge(cmd, cwd):
    """Prefix `gh pr merge` with a live base check: refuse unless the PR targets its repo's merge base."""
    nums = merge_targets(cmd)
    if not nums or "token-guard: merge refused" in cmd:
        return None
    src = remote_of(cmd, cwd)
    repo = slug(src)
    urls = [u.group(1) for u in (re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/\d+", n) for n in nums) if u]
    if src and repo is None and not any(slug(u) for u in urls):
        return None  # a known repo that is not guarded (the design pack, scripts): nothing to check
    rflag = f" -R {repo}" if repo else ""
    ok = "|".join(f'*/{r}/pull/*" {b[0]}"' for r, b in PR_BASES.items())
    guarded = "|".join(f"*/{r}/pull/*" for r in PR_BASES)
    # the body check is this file's body_missing(), so the wrapper and the laptop apply the same rule
    me = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))}"
    body = lambda n: "".join(
        f'*/{r}/pull/*) gh pr view {n}{rflag} --json body -q .body | {me} --check-body {r} {n} >&2 || _ok=0 ;; '
        for r in TASK_LINE_REPOS)
    status = lambda n: "".join(
        f'*/{r}/pull/*) _r=${{_b#https://github.com/}}; '
        f'_c=$(gh api "repos/${{_r%%/pull/*}}/commits/$_h/status" -q \'{status_q(c)}\'); [ "$_c" = success ] || '
        f'{{ _ok=0; echo "token-guard: merge refused: PR {n} head ${{_h:0:7}} has {c}=${{_c:-none}}; '
        f'{status_help(r, n)}" >&2; }} ;; '
        for r, c in STATUS.items())
    checks = "".join(
        f'_v=$(gh pr view {n}{rflag} --json url,baseRefName,headRefOid -q \'.url+" "+.baseRefName+" "+.headRefOid\') '
        f'|| _ok=0; _h=${{_v##* }}; _b=${{_v% *}}; '
        f'case "$_b" in {ok}) ;; {guarded}|"") _ok=0; echo "token-guard: merge refused: PR {n} targets '
        f'${{_b##* }}; merge only into the repo\'s merge base (the first base in routine-policy.json)" >&2 ;; esac; '
        f'case "$_b" in {status(n)}esac; '
        f'case "$_b" in {body(n)}esac; '
        for n in nums)
    return f"_ok=1; {checks}[ \"$_ok\" = 1 ] && {{ {cmd}\n}}"


def main():
    if sys.argv[1:2] == ["--check-body"]:  # the VPS merge wrapper: --check-body <repo> <pr>, PR body on stdin
        missing = body_missing(sys.argv[2], sys.stdin.read())
        n = sys.argv[3] if len(sys.argv) > 3 else "(current branch)"
        if missing:
            print(f"token-guard: merge refused: PR {n} body lacks: {'; '.join(missing)}. "
                  "Fix it with gh pr edit --body-file <file> first." + task_format(sys.argv[2]))
            sys.exit(1)
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # allow-failopen: a malformed event must never block the session
    event = data.get("hook_event_name")
    if "--pr-only" in sys.argv:  # the laptop: same PR rules, none of the VPS token limits
        if data.get("tool_name") == "Bash":
            cmd, cwd = (data.get("tool_input") or {}).get("command", ""), data.get("cwd") or os.getcwd()
            check_pr_create(cmd, cwd)
            check_api_merge(cmd, cwd)
            check_merge_now(cmd, cwd)
            check_task_push(cmd, cwd)
        if data.get("tool_name") in ("Agent", "Task"):
            check_dispatch((data.get("tool_input") or {}).get("prompt", ""))
        return
    if event == "SessionStart":
        if data.get("source") == "compact":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                "This session just auto-compacted. Rule: one compaction per session. Finish only the step in "
                "progress, then stop and give the founder a three-line handoff (repo, branch, next step) to "
                "start a fresh session. Do not re-read files you already summarised."}}))
        return
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()

    if tool in ("Agent", "Task"):
        check_dispatch(ti.get("prompt", ""))
        return

    if tool.startswith("mcp__"):
        size = len(json.dumps(ti))
        if size > MAX_TOOL_CHARS:
            deny(f"{tool} payload is {size} chars; every char is output tokens. Never pass file "
                 "contents through a connector. Upload from disk: `rclone copy <file-or-dir> gdrive:<folder>`, "
                 "then `rclone link gdrive:<folder>/<file>` and send only the link. Repo code/docs go through git/gh.")
        return

    if tool == "Read":
        if ti.get("limit"):
            if int(ti["limit"]) > MAX_LINES * 2:
                deny(f"Read limit {ti['limit']} is too big. Use limit <= {MAX_LINES * 2}. " + READ_HELP)
            return
        n = count_lines(ti.get("file_path", ""), cwd)
        if n and n > MAX_LINES * 2:
            deny(f"{ti.get('file_path')} has {n} lines; reading it whole costs ~{n * 12 // 1000}K tokens on "
                 "every later call. Pass offset/limit. " + READ_HELP)
        return

    if tool == "Bash":
        cmd = ti.get("command", "")
        check_cat(cmd, cwd)
        check_ranges(cmd, cwd)
        check_pr_create(cmd, cwd)
        check_task_push(cmd, cwd)
        check_api_merge(cmd, cwd)
        check_local_ci_post(cmd)
        guarded = guard_merge(cmd, cwd)
        if guarded:
            new = dict(ti); new["command"] = guarded
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                "permissionDecision": "allow", "updatedInput": new,
                "permissionDecisionReason": "token-guard: merge runs only if the PR targets its repo's merge base"}}))


if __name__ == "__main__":
    main()
