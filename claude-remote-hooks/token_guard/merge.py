"""Merges. `gh pr merge` is checked before it runs on the laptop (check_merge_now) and wrapped in a live base,
status and body check on the VPS (guard_merge). Merging without `gh pr merge` skipped every merge check (2026-10-29
audit: `gh api -X PUT .../pulls/90/merge` and the GraphQL mergePullRequest mutation passed with no output, and the
integration branch has no GitHub protection, so this hook is the only gate). Agents already fall back to `gh api`
when a gh subcommand fails."""
import os, re, shlex, subprocess, sys

from .common import deny, remote_of, repo_of
from .policy import (PR_BASES, RUNNER, STATUS, TASK_LINE_REPOS, body_missing, slug, status_help, status_q,
                     task_format)

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


def guard_merge(cmd, cwd, hook_file):
    """Prefix `gh pr merge` with a live base check: refuse unless the PR targets its repo's merge base.
    hook_file is token-guard.py itself: the wrapper pipes each PR body to `hook_file --check-body`."""
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
    # the body check is the hook's body_missing(), so the wrapper and the laptop apply the same rule
    me = f"{shlex.quote(sys.executable)} {shlex.quote(hook_file)}"
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
