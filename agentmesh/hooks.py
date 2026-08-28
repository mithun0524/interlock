"""Hook payload parsing and the gate.

An Edit-only matcher is trivially bypassed by `sed -i` or `cat > file`, and
agents write files through Bash routinely, so Bash is matched too and its
command string scanned for write shapes.
"""

import os
import re
import time

from . import detect, state

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


ALLOW = 0
BLOCK = 2

_DECLARE_HINT = (
    "agentmesh: declare your intent before editing.\n"
    "Run: agentmesh declare --goal \"<one sentence>\" "
    "--globs \"<paths you will touch>\" "
    "[--removes SYM] [--sig SYM] [--deps SYM] "
    "[--progress \"<what you have produced>\"]"
)


def _rel(path, root):
    if os.path.isabs(path):
        try:
            return os.path.relpath(path, root)
        except ValueError:
            return path
    return path


def _roster(agents, me_id):
    lines = []
    for aid, row in sorted(agents.items()):
        if aid == me_id:
            continue
        line = f"  {aid}: {row.get('goal', '') or '(no goal declared)'}"
        if row.get("progress"):
            line += f"\n      produced so far: {row['progress']}"
        if row.get("globs"):
            line += f"\n      files: {', '.join(row['globs'])}"
        lines.append(line)
    if not lines:
        return "agentmesh: no other agents active in this repo."
    return "agentmesh: other agents active in this repo:\n" + "\n".join(lines)


def _conflict_message(conflicts, agents):
    out = ["agentmesh: blocked by unresolved conflicts."]
    for c in conflicts:
        other = agents.get(c.get("with"), {})
        out.append(
            f"  [{c['kind']}] {c['ref'] or '(files)'} with "
            f"{c.get('with') or '(repo-wide)'}: {c['detail']}"
        )
        if other.get("goal"):
            out.append(f"      their goal: {other['goal']}")
        if c["kind"] == detect.GOD_SYMBOL:
            out.append(
                "      repo-wide change — ask the human, then: "
                f"agentmesh resolve --ref {c['ref']} --decision \"<verdict>\""
            )
        else:
            out.append(
                "      you are blocked, so you propose: "
                f"agentmesh reply --to {c.get('with')} --kind offer "
                f"--ref {c['ref']} --msg \"<shim / migration / other approach>\""
            )
    return "\n".join(out)


def gate(payload):
    targets = payload_targets(payload)
    if not targets:
        return ALLOW, ""

    me_id = agent_id(payload)
    agents = state.get_all()
    me = agents.get(me_id)
    if me is None:
        return BLOCK, _DECLARE_HINT

    state.bump(me_id)
    root = state.worktree_root()

    open_conflicts = [c for c in me.get("conflicts", [])
                      if c.get("status") != "resolved"]
    if open_conflicts:
        return BLOCK, _conflict_message(open_conflicts, agents)

    mine = detect.claimed_paths(me, root)
    for raw in targets:
        rel = _rel(raw, root)
        for aid, row in sorted(agents.items()):
            if aid == me_id or row.get("state") == "done":
                continue
            if row.get("worktree") != me.get("worktree"):
                continue
            if rel in detect.claimed_paths(row, root):
                held = int(time.time() - row.get("hb", 0))
                return BLOCK, (
                    f"agentmesh: {rel} is claimed by agent {aid}.\n"
                    f"  its goal: {row.get('goal', '')}\n"
                    f"  produced so far: {row.get('progress') or '(nothing yet)'}\n"
                    f"  idle for: {held}s\n"
                    f"Work on another file, or negotiate:\n"
                    f"  agentmesh reply --to {aid} --kind objection "
                    f"--ref {rel} --msg \"<why you need it>\""
                )
        if rel not in mine and not detect.matches_globs(
                rel, me.get("globs", [])):
            return BLOCK, (
                f"agentmesh: {rel} is outside your declared globs "
                f"({', '.join(me.get('globs', [])) or 'none'}).\n"
                f"Re-declare to widen your claim and re-run detection:\n"
                f"  agentmesh declare --goal \"{me.get('goal', '')}\" "
                f"--globs \"{' '.join(me.get('globs', []) + [rel])}\""
            )
    return ALLOW, ""


def run(event, payload):
    """Returns (exit_code, stdout, stderr)."""
    me_id = agent_id(payload)

    if event == "SessionStart":
        agents = state.get_all()
        ctx = _roster(agents, me_id) + "\n" + _DECLARE_HINT
        return ALLOW, ctx, ""

    if event == "PreToolUse":
        code, msg = gate(payload)
        if code == BLOCK:
            return BLOCK, "", msg
        return ALLOW, "", ""       # message delivery is wired in Task 7

    if event == "PostToolUse":
        if me_id in state.get_all():
            root = state.worktree_root()
            rels = [_rel(t, root) for t in payload_targets(payload)]
            if rels:
                row = state.get_all()[me_id]
                merged = sorted(set(row.get("touched", [])) | set(rels))
                state.upsert(me_id, touched=merged)
        return ALLOW, "", ""

    if event == "SessionEnd":
        if me_id in state.get_all():
            state.upsert(me_id, state="done")
        return ALLOW, "", ""

    return ALLOW, "", ""       # Stop and anything else: no-op by design
