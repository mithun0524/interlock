"""Conflict detection. Pure functions over plain dicts — no disk access."""

import json
import os
import re
import shutil
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

BREAKING_KINDS = ("remove", "sig")


def load_refcache():
    p = state.state_dir() / "refcache.json"
    if not p.exists():
        return {}
    try:
        cache = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    if cache.get("head") != state.head_sha():
        return {}                      # dropped wholesale when HEAD moves
    return cache.get("refs", {})


def save_refcache(cache):
    p = state.state_dir() / "refcache.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"head": state.head_sha(), "refs": cache}))
    os.replace(tmp, p)


def _grep(symbol, root, path=None):
    """git grep is always present in a git repo. rg is used only if found."""
    if shutil.which("rg"):
        cmd = ["rg", "-l", "-w", "--", symbol]
        if path:
            cmd.append(path)
    else:
        cmd = ["git", "grep", "-l", "-w", "-e", symbol]
        if path:
            cmd += ["--", path]
    out = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                         check=False)
    return sorted(l for l in out.stdout.splitlines() if l)


def refs_for_symbol(symbol, root, cache):
    path = None
    if ":" in symbol:
        path, symbol = symbol.rsplit(":", 1)
    key = f"{path + ':' if path else ''}{symbol}@{state.head_sha()}"
    if key not in cache:
        cache[key] = _grep(symbol, root, path)
    return cache[key]


def _bare(symbol):
    return symbol.rsplit(":", 1)[-1]


def interface_conflicts(me, others, root, my_id=None, cache=None):
    if cache is None:
        cache = load_refcache()
    mine = claimed_paths(me, root)
    live = _live(others, my_id)
    found = []
    for delta in me.get("deltas", []):
        if delta.get("kind") not in BREAKING_KINDS:
            continue
        symbol = delta["symbol"]
        hits = set(refs_for_symbol(symbol, root, cache)) - mine
        for aid, row in sorted(live.items()):
            theirs = claimed_paths(row, root)
            shared = sorted(hits & theirs)
            if shared:
                found.append(conflict(
                    INTERFACE_BREAK, symbol, aid,
                    f"referenced in {', '.join(shared)}, claimed by {aid}",
                ))
            elif _bare(symbol) in row.get("deps", []):
                found.append(conflict(
                    INTERFACE_BREAK, symbol, aid,
                    f"declared dependency of {aid} ({row.get('goal', '')!r})",
                ))
    save_refcache(cache)
    return found


class SymbolTooVague(Exception):
    """A bare symbol too short to grep for without matching the world."""

    def __init__(self, symbol):
        self.symbol = symbol
        super().__init__(
            f"symbol {symbol!r} is too short to search for unambiguously. "
            f"Qualify it as path/to/file.py:{symbol} to restrict the search."
        )


def validate_symbols(deltas, deps):
    """Ambiguity guard only — deliberately blind to hit counts, which are
    GOD_SYMBOL's business."""
    names = [d["symbol"] for d in deltas] + list(deps)
    for name in names:
        if ":" in name:
            continue
        if len(name) < state.MIN_SYMBOL_LEN:
            raise SymbolTooVague(name)


def _added(row):
    return {_bare(d["symbol"]) for d in row.get("deltas", [])
            if d.get("kind") == "add"}


def add_collisions(me, others, root, my_id=None):
    mine = _added(me)
    tracked = set(tracked_files(root))          # hoisted: one call, not N
    new_mine = set(me.get("globs", [])) - tracked
    found = []
    for aid, row in sorted(_live(others, my_id).items()):
        for symbol in sorted(mine & _added(row)):
            found.append(conflict(
                ADD_COLLISION, symbol, aid,
                f"{aid} is also adding {symbol}",
            ))
        if row.get("worktree") == me.get("worktree"):
            new_theirs = set(row.get("globs", [])) - tracked
            for path in sorted(new_mine & new_theirs):
                found.append(conflict(
                    ADD_COLLISION, path, aid,
                    f"{aid} is also creating {path}",
                ))
    return found


def god_symbols(me, root, cache=None):
    if cache is None:
        cache = load_refcache()
    found = []
    for delta in me.get("deltas", []):
        if delta.get("kind") not in BREAKING_KINDS:
            continue
        symbol = delta["symbol"]
        n = len(refs_for_symbol(symbol, root, cache))
        if n > state.GOD_SYMBOL_REFS:
            c = conflict(
                GOD_SYMBOL, symbol, "",
                f"{n} references exceed the cap of {state.GOD_SYMBOL_REFS}; "
                f"a repo-wide architectural decision, not a negotiation",
            )
            c["status"] = "escalated"
            found.append(c)
    save_refcache(cache)
    return found


def goal_overlaps(me, others, root, my_id=None):
    """Advisory. Two agents reading the same area is normal; blocking it
    would make the tool unusable."""
    if me.get("deltas"):
        return []
    mine = claimed_paths(me, root)
    found = []
    for aid, row in sorted(_live(others, my_id).items()):
        if row.get("deltas"):
            continue
        if mine & claimed_paths(row, root):
            c = conflict(
                GOAL_OVERLAP, "", aid,
                f"{aid} is working the same files with goal "
                f"{row.get('goal', '')!r} — possible duplicate work",
            )
            c["status"] = "resolved"          # recorded, never blocks
            found.append(c)
    return found


def detect_all(me, others, root, my_id=None):
    cache = load_refcache()
    found = []
    found += file_conflicts(me, others, root, my_id)
    found += interface_conflicts(me, others, root, my_id, cache)
    found += add_collisions(me, others, root, my_id)
    found += god_symbols(me, root, cache)
    found += goal_overlaps(me, others, root, my_id)
    return found
