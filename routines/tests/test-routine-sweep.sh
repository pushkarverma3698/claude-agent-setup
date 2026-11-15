#!/bin/bash
# Runs routines/bin/routine-sweep against fake tracker, claude-routine, claude-notify and claude on PATH. No network, no tokens.
# Usage: bash routines/tests/test-routine-sweep.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWEEP="$HERE/../bin/routine-sweep"
T=$(mktemp -d); trap 'rm -r -- "$T"' EXIT
fails=0
ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1"; fails=$((fails+1)); }
mkdir -p "$T/bin"

# tracker: prints $FAKE/next.json (exit $FAKE/tracker_rc). claude-routine and claude-notify only log what they got.
cat >"$T/bin/tracker" <<'EOS'
#!/bin/bash
echo "$*" >>"$FAKE/tracker.log"; cat "$FAKE/next.json"; exit "$(cat "$FAKE/tracker_rc" 2>/dev/null || echo 0)"
EOS
cat >"$T/bin/claude-routine" <<'EOS'
#!/bin/bash
echo "$*" >>"$FAKE/started"; [ ! -f "$FAKE/routine_out" ] || cat "$FAKE/routine_out"
EOS
cat >"$T/bin/claude-notify" <<'EOS'
#!/bin/bash
if [ $# -gt 0 ]; then printf '%s\n----\n' "$1" >>"$FAKE/notify"; else cat >>"$FAKE/notify"; echo ----  >>"$FAKE/notify"; fi
echo sent
EOS
cat >"$T/claude" <<'EOS'
#!/bin/bash
[ "$1 $2 $3" = "agents --json --all" ] && cat "$FAKE/agents.json"
EOS
cat >"$T/profile-env" <<'EOS'
claude="$FAKE/claude"; export CLAUDE_CONFIG_DIR="$FAKE/cfg"
EOS
chmod +x "$T/bin/"* "$T/claude"

# fresh <name> [conf body]: new fake world; sets FAKE
fresh() {
  FAKE="$T/$1"; export FAKE; mkdir -p "$FAKE/conf" "$FAKE/cfg/jobs" "$FAKE/home"
  cp "$T/claude" "$FAKE/claude"; echo '[]' >"$FAKE/agents.json"
  echo '{"next":{"kind":"NONE"},"leftover":[],"held":[],"blocked":[],"new":[],"notready":[]}' >"$FAKE/next.json"
  printf '%s\n' "${2-TRACKER_REPO=acme/tasks
PROFILE=main}" >"$FAKE/conf/proj.conf"
}
sweep() { # sweep [args]: stdout+stderr in $FAKE/out, exit code in $RC
  PATH="$T/bin:$PATH" SWEEP_NO_SUDO=1 SWEEP_CONF_DIR="$FAKE/conf" SWEEP_STATE_DIR="$FAKE/state" SWEEP_PROFILE_ENV="$T/profile-env" \
    SWEEP_HOME="$FAKE/home" NOTIFY_TZ=UTC bash "$SWEEP" "$@" >"$FAKE/out" 2>&1; RC=$?
}
lines() { if [ -f "$FAKE/$1" ]; then grep -c "$2" "$FAKE/$1" || true; else echo 0; fi; }
now_ms() { echo $(( $(date +%s) * 1000 - ${1:-3600} * 1000 )); }
LEFT='{"next":{"kind":"LEFTOVER","task":"T-207"},"leftover":[{"task":"T-207","title":"Fulfil","status":"Building","run":"e1dfd548","environment":"vps"}],"held":[],"blocked":[],"new":[],"notready":[]}'
blocked_json() { # blocked_json <updated ISO>
  echo '{"next":{"kind":"NONE"},"leftover":[],"held":[],"new":[],"notready":[],"blocked":[{"task":"T-9","title":"Pay vendor","status":"Blocked","environment":"vps","run":"abc123","branch":"feat/t9-pay","pr":"12","blocker":"needs founder: approve the invoice","updated":"'"$1"'"}]}'; }
dead_agent() { echo '[{"id":"e1dfd548","kind":"background","name":"routine: proj 2026-10-31 03:04 UTC","state":"failed","startedAt":'"$(now_ms)"'}]'; }

# 1. leftovers -> claude-routine <name>
fresh a; echo "$LEFT" >"$FAKE/next.json"; sweep
[ "$RC" = 0 ] && [ "$(lines started '^proj$')" = 1 ] && ok "leftovers start the routine" || bad "leftovers: rc=$RC started=$(cat "$FAKE/started" 2>/dev/null) out=$(cat "$FAKE/out")"
grep -q -- '--conf' "$FAKE/tracker.log" && ok "tracker is asked with --conf" || bad "tracker args: $(cat "$FAKE/tracker.log")"

# 1b. a live lead: claude-routine itself skips (slot_busy) and says why; the sweep passes that reason on and does not fail
fresh a2; echo "$LEFT" >"$FAKE/next.json"; echo 'claude-routine: slot skipped, proj run e1dfd548 (main) is still working (last record 3 min ago); it is left alone' >"$FAKE/routine_out"; sweep
[ "$RC" = 0 ] && grep -q 'slot skipped, proj run e1dfd548' "$FAKE/out" && ok "live lead: skip reason printed, exit 0" || bad "live lead: rc=$RC out=$(cat "$FAKE/out")"

# 2. nothing left over (new work is the routine's own schedule) -> no start
fresh b; sweep
[ "$RC" = 0 ] && [ ! -e "$FAKE/started" ] && grep -q 'skip proj: no leftovers' "$FAKE/out" && ok "no leftovers, no start, says why" || bad "none: rc=$RC out=$(cat "$FAKE/out")"

# 3. PAUSED=1 and no TRACKER_REPO -> skipped before tracker is even called
fresh c "TRACKER_REPO=acme/tasks
PROFILE=main
PAUSED=1"; echo "$LEFT" >"$FAKE/next.json"; sweep
[ ! -e "$FAKE/started" ] && [ ! -e "$FAKE/tracker.log" ] && grep -q 'paused' "$FAKE/out" && ok "PAUSED=1 is skipped" || bad "paused: $(cat "$FAKE/out")"
fresh d "PROFILE=main"; echo "$LEFT" >"$FAKE/next.json"; sweep
[ ! -e "$FAKE/started" ] && [ ! -e "$FAKE/tracker.log" ] && grep -q 'no TRACKER_REPO' "$FAKE/out" && ok "no TRACKER_REPO is skipped" || bad "no repo: $(cat "$FAKE/out")"

# 4. tracker failure: no start, no notify, non-zero exit so the unit shows failed
fresh e; echo "$LEFT" >"$FAKE/next.json"; echo 2 >"$FAKE/tracker_rc"; sweep
[ "$RC" != 0 ] && [ ! -e "$FAKE/started" ] && ok "tracker error: no start, exit non-zero" || bad "tracker rc: rc=$RC"

# 5. dead run: one notify naming the run and the task back in the queue; the second sweep says nothing
fresh f; echo "$LEFT" >"$FAKE/next.json"; dead_agent >"$FAKE/agents.json"; mkdir -p "$FAKE/cfg/jobs/e1dfd548"; echo '{}' >"$FAKE/cfg/jobs/e1dfd548/state.json"
sweep; sweep
n=$(lines notify '^proj e1dfd548 died ')
[ "$n" = 1 ] && grep -q 'T-207 back in queue' "$FAKE/notify" && ok "dead run: one notify, second sweep silent" || bad "dead: n=$n notify=$(cat "$FAKE/notify" 2>/dev/null)"
# a run that is still working, or done, or died days ago, is not reported
fresh g; echo "$LEFT" >"$FAKE/next.json"
echo '[{"id":"11111111","kind":"background","name":"routine: proj x","state":"working","startedAt":'"$(now_ms)"'},{"id":"22222222","kind":"background","name":"routine: proj y","state":"done","startedAt":'"$(now_ms)"'},{"id":"33333333","kind":"background","name":"routine: proj z","state":"failed","startedAt":'"$(now_ms 259200)"'},{"id":"44444444","kind":"background","name":"routine: other x","state":"failed","startedAt":'"$(now_ms)"'}]' >"$FAKE/agents.json"
sweep
[ ! -e "$FAKE/notify" ] && ok "working, done, old and other-routine runs are not reported" || bad "false dead: $(cat "$FAKE/notify")"

# 6. Blocked row: the sweep sends nothing (tracker block sent the first notice; routine-verdict lists it daily under "Needs you")
fresh h; blocked_json "2026-01-01T00:00Z" >"$FAKE/next.json"; sweep
[ "$RC" = 0 ] && [ ! -e "$FAKE/notify" ] && ok "blocked row: the sweep sends nothing" || bad "blocked: rc=$RC notify=$(cat "$FAKE/notify" 2>/dev/null)"

# 7. --dry-run: prints the decisions, starts nothing, sends nothing, writes no marker
fresh j; echo "$LEFT" >"$FAKE/next.json"; dead_agent >"$FAKE/agents.json"; sweep --dry-run
[ "$RC" = 0 ] && [ ! -e "$FAKE/started" ] && [ ! -e "$FAKE/notify" ] && [ ! -e "$FAKE/state" ] && grep -q 'would start proj' "$FAKE/out" && grep -q 'would notify' "$FAKE/out" \
  && ok "--dry-run: decisions printed, no side effects" || bad "dry-run: rc=$RC out=$(cat "$FAKE/out")"

[ "$fails" = 0 ] && { echo "all passed"; exit 0; } || { echo "$fails failed"; exit 1; }
