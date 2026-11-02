#!/bin/bash
# Characterization test for token-guard.py: every hook call that test-token-guard.sh makes, plus a few direct ones
# below, is recorded with its arguments, FAKE_*/TOKEN_GUARD_* env, stdin, exit code, stdout and stderr, and the
# whole record must equal testdata/token-guard.golden byte for byte. A refactor that changes any refusal text, exit
# code or merge wrapper fails here.
# Run: ./test-token-guard-golden.sh            (no network, no tokens). Exit 0 = identical.
#      ./test-token-guard-golden.sh --update   rewrite the golden file (only for an intended behaviour change).
set -u
D="$(cd "$(dirname "$0")" && pwd)"; HOOK="$D/token-guard.py"; GOLD="$D/testdata/token-guard.golden"
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
mkdir -p "$W/run"
# the shim stands in for token-guard.py inside a copy of test-token-guard.sh: it runs the real hook and logs the call
cat >"$W/run/token-guard.py" <<'PY'
import json, os, subprocess, sys
data = sys.stdin.buffer.read()
r = subprocess.run([sys.executable, os.environ["TG_REAL_HOOK"], *sys.argv[1:]], input=data, capture_output=True)
env = {k: v for k, v in sorted(os.environ.items()) if k.startswith(("FAKE_", "TOKEN_GUARD_"))}
rec = {"args": sys.argv[1:], "env": env, "stdin": data.decode("utf-8", "replace"), "rc": r.returncode,
       "stdout": r.stdout.decode("utf-8", "replace"), "stderr": r.stderr.decode("utf-8", "replace")}
with open(os.environ["TG_GOLDEN_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, indent=1, sort_keys=True) + "\n")
sys.stdout.buffer.write(r.stdout); sys.stderr.buffer.write(r.stderr); sys.exit(r.returncode)
PY
cp "$D/test-token-guard.sh" "$W/run/test-token-guard.sh"
export TG_REAL_HOOK="$HOOK" TG_GOLDEN_LOG="$W/log"
bash "$W/run/test-token-guard.sh" >"$W/suite.out" 2>&1; rc=$?
[ $rc -eq 0 ] || { cat "$W/suite.out"; echo "FAIL test-token-guard.sh exited $rc under the shim"; exit 1; }
# direct calls test-token-guard.sh does not make
S="$W/run/token-guard.py"; F="$W/direct"; mkdir -p "$F"; seq 1 1000 >"$F/big.txt"
export TOKEN_GUARD_DEFAULTS="$W/run/none.json" TOKEN_GUARD_POLICY="$W/run/none.json"
printf '{"repos": {"acme/task-api": {"bases": ["chore/integration"], "task": "T-NNN", "format": "task-contract.md step 7"}}}' >"$W/run/none.json"
call() { printf '%s' "$1" | python3 "$S" "${@:2}" >/dev/null 2>&1; }
call "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"cwd\":\"$F\",\"tool_input\":{\"file_path\":\"$F/big.txt\",\"limit\":601}}"
call "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"cwd\":\"$F\",\"tool_input\":{\"file_path\":\"$F/big.txt\",\"limit\":600}}"
call "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"cwd\":\"$F\",\"tool_input\":{\"file_path\":\"$F/big.txt\"}}" --pr-only
call "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"mcp__x__send\",\"tool_input\":{\"t\":\"$(head -c 6000 /dev/zero | tr '\0' a)\"}}" --pr-only
call '{"hook_event_name":"SessionStart","source":"compact"}' --pr-only
call "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"cwd\":\"$F\",\"tool_input\":{\"command\":\"tail -n 400 big.txt\"}}"
call "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"cwd\":\"$F\",\"tool_input\":{\"command\":\"while read f; do cat \$f; done < list\"}}"
call "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\",\"cwd\":\"$F\",\"tool_input\":{\"command\":\"cat 'unclosed\"}}"
call '{"hook_event_name":"PreToolUse","tool_name":"Write","tool_input":{"file_path":"/x","content":"y"}}'
call '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{}}'
call '{"hook_event_name":"PreToolUse","tool_name":"Agent","tool_input":{}}'
call 'Closes T-1
## What changed
## Criteria
- [x] C1: t
## Spec refs
## How it was verified
## Not verified
## Risk' --check-body acme/task-api 77
call '## What changed' --check-body acme/task-api 77
call '## What changed' --check-body acme/task-api
call '## What changed' --check-body someone/other 3
# normalize run-specific paths, then compare
python3 - "$W" "$HOOK" >"$W/got" <<'PY'
import os, re, sys
w, hook = sys.argv[1], sys.argv[2]
text = open(os.path.join(w, "log"), encoding="utf-8").read()
for p in sorted({hook, os.path.realpath(hook), w, os.path.realpath(w)}, key=len, reverse=True):
    text = text.replace(p, "<HOOK>" if p.endswith(".py") else "<W>")
text = re.sub(r"(?:/private)?/[^\s\"'\\]*?/tmp\.[A-Za-z0-9]{6,}", "<T>", text)  # test-token-guard.sh's own mktemp -d
text = text.replace(sys.executable, "<PY>")  # the merge wrapper runs the hook with the interpreter that ran it
sys.stdout.write(text)
PY
if [ "${1:-}" = --update ]; then cp "$W/got" "$GOLD"; echo "updated $GOLD ($(grep -c '^{' "$GOLD") calls)"; exit 0; fi
n=$(grep -c '^{' "$W/got")
if diff -u "$GOLD" "$W/got" >"$W/diff"; then echo "ok   golden: $n hook calls identical"; exit 0; fi
head -80 "$W/diff"; echo "FAIL golden: hook output differs from testdata/token-guard.golden"; exit 1
