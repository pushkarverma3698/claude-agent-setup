#!/bin/bash
# PreToolUse(Bash): block recursive force-delete and direct pushes to main/master.
# Exit 2 = deny; stderr is shown to Claude. Pushes to branches merely containing "main" pass.
CMD=$(jq -r '.tool_input.command // ""')
if echo "$CMD" | grep -qE '(^|[;&|[:space:]])rm[[:space:]]+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-rf|-fr)\b'; then
  echo 'BLOCKED: rm -rf. Use trash, or delete the exact path without -rf.' >&2; exit 2
fi
# Each command of a chain is judged on its own (2026-11-07: `git push -u origin feat/x && gh pr create --base main`
# was refused 47 times in 7 days, because the old pattern ran from `git push` to the end of the line).
segs=${CMD//&&/$'\n'}; segs=${segs//||/$'\n'}; segs=${segs//;/$'\n'}; segs=${segs//|/$'\n'}
while IFS= read -r seg; do
  if echo "$seg" | grep -qE '(^|[[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push\b.*([[:space:]]|:|\+)(main|master)([[:space:]]|$)'; then
    echo 'BLOCKED: direct push to main. Push a feature branch and open a PR.' >&2; exit 2
  fi
done <<<"$segs"
exit 0
