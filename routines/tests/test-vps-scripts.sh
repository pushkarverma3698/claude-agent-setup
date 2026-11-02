#!/bin/bash
# Runs setup-claude-routines against a fake ssh, systemctl and install, with every absolute install path remapped into a
# temp root, and checks that the six VPS scripts and nine units land byte-identical with the mode and owner the VPS has
# (2026-11-02: they lived only on the VPS, so nobody could review or reinstall them). No network, no root.
# Usage: bash routines/tests/test-vps-scripts.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$HERE/../.."
T=$(mktemp -d); trap 'rm -r -- "$T"' EXIT
fails=0
ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1"; fails=$((fails+1)); }
R="$T/root"; mkdir -p "$T/bin" "$T/defs" "$R" "$R/var/lib/systemd/timers"
: >"$T/calls.log"
printf 'PROFILE=main\nREPO=acme\nSCHEDULE="03:04"\n' >"$T/defs/demo.conf"; echo demo >"$T/defs/demo.md"

# ssh <host> <cmd>: runs locally. The root script on stdin gets its absolute paths remapped into $R.
cat >"$T/bin/ssh" <<'EOS'
#!/bin/bash
shift; cmd="$*"
case "$cmd" in
  "mktemp -d") mktemp -d ;;
  "tar -xf - -C "*) bash -c "$cmd" ;;
  "sudo -n env STAGE="*)
    sed -e "s#/usr/local/bin#$ROOT/usr/local/bin#g" -e "s#/etc/systemd/system#$ROOT/etc/systemd/system#g" \
        -e "s#/etc/claude-routines#$ROOT/etc/claude-routines#g" -e "s#/home/claude-agent#$ROOT/home/claude-agent#g" \
        -e "s#/var/lib/systemd#$ROOT/var/lib/systemd#g" \
        -e 's#"${rearm\[@\]}"#${rearm[@]+"${rearm[@]}"}#' |   # bash 3.2 (macOS) trips on an empty array under set -u; the VPS has bash 5
     eval "${cmd#sudo -n }" ;;
  *) cat >/dev/null ;;
esac
EOS
cat >"$T/bin/systemctl" <<'EOS'
#!/bin/bash
echo "systemctl $*" >>"$CALLS"; case "$1" in is-active) exit 1;; show) echo soon;; esac; exit 0
EOS
# GNU `install -D -m MODE [-o O -g G] SRC DEST` in bash (the laptop's BSD install has no -D); the requested owner is logged
cat >"$T/bin/install" <<'EOS'
#!/bin/bash
m=755; while [ $# -gt 2 ]; do case "$1" in -m) m=$2; shift 2;; -o|-g) echo "owner $2" >>"$CALLS"; shift 2;; *) shift;; esac; done
mkdir -p "$(dirname "$2")" && cp "$1" "$2" && chmod "$m" "$2"
EOS
printf '#!/bin/bash\nexit 0\n' >"$T/bin/chown"   # not root; claude-agent does not exist here
chmod +x "$T/bin/"*

printf 'AGENT_RULES_HOST=fake\n' >"$T/env"
run() {
  mkdir -p "$T/home/.claude/hooks"
  # HOME is a temp dir: routine-policy --install writes under ~/.claude/hooks
  HOME="$T/home" PATH="$T/bin:$PATH" ROOT="$R" CALLS="$T/calls.log" AGENT_SETUP_ENV="$T/env" ROUTINE_CONF_DIR="$T/defs" \
    bash "$REPO/setup-claude-routines" >"$T/out" 2>&1; echo "$?"
}
mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

rc=$(run)
[ -z "${VERBOSE:-}" ] || { cat "$T/out" "$T/calls.log"; }
[ "$rc" = 0 ] && ok "setup-claude-routines runs against the fakes" || bad "setup rc=$rc: $(tail -5 "$T/out")"

bins="claude-agent-fetch claude-clutter-clean claude-remote-serve claude-remote-update claude-session-reaper routine-worktree"
units="claude-agent-fetch.service claude-agent-fetch.timer claude-clutter-clean.service claude-clutter-clean.timer claude-remote-update.service claude-remote-update.timer claude-remote@.service claude-session-reaper.service claude-session-reaper.timer"
for f in $bins; do
  d="$R/usr/local/bin/$f"
  if cmp -s "$REPO/routines/bin/$f" "$d" && [ "$(mode "$d")" = 755 ]; then ok "$f installed, 755, same bytes"; else bad "$f missing, differs or wrong mode"; fi
done
for f in $units; do
  d="$R/etc/systemd/system/$f"
  if cmp -s "$REPO/routines/systemd/$f" "$d" && [ "$(mode "$d")" = 644 ]; then ok "$f installed, 644, same bytes"; else bad "$f missing, differs or wrong mode"; fi
done
# root:root on the VPS: put() must pass no owner for these
! grep -q '^owner' "$T/calls.log" && ok "no owner requested (root:root, as on the VPS)" || bad "owner requested: $(grep '^owner' "$T/calls.log")"
grep -q '^systemctl daemon-reload' "$T/calls.log" && ok "daemon-reload runs" || bad "no daemon-reload"
# the four timers that are enabled on the VPS are enabled by the installer; nothing else new
for t in claude-agent-fetch claude-clutter-clean claude-remote-update claude-session-reaper; do
  grep -q "^systemctl enable $t.timer" "$T/calls.log" && ok "$t.timer enabled" || bad "$t.timer not enabled"
done
# a second run changes nothing
: >"$T/calls.log"; before=$(cd "$R" && find . -type f -exec cksum {} + | sort | cksum); rc=$(run)
after=$(cd "$R" && find . -type f -exec cksum {} + | sort | cksum)
[ "$rc" = 0 ] && [ "$before" = "$after" ] && ! grep -q 'wrote /.*claude-remote-serve' "$T/out" && ok "second run is idempotent" || bad "second run: rc=$rc"
# public repo: the genericised files carry no private names; the values come from routines.env (HOST_LABEL, REVIEW_KEEP, TESTDB_CONTAINER)
files=$(for f in $bins; do echo "$REPO/routines/bin/$f"; done; for f in $units; do echo "$REPO/routines/systemd/$f"; done)
# shellcheck disable=SC2086
! grep -lniE 'oplify|founderos|bachelor|bh-verify|applyqa' $files >/dev/null && ok "no private names in the six scripts or nine units" || bad "private name in: $(grep -lniE 'oplify|founderos|bachelor|bh-verify|applyqa' $files)"
grep -q 'HOST_LABEL' "$REPO/routines/bin/claude-remote-serve" && grep -q 'REVIEW_KEEP' "$REPO/routines/bin/claude-clutter-clean" && ok "host label and keep-list are read from routines.env" || bad "routines.env keys missing"
[ "$fails" = 0 ] && echo "all ok" || { echo "$fails failed"; exit 1; }
