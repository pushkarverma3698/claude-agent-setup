"""token-guard's rules, one module per concern. token-guard.py (beside this package) is the hook entry point and
the only dispatcher; it sets HOOK_DIR to its own directory before importing any rule module, so routine-policy.json
is read next to the hook file exactly as before the split.

  policy.py    routine-policy(.defaults).json and the PR-body / task-line rules built from it
  common.py    deny(), line counting, and which repo / head branch a shell command acts on
  reads.py     VPS token limits: cat loops, big ranges, big Reads, big connector payloads
  pr.py        gh pr create: base, body sections, task line, closes=issue head branch
  push.py      git push: tracker owner check, claim before push
  dispatch.py  Agent/Task builder prompts: claim before dispatch
  merge.py     gh pr merge (laptop check, VPS wrapper), gh api merges, local-ci status posts
"""
HOOK_DIR = None  # set by token-guard.py; None means the directory that holds this package
