"""The only module that touches disk.

Everything else in interlock takes and returns plain dicts, which is what
makes it testable without a filesystem.
"""

import contextlib
import fcntl
import json
import os
import pathlib
import subprocess
import time

STATE_VERSION = 1
TTL_PRUNE = 1800          # 30 min: drop the row entirely
TTL_ABSENT = 300          # 5 min: treat as absent for negotiation
GOD_SYMBOL_REFS = 200
MIN_SYMBOL_LEN = 4
ROUND_CAP = 2
NUDGE_AFTER_INV = 2
ESCALATE_AFTER_INV = 4

_DIRNAME = "interlock"


def _git(*args):
    out = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def state_dir():
    """<git-common-dir>/interlock — shared by every worktree of the repo.

    git prints --git-common-dir relative to cwd when cwd is the toplevel and
    absolute otherwise, so abspath covers both without needing git >= 2.31's
    --path-format flag.
    """
    common = _git("rev-parse", "--git-common-dir")
    if not common:
        raise RuntimeError("interlock must run inside a git repository")
    d = pathlib.Path(os.path.abspath(common)) / _DIRNAME
    (d / "inbox").mkdir(parents=True, exist_ok=True)
    return d


def worktree_root():
    return _git("rev-parse", "--show-toplevel")


def head_sha():
    return _git("rev-parse", "HEAD")


@contextlib.contextmanager
def locked():
    lock = state_dir() / "lock"
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _empty():
    return {"v": STATE_VERSION, "agents": {}}


def load():
    """Call inside locked()."""
    p = state_dir() / "declarations.json"
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return _empty()
    if data.get("v") != STATE_VERSION:
        return _empty()
    return data


def save(data):
    """Call inside locked(). Atomic: no reader ever sees a partial file."""
    d = state_dir()
    tmp = d / "declarations.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, d / "declarations.json")


def prune(data, now):
    keep = {}
    for aid, row in data.get("agents", {}).items():
        # A finished agent that left declared work undone survives until it
        # goes stale, so `status` can show a human what was abandoned. It holds
        # no claims -- everything that reads claims skips state == "done".
        if row.get("state") == "done" and not row.get("incomplete"):
            continue
        if now - row.get("hb", 0) > TTL_PRUNE:
            continue
        keep[aid] = row
    return {"v": STATE_VERSION, "agents": keep}


def _blank_row(now):
    return {
        "goal": "", "globs": [], "deltas": [], "deps": [], "touched": [],
        "progress": "", "worktree": worktree_root(), "conflicts": [],
        "resolved": [], "state": "active", "hb": now, "inv": 0,
        "declared_at": now,
    }


def upsert(agent_id, **fields):
    now = time.time()
    with locked():
        data = prune(load(), now)
        row = data["agents"].get(agent_id) or _blank_row(now)
        row.update(fields)
        row["hb"] = now
        data["agents"][agent_id] = row
        save(data)
        return row


def get_all():
    now = time.time()
    with locked():
        data = prune(load(), now)
        save(data)
        return data["agents"]


def bump(agent_id):
    now = time.time()
    with locked():
        data = prune(load(), now)
        row = data["agents"].get(agent_id)
        if row is None:
            return None
        row["hb"] = now
        row["inv"] = row.get("inv", 0) + 1
        save(data)
        return row
