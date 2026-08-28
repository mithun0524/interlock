"""Hook payload parsing and the gate.

An Edit-only matcher is trivially bypassed by `sed -i` or `cat > file`, and
agents write files through Bash routinely, so Bash is matched too and its
command string scanned for write shapes.
"""

import re
import time

# ponytail: regex over shell strings. Misses writes inside invoked scripts
# and python -c. Upgrade path: an mtime-diffing filesystem watcher.
_PATH = r"([\w./~-]+)"
_ARG = r"(?:'[^']*'|\"[^\"]*\"|\S+)"

# (regex, which capture groups name a written path)
#
# Redirects must be matched on the quote-scrubbed string, or `echo 'a > b'`
# reads as a write. Everything else matches the raw string, because the
# scrubbed version has lost the arguments these patterns step over
# (`sed -i '' 's/a/b/' f` becomes `sed -i          f`).
_REDIRECT = (re.compile(r">>?\s*" + _PATH), (0,))
_COMMANDS = [
    # sed -i [suffix] [script args...] FILE  — only the final path is written
    (re.compile(r"\bsed\s+-i\S*\s+(?:" + _ARG + r"\s+)*?" + _PATH
                + r"(?:\s|$)"), (0,)),
    (re.compile(r"\btee\s+(?:-a\s+)?" + _PATH), (0,)),
    # mv writes both ends: the source ceases to exist, the destination appears
    (re.compile(r"\bmv\s+(?:-\S+\s+)*" + _PATH + r"\s+" + _PATH), (0, 1)),
    # cp writes only the destination
    (re.compile(r"\bcp\s+(?:-\S+\s+)*" + _PATH + r"\s+" + _PATH), (1,)),
    (re.compile(r"\brm\s+(?:-\S+\s+)*" + _PATH), (0,)),
    (re.compile(r"\bpatch\b[^<]*<\s*" + _PATH), (0,)),
    (re.compile(r"\bgit\s+apply\s+(?:-\S+\s+)*" + _PATH), (0,)),
    (re.compile(r"\bgit\s+(?:checkout|restore)\s+(?:--\s+|-\S+\s+)*"
                + _PATH), (0,)),
]
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_HEREDOC = re.compile(r"<<-?\s*'?\w+'?")


def _strip_quotes_and_heredocs(cmd):
    """A redirect inside quotes is data, not a write. Blank quoted runs out
    but keep the length so nothing else shifts."""
    cmd = _HEREDOC.sub(" ", cmd)
    return _QUOTED.sub(lambda m: " " * len(m.group(0)), cmd)


def _collect(text, patterns, into):
    for rx, groups in patterns:
        for m in rx.finditer(text):
            for i in groups:
                g = m.group(i + 1)
                if g and g != "--" and not g.startswith("-"):
                    into.append(g)


def targets_from_bash(cmd):
    found = []
    _collect(_strip_quotes_and_heredocs(cmd), [_REDIRECT], found)
    _collect(cmd, _COMMANDS, found)
    seen, out = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def payload_targets(payload):
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input", {}) or {}
    if tool in ("Edit", "Write"):
        return [ti["file_path"]] if ti.get("file_path") else []
    if tool == "NotebookEdit":
        return [ti["notebook_path"]] if ti.get("notebook_path") else []
    if tool == "Bash":
        return targets_from_bash(ti.get("command", ""))
    return []


def agent_id(payload):
    return (payload.get("session_id") or "")[:6]
