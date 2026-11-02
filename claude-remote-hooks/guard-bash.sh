#!/bin/bash
# PreToolUse(Bash): block recursive force-delete and direct pushes to main/master.
# Exit 2 = deny; stderr is shown to Claude. Pushes to branches merely containing "main" pass.
CMD=$(jq -r '.tool_input.command // ""')
if echo "$CMD" | grep -qE '(^|[;&|[:space:]])rm[[:space:]]+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-rf|-fr)\b'; then
  echo 'BLOCKED: rm -rf. Use trash, or delete the exact path without -rf.' >&2; exit 2
fi
if echo "$CMD" | grep -qE 'git[[:space:]]+push\b.*([[:space:]]|:)(main|master)([[:space:]]|$)'; then
  echo 'BLOCKED: direct push to main. Push a feature branch and open a PR.' >&2; exit 2
fi
exit 0
