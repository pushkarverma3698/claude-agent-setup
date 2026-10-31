#!/bin/bash
# Runs routines/bin/claude-bgd against a fake `claude` in a temp HOME. No real daemon, no root.
# Usage: bash routines/tests/test-claude-bgd.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BGD="$HERE/../bin/claude-bgd"
T=$(mktemp -d); pids=()
trap 'for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done; rm -r -- "$T"' EXIT
fails=0
ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1"; fails=$((fails+1)); }

# Fake profile-env: points $claude at the fake. Fake claude: state lives in files under $FAKE.
# status prints "origin: foreground" while $FAKE/fg exists. `daemon run` logs a line, creates fg, then
# blocks until $FAKE/exit_run appears (exit_keep_fg: leave fg behind, like a successor that took over).
cat >"$T/profile-env" <<'EOS'
claude="$FAKE/claude"
EOS
cat >"$T/claude" <<'EOS'
#!/bin/bash
case "$1 $2" in
  "daemon status") if [ -e "$FAKE/fg" ]; then printf 'pid: 1\norigin:  foreground\n'; else printf 'pid: 1\norigin: transient\n'; fi ;;
  "daemon run")
    echo "run $$" >>"$FAKE/runs"; touch "$FAKE/fg"
    while [ ! -e "$FAKE/exit_run" ]; do sleep 0.05; done
    rm -f "$FAKE/exit_run"; [ -e "$FAKE/exit_keep_fg" ] || rm -f "$FAKE/fg"; exit 3 ;;
esac
EOS
chmod +x "$T/claude"

start() { # start <name>: fresh state dir, wrapper in the background; sets FAKE, W (pid), OUT
  FAKE="$T/$1"; mkdir -p "$FAKE" "$FAKE/home"; export FAKE
  cp "$T/claude" "$FAKE/claude"
  OUT="$FAKE/out"; : >"$OUT"
  CLAUDE_BGD_PROFILE_ENV="$T/profile-env" CLAUDE_BGD_HOME="$FAKE/home" CLAUDE_BGD_TICK=0.2 \
    bash "$BGD" main >"$OUT" 2>&1 & W=$!; pids+=("$W")
}
stop_() { kill "$W" 2>/dev/null; wait "$W" 2>/dev/null; }
runs() { if [ -f "$FAKE/runs" ]; then wc -l <"$FAKE/runs" | tr -d ' '; else echo 0; fi; }
until_() { local n=$1; shift; for _ in $(seq 1 $((n*20))); do "$@" && return 0; sleep 0.05; done; return 1; } # until_ <secs> cmd...
runs_ge() { [ "$(runs)" -ge "$1" ]; }
gone() { ! kill -0 "$W" 2>/dev/null; }

# (a) no foreground daemon: the loop starts `daemon run`, and logs it
start a
if until_ 5 runs_ge 1; then ok "a: starts daemon run when status is not foreground"; else bad "a: no daemon run started"; fi
grep -q 'starting daemon run (previous run exit: none)' "$OUT" && ok "a: journal line on first start" || bad "a: no start line: $(cat "$OUT")"
# (b) while status says foreground and the run is still going, no second run over many ticks
sleep 1.2
[ "$(runs)" = 1 ] && ok "b: no second daemon run while foreground (5+ ticks)" || bad "b: runs=$(runs)"
stop_

# (b2) successor case: `daemon run` exits but a foreground daemon is still answering -> do not start another
start b2; touch "$FAKE/exit_keep_fg"
until_ 5 runs_ge 1; touch "$FAKE/exit_run"; sleep 1.2
[ "$(runs)" = 1 ] && ok "b2: run exited but status still foreground: no new run" || bad "b2: runs=$(runs)"
stop_

# (b3) status already foreground at start (daemon not ours): never starts one
start b3; touch "$FAKE/fg"; sleep 1
[ "$(runs)" = 0 ] && ok "b3: foreground daemon already up: no run" || bad "b3: runs=$(runs)"
stop_

# (c) daemon run exits (daemon gone): started again on the next tick, previous exit code logged
start c
until_ 5 runs_ge 1; touch "$FAKE/exit_run"
if until_ 5 runs_ge 2; then ok "c: restarted after daemon run exited"; else bad "c: runs=$(runs)"; fi
grep -q 'previous run exit: 3' "$OUT" && ok "c: journal line carries previous exit code" || bad "c: no exit code line: $(cat "$OUT")"
stop_

# (d) SIGTERM: loop ends with exit 0 within 3s, and the running `daemon run` child is gone
start d
until_ 5 runs_ge 1
child=$(awk '{print $2}' "$FAKE/runs" | head -1)
kill -TERM "$W"
if until_ 3 gone; then wait "$W"; rc=$?; [ "$rc" = 0 ] && ok "d: SIGTERM ends the loop, exit 0" || bad "d: exit=$rc"; else bad "d: wrapper still alive 3s after SIGTERM"; fi
if kill -0 "$child" 2>/dev/null; then bad "d: daemon run child $child still alive"; else ok "d: daemon run child stopped"; fi
sleep 0.5; [ "$(runs)" = 1 ] && ok "d: nothing restarted after SIGTERM" || bad "d: runs=$(runs)"
# (d2) SIGTERM during the sleep between ticks (daemon already foreground, no child run)
start d2; touch "$FAKE/fg"; sleep 0.5; kill -TERM "$W"
if until_ 3 gone; then ok "d2: SIGTERM during the tick sleep ends the loop"; else bad "d2: wrapper still alive"; fi

echo "fails=$fails"; [ "$fails" = 0 ]
