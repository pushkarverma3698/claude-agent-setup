#!/bin/bash
# Run: ./test-token-guard.sh   (no network, no tokens). Exit 0 = all cases pass.
H="$(dirname "$0")/token-guard.py"; T=$(mktemp -d); trap 'rm -r "$T"' EXIT
# fixture policy: the guard's repo rules come from routine-policy(.defaults).json, never from this machine's install
cat >"$T/defaults.json" <<'JSON'
{"repos": {
  "acme/task-api": {"bases": ["chore/integration"], "status": "local-ci", "runner": "ci-run", "task": "T-NNN",
    "format": "task-contract.md step 7", "branch": "^feat/t(\\d+)-", "builder": "Task API, task T-?(\\d+)\\b"},
  "widgets-co/widgets-api": {"bases": ["beta", "production"], "branch": "^task/issue-(\\d+)-", "from": "production"},
  "widgets-co/widgets-app": {"bases": ["beta", "production"], "branch": "^task/issue-(\\d+)-", "from": "production"}}}
JSON
export TOKEN_GUARD_DEFAULTS="$T/defaults.json" TOKEN_GUARD_POLICY="$T/no-policy.json"
seq 1 1000 >"$T/big.txt"; seq 1 20 >"$T/small.txt"; seq 1 20 >"$T/small2.txt"
fail=0
run() { # run <expect: deny|allow|wrap|ctx> <json>
  out=$(printf '%s' "$2" | python3 "$H" 2>/dev/null); rc=$?
  case "$1" in
    deny) [ $rc -eq 2 ] ;;
    allow) [ $rc -eq 0 ] && [ -z "$out" ] ;;
    wrap) [ $rc -eq 0 ] && grep -q 'merge refused' <<<"$out" ;;
    ctx) [ $rc -eq 0 ] && grep -q 'auto-compacted' <<<"$out" ;;
  esac && echo "ok   $1  $3" || { echo "FAIL $1  $3 (rc=$rc)"; fail=1; }
}
b() { printf '{"hook_event_name":"PreToolUse","tool_name":"Bash","cwd":"%s","tool_input":{"command":%s}}' "$T" "$(jq -Rn --arg c "$1" '$c')"; }
run deny  "$(b 'for f in src/*.ts; do cat $f; done')" "cat loop"
run deny  "$(b 'find . -name "*.ts" | xargs cat')" "xargs cat"
run deny  "$(b 'find . -name "*.ts" -exec cat {} \;')" "-exec cat"
run deny  "$(b 'cat src/*.ts')" "cat glob"
run deny  "$(b 'cat big.txt')" "cat 1000-line file"
run deny  "$(b "sed -n '1,900p' big.txt")" "sed huge range"
run deny  "$(b 'head -n 2000 big.txt')" "head 2000"
run allow "$(b 'cat small.txt small2.txt')" "cat two small files"
run allow "$(b 'cat big.txt | grep 5')" "cat piped to grep"
run allow "$(b 'cat > out.txt <<EOF
x
EOF')" "heredoc write"
run allow "$(b "sed -n '10,80p' big.txt")" "sed small range"
run allow "$(b 'git log | head -n 500 > log.txt')" "head to file"
run allow "$(b 'git status')" "plain command"
run allow "$(b 'npm run verify')" "npm run verify runs unlocked"
run allow "$(b 'npm ci')" "npm ci runs unlocked (2026-10-25: four parallel installs froze the slice)"
run allow "$(b 'cd x && pnpm install --frozen-lockfile')" "pnpm install runs unlocked"
run allow "$(b 'cd x && pnpm test')" "pnpm test runs unlocked"
run allow "$(b 'npx tsc --noEmit')" "npx tsc runs unlocked"
run allow "$(b 'pnpm -C packages/app install')" "pnpm -C dir install runs unlocked (flag before subcommand)"
run allow "$(b 'npm --prefix web ci')" "npm --prefix web ci runs unlocked"
run allow "$(b 'pnpm --filter api i --frozen-lockfile')" "pnpm --filter api i runs unlocked"
run allow "$(b 'npm view left-pad version')" "npm view not runs unlocked"
run deny  "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"mcp__claude_ai_Google_Drive__create_file\",\"tool_input\":{\"content\":\"$(head -c 6000 /dev/zero | tr '\0' a)\"}}" "Drive create_file 6K"
run allow '{"hook_event_name":"PreToolUse","tool_name":"mcp__claude_ai_Gmail__create_draft","tool_input":{"body":"short email with a link"}}' "short email"
run deny  "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"cwd\":\"$T\",\"tool_input\":{\"file_path\":\"$T/big.txt\"}}" "Read whole 1000-line file"
run allow "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"cwd\":\"$T\",\"tool_input\":{\"file_path\":\"$T/big.txt\",\"offset\":1,\"limit\":200}}" "Read with limit"
run allow "{\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Read\",\"cwd\":\"$T\",\"tool_input\":{\"file_path\":\"$T/small.txt\"}}" "Read small file"
run ctx   '{"hook_event_name":"SessionStart","source":"compact"}' "after compaction"
run allow '{"hook_event_name":"SessionStart","source":"startup"}' "normal start"
run allow 'not json' "malformed input"
# PR guards (2026-10-29: a phone PR merged into the task repo beta; phone PRs without "Closes T-NNN")
BH=acme/task-api
# the full task-contract.md step 7 format; REST is everything after the task line
export REST='## What changed\nx\n## Criteria\n- [x] C1: test\n## Spec refs\nC1\n## How it was verified\ny\n## Not verified\nnone\n## Risk\nlow\n'
printf "Closes T-127\n$REST" >"$T/good.md"
printf "$REST" >"$T/nocloses.md"
# 2026-10-29: the phone PR format (#80): Closes, but no criteria checklist, Spec refs or Risk
printf 'Checked first: x\n## What changed\nx\n## How it was verified\ny\n## NOT VERIFIED\nnone\nCloses T-204\n' >"$T/phone.md"
printf 'Fixes #12\nChecked first: x\nRoot cause: y\n' >"$T/oldwidgets.md"
run deny  "$(b "gh pr create -R $BH --base beta --title t --body-file good.md")" "task-repo PR into beta refused"
run deny  "$(b "gh pr create -R $BH --title t --body-file good.md")" "task-repo PR without --base refused"
run deny  "$(b "gh pr create -R $BH --base chore/integration --title t --body-file nocloses.md")" "task-repo PR without Closes/Part of refused"
printf "Part of T-127\n$REST" >"$T/partnoleft.md"
run deny  "$(b "gh pr create -R $BH --base chore/integration --title t --body-file $T/partnoleft.md")" "task-repo PR with Part of but no Left: refused"
run allow "$(b "gh pr create --draft -R $BH --base chore/integration --title t --body-file $T/good.md")" "task-repo PR with base, Closes and sections"
run deny  "$(b "gh pr create --draft -R $BH --base chore/integration --title t --body-file $T/phone.md")" "task-repo PR in the old phone format (no criteria, Spec refs, Risk) refused"
o=$(printf '%s' "$(b "gh pr create -R $BH --base chore/integration --title t --body-file $T/phone.md")" | python3 "$H" 2>&1 >/dev/null)
grep -q 'criteria checklist.*Spec refs.*Risk.*task-contract.md' <<<"$o" && echo "ok   refusal names each missing part and task-contract.md" || { echo "FAIL refusal text: $o"; fail=1; }
run allow "$(b "gh pr create -R $BH --base chore/integration --title t --body 'Closes T-127
## What changed
## Criteria
- [x] C1: test
## Spec refs
## How it was verified
## Not verified
## Risk'")" "task-repo PR with an inline --body opening on Closes"
run allow "$(b "gh pr create -R widgets-co/widgets-api --base beta --title t --body \"## What changed
x
## How it was verified
## Not verified\"")" "widgets PR with an inline --body opening on a heading"
run deny  "$(b "gh pr create -R widgets-co/widgets-api --base beta --title t --body-file oldwidgets.md")" "widgets PR without the three sections refused"
run allow "$(b "gh pr create -R widgets-co/widgets-api --base production --head beta --title promote --body x")" "widgets promotion PR to production"
run allow "$(b "gh pr create -R someone/other --base main --title t --body x")" "other repos untouched"
run wrap  "$(b "gh pr merge 84 -R $BH --squash --delete-branch")" "merge gets a live base check"
run allow "$(b "gh pr view 84 -R $BH")" "pr view untouched"
run deny  "$(b "gh api repos/$BH/statuses/abc -f state=success -f context=local-ci")" "agents cannot post local-ci themselves"
run deny  "$(b "gh api -X POST repos/$BH/statuses/abc -f state=success -f context=local-ci")" "local-ci post with -X POST refused"
run deny  "$(b "gh api repos/$BH/statuses/abc --method POST -f state=success -f context=local-ci")" "local-ci post with --method POST refused"
run deny  "$(b "gh api repos/$BH/statuses/abc -F context=local-ci -F state=success")" "local-ci post with -F refused"
run deny  "$(b "echo go && gh api repos/$BH/statuses/abc --raw-field context=local-ci -f state=success --jq '.a | .b'")" "local-ci post after && with a jq pipe refused"
run deny  "$(b "curl -X POST -H 'Authorization: token x' -d '{\"state\":\"success\",\"context\":\"local-ci\"}' https://api.github.com/repos/$BH/statuses/abc")" "curl POST of local-ci refused"
run allow "$(b "gh api repos/$BH/statuses/abc --jq '.[] | select(.context==\"local-ci\")'")" "read of local-ci statuses with jq select (2026-10-30 false positive)"
run allow "$(b "gh api repos/$BH/statuses/abc --jq '.[] | select(.context == \"local-ci\") | .state'")" "read with spaced jq select"
run allow "$(b "gh api -X GET repos/$BH/statuses/abc -f per_page=5 --jq '.[]|select(.context==\"local-ci\")'")" "explicit GET read"
run allow "$(b "gh api repos/$BH/statuses/abc -f state=success -f context=routine/review -f description=ok")" "routine/review status post untouched"
run allow "$(b "curl -s https://api.github.com/repos/$BH/statuses/abc | grep local-ci")" "curl read untouched"
run allow "$(b "ci-run 77")" "ci-run itself untouched (it posts from its own script, outside the hook)"
run allow "$(b "ci-run 84 > /tmp/ci-run-84.log 2>&1")" "ci-run itself runs"
# the wrapped merge runs only when the PR's base is the merge base and, for the task repo, the head commit
# has local-ci=success (fake gh: view prints url + base + head sha, api prints the local-ci state)
mkdir -p "$T/bin"; cat >"$T/bin/gh" <<'GH'
#!/bin/bash
[ "$2" = view ] && grep -q -- '--json body' <<<"$*" && { printf '%b\n' "${FAKE_BODY-Closes T-1\n$REST}"; exit 0; }
[ "$2" = view ] && { echo "https://github.com/acme/task-api/pull/$3 $FAKE_BASE abc1234def5678"; exit 0; }
[ "$1" = api ] && grep -q '/compare/' <<<"$*" && { echo "$FAKE_CMP"; exit 0; }
[ "$1" = api ] && { grep -q 'commits/abc1234def5678/status' <<<"$*" && echo "${FAKE_CI-success}"; exit 0; }
echo "MERGED $3"
GH
chmod +x "$T/bin/gh"
w=$(printf '%s' "$(b "gh pr merge 77 -R $BH --squash")" | python3 "$H" | jq -r .hookSpecificOutput.updatedInput.command)
o=$(FAKE_BASE=beta PATH="$T/bin:$PATH" bash -c "$w" 2>&1); r=$?
[ $r -ne 0 ] && ! grep -q MERGED <<<"$o" && grep -q "merge refused: PR 77 targets beta" <<<"$o" && echo "ok   merge into beta refused at run time" || { echo "FAIL merge into beta: rc=$r $o"; fail=1; }
o=$(FAKE_BASE=chore/integration PATH="$T/bin:$PATH" bash -c "$w" 2>&1); r=$?
[ $r -eq 0 ] && grep -q "MERGED 77" <<<"$o" && echo "ok   merge into the integration branch runs" || { echo "FAIL merge into integration: rc=$r $o"; fail=1; }
for ci in failure pending ""; do  # 2026-10-29: PRs #80-#88 merged with every GitHub check red; ci-run is the gate
  o=$(FAKE_CI=$ci FAKE_BASE=chore/integration PATH="$T/bin:$PATH" bash -c "$w" 2>&1); r=$?
  [ $r -ne 0 ] && ! grep -q MERGED <<<"$o" && grep -q "local-ci=${ci:-none}; run ci-run 77" <<<"$o" && echo "ok   merge refused at run time without local-ci success (${ci:-none})" || { echo "FAIL local-ci ${ci:-none}: rc=$r $o"; fail=1; }
done
# 2026-10-29: the task line is checked again at merge (body edited later, or a PR opened on github.com)
for body in "$REST" "Part of T-1\nno left line\n$REST" "see Closes T-1 in prose\n$REST" "$(cat "$T/phone.md")"; do
  o=$(FAKE_BODY=$body FAKE_BASE=chore/integration PATH="$T/bin:$PATH" bash -c "$w" 2>&1); r=$?
  [ $r -ne 0 ] && ! grep -q MERGED <<<"$o" && grep -q "PR 77 body lacks" <<<"$o" && echo "ok   merge refused at run time: body '$body'" || { echo "FAIL body '$body': rc=$r $o"; fail=1; }
done
for body in "x\n- Closes T-1, T-2\n$REST" "Part of T-1\nLeft: AD3\n$REST" "Closes T-1\n${REST/How it was verified/How verified}"; do
  o=$(FAKE_BODY=$body FAKE_BASE=chore/integration PATH="$T/bin:$PATH" bash -c "$w" 2>&1); r=$?
  [ $r -eq 0 ] && grep -q "MERGED 77" <<<"$o" && echo "ok   merge runs: body '$body'" || { echo "FAIL body '$body': rc=$r $o"; fail=1; }
done
# --pr-only (the laptop): PR guards only, and a merge is checked before it runs, never rewritten,
# so the laptop's permission prompts still apply to the command
lap() { # lap <expect: deny|allow> <base the fake gh reports> <command> <label>   (FAKE_CI from the caller)
  out=$(printf '%s' "$(b "$3")" | FAKE_BASE=$2 PATH="$T/bin:$PATH" python3 "$H" --pr-only 2>/dev/null); rc=$?
  case "$1" in deny) [ $rc -eq 2 ] ;; allow) [ $rc -eq 0 ] && [ -z "$out" ] ;; esac \
    && echo "ok   laptop $1  $4" || { echo "FAIL laptop $1  $4 (rc=$rc $out)"; fail=1; }
}
lap allow x 'cat big.txt' "cat limits are VPS-only"
lap deny  x "gh pr create -R $BH --base beta --title t --body-file good.md" "task-repo PR into beta refused"
lap deny  beta "gh pr merge 77 -R $BH --squash" "merge into beta refused before it runs"
lap allow chore/integration "gh pr merge 85 -R $BH --squash" "merge into the integration branch allowed, not rewritten"
FAKE_CI=failure lap deny chore/integration "gh pr merge 85 -R $BH --squash" "merge with local-ci failure refused"
FAKE_CI= lap deny chore/integration "gh pr merge 85 -R $BH --squash" "merge without a local-ci status refused"
FAKE_BODY='Part of T-1' lap deny chore/integration "gh pr merge 85 -R $BH --squash" "merge of a Part of PR without Left: refused"
FAKE_BODY="$(cat "$T/phone.md")" lap deny chore/integration "gh pr merge 85 -R $BH --squash" "merge of an old phone-format PR refused"
FAKE_BODY='## What changed' lap deny chore/integration "gh pr merge 85 -R $BH --squash" "merge without a task line refused"
lap deny  beta "gh pr merge https://github.com/acme/task-api/pull/77" "merge by URL checked too"
lap allow beta "gh pr merge 12 -R someone/scripts --squash" "other repos untouched"
lap deny  beta "gh pr merge -R $BH --squash" "merge of the current branch's PR checked too"
w=$(printf '%s' "$(b "gh pr merge --squash -R $BH")" | python3 "$H" | jq -r .hookSpecificOutput.updatedInput.command)
grep -q 'gh pr view  -R' <<<"$w" && ! grep -q 'view --squash' <<<"$w" && echo "ok   VPS wrapper reads the current branch's PR, not '--squash'" || { echo "FAIL no-number merge wrapper: $w"; fail=1; }
# claim before push (2026-10-29: T-247 built twice): a task-repo feat/t<id>-* push needs the branch on GitHub already
G="$T/gh/acme/task-api.git"; mkdir -p "$G"; git init -q --bare "$G"
git init -q "$T/tr" && git -C "$T/tr" remote add origin "$G" && git -C "$T/tr" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
git -C "$T/tr" push -q origin HEAD:refs/heads/feat/t247-disputes HEAD:refs/heads/routine-wip/nightly/r1/feat/t205-x 2>/dev/null
git -C "$T/tr" checkout -q -b feat/t205-x
p() { printf '{"hook_event_name":"PreToolUse","tool_name":"Bash","cwd":"%s","tool_input":{"command":%s}}' "$T/tr" "$(jq -Rn --arg c "$1" '$c')"; }
pr() { out=$(printf '%s' "$(p "$2")" | python3 "$H" $4 2>/dev/null); rc=$?
  case "$1" in deny) [ $rc -eq 2 ] ;; allow) [ $rc -eq 0 ] && [ -z "$out" ] ;; esac \
    && echo "ok   $1  $3 $4" || { echo "FAIL $1  $3 $4 (rc=$rc $out)"; fail=1; }; }
for m in "" --pr-only; do
  pr deny  "git push -u origin feat/t247-ownership-disputes" "unclaimed task branch refused" $m
  pr deny  "git push" "unclaimed current branch refused (only a routine-wip copy exists)" $m
  pr deny  "git -C $T/tr push origin HEAD:refs/heads/feat/t1-new" "unclaimed refspec dest refused" $m
  pr allow "git push origin feat/t247-disputes" "claimed task branch pushes" $m
  pr allow "git push --force-with-lease origin feat/t247-disputes" "force-with-lease on a claimed branch" $m
  pr deny  "git push -f origin feat/t247-disputes" "plain --force on a task branch refused" $m
  pr deny  "git push origin +feat/t247-disputes" "+refspec force refused" $m
  pr allow "git push origin HEAD:routine-wip/nightly/r2/feat/t205-x" "routine-wip pushes untouched" $m
  pr allow "git push origin --delete feat/t1-old" "branch delete untouched" $m
done
run allow "$(b 'git push origin feat/t1-x')" "push in a non-task-repo repo untouched"
# exact owner/repo (2026-10-29: the nightly lead, cwd in a task-api worktree, ran `R=<design pack>;
# gh pr create -R $R --base main`; `$R` was not read and the design-pack backlog PR got the task-repo rules)
PK="$T/gh/acme/design-pack.git"; mkdir -p "$PK"; git init -q --bare "$PK"
git init -q "$T/pack" && git -C "$T/pack" remote add origin "$PK"
printf 'Backlog status: 57/117 done.\n' >"$T/pack.md"
q() { # q <expect: deny|allow> <cwd> <command> <label> [--pr-only]
  out=$(printf '{"hook_event_name":"PreToolUse","tool_name":"Bash","cwd":"%s","tool_input":{"command":%s}}' "$2" \
    "$(jq -Rn --arg c "$3" '$c')" | python3 "$H" $5 2>/dev/null); rc=$?
  case "$1" in deny) [ $rc -eq 2 ] ;; allow) [ $rc -eq 0 ] && [ -z "$out" ] ;; esac \
    && echo "ok   $1  $4 $5" || { echo "FAIL $1  $4 $5 (rc=$rc $out)"; fail=1; }; }
for m in "" --pr-only; do
  q allow "$T/tr" "R=acme/design-pack; gh pr create -R \$R --base main --head chore/backlog-status-2026-10-29b --title t --body-file $T/pack.md" "design-pack PR via -R \$R from a task-repo cwd" $m
  q allow "$T/tr" "cd $T/pack && R=\$(git remote get-url origin | sed -E 's#.*github.com[:/]##'); gh pr create -R \$R --base main --title t --body-file $T/pack.md && gh pr merge -R \$R --squash --delete-branch chore/x" "design-pack PR + merge via cd and R=\$(...)" $m
  q allow "$T/tr" "gh pr create -R acme/design-pack --base main --title t --body-file $T/pack.md" "design-pack PR via literal -R from a task-repo cwd" $m
  q allow "$T/tr" "gh pr create -R someone/task-api --base main --title t --body-file $T/nocloses.md" "same repo name, other owner untouched" $m
  q allow "$T/tr" "gh pr create -R acme/task-api-v2 --base main --title t --body-file $T/nocloses.md" "longer repo name untouched" $m
  q deny  "$T/pack" "gh pr create -R $BH --base chore/integration --title t --body-file $T/nocloses.md" "task-repo PR without Closes still refused from a pack cwd" $m
  q deny  "$T/pack" "R=$BH; gh pr create -R \$R --base chore/integration --title t --body-file $T/nocloses.md" "task-repo PR without Closes refused via -R \$R" $m
  q deny  "$T/tr" "gh pr create --base chore/integration --title t --body-file $T/nocloses.md" "task-repo PR without Closes refused from a task-repo cwd (no -R)" $m
  q deny  "$T/pack" "gh pr create -R ACME/Task-API --base main --title t --body-file $T/good.md" "owner/repo case does not dodge the guard" $m
done
q allow "$T/pack" "gh pr merge 4 --squash" "design-pack merge is not wrapped"
q allow "$T/tr" "gh pr merge 4 -R acme/design-pack --squash" "design-pack merge by -R from a task-repo cwd is not wrapped"
q allow "$T/tr" "gh pr merge 12 -R someone/scripts --squash" "other repo merge is not wrapped"
o=$(printf '{"hook_event_name":"PreToolUse","tool_name":"Bash","cwd":"%s","tool_input":{"command":"gh pr merge 77 --squash"}}' "$T/tr" | python3 "$H")
grep -q 'merge refused' <<<"$o" && grep -q -- "view 77 -R $BH" <<<"$o" && echo "ok   task-repo merge from a task-repo cwd (no -R) is wrapped with -R $BH" || { echo "FAIL task-repo cwd merge wrap: $o"; fail=1; }
U="https://github.com/$BH/pull/77"
o=$(printf '{"hook_event_name":"PreToolUse","tool_name":"Bash","cwd":"%s","tool_input":{"command":"gh pr merge %s"}}' "$T/pack" "$U" | python3 "$H")
grep -q 'merge refused' <<<"$o" && echo "ok   task-repo merge by URL from a pack cwd is wrapped" || { echo "FAIL task-repo URL merge wrap: $o"; fail=1; }
# 2026-10-29 audit: merge_targets only matched `gh pr merge`, so the REST merge call and the GraphQL mutation
# skipped the base, task-line and local-ci checks. Strings are built from G so no hook on a laptop matches this file.
G='g''h'; OP=acme/task-api
printf 'mutation { mergePullRequest(input: {pullRequestId: "PR_x"}) { clientMutationId } }\n' >"$T/m.graphql"
printf 'query { viewer { login } }\n' >"$T/ok.graphql"
for m in "" --pr-only; do
  q deny  "$T/tr" "$G api -X PUT repos/$OP/pulls/90/merge -f merge_method=squash" "REST merge refused" $m
  q deny  "$T/pack" "$G api --method PUT /repos/$OP/pulls/90/merge" "REST merge with --method and leading slash refused" $m
  q deny  "$T/pack" "$G api --method=put repos/ACME/Task-API/pulls/90/merge" "REST merge: repo case and method case do not dodge" $m
  q deny  "$T/tr" "$G api repos/{owner}/{repo}/pulls/90/merge -X PUT" "REST merge with {owner}/{repo} placeholders from a task-repo cwd refused" $m
  q deny  "$T/tr" "R=$OP; $G api -X PUT repos/\$R/pulls/90/merge" "REST merge through \$R refused" $m
  q deny  "$T/pack" "$G api -X PUT repos/widgets-co/widgets-api/pulls/3/merge" "widgets REST merge refused" $m
  q deny  "$T/tr" "$G api graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\"X\"}) { clientMutationId } }'" "GraphQL mergePullRequest refused" $m
  q deny  "$T/pack" "$G api graphql -f query='mutation { enablePullRequestAutoMerge(input:{pullRequestId:\"X\"}) { clientMutationId } }'" "GraphQL auto-merge refused" $m
  q deny  "$T/pack" "$G api graphql -F query=@$T/m.graphql" "GraphQL mutation hidden in a query file refused" $m
  q deny  "$T/pack" "$G api -X POST repos/$OP/merges -f base=chore/integration -f head=feat/t1-x" "branch-merge API refused" $m
  q deny  "$T/tr" "$G api -X PATCH repos/$OP/git/refs/heads/chore/integration -f sha=abc" "moving the integration branch ref refused" $m
  q allow "$T/tr" "$G api repos/$OP/pulls/90" "PR read untouched" $m
  q allow "$T/tr" "$G api repos/$OP/pulls/90/merge" "GET is-merged check untouched" $m
  q allow "$T/tr" "$G api -X PATCH repos/$OP/pulls/78 -f base=chore/integration" "retargeting a PR untouched" $m
  q allow "$T/tr" "$G api -X PUT repos/someone/scripts/pulls/3/merge" "other repo's REST merge, named literally, untouched even from a task-repo cwd" $m
  q allow "$T/pack" "$G api graphql -F query=@$T/ok.graphql" "GraphQL read from a file untouched" $m
  q allow "$T/tr" "$G api repos/$OP/git/ref/heads/chore/integration" "reading the integration branch ref untouched" $m
done
# claim before dispatch (2026-10-29): a task-repo builder prompt needs the branch and claim sha its lead got from claim-branch,
# and GitHub (fake gh compare: "<status> <claim commit subject>") must show that claim commit on the branch
CL="chore(claim): reserve feat/t205-fulfil for one session"
P205="Task API, task T-205, new, branch feat/t205-fulfil claim abc1234, schema: no. Read /home/claude-agent/agent-jobs/task-contract.md and follow its \"## Build\" section."
d() { # d <expect: deny|allow> <compare answer> <prompt> <label> [--pr-only] [tool]
  out=$(printf '{"hook_event_name":"PreToolUse","tool_name":"%s","cwd":"%s","tool_input":{"description":"b","prompt":%s}}' "${6:-Agent}" "$T" \
    "$(jq -Rn --arg c "$3" '$c')" | FAKE_CMP=$2 PATH="$T/bin:$PATH" python3 "$H" $5 2>/dev/null); rc=$?
  case "$1" in deny) [ $rc -eq 2 ] ;; allow) [ $rc -eq 0 ] && [ -z "$out" ] ;; esac \
    && echo "ok   $1  $4 $5" || { echo "FAIL $1  $4 $5 (rc=$rc $out)"; fail=1; }; }
for m in "" --pr-only; do
  d allow "identical $CL" "$P205" "builder on its claimed branch (tip is the claim)" $m
  d allow "ahead $CL" "${P205/new/resume branch feat/t205-fulfil}" "builder on a claimed branch with work on top" $m
  d allow "identical $CL" "$P205" "older Task tool name checked too" "$m" Task
  d allow "identical $CL" "Task API, task T-205, new, schema: no, branch feat/t205-fulfil claim abc1234." "branch read without a trailing full stop" $m
  d deny  "identical $CL" "Task API, task T-205, new, schema: no. Read task-contract.md" "builder without branch and claim refused" $m
  d deny  "identical $CL" "${P205/ claim abc1234/}" "builder without claim sha refused" $m
  d deny  "identical $CL" "${P205/feat\/t205-fulfil/feat/t214-coupons}" "branch of another task refused" $m
  d deny  "" "$P205" "no answer from GitHub (claim exit 3 left no sha, or network) refused" $m
  d deny  "diverged $CL" "$P205" "claim not on the branch refused" $m
  d deny  "identical feat(x): work" "$P205" "sha that is not a claim commit refused" $m
  d allow "" "Review PR 77 in acme/task-api and reply in 10 lines" "other agent prompts untouched" $m
done
# the generic marker `task <owner/repo>#<id>` (2026-10-30): any claimed repo in routine-policy, widgets included
OPI="widgets-co/widgets-api"
CLO="chore(claim): reserve task/issue-42-login for one session"
PO="Repo $OPI, task $OPI#42, new, branch task/issue-42-login claim abc1234, gate: pnpm test."
for m in "" --pr-only; do
  d allow "identical $CL" "task acme/task-api#T-205, new, branch feat/t205-fulfil claim abc1234." "task-repo marker form checked like the old phrase" $m
  d deny  "identical $CL" "task acme/task-api#205, new. Read task-contract.md" "task-repo marker without claim refused" $m
  d allow "identical $CLO" "$PO" "widgets builder on its claimed branch" $m
  d deny  "identical $CLO" "${PO/ claim abc1234/}" "widgets builder without claim sha refused" $m
  d deny  "identical $CLO" "${PO/issue-42-login/issue-7-other}" "widgets branch of another issue refused" $m
  d deny  "identical $CLO" "${PO/branch task\/issue-42-login/branch feat/t42-login}" "branch outside the repo's pattern refused" $m
  d allow "" "Repo $OPI, issue #42: summarise it" "widgets prompt without the marker untouched" $m
done
# routine-policy.json next to the hook (TOKEN_GUARD_POLICY in tests): it adds repos; a broken file keeps the defaults
printf '%s' '{"repos":{"acme/widgets":{"bases":["main"],"branch":"^task/w(\\d+)-"}}}' > "$T/pol.json"
printf '%s' '{"repos":' > "$T/bad.json"
CLW="chore(claim): reserve task/w9-x for one session"
TOKEN_GUARD_POLICY="$T/pol.json" d allow "identical $CLW" "task acme/widgets#9, branch task/w9-x claim abc1234" "repo added by the policy file: claimed builder runs" --pr-only
TOKEN_GUARD_POLICY="$T/pol.json" d deny  "identical $CLW" "task acme/widgets#9, new" "repo added by the policy file: unclaimed builder refused" --pr-only
TOKEN_GUARD_POLICY="$T/pol.json" d deny  "identical $CL" "${P205/ claim abc1234/}" "policy file keeps the default task-repo rule" --pr-only
TOKEN_GUARD_POLICY="$T/bad.json" d deny  "identical $CL" "${P205/ claim abc1234/}" "broken policy file falls back to the defaults" --pr-only
printf '%s' '{"repos":{"acme/widgets":{"bases":["main"],"branch":"^task/w("},"acme/quote":{"bases":["main'"'"'x"],"branch":"^q(\\d+)-"}}}' > "$T/badrx.json"
TOKEN_GUARD_POLICY="$T/badrx.json" d allow "" "task acme/widgets#9, new" "entry with a broken regex dropped, hook does not crash" --pr-only
TOKEN_GUARD_POLICY="$T/badrx.json" d allow "" "task acme/quote#9, new" "entry with a quote in a base dropped" --pr-only
TOKEN_GUARD_POLICY="$T/nope.json" d allow "" "task acme/widgets#9, new" "repo absent from every policy untouched" --pr-only
exit $fail
