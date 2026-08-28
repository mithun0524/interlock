"""Conflict detection. Pure functions over plain dicts — no disk access."""

import re
import subprocess

from . import state

FILE_OVERLAP = "FILE_OVERLAP"
INTERFACE_BREAK = "INTERFACE_BREAK"
ADD_COLLISION = "ADD_COLLISION"
GOD_SYMBOL = "GOD_SYMBOL"
GOAL_OVERLAP = "GOAL_OVERLAP"


def tracked_files(root):
    out = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True,
        check=False,
    )
    return [l for l in out.stdout.splitlines() if l]


def _to_regex(pattern):
    """fnmatch treats * as crossing separators; we need ** for that and
    * to stop at one path segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def expand(globs, root):
    files = tracked_files(root)
    hits = set()
    for pattern in globs:
        rx = _to_regex(pattern.rstrip("/"))
        for f in files:
            if rx.match(f):
                hits.add(f)
        if pattern in files:          # exact path, already tracked
            hits.add(pattern)
    return hits


def claimed_paths(row, root):
    return expand(row.get("globs", []), root) | set(row.get("touched", []))


def matches_globs(path, globs):
    """Pattern match against a path string, tracked or not.

    claimed_paths() only ever returns tracked files, so it cannot answer
    "may this agent create src/new.py?" — this can.
    """
    return any(_to_regex(g.rstrip("/")).match(path) for g in globs)


def conflict(kind, ref, other_id, detail):
    return {"kind": kind, "ref": ref, "with": other_id,
            "status": "open", "round": 0, "detail": detail}


def _live(others, my_id):
    return {
        aid: row for aid, row in others.items()
        if aid != my_id and row.get("state") != "done"
    }


def file_conflicts(me, others, root, my_id=None):
    """FILE_OVERLAP only fires inside one worktree. Two agents editing the
    same path in different worktrees are editing different files."""
    mine = claimed_paths(me, root)
    my_wt = me.get("worktree")
    found = []
    for aid, row in sorted(_live(others, my_id).items()):
        if row.get("worktree") != my_wt:
            continue
        for path in sorted(mine & claimed_paths(row, root)):
            found.append(conflict(
                FILE_OVERLAP, path, aid,
                f"{path} also claimed by {aid} ({row.get('goal', '')!r})",
            ))
    return found
