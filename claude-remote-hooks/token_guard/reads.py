"""VPS-only token limits (2026-10-14: whole-repo `cat` loops, compacted 3x, then re-read the same files;
2026-10-15: 10 docs re-typed into Drive create_file, 159K output tokens to send one email)."""
import json, re, shlex

from .common import MAX_LINES, MAX_TOOL_CHARS, READ_HELP, count_lines, deny


def check_cat(cmd, cwd):
    if re.search(r"\bxargs\b[^|;&]*\bcat\b|-exec\s+cat\b|\bfor\b[^;]*;\s*do\b[^;]*\bcat\b|\bwhile\b.*\bdo\b[^;]*\bcat\b", cmd):
        deny("`cat` in a loop / xargs / -exec prints every file into context. " + READ_HELP)
    for seg in re.split(r"\|\||&&|;|\n", cmd):
        m = re.search(r"(?:^|\s|\()cat\s+(.*)", seg)
        if not m:
            continue
        rest = m.group(1)
        if re.match(r"\s*(<<|>)", rest):          # heredoc / file write
            continue
        piped = "|" in rest
        args_part = re.split(r"[|<>]", rest)[0]
        try:
            args = [a for a in shlex.split(args_part) if not a.startswith("-")]
        except ValueError:
            args = args_part.split()
        if not args or piped:
            continue                              # cat x | grep … is a filter, not a dump
        if any(c in a for a in args for c in "*?["):
            deny(f"`cat {args_part.strip()}` dumps every matching file. " + READ_HELP)
        total = 0
        for a in args:
            n = count_lines(a, cwd)
            if n is None:
                continue
            total += n
        if total > MAX_LINES:
            deny(f"`cat` would print {total} lines. " + READ_HELP)


def check_ranges(cmd, cwd):
    for a, b in re.findall(r"sed\s+-n\s+['\"]?(\d+),(\d+)p", cmd):
        if int(b) - int(a) > MAX_LINES:
            deny(f"sed range {a},{b} is over {MAX_LINES} lines. Narrow it. " + READ_HELP)
    for seg in re.split(r"\|\||&&|;|\n", cmd):
        for m in re.finditer(r"\b(?:head|tail)\s+-n?\s*\+?(\d+)([^|>]*)(.?)", seg):
            if int(m.group(1)) > MAX_LINES and m.group(3) not in ("|", ">"):
                deny(f"head/tail of {m.group(1)} lines is over {MAX_LINES}. " + READ_HELP)


def check_connector(tool, ti):
    """An mcp__ call: every char of its input is output tokens."""
    size = len(json.dumps(ti))
    if size > MAX_TOOL_CHARS:
        deny(f"{tool} payload is {size} chars; every char is output tokens. Never pass file "
             "contents through a connector. Upload from disk: `rclone copy <file-or-dir> gdrive:<folder>`, "
             "then `rclone link gdrive:<folder>/<file>` and send only the link. Repo code/docs go through git/gh.")


def check_read(ti, cwd):
    """A Read: an explicit limit up to 2 * MAX_LINES, or a file no longer than that."""
    if ti.get("limit"):
        if int(ti["limit"]) > MAX_LINES * 2:
            deny(f"Read limit {ti['limit']} is too big. Use limit <= {MAX_LINES * 2}. " + READ_HELP)
        return
    n = count_lines(ti.get("file_path", ""), cwd)
    if n and n > MAX_LINES * 2:
        deny(f"{ti.get('file_path')} has {n} lines; reading it whole costs ~{n * 12 // 1000}K tokens on "
             "every later call. Pass offset/limit. " + READ_HELP)
