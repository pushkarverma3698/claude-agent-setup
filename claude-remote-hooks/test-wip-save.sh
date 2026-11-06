#!/bin/bash
# Run: ./test-wip-save.sh   (no network: origin is a local bare repo). Exit 0 = all cases pass.
H="$(cd "$(dirname "$0")" && pwd)/wip-save.py"; T=$(mktemp -d); trap 'rm -r -f "$T"' EXIT
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t GIT_CONFIG_GLOBAL=/dev/null
printf '{"repos":{"acme/app":{"bases":["main"],"branch":"^feat/t(\\\\d+)-"}}}' >"$T/policy.json"
export TOKEN_GUARD_POLICY="$T/policy.json" TOKEN_GUARD_DEFAULTS="$T/none.json"
B=feat/t1-thing W="refs/wip/feat/t1-thing"
mkdir -p "$T/acme" && git init -q --bare -b main "$T/acme/app.git"
git clone -q "$T/acme/app.git" "$T/clone" 2>/dev/null
G="git -C $T/clone"
echo a >"$T/clone/a.txt" && echo '.env' >"$T/clone/.gitignore" && $G add a.txt .gitignore && $G commit -qm init && $G push -q origin main
$G commit -q --allow-empty -m "chore(claim): reserve $B" && $G push -q origin "HEAD:refs/heads/$B" && $G reset -q --hard origin/main
$G worktree add -q -b "$B" "$T/clone/.claude/worktrees/t1" "origin/$B" 2>/dev/null
WT="$T/clone/.claude/worktrees/t1"; GW="git -C $WT"
remote() { git -C "$T/acme/app.git" rev-parse -q --verify "$1"; }
hook() { echo "{\"cwd\":\"$1\"}" | python3 "$H"; }

# a dead session's state: one unpushed commit, one edited file, one new file, a secret and a big file
echo work >"$WT/b.txt" && $GW add b.txt && $GW commit -qm "feat: b"
echo edited >"$WT/a.txt"; echo new >"$WT/c.txt"; echo SECRET=1 >"$WT/.env.local"; echo k >"$WT/deploy.pem"
head -c 11000000 /dev/zero >"$WT/dump.bin"
hook "$T/clone"   # the session ran in the clone; its task is in a worktree
check "hook from the clone snapshots the task worktree" 'remote $W >/dev/null'
S=$(remote $W)
check "snapshot parent is the worktree HEAD (unpushed commit goes too)" '[ "$(git -C $T/acme/app.git rev-parse $W^)" = "$($GW rev-parse HEAD)" ]'
check "snapshot holds edited, new and committed files" '[ "$(git -C $T/acme/app.git show $W:a.txt)" = edited ] && git -C $T/acme/app.git cat-file -e $W:c.txt && git -C $T/acme/app.git cat-file -e $W:b.txt'
check "snapshot leaves out .env*, *.pem and files over 10 MB" '! git -C $T/acme/app.git cat-file -e $W:.env.local 2>/dev/null && ! git -C $T/acme/app.git cat-file -e $W:deploy.pem 2>/dev/null && ! git -C $T/acme/app.git cat-file -e $W:dump.bin 2>/dev/null'
check "the real index is untouched" '[ "$($GW diff --cached --name-only)" = "" ] && $GW status --porcelain | grep -q "^?? c.txt"'
check "the task branch on origin did not move" '[ "$(remote refs/heads/$B)" = "$($G rev-parse origin/$B)" ]'
hook "$WT"
check "an unchanged worktree pushes nothing new" '[ "$(remote $W)" = "$S" ]'
echo more >>"$WT/c.txt"; hook "$WT"
check "a further edit replaces the snapshot" '[ "$(remote $W)" != "$S" ]'

# another machine resumes: the branch got a new claim commit on top, the worktree is fresh
git clone -q "$T/acme/app.git" "$T/other" 2>/dev/null; O="git -C $T/other"
$O checkout -q -b "$B" "origin/$B" && $O commit -q --allow-empty -m "chore(claim): resume" && $O push -q origin "$B"
(cd "$T/other" && python3 "$H" --restore >"$T/r.out"); rc=$?
check "restore exits 0" '[ $rc = 0 ]'
check "restore merges the dead session's commit" '$O log --format=%s | grep -qx "feat: b"'
check "restore lays the uncommitted files back as changes" '[ "$(cat $T/other/a.txt)" = edited ] && grep -q more $T/other/c.txt && $O status --porcelain | grep -q "^?? c.txt" && [ "$($O diff --cached --name-only)" = "" ]'
check "restore leaves no cherry-pick in progress" '! $O rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null'
(cd "$T/other" && python3 "$H" --restore >/dev/null); rc=$?
check "restore refuses a dirty worktree" '[ $rc = 2 ]'

# the original session cleans up and pushes: the snapshot is deleted
rm "$WT/.env.local" "$WT/deploy.pem" "$WT/dump.bin"; $GW add -A && $GW commit -qm wip-done && $GW pull -q --no-rebase --no-edit origin "$B" && $GW push -q origin "$B"
hook "$WT"
check "a clean, pushed worktree deletes its snapshot" '! remote $W >/dev/null'
(cd "$T/other" && git stash -q -u && python3 "$H" --restore >"$T/r.out"); rc=$?
check "restore with no snapshot exits 0" '[ $rc = 0 ] && grep -q "no WIP snapshot" $T/r.out'

# not a task branch / not a guarded repo: nothing happens
$G checkout -q -b scratch && echo x >"$T/clone/x.txt"; hook "$T/clone"
check "a non-task branch is never pushed" '[ -z "$(git -C $T/acme/app.git for-each-ref refs/wip)" ]'
printf '{"repos":{}}' >"$T/policy.json"; echo y >>"$WT/a.txt"; hook "$WT"
check "an unguarded repo is never pushed" '[ -z "$(git -C $T/acme/app.git for-each-ref refs/wip)" ]'
(cd "$T" && echo "{}" | python3 "$H"); check "hook with no cwd outside git exits 0" "[ $? = 0 ]"
exit $fail
