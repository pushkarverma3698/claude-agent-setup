#!/bin/bash
# Usage: ./claude-remote-hooks/test-guard-bash.sh   (needs jq)
HOOK="$(cd "$(dirname "$0")" && pwd)/guard-bash.sh"; fails=0
check() { # check <want exit> <command>
  local got; got=$(jq -n --arg c "$2" '{tool_name: "Bash", tool_input: {command: $c}}' | "$HOOK" 2>/dev/null; echo $?)
  if [ "$got" = "$1" ]; then echo "ok   $1 $2"; else echo "FAIL want $1 got $got: $2"; fails=$((fails + 1)); fi
}
check 2 'git push origin main'
check 2 'git push -u origin HEAD:main'
check 2 'git push origin +master'
check 2 'git -C /repo push origin main'
check 2 'cd /x && git push origin main && echo done'
check 2 'rm -rf /tmp/x'
check 0 'git push -u origin feat/x && gh pr create --base main --title t'
check 0 'git push -q -u origin chore/x 2>&1 | tail -1; gh pr create --base main'
check 0 'git push origin feat/main-fix'
check 0 'git push origin main-fix'
check 0 'git push; echo main'
check 0 'gh pr merge 12 --merge && git checkout main'
[ "$fails" = 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
