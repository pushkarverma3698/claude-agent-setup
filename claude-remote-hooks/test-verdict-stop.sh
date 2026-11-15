#!/bin/bash
# Run: ./test-verdict-stop.sh   (no network, no tokens). Exit 0 = all cases pass.
H="$(dirname "$0")/verdict-stop.py"; T=$(mktemp -d); trap 'rm -r "$T"' EXIT
fail=0
user() { printf '{"type":"user","message":{"content":%s}}\n' "$(jq -Rn --arg c "$1" '$c')"; }
result() { echo '{"type":"user","message":{"content":[{"type":"tool_result","content":"ok"}]}}'; }
tool() { printf '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"%s","input":%s}]}}\n' "$1" "$2"; }
say() { printf '{"type":"assistant","message":{"content":[{"type":"text","text":%s}]}}\n' "$(jq -Rn --arg c "$1" '$c')"; }
run() { # run <block|pass> <final message> <label>; the transcript is on stdin
  cat >"$T/t.jsonl"
  jq -n --arg t "$T/t.jsonl" --arg m "$2" --argjson a "${ACTIVE:-false}" \
    '{hook_event_name:"Stop",transcript_path:$t,last_assistant_message:$m,stop_hook_active:$a}' >"$T/in.json"
  python3 "$H" <"$T/in.json" >/dev/null 2>"$T/err"; rc=$?
  case "$1" in
    block) [ $rc -eq 2 ] && grep -q 'Verdict: YES' "$T/err" ;;
    pass) [ $rc -eq 0 ] && [ ! -s "$T/err" ] ;;
  esac && echo "ok   $1  $3" || { echo "FAIL $1  $3 (rc=$rc)"; fail=1; }
}
edit() { user "fix it"; tool Edit '{"file_path":"a"}'; result; }

edit | run block "All done, tests pass." "edited, no verdict"
{ user "ship"; tool Bash '{"command":"git -C /x push origin b"}'; result; } | run block "Pushed." "git -C push, no verdict"
{ user "ship"; tool Bash '{"command":"gh pr create --base beta"}'; result; } | run block "Opened." "PR, no verdict"
edit | run block "Verdict: maybe" "not a verdict value"
edit | run block "Verdict: NO: CI is red" "NO without a check command"
edit | run block "I will write Verdict: YES next" "verdict not on its own line"
edit | run pass "Fixed.
Verdict: YES" "edited, YES"
edit | run pass "Blocked.
**Verdict: NO: staging is down, checked with curl -I staging**
Outstanding from your end: none" "bold NO with a check, then the closing section"
ACTIVE=true edit | ACTIVE=true run pass "still none" "stop_hook_active: one block only, never a loop"
{ user "what does it do?"; tool Read '{"file_path":"a"}'; result; } | run pass "It does X." "read-only turn"
{ user "status?"; tool Bash '{"command":"git status"}'; result; } | run pass "Clean." "read-only Bash"
{ user "why"; } | run pass "Because." "no tools"
{ edit; say "Fixed. Verdict: YES"; user "and what is left?"; tool Read '{"file_path":"a"}'; result; } | run pass "Nothing." "an edit in an earlier turn does not count"
{ user "x"; tool Edit '{"file_path":"a"}'; result; printf '{"type":"assistant","isSidechain":true,"message":{"content":[{"type":"text","text":"x"}]}}\n'; } | run block "Done." "sidechain rows do not end the turn"
{ user "x"; echo 'not json'; tool Edit '{"file_path":"a"}'; result; } | run block "Done." "a corrupt row is skipped"

# no last_assistant_message: falls back to the transcript's last text
{ edit; say "Done.
Verdict: YES"; } >"$T/f.jsonl"
jq -n --arg t "$T/f.jsonl" '{hook_event_name:"Stop",transcript_path:$t}' | python3 "$H" >/dev/null 2>&1
[ $? -eq 0 ] && echo "ok   pass  verdict read from the transcript" || { echo "FAIL verdict read from the transcript"; fail=1; }
# fail open
printf 'not json' | python3 "$H" >/dev/null 2>&1; [ $? -eq 0 ] && echo "ok   pass  malformed input" || { echo "FAIL malformed input"; fail=1; }
jq -n '{transcript_path:"/nope/x.jsonl",last_assistant_message:"x"}' | python3 "$H" >/dev/null 2>&1
[ $? -eq 0 ] && echo "ok   pass  missing transcript" || { echo "FAIL missing transcript"; fail=1; }
exit $fail
