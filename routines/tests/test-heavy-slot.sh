#!/bin/bash
# heavy-slot: at most HEAVY_SLOTS heavy checks at once, the lock dies with the process, exit codes and the cwd survive,
# and it works under sh, since husky hooks are sh. Needs flock (util-linux). Usage: bash routines/tests/test-heavy-slot.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S="$HERE/../bin/heavy-slot"; G="$HERE/../../claude-remote-hooks/token-guard.py"
T=$(mktemp -d); trap 'kill $(jobs -p) 2>/dev/null; rm -r -- "$T"' EXIT
export HOME="$T" HEAVY_SLOTS=2 HEAVY_SLOT_POLL=0.1
unset HEAVY_SLOT_HELD
fails=0
ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1"; fails=$((fails+1)); }
hold() { sh -c ". '$S'; exec sleep 30" 2>/dev/null & sleep 0.3; }   # takes a slot and keeps it

sh -c ". '$S'; echo \$HEAVY_SLOT_HELD" >"$T/o" 2>&1; [ "$(cat "$T/o")" = 1 ] && ok "free slot: no wait, no output" || bad "free slot printed: $(cat "$T/o")"
sh -c ". '$S'; exit 7"; [ $? = 7 ] && ok "the command's exit code is kept" || bad "exit code lost"
d=$(mkdir -p "$T/sub"; bash -c ". '$S'; cd '$T/sub' && pwd") ; [ "$d" = "$T/sub" ] && ok "runs in the caller's shell, so cd sticks" || bad "cd lost: $d"
HEAVY_SLOT_HELD=1 sh -c ". '$S'; echo nested" >"$T/o" 2>&1; [ "$(cat "$T/o")" = nested ] && ok "already holding: nested check does not queue" || bad "nested: $(cat "$T/o")"

hold; p1=$!
sh -c ". '$S'; echo \$HEAVY_SLOT_HELD" >"$T/o" 2>&1; [ "$(cat "$T/o")" = 2 ] && ok "one busy: takes the second slot" || bad "second slot: $(cat "$T/o")"
hold; p2=$!
HEAVY_SLOT_WAIT=1 sh -c ". '$S'; echo RAN" >"$T/o" 2>&1; rc=$?
[ $rc = 75 ] && ! grep -q RAN "$T/o" && ok "all busy past the wait: exit 75, command not run" || bad "full: rc=$rc $(cat "$T/o")"
grep -q 'waiting for a free slot' "$T/o" && grep -q 'no slot after' "$T/o" && ok "says it waits and why it gave up" || bad "messages: $(cat "$T/o")"

HEAVY_SLOT_WAIT=20 sh -c ". '$S'; echo RAN \$HEAVY_SLOT_HELD" >"$T/o" 2>&1 & w=$!
sleep 0.5; kill "$p1" 2>/dev/null
wait "$w"; rc=$?
[ $rc = 0 ] && grep -q 'RAN 1' "$T/o" && ok "a killed holder frees its slot and the waiter runs" || bad "waiter: rc=$rc $(cat "$T/o")"
kill "$p2" 2>/dev/null
wait 2>/dev/null

# the hook's real rewrite runs under bash with the real script, keeping the cwd and the exit code
b() { jq -cn --arg c "$1" --arg d "$T" '{hook_event_name:"PreToolUse",tool_name:"Bash",cwd:$d,tool_input:{command:$c}}'; }
out=$(b 'cd sub && npm_config_x=1 npm test; exit 5' | TOKEN_GUARD_DEFAULTS=/nonexistent TOKEN_GUARD_POLICY=/nonexistent python3 "$G" | jq -r .hookSpecificOutput.updatedInput.command)
mkdir -p "$T/fake"; printf '#!/bin/sh\npwd >"%s"\n' "$T/ran" >"$T/fake/npm"; chmod +x "$T/fake/npm"
( cd "$T"; PATH="$T/fake:$PATH" bash -c "${out//\/usr\/local\/bin\/heavy-slot/$S}" ); rc=$?
[ $rc = 5 ] && [ "$(cat "$T/ran" 2>/dev/null)" = "$T/sub" ] && ok "hook rewrite: command runs in place, exit code kept" || bad "hook rewrite: rc=$rc ran=$(cat "$T/ran" 2>/dev/null) cmd=$out"

[ "$fails" = 0 ] && echo "all ok" || { echo "$fails failed"; exit 1; }
