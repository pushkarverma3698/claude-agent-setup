#!/bin/bash
# Run: ./test_claude_notify.sh   (no network, no tokens). Exit 0 = all cases pass.
cd "$(dirname "$0")" || exit 1
CLAUDE_NOTIFY_LIB=1 . ./claude-notify
T=$(mktemp -d); trap 'rm -r "$T"' EXIT
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1: $2"; fail=1; }
eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1" "got [$2] want [$3]"; }

eq "escape & < >" "$(notify_escape 'a<b> & c')" 'a&lt;b&gt; &amp; c'
eq "escape no double-order (&lt; stays escaped once)" "$(notify_escape '&lt;')" '&amp;lt;'
eq "escape plain text unchanged" "$(notify_escape 'hello world')" 'hello world'
eq "escape keeps newlines" "$(notify_escape $'a<\nb>')" $'a&lt;\nb&gt;'

# Splitter: short text -> one part, unchanged
printf '%s' '<b>hi</b>' | notify_split "$T/s1" 4000
eq "short: one part" "$(ls "$T/s1" | wc -l | tr -d ' ')" 1
eq "short: unchanged" "$(cat "$T/s1/part.001")" '<b>hi</b>'

# Long: 300 lines of 40 chars, a bold span crossing a line break in the middle
{ for i in $(seq 1 150); do echo "line $i xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"; done
  echo '<b>bold start'; for i in $(seq 1 150); do echo "more $i yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"; done; echo 'end</b>'; } >"$T/long.txt"
notify_split "$T/s2" 4000 <"$T/long.txt"
n=$(ls "$T/s2" | wc -l | tr -d ' ')
[ "$n" -ge 2 ] && ok "long: split into $n parts" || bad "long: parts" "$n"
over=0; for f in "$T"/s2/part.*; do [ "$(python3 -c 'import sys;print(len(open(sys.argv[1],encoding="utf-8").read()))' "$f")" -le 4096 ] || over=1; done
eq "long: every part <= 4096" "$over" 0
cat "$T"/s2/part.* >"$T/joined"
# every part must have balanced <b> tags
unbal=0; for f in "$T"/s2/part.*; do o=$(grep -o '<b>' "$f" | wc -l); c=$(grep -o '</b>' "$f" | wc -l); [ "$o" = "$c" ] || unbal=1; done
eq "long: tags balanced per part" "$unbal" 0
# parts end at line breaks: stripped text of all parts, with added tags removed, equals the original lines
python3 - "$T/s2" "$T/long.txt" <<'PY' && ok "long: no line is cut" || bad "long: line cut" "see diff"
import sys,glob,re
parts=[open(f,encoding="utf-8").read() for f in sorted(glob.glob(sys.argv[1]+"/part.*"))]
orig=open(sys.argv[2],encoding="utf-8").read().rstrip("\n").split("\n")
got=[]
for p in parts: got+=[re.sub(r"</?b>","",l) for l in p.split("\n")]
want=[re.sub(r"</?b>","",l) for l in orig]
sys.exit(0 if [g for g in got if g]==[w for w in want if w] else 1)
PY

# One huge single line with tags: never cut inside a tag or entity
python3 -c 'print(("word <a href=\"https://x.test/p\">link</a> &amp; "*400))' >"$T/oneline.txt"
notify_split "$T/s3" 4000 <"$T/oneline.txt"
bad_tag=0; for f in "$T"/s3/part.*; do python3 - "$f" <<'PY' || bad_tag=1
import sys,re
s=open(sys.argv[1],encoding="utf-8").read()
# every '<' closes with '>' and every '&' ends in ';' before the next space
sys.exit(1 if re.search(r"<[^>]*$",s) or re.search(r"&[a-z]*$",s.rstrip("\n")) or re.search(r"^[^<]*>",s) else 0)
PY
done
eq "oneline: no cut tag/entity" "$bad_tag" 0
[ "$(ls "$T/s3" | wc -l)" -ge 2 ] && ok "oneline: split" || bad "oneline" "not split"

# Buttons
printf 'Open PR|https://github.com/o/r/pull/1\nDocs|https://x.test/d\n\nSecond row|https://x.test/2\n' >"$T/b.txt"
eq "buttons json" "$(notify_buttons_json "$T/b.txt")" \
 '{"inline_keyboard":[[{"text":"Open PR","url":"https://github.com/o/r/pull/1"},{"text":"Docs","url":"https://x.test/d"}],[{"text":"Second row","url":"https://x.test/2"}]]}'
printf 'bad line no pipe\n' >"$T/b2.txt"
eq "buttons: invalid line skipped -> empty" "$(notify_buttons_json "$T/b2.txt")" ""
printf 'X|javascript:alert(1)\n' >"$T/b3.txt"
eq "buttons: non-http url rejected" "$(notify_buttons_json "$T/b3.txt")" ""

exit $fail
