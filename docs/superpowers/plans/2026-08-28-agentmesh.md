# agentmesh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a CLI + Claude Code hook set that lets 3-7 concurrent coding agents on one repo see each other's intent, detect interface-level conflicts before they land, and negotiate without a human.

**Architecture:** All state is JSON under `<git-common-dir>/agentmesh/`, mutated only under `flock`. A single blocking `PreToolUse` hook is the enforcement gate; expensive conflict detection (`git grep`) runs once at `declare` time and its result is cached in the declaration, so the gate is a JSON read plus a glob match. Five modules: `state` (only module touching disk), `detect`, `negotiate`, `hooks`, `cli`.

**Tech Stack:** Python 3.9+, standard library only (`json`, `fcntl`, `fnmatch`, `subprocess`, `argparse`, `re`, `pathlib`). `git` is the only external binary. `pytest` for tests. Apache-2.0.

**Spec:** `docs/superpowers/specs/2026-08-28-agentmesh-design.md` — read it alongside this plan; the plan argues from it.

## Global Constraints

- Python `>=3.9`. Runtime dependencies: **none**. Test dependency: `pytest` only.
- No external binary beyond `git`. `rg` may be used only if present on `PATH`, never required.
- Every write to `declarations.json` happens under `flock` and via atomic replace (tmp file + `os.replace`).
- `declarations.json` is upsert-by-id. Never append. No history table.
- Constants, defined once in `state.py` and imported everywhere:
  `TTL_PRUNE = 1800`, `TTL_ABSENT = 300`, `GOD_SYMBOL_REFS = 200`,
  `MIN_SYMBOL_LEN = 4`, `ROUND_CAP = 2`, `NUDGE_AFTER_INV = 2`, `ESCALATE_AFTER_INV = 4`,
  `STATE_VERSION = 1`.
- `Stop` is never registered as a hook. Only `SessionEnd` releases claims.
- Agent id is `session_id[:6]`.
- **Commit messages carry no AI attribution.** No `Co-Authored-By: Claude`, no
  "Generated with Claude Code". Subject, blank line, body. This is a hard
  project rule.
- Author commits with the repo's configured git identity; never pass `-c user.email`.

---

### Task 1: Package skeleton and locked state store

**Files:**
- Create: `pyproject.toml`
- Create: `agentmesh/__init__.py`
- Create: `agentmesh/state.py`
- Create: `tests/conftest.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `state.state_dir() -> pathlib.Path` — `<git-common-dir>/agentmesh`, created if absent
  - `state.worktree_root() -> str` — absolute path from `git rev-parse --show-toplevel`
  - `state.head_sha() -> str` — `git rev-parse HEAD`, or `""` in a repo with no commits
  - `state.locked()` — context manager holding an exclusive `flock`
  - `state.load() -> dict` — `{"v": 1, "agents": {id: decl}}`; call inside `locked()`
  - `state.save(data: dict) -> None` — atomic write; call inside `locked()`
  - `state.prune(data: dict, now: float) -> dict` — drops `state == "done"` and `hb` older than `TTL_PRUNE`
  - `state.upsert(agent_id: str, **fields) -> dict` — takes the lock itself, prunes, merges fields into the agent's row, returns the row
  - `state.get_all() -> dict` — takes the lock, prunes, returns `{id: decl}`
  - `state.bump(agent_id: str) -> dict` — refreshes `hb`, increments `inv`
  - Constants listed in Global Constraints

- [ ] **Step 1: Write the failing test**

`tests/conftest.py`:

```python
import subprocess
import pytest


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo with a couple of source files and one commit."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.py").write_text(
        "def getUserFromSession(req):\n    return req.session\n"
    )
    (root / "src" / "api.py").write_text(
        "from .auth import getUserFromSession\n\n"
        "def me(req):\n    return getUserFromSession(req)\n"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def worktree(repo, tmp_path):
    """A linked worktree of `repo`, sharing its git common dir."""
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    return wt
```

`tests/test_state.py`:

```python
import json
import time

from agentmesh import state


def test_state_dir_is_inside_git_common_dir(repo):
    d = state.state_dir()
    assert d == repo / ".git" / "agentmesh"
    assert d.is_dir()


def test_state_dir_is_shared_across_worktrees(repo, worktree, monkeypatch):
    from_main = state.state_dir()
    monkeypatch.chdir(worktree)
    from_wt = state.state_dir()
    assert from_main == from_wt


def test_worktree_root_differs_per_worktree(repo, worktree, monkeypatch):
    main_root = state.worktree_root()
    monkeypatch.chdir(worktree)
    assert state.worktree_root() != main_root


def test_upsert_then_get_all_round_trips(repo):
    state.upsert("a1b2c3", goal="do a thing", globs=["src/**"])
    agents = state.get_all()
    assert agents["a1b2c3"]["goal"] == "do a thing"
    assert agents["a1b2c3"]["globs"] == ["src/**"]
    assert agents["a1b2c3"]["state"] == "active"


def test_upsert_merges_rather_than_replaces(repo):
    state.upsert("a1b2c3", goal="first", globs=["src/**"])
    state.upsert("a1b2c3", progress="halfway")
    row = state.get_all()["a1b2c3"]
    assert row["goal"] == "first"
    assert row["progress"] == "halfway"


def test_bump_refreshes_hb_and_counts_invocations(repo):
    state.upsert("a1b2c3", goal="g")
    first = state.get_all()["a1b2c3"]
    state.bump("a1b2c3")
    state.bump("a1b2c3")
    row = state.get_all()["a1b2c3"]
    assert row["inv"] == first["inv"] + 2
    assert row["hb"] >= first["hb"]


def test_prune_drops_done_and_stale_rows_but_keeps_absent_ones(repo):
    now = time.time()
    data = {"v": state.STATE_VERSION, "agents": {
        "live":   {"state": "active", "hb": now},
        "absent": {"state": "active", "hb": now - state.TTL_ABSENT - 1},
        "stale":  {"state": "active", "hb": now - state.TTL_PRUNE - 1},
        "done":   {"state": "done",   "hb": now},
    }}
    kept = state.prune(data, now)["agents"]
    assert set(kept) == {"live", "absent"}


def test_save_is_atomic_and_leaves_no_tmp_files(repo):
    with state.locked():
        state.save({"v": state.STATE_VERSION, "agents": {"x": {"state": "active"}}})
    d = state.state_dir()
    assert json.loads((d / "declarations.json").read_text())["agents"]["x"]
    assert [p.name for p in d.iterdir() if p.suffix == ".tmp"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentmesh'`

- [ ] **Step 3: Write the implementation**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "agentmesh"
version = "0.1.0"
description = "Coordination layer for concurrent AI coding agents on one repo"
requires-python = ">=3.9"
dependencies = []
license = { text = "Apache-2.0" }

[project.scripts]
agentmesh = "agentmesh.cli:main"

[project.optional-dependencies]
dev = ["pytest>=7"]

[tool.setuptools]
packages = ["agentmesh"]
```

`agentmesh/__init__.py`:

```python
__version__ = "0.1.0"
```

`agentmesh/state.py`:

```python
"""The only module that touches disk.

Everything else in agentmesh takes and returns plain dicts, which is what
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

_DIRNAME = "agentmesh"


def _git(*args):
    out = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def state_dir():
    """<git-common-dir>/agentmesh — shared by every worktree of the repo.

    git prints --git-common-dir relative to cwd when cwd is the toplevel and
    absolute otherwise, so abspath covers both without needing git >= 2.31's
    --path-format flag.
    """
    common = _git("rev-parse", "--git-common-dir")
    if not common:
        raise RuntimeError("agentmesh must run inside a git repository")
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
        if row.get("state") == "done":
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pip install -e ".[dev]" && python -m pytest tests/test_state.py -v`
Expected: all 8 PASS

- [ ] **Step 5: Add the concurrency test — this is where a file-backed design actually fails**

`tests/test_state_race.py`:

```python
import json
import subprocess
import sys

from agentmesh import state

WRITER = """
import sys
from agentmesh import state
state.upsert(sys.argv[1], goal="agent " + sys.argv[1])
"""


def test_twenty_concurrent_writers_lose_nothing(repo, tmp_path):
    script = tmp_path / "writer.py"
    script.write_text(WRITER)
    ids = [f"ag{i:04d}" for i in range(20)]
    procs = [
        subprocess.Popen([sys.executable, str(script), aid], cwd=repo)
        for aid in ids
    ]
    for p in procs:
        assert p.wait() == 0

    raw = (state.state_dir() / "declarations.json").read_text()
    json.loads(raw)                      # never corrupt
    assert set(state.get_all()) == set(ids)   # never a lost write
```

- [ ] **Step 6: Run it**

Run: `python -m pytest tests/test_state_race.py -v`
Expected: PASS. If it fails, the bug is in `locked()`/`save()`, not the test — fix before continuing, every later task builds on this.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml agentmesh/ tests/
git commit -m "feat: locked, worktree-shared state store

State lives in <git-common-dir>/agentmesh so every worktree of a repo
shares one coordination view. Upsert-by-id, atomic replace under flock,
prune in the write path."
```

---

### Task 2: File-level conflict detection, split by worktree

**Files:**
- Create: `agentmesh/detect.py`
- Test: `tests/test_detect_files.py`

**Interfaces:**
- Consumes: `state.worktree_root()`, `state.get_all()`.
- Produces:
  - `detect.FILE_OVERLAP`, `detect.INTERFACE_BREAK`, `detect.ADD_COLLISION`, `detect.GOD_SYMBOL`, `detect.GOAL_OVERLAP` — string constants equal to their names
  - `detect.tracked_files(root: str) -> list[str]` — repo-relative paths from `git ls-files`
  - `detect.expand(globs: list[str], root: str) -> set[str]` — glob patterns to repo-relative tracked paths, `**` matching across separators
  - `detect.claimed_paths(row: dict, root: str) -> set[str]` — `expand(row["globs"]) | set(row["touched"])`
  - `detect.matches_globs(path: str, globs: list[str]) -> bool` — pattern match against a path string, tracked or not. `claimed_paths` cannot answer this: it only ever returns tracked files, so a brand-new file inside your own globs would otherwise look unclaimed forever.
  - `detect.conflict(kind, ref, other_id, detail) -> dict` — `{"kind","ref","with","status":"open","round":0,"detail"}`
  - `detect.file_conflicts(me: dict, others: dict, root: str) -> list[dict]`

- [ ] **Step 1: Write the failing test**

`tests/test_detect_files.py`:

```python
from agentmesh import detect


def _row(**kw):
    base = {"goal": "", "globs": [], "deltas": [], "deps": [], "touched": [],
            "worktree": "/repo", "state": "active"}
    base.update(kw)
    return base


def test_expand_matches_double_star_across_directories(repo):
    got = detect.expand(["src/**"], str(repo))
    assert got == {"src/auth.py", "src/api.py"}


def test_expand_matches_an_exact_path(repo):
    assert detect.expand(["src/auth.py"], str(repo)) == {"src/auth.py"}


def test_expand_ignores_untracked_files(repo):
    (repo / "src" / "scratch.py").write_text("x = 1\n")
    assert "src/scratch.py" not in detect.expand(["src/**"], str(repo))


def test_matches_globs_covers_files_that_do_not_exist_yet(repo):
    assert detect.matches_globs("src/brand_new.py", ["src/**"])
    assert detect.matches_globs("src/a/b/deep.py", ["src/**"])
    assert not detect.matches_globs("docs/guide.md", ["src/**"])


def test_matches_globs_single_star_stops_at_a_separator(repo):
    assert detect.matches_globs("src/auth.py", ["src/*.py"])
    assert not detect.matches_globs("src/deep/auth.py", ["src/*.py"])


def test_same_worktree_glob_overlap_is_a_conflict(repo):
    me = _row(globs=["src/**"], worktree="/repo")
    them = {"b7d2e0": _row(globs=["src/auth.py"], worktree="/repo",
                           goal="patch auth")}
    got = detect.file_conflicts(me, them, str(repo))
    assert [c["kind"] for c in got] == [detect.FILE_OVERLAP]
    assert got[0]["ref"] == "src/auth.py"
    assert got[0]["with"] == "b7d2e0"


def test_touched_files_count_as_claimed(repo):
    me = _row(globs=["src/auth.py"], worktree="/repo")
    them = {"b7d2e0": _row(globs=[], touched=["src/auth.py"], worktree="/repo")}
    assert len(detect.file_conflicts(me, them, str(repo))) == 1


def test_different_worktrees_never_collide_on_files(repo):
    me = _row(globs=["src/**"], worktree="/repo")
    them = {"b7d2e0": _row(globs=["src/auth.py"], worktree="/elsewhere/wt-a")}
    assert detect.file_conflicts(me, them, str(repo)) == []


def test_an_agent_does_not_conflict_with_itself(repo):
    me = _row(globs=["src/**"], worktree="/repo")
    me_id_row = {"me": me}
    assert detect.file_conflicts(me, me_id_row, str(repo), my_id="me") == []


def test_done_agents_hold_no_claims(repo):
    me = _row(globs=["src/**"], worktree="/repo")
    them = {"b7d2e0": _row(globs=["src/auth.py"], worktree="/repo",
                           state="done")}
    assert detect.file_conflicts(me, them, str(repo)) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_detect_files.py -v`
Expected: FAIL — `ImportError: cannot import name 'detect'`

- [ ] **Step 3: Write the implementation**

`agentmesh/detect.py`:

```python
"""Conflict detection. Pure functions over plain dicts — no disk access."""

import fnmatch
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_detect_files.py -v`
Expected: all 8 PASS

- [ ] **Step 5: Commit**

```bash
git add agentmesh/detect.py tests/test_detect_files.py
git commit -m "feat: file-level conflict detection, worktree-aware

Same path in two worktrees is not a conflict; the files are separate.
Glob expansion restricted to tracked files, ** crossing separators."
```

---

### Task 3: Symbol references and INTERFACE_BREAK

**Files:**
- Modify: `agentmesh/detect.py`
- Test: `tests/test_detect_interface.py`

**Interfaces:**
- Consumes: everything from Task 2.
- Produces:
  - `detect.refs_for_symbol(symbol: str, root: str, cache: dict) -> list[str]` — repo-relative paths referencing `symbol`, word-bounded; qualified `path:symbol` restricts the search to that path; `cache` is mutated in place, keyed `f"{symbol}@{head}"`
  - `detect.load_refcache() -> dict` and `detect.save_refcache(cache: dict) -> None` — dropped wholesale when `HEAD` moves
  - `detect.interface_conflicts(me: dict, others: dict, root: str, my_id=None) -> list[dict]`

- [ ] **Step 1: Write the failing test**

`tests/test_detect_interface.py`:

```python
from agentmesh import detect, state


def _row(**kw):
    base = {"goal": "", "globs": [], "deltas": [], "deps": [], "touched": [],
            "worktree": "/repo", "state": "active"}
    base.update(kw)
    return base


def test_refs_for_symbol_finds_call_sites(repo):
    hits = detect.refs_for_symbol("getUserFromSession", str(repo), {})
    assert set(hits) == {"src/auth.py", "src/api.py"}


def test_refs_are_word_bounded(repo):
    (repo / "src" / "noise.py").write_text("getUserFromSessionExtra = 1\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    hits = detect.refs_for_symbol("getUserFromSession", str(repo), {})
    assert "src/noise.py" not in hits


def test_qualified_symbol_restricts_the_search(repo):
    hits = detect.refs_for_symbol("src/auth.py:getUserFromSession",
                                  str(repo), {})
    assert hits == ["src/auth.py"]


def test_refcache_is_reused_within_one_head(repo):
    cache = {}
    detect.refs_for_symbol("getUserFromSession", str(repo), cache)
    key = f"getUserFromSession@{state.head_sha()}"
    cache[key] = ["sentinel.py"]
    assert detect.refs_for_symbol("getUserFromSession", str(repo), cache) == [
        "sentinel.py"
    ]


def test_unclaimed_call_site_is_not_a_conflict(repo):
    """The rule that makes grep viable: updating 40 unclaimed call sites is
    ordinary work, not contention."""
    me = _row(deltas=[{"kind": "remove", "symbol": "getUserFromSession"}])
    them = {"b7d2e0": _row(globs=["docs/**"], goal="write docs")}
    assert detect.interface_conflicts(me, them, str(repo)) == []


def test_removal_conflicts_when_a_call_site_is_claimed(repo):
    me = _row(deltas=[{"kind": "remove", "symbol": "getUserFromSession"}],
              globs=["src/auth.py"])
    them = {"b7d2e0": _row(globs=["src/api.py"], goal="add pagination")}
    got = detect.interface_conflicts(me, them, str(repo))
    assert [c["kind"] for c in got] == [detect.INTERFACE_BREAK]
    assert got[0]["ref"] == "getUserFromSession"
    assert "src/api.py" in got[0]["detail"]


def test_declared_deps_conflict_even_with_no_call_site_yet(repo):
    """deps cover code the other agent has not written yet, which grep
    cannot see."""
    me = _row(deltas=[{"kind": "remove", "symbol": "createSession"}])
    them = {"b7d2e0": _row(deps=["createSession"], goal="build signup")}
    got = detect.interface_conflicts(me, them, str(repo))
    assert [c["kind"] for c in got] == [detect.INTERFACE_BREAK]
    assert "declared dependency" in got[0]["detail"]


def test_sig_change_behaves_exactly_like_remove(repo):
    me = _row(deltas=[{"kind": "sig", "symbol": "getUserFromSession"}],
              globs=["src/auth.py"])
    them = {"b7d2e0": _row(globs=["src/api.py"])}
    assert len(detect.interface_conflicts(me, them, str(repo))) == 1


def test_interface_break_crosses_worktrees(repo):
    """Files are isolated across worktrees; interfaces are not."""
    me = _row(deltas=[{"kind": "remove", "symbol": "getUserFromSession"}],
              worktree="/repo")
    them = {"b7d2e0": _row(globs=["src/api.py"], worktree="/elsewhere/wt-a")}
    assert len(detect.interface_conflicts(me, them, str(repo))) == 1


def test_own_claimed_files_are_not_conflicts(repo):
    me = _row(deltas=[{"kind": "remove", "symbol": "getUserFromSession"}],
              globs=["src/**"])
    assert detect.interface_conflicts(me, {}, str(repo)) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_detect_interface.py -v`
Expected: FAIL — `AttributeError: module 'agentmesh.detect' has no attribute 'refs_for_symbol'`

- [ ] **Step 3: Write the implementation — append to `agentmesh/detect.py`**

```python
import json
import shutil

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
    import os
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_detect_interface.py -v`
Expected: all 10 PASS

- [ ] **Step 5: Commit**

```bash
git add agentmesh/detect.py tests/test_detect_interface.py
git commit -m "feat: interface-break detection via git grep

Grep hits are filtered to those intersecting another live declaration, so
an unclaimed call site is never a conflict. Interface breaks cross
worktree boundaries; file conflicts do not."
```

---

### Task 4: Remaining rules, the short-symbol guard, and one entry point

**Files:**
- Modify: `agentmesh/detect.py`
- Test: `tests/test_detect_rules.py`

**Interfaces:**
- Consumes: everything from Tasks 2-3.
- Produces:
  - `detect.SymbolTooVague` — exception carrying `.symbol`
  - `detect.validate_symbols(deltas: list[dict], deps: list[str]) -> None` — raises `SymbolTooVague` when a bare symbol is shorter than `MIN_SYMBOL_LEN`
  - `detect.add_collisions(me, others, root, my_id=None) -> list[dict]`
  - `detect.god_symbols(me, root, cache=None) -> list[dict]`
  - `detect.goal_overlaps(me, others, root, my_id=None) -> list[dict]`
  - `detect.detect_all(me, others, root, my_id=None) -> list[dict]` — the single entry point `cli.declare` calls

- [ ] **Step 1: Write the failing test**

`tests/test_detect_rules.py`:

```python
import pytest

from agentmesh import detect, state


def _row(**kw):
    base = {"goal": "", "globs": [], "deltas": [], "deps": [], "touched": [],
            "worktree": "/repo", "state": "active"}
    base.update(kw)
    return base


def test_short_bare_symbol_is_rejected(repo):
    with pytest.raises(detect.SymbolTooVague) as e:
        detect.validate_symbols([{"kind": "remove", "symbol": "get"}], [])
    assert e.value.symbol == "get"


def test_short_symbol_is_accepted_once_qualified(repo):
    detect.validate_symbols(
        [{"kind": "remove", "symbol": "src/auth.py:get"}], []
    )


def test_short_dep_is_rejected_too(repo):
    with pytest.raises(detect.SymbolTooVague):
        detect.validate_symbols([], ["run"])


def test_the_guard_never_looks_at_hit_counts(repo):
    """Ambiguity only. Hit counts belong to GOD_SYMBOL — one symbol must
    never be both 'rejected' and 'escalated'."""
    detect.validate_symbols(
        [{"kind": "remove", "symbol": "getUserFromSession"}], []
    )


def test_two_agents_adding_the_same_symbol_collide(repo):
    me = _row(deltas=[{"kind": "add", "symbol": "verifyJwt"}])
    them = {"b7d2e0": _row(deltas=[{"kind": "add", "symbol": "verifyJwt"}],
                           goal="also doing jwt")}
    got = detect.add_collisions(me, them, str(repo))
    assert [c["kind"] for c in got] == [detect.ADD_COLLISION]
    assert got[0]["ref"] == "verifyJwt"


def test_add_collision_on_a_symbol_crosses_worktrees(repo):
    me = _row(deltas=[{"kind": "add", "symbol": "verifyJwt"}],
              worktree="/repo")
    them = {"b7d2e0": _row(deltas=[{"kind": "add", "symbol": "verifyJwt"}],
                           worktree="/elsewhere/wt-a")}
    assert len(detect.add_collisions(me, them, str(repo))) == 1


def test_adding_different_symbols_is_fine(repo):
    me = _row(deltas=[{"kind": "add", "symbol": "verifyJwt"}])
    them = {"b7d2e0": _row(deltas=[{"kind": "add", "symbol": "signJwt"}])}
    assert detect.add_collisions(me, them, str(repo)) == []


def test_symbol_over_the_reference_cap_is_a_god_symbol(repo, monkeypatch):
    monkeypatch.setattr(state, "GOD_SYMBOL_REFS", 1)
    me = _row(deltas=[{"kind": "remove", "symbol": "getUserFromSession"}])
    got = detect.god_symbols(me, str(repo))
    assert [c["kind"] for c in got] == [detect.GOD_SYMBOL]
    assert got[0]["status"] == "escalated"


def test_symbol_under_the_cap_is_not_a_god_symbol(repo):
    me = _row(deltas=[{"kind": "remove", "symbol": "getUserFromSession"}])
    assert detect.god_symbols(me, str(repo)) == []


def test_same_globs_with_no_deltas_warns_but_does_not_block(repo):
    me = _row(globs=["src/**"], goal="tidy imports")
    them = {"b7d2e0": _row(globs=["src/**"], goal="tidy imports too")}
    got = detect.goal_overlaps(me, them, str(repo))
    assert [c["kind"] for c in got] == [detect.GOAL_OVERLAP]
    assert got[0]["status"] == "resolved"     # never blocks the gate


def test_no_goal_overlap_when_either_side_has_deltas(repo):
    me = _row(globs=["src/**"], deltas=[{"kind": "add", "symbol": "helperFn"}])
    them = {"b7d2e0": _row(globs=["src/**"])}
    assert detect.goal_overlaps(me, them, str(repo)) == []


def test_detect_all_returns_every_class_at_once(repo):
    me = _row(globs=["src/auth.py"],
              deltas=[{"kind": "remove", "symbol": "getUserFromSession"},
                      {"kind": "add", "symbol": "verifyJwt"}])
    them = {"b7d2e0": _row(globs=["src/api.py", "src/auth.py"],
                           deltas=[{"kind": "add", "symbol": "verifyJwt"}],
                           goal="other work")}
    kinds = {c["kind"] for c in detect.detect_all(me, them, str(repo))}
    assert kinds == {detect.FILE_OVERLAP, detect.INTERFACE_BREAK,
                     detect.ADD_COLLISION}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_detect_rules.py -v`
Expected: FAIL — `AttributeError: module 'agentmesh.detect' has no attribute 'SymbolTooVague'`

- [ ] **Step 3: Write the implementation — append to `agentmesh/detect.py`**

```python
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
    found = []
    for aid, row in sorted(_live(others, my_id).items()):
        for symbol in sorted(mine & _added(row)):
            found.append(conflict(
                ADD_COLLISION, symbol, aid,
                f"{aid} is also adding {symbol}",
            ))
        if row.get("worktree") == me.get("worktree"):
            new_mine = set(me.get("globs", [])) - set(tracked_files(root))
            new_theirs = set(row.get("globs", [])) - set(tracked_files(root))
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_detect_rules.py -v`
Expected: all 12 PASS

- [ ] **Step 5: Run the whole detect suite together**

Run: `python -m pytest tests/ -v`
Expected: all PASS (Tasks 1-4)

- [ ] **Step 6: Commit**

```bash
git add agentmesh/detect.py tests/test_detect_rules.py
git commit -m "feat: add-collision, god-symbol, goal-overlap, symbol guard

The short-symbol guard is ambiguity-only and never reads hit counts, so a
symbol can never be both rejected and escalated. detect_all is the single
entry point declare calls."
```

---

### Task 5: Hook payload parsing and the Bash write-shape scanner

**Files:**
- Create: `agentmesh/hooks.py`
- Test: `tests/test_hooks_payload.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `hooks.targets_from_bash(cmd: str) -> list[str]` — repo-relative-or-absolute paths a shell command would write
  - `hooks.payload_targets(payload: dict) -> list[str]` — write targets for any tool, `[]` for reads
  - `hooks.agent_id(payload: dict) -> str` — `payload["session_id"][:6]`

- [ ] **Step 1: Write the failing test**

`tests/test_hooks_payload.py`:

```python
import pytest

from agentmesh import hooks

WRITES = [
    ("cat > src/auth.py <<'EOF'\nx\nEOF", ["src/auth.py"]),
    ("echo hi >> src/log.txt", ["src/log.txt"]),
    ("sed -i '' 's/a/b/' src/auth.py", ["src/auth.py"]),
    ("sed -i.bak 's/a/b/' src/auth.py", ["src/auth.py"]),
    ("tee src/out.txt < in.txt", ["src/out.txt"]),
    ("mv src/a.py src/b.py", ["src/a.py", "src/b.py"]),
    ("cp src/a.py src/b.py", ["src/b.py"]),
    ("rm -f src/gone.py", ["src/gone.py"]),
    ("patch -p1 < fix.diff", ["fix.diff"]),
    ("git apply fix.diff", ["fix.diff"]),
    ("git checkout -- src/auth.py", ["src/auth.py"]),
    ("git restore src/auth.py", ["src/auth.py"]),
    ("python3 -c 'x' && echo y > src/z.txt", ["src/z.txt"]),
]

READS = [
    "grep -rn getUserFromSession src/",
    "cat src/auth.py",
    "git status --short",
    "ls -la src/",
    "python -m pytest tests/ -v",
    "echo 'a > b'",                      # redirect inside quotes is not a write
]


@pytest.mark.parametrize("cmd,expected", WRITES)
def test_write_shapes_are_detected(cmd, expected):
    assert sorted(hooks.targets_from_bash(cmd)) == sorted(expected)


@pytest.mark.parametrize("cmd", READS)
def test_reads_are_never_treated_as_writes(cmd):
    assert hooks.targets_from_bash(cmd) == []


def test_edit_tool_target():
    payload = {"tool_name": "Edit",
               "tool_input": {"file_path": "/repo/src/auth.py"}}
    assert hooks.payload_targets(payload) == ["/repo/src/auth.py"]


def test_write_tool_target():
    payload = {"tool_name": "Write",
               "tool_input": {"file_path": "/repo/src/new.py"}}
    assert hooks.payload_targets(payload) == ["/repo/src/new.py"]


def test_notebook_edit_target():
    payload = {"tool_name": "NotebookEdit",
               "tool_input": {"notebook_path": "/repo/nb.ipynb"}}
    assert hooks.payload_targets(payload) == ["/repo/nb.ipynb"]


def test_bash_tool_routes_through_the_scanner():
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "echo x > src/a.txt"}}
    assert hooks.payload_targets(payload) == ["src/a.txt"]


def test_unknown_tool_has_no_targets():
    assert hooks.payload_targets({"tool_name": "Grep",
                                  "tool_input": {"pattern": "x"}}) == []


def test_agent_id_is_the_session_prefix():
    assert hooks.agent_id({"session_id": "a3f9c1de-dead-beef"}) == "a3f9c1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_hooks_payload.py -v`
Expected: FAIL — `ImportError: cannot import name 'hooks'`

- [ ] **Step 3: Write the implementation**

`agentmesh/hooks.py`:

```python
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
_WRITE_PATTERNS = [
    re.compile(r">>?\s*" + _PATH),                       # > file, >> file
    re.compile(r"\bsed\s+-i\S*\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s+" + _PATH),
    re.compile(r"\btee\s+(?:-a\s+)?" + _PATH),
    re.compile(r"\bmv\s+(?:-\S+\s+)*" + _PATH + r"\s+" + _PATH),
    re.compile(r"\bcp\s+(?:-\S+\s+)*" + _PATH + r"\s+" + _PATH),
    re.compile(r"\brm\s+(?:-\S+\s+)*" + _PATH),
    re.compile(r"\bpatch\b[^<]*<\s*" + _PATH),
    re.compile(r"\bgit\s+apply\s+(?:-\S+\s+)*" + _PATH),
    re.compile(r"\bgit\s+(?:checkout|restore)\s+(?:--\s+|-\S+\s+)*" + _PATH),
]
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_HEREDOC = re.compile(r"<<-?\s*'?\w+'?")


def _strip_quotes_and_heredocs(cmd):
    """A redirect inside quotes is data, not a write. Blank quoted runs out
    but keep the length so nothing else shifts."""
    cmd = _HEREDOC.sub(" ", cmd)
    return _QUOTED.sub(lambda m: " " * len(m.group(0)), cmd)


def targets_from_bash(cmd):
    scrubbed = _strip_quotes_and_heredocs(cmd)
    found = []
    for rx in _WRITE_PATTERNS:
        for m in rx.finditer(scrubbed):
            for g in m.groups():
                if g and g not in ("--",) and not g.startswith("-"):
                    found.append(g)
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_hooks_payload.py -v`
Expected: all 20 PASS. If `mv` or `cp` groups leak a flag, tighten `_PATH` rather than special-casing.

- [ ] **Step 5: Commit**

```bash
git add agentmesh/hooks.py tests/test_hooks_payload.py
git commit -m "feat: hook payload parsing and Bash write-shape scanner

Bash is matched alongside Edit/Write because sed -i and cat > file bypass
an Edit-only gate. Quoted redirects and heredoc bodies are scrubbed so
reads are never blocked."
```

---

### Task 6: The gate

**Files:**
- Modify: `agentmesh/hooks.py`
- Test: `tests/test_hooks_gate.py`

**Interfaces:**
- Consumes: `state.get_all`, `state.bump`, `detect.claimed_paths`, `detect.matches_globs`. Message delivery is added in Task 7; `PreToolUse` returns empty context until then.
- Produces:
  - `hooks.ALLOW = 0`, `hooks.BLOCK = 2`
  - `hooks.gate(payload: dict) -> tuple[int, str]` — exit code and the text for stderr (on BLOCK) or `additionalContext` (on ALLOW)
  - `hooks.run(event: str, payload: dict) -> tuple[int, str, str]` — `(exit_code, stdout, stderr)` for every hook event: `SessionStart`, `PreToolUse`, `PostToolUse`, `SessionEnd`

- [ ] **Step 1: Write the failing test**

`tests/test_hooks_gate.py`:

```python
import time

from agentmesh import detect, hooks, state


def _payload(sid="a3f9c1", tool="Edit", path="src/auth.py"):
    return {"session_id": sid, "cwd": ".", "tool_name": tool,
            "tool_input": {"file_path": path}}


def test_undeclared_agent_is_blocked_with_instructions(repo):
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "agentmesh declare" in msg


def test_declared_agent_editing_its_own_file_passes(repo):
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root())
    code, _ = hooks.gate(_payload())
    assert code == hooks.ALLOW


def test_a_file_claimed_by_another_agent_blocks(repo):
    wt = state.worktree_root()
    state.upsert("a3f9c1", goal="mine", globs=["src/**"], worktree=wt)
    state.upsert("b7d2e0", goal="patch auth", globs=["src/auth.py"],
                 worktree=wt)
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "b7d2e0" in msg and "patch auth" in msg


def test_an_open_conflict_blocks_even_on_an_unrelated_file(repo):
    wt = state.worktree_root()
    state.upsert("a3f9c1", goal="mine", globs=["src/**"], worktree=wt,
                 conflicts=[detect.conflict(detect.INTERFACE_BREAK,
                                            "getUserFromSession", "b7d2e0",
                                            "claimed by b7d2e0")])
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "getUserFromSession" in msg


def test_a_resolved_conflict_opens_the_gate(repo):
    wt = state.worktree_root()
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "b7d2e0", "d")
    c["status"] = "resolved"
    state.upsert("a3f9c1", goal="mine", globs=["src/**"], worktree=wt,
                 conflicts=[c])
    assert hooks.gate(_payload())[0] == hooks.ALLOW


def test_creating_a_new_file_inside_declared_globs_passes(repo):
    """A file that does not exist yet is not tracked, so claimed_paths
    cannot see it. matches_globs must."""
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root())
    p = _payload(path="src/brand_new.py")
    assert hooks.gate(p)[0] == hooks.ALLOW


def test_touching_a_file_outside_declared_globs_blocks_for_redeclare(repo):
    state.upsert("a3f9c1", goal="g", globs=["docs/**"],
                 worktree=state.worktree_root())
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "re-declare" in msg


def test_a_read_is_never_blocked(repo):
    p = {"session_id": "a3f9c1", "tool_name": "Bash",
         "tool_input": {"command": "cat src/auth.py"}}
    assert hooks.gate(p)[0] == hooks.ALLOW


def test_the_gate_bumps_the_invocation_counter(repo):
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root())
    before = state.get_all()["a3f9c1"]["inv"]
    hooks.gate(_payload())
    assert state.get_all()["a3f9c1"]["inv"] == before + 1


def test_post_tool_use_records_the_touched_file(repo):
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root())
    hooks.run("PostToolUse", _payload())
    assert "src/auth.py" in state.get_all()["a3f9c1"]["touched"]


def test_session_end_releases_claims(repo):
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root())
    hooks.run("SessionEnd", {"session_id": "a3f9c1"})
    assert "a3f9c1" not in state.get_all()


def test_stop_is_not_an_event_the_gate_handles(repo):
    """Stop fires per response, not per session. Registering it would drop
    every agent's claims several times an hour."""
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root())
    code, _, _ = hooks.run("Stop", {"session_id": "a3f9c1"})
    assert code == hooks.ALLOW
    assert "a3f9c1" in state.get_all()          # claims survive


def test_session_start_injects_the_roster(repo):
    state.upsert("b7d2e0", goal="patch auth", globs=["src/auth.py"],
                 worktree=state.worktree_root())
    code, out, _ = hooks.run("SessionStart", {"session_id": "a3f9c1"})
    assert code == hooks.ALLOW
    assert "b7d2e0" in out and "patch auth" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_hooks_gate.py -v`
Expected: FAIL — `AttributeError: module 'agentmesh.hooks' has no attribute 'gate'`

- [ ] **Step 3: Write the implementation — append to `agentmesh/hooks.py`**

```python
import json
import os

from . import detect, state

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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_hooks_gate.py -v`
Expected: all 13 PASS

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS (Tasks 1-6)

- [ ] **Step 6: Commit**

```bash
git add agentmesh/hooks.py tests/test_hooks_gate.py
git commit -m "feat: the gate

Blocks on an unresolved conflict, another agent's claim, or a write
outside declared globs. Stop is a deliberate no-op; only SessionEnd
releases claims."
```

---

### Task 7: Negotiation messages and the four verbs

**Files:**
- Create: `agentmesh/negotiate.py`
- Modify: `agentmesh/hooks.py` (wire message delivery into `PreToolUse`)
- Test: `tests/test_negotiate.py`

**Interfaces:**
- Consumes: `state.locked/load/save/get_all/upsert`, `state.ROUND_CAP`.
- Produces:
  - `negotiate.KINDS = ("objection", "offer", "accept", "refuse")`
  - `negotiate.send(frm: str, to: str, kind: str, ref: str, msg: str) -> dict` — appends to the recipient's inbox, stamps `round` and `sent_at_inv` (the recipient's current `inv`), returns the message
  - `negotiate.drain(agent_id: str) -> list[dict]` — returns pending messages and truncates the inbox
  - `negotiate.format_for_agent(msgs: list[dict]) -> str` — text for `additionalContext`
  - `negotiate.apply(msg: dict) -> None` — mutates declarations: `accept` marks the matching conflict `resolved` and records the agreement; `refuse` increments `round`; `objection`/`offer` mark it `negotiating`
  - `negotiate.ForgedAccept` — exception raised when a sender accepts on its own behalf

- [ ] **Step 1: Write the failing test**

`tests/test_negotiate.py`:

```python
import pytest

from agentmesh import detect, negotiate, state


def _setup_conflict(repo):
    wt = state.worktree_root()
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "b7d2e0", "referenced in src/api.py")
    state.upsert("a3f9c1", goal="jwt migration", globs=["src/auth.py"],
                 worktree=wt, conflicts=[c])
    state.upsert("b7d2e0", goal="add pagination", globs=["src/api.py"],
                 worktree=wt, deps=["getUserFromSession"])
    return c


def test_send_then_drain_round_trips(repo):
    _setup_conflict(repo)
    negotiate.send("a3f9c1", "b7d2e0", "offer", "getUserFromSession",
                   "I will leave a shim")
    got = negotiate.drain("b7d2e0")
    assert len(got) == 1
    assert got[0]["from"] == "a3f9c1"
    assert got[0]["kind"] == "offer"
    assert got[0]["msg"] == "I will leave a shim"


def test_drain_truncates_the_inbox(repo):
    _setup_conflict(repo)
    negotiate.send("a3f9c1", "b7d2e0", "offer", "getUserFromSession", "m")
    negotiate.drain("b7d2e0")
    assert negotiate.drain("b7d2e0") == []


def test_send_records_the_recipients_invocation_count(repo):
    _setup_conflict(repo)
    state.bump("b7d2e0")
    state.bump("b7d2e0")
    msg = negotiate.send("a3f9c1", "b7d2e0", "offer", "getUserFromSession",
                         "m")
    assert msg["sent_at_inv"] == state.get_all()["b7d2e0"]["inv"]


def test_an_unknown_kind_is_rejected(repo):
    _setup_conflict(repo)
    with pytest.raises(ValueError):
        negotiate.send("a3f9c1", "b7d2e0", "shrug", "x", "m")


def test_accept_resolves_the_conflict_and_records_the_agreement(repo):
    _setup_conflict(repo)
    msg = negotiate.send("b7d2e0", "a3f9c1", "accept", "getUserFromSession",
                         "shim is fine")
    negotiate.apply(msg)
    row = state.get_all()["a3f9c1"]
    assert row["conflicts"][0]["status"] == "resolved"
    assert row["resolved"][0]["ref"] == "getUserFromSession"
    assert row["resolved"][0]["with"] == "b7d2e0"
    assert row["resolved"][0]["agreement"] == "shim is fine"


def test_an_agent_cannot_accept_on_its_own_behalf(repo):
    """Otherwise A unblocks itself in one line."""
    _setup_conflict(repo)
    msg = negotiate.send("a3f9c1", "a3f9c1", "accept", "getUserFromSession",
                         "I agree with me")
    with pytest.raises(negotiate.ForgedAccept):
        negotiate.apply(msg)
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] != "resolved"


def test_refuse_increments_the_round(repo):
    _setup_conflict(repo)
    msg = negotiate.send("b7d2e0", "a3f9c1", "refuse", "getUserFromSession",
                         "keep it as is")
    negotiate.apply(msg)
    assert state.get_all()["a3f9c1"]["conflicts"][0]["round"] == 1


def test_objection_marks_the_conflict_negotiating(repo):
    _setup_conflict(repo)
    msg = negotiate.send("b7d2e0", "a3f9c1", "objection",
                         "getUserFromSession", "I rely on this")
    negotiate.apply(msg)
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] == "negotiating"


def test_format_for_agent_names_sender_and_verb(repo):
    _setup_conflict(repo)
    msgs = [negotiate.send("b7d2e0", "a3f9c1", "objection",
                           "getUserFromSession", "I rely on this")]
    text = negotiate.format_for_agent(msgs)
    assert "b7d2e0" in text and "objection" in text and "I rely on this" in text


def test_format_for_agent_is_empty_when_there_is_nothing(repo):
    assert negotiate.format_for_agent([]) == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_negotiate.py -v`
Expected: FAIL — `ImportError: cannot import name 'negotiate'`

- [ ] **Step 3: Write the implementation**

`agentmesh/negotiate.py`:

```python
"""Messages between agents.

Verb roles are asymmetric on purpose: the blocked agent carries the burden
of proposing. Without that, both sides wait for the other to move.

  objection  sent by the dependent agent  "I rely on ref, do not remove it"
  offer      sent by the blocked agent     a concrete way forward
  accept     reply to an offer             agreed; terms recorded
  refuse     reply to an offer             rejected, counter-offer in msg
"""

import json
import time

from . import state

KINDS = ("objection", "offer", "accept", "refuse")


class ForgedAccept(Exception):
    """An accept whose sender is the agent it would unblock."""


def _inbox(agent_id):
    return state.state_dir() / "inbox" / f"{agent_id}.jsonl"


def send(frm, to, kind, ref, msg):
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    agents = state.get_all()
    recipient = agents.get(to, {})
    record = {
        "from": frm, "to": to, "kind": kind, "ref": ref, "msg": msg,
        "round": _current_round(agents, frm, to, ref),
        "sent_at_inv": recipient.get("inv", 0),
        "ts": time.time(),
    }
    with state.locked():
        p = _inbox(to)
        with open(p, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    return record


def _current_round(agents, frm, to, ref):
    for aid in (frm, to):
        for c in agents.get(aid, {}).get("conflicts", []):
            if c.get("ref") == ref:
                return c.get("round", 0)
    return 0


def drain(agent_id):
    with state.locked():
        p = _inbox(agent_id)
        if not p.exists():
            return []
        lines = [l for l in p.read_text().splitlines() if l.strip()]
        p.write_text("")                 # truncated the moment it is drained
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def format_for_agent(msgs):
    if not msgs:
        return ""
    lines = ["agentmesh: messages from other agents:"]
    for m in msgs:
        lines.append(
            f"  [{m['kind']}] from {m['from']} re {m['ref'] or '(files)'}: "
            f"{m['msg']}"
        )
        if m["kind"] == "objection":
            lines.append(
                f"      you are blocked, so you propose: agentmesh reply "
                f"--to {m['from']} --kind offer --ref {m['ref']} "
                f"--msg \"<shim / migration / other approach>\""
            )
        if m["kind"] == "offer":
            lines.append(
                f"      answer it: agentmesh reply --to {m['from']} "
                f"--kind accept|refuse --ref {m['ref']} --msg \"<why>\""
            )
    return "\n".join(lines)


def apply(msg):
    """Mutates the recipient's declaration according to the verb."""
    kind, ref, frm, to = msg["kind"], msg["ref"], msg["from"], msg["to"]
    if kind == "accept" and frm == to:
        raise ForgedAccept(
            f"agent {frm} cannot accept an offer that unblocks itself"
        )
    now = time.time()
    with state.locked():
        data = state.prune(state.load(), now)
        row = data["agents"].get(to)
        if row is None:
            return
        for c in row.get("conflicts", []):
            if c.get("ref") != ref:
                continue
            if kind == "accept":
                c["status"] = "resolved"
                row.setdefault("resolved", []).append({
                    "ref": ref, "with": frm, "agreement": msg["msg"],
                })
            elif kind == "refuse":
                c["status"] = "negotiating"
                c["round"] = c.get("round", 0) + 1
            else:
                c["status"] = "negotiating"
        state.save(data)
```

Then in `agentmesh/hooks.py`, wire delivery into the `PreToolUse` branch:

```python
from . import detect, negotiate, state
...
    if event == "PreToolUse":
        code, msg = gate(payload)
        if code == BLOCK:
            return BLOCK, "", msg
        return ALLOW, negotiate.format_for_agent(negotiate.drain(me_id)), ""
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_negotiate.py tests/test_hooks_gate.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agentmesh/negotiate.py agentmesh/hooks.py tests/test_negotiate.py
git commit -m "feat: negotiation messages and the four verbs

Blocked agent proposes, dependent agent objects, so neither side waits.
An accept whose sender is the agent it would unblock is rejected."
```

---

### Task 8: Timers, escalation, and the deterministic fallback

**Files:**
- Modify: `agentmesh/negotiate.py`
- Test: `tests/test_negotiate_timers.py`

**Interfaces:**
- Consumes: everything from Task 7.
- Produces:
  - `negotiate.is_absent(row: dict, now: float) -> bool` — `now - row["hb"] > TTL_ABSENT`
  - `negotiate.check_timers(now: float = None) -> list[dict]` — walks every open conflict and applies: absent counterparty → conflict `resolved` with an explanatory note left in the absent agent's inbox; silence past `NUDGE_AFTER_INV` → nudge queued; past `ESCALATE_AFTER_INV` or `round > ROUND_CAP` → `escalated`. Returns the conflicts it changed.
  - `negotiate.mark_waiting(agent_id: str, ref: str, recipient_inv: int) -> None` — records when an offer started waiting, so silence can be measured against the recipient's own activity
  - `negotiate.escalations() -> list[dict]` — every conflict with `status == "escalated"`, for `status` output
  - `negotiate._apply_fallback(now: float) -> list[dict]` — called by `check_timers`; applies first-claim-wins when an escalation goes unanswered
  - `negotiate.resolve(ref: str, decision: str) -> int` — the human verdict; marks every conflict on that `ref` `resolved` across all agents, returns how many it changed
  - `negotiate.yield_later_declarer(ref: str) -> str` — deterministic fallback; returns the id told to yield

- [ ] **Step 1: Write the failing test**

`tests/test_negotiate_timers.py`:

```python
import time

from agentmesh import detect, negotiate, state


def _pair(repo, *, a_conflict=True, hb_b=None):
    wt = state.worktree_root()
    now = time.time()
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "b7d2e0", "referenced in src/api.py")
    state.upsert("a3f9c1", goal="jwt migration", globs=["src/auth.py"],
                 worktree=wt, conflicts=[c] if a_conflict else [])
    state.upsert("b7d2e0", goal="add pagination", globs=["src/api.py"],
                 worktree=wt)
    if hb_b is not None:
        with state.locked():
            data = state.load()
            data["agents"]["b7d2e0"]["hb"] = hb_b
            state.save(data)
    return now


def test_absent_counterparty_lets_the_blocked_agent_through(repo):
    now = _pair(repo, hb_b=time.time() - state.TTL_ABSENT - 1)
    changed = negotiate.check_timers(now)
    assert [c["status"] for c in changed] == ["resolved"]
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] == "resolved"


def test_absent_counterparty_gets_a_note_explaining_what_happened(repo):
    now = _pair(repo, hb_b=time.time() - state.TTL_ABSENT - 1)
    negotiate.check_timers(now)
    notes = negotiate.drain("b7d2e0")
    assert len(notes) == 1
    assert "getUserFromSession" in notes[0]["msg"]
    assert "proceeded" in notes[0]["msg"]


def test_a_live_counterparty_does_not_trigger_the_absent_path(repo):
    now = _pair(repo)
    assert negotiate.check_timers(now) == []
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] == "open"


def test_silence_past_the_nudge_threshold_queues_one_nudge(repo):
    now = _pair(repo)
    negotiate.send("a3f9c1", "b7d2e0", "offer", "getUserFromSession", "shim?")
    negotiate.drain("b7d2e0")                 # B read it and said nothing
    for _ in range(state.NUDGE_AFTER_INV):
        state.bump("b7d2e0")
    negotiate.check_timers(now)
    nudges = [m for m in negotiate.drain("b7d2e0") if "nudge" in m["msg"]]
    assert len(nudges) == 1


def test_silence_past_the_escalate_threshold_escalates(repo):
    now = _pair(repo)
    negotiate.send("a3f9c1", "b7d2e0", "offer", "getUserFromSession", "shim?")
    negotiate.drain("b7d2e0")
    for _ in range(state.ESCALATE_AFTER_INV):
        state.bump("b7d2e0")
    negotiate.check_timers(now)
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] == "escalated"


def test_exceeding_the_round_cap_escalates(repo):
    now = _pair(repo)
    with state.locked():
        data = state.load()
        data["agents"]["a3f9c1"]["conflicts"][0]["round"] = state.ROUND_CAP + 1
        state.save(data)
    negotiate.check_timers(now)
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] == "escalated"


def test_escalations_lists_what_needs_a_human(repo):
    now = _pair(repo)
    with state.locked():
        data = state.load()
        data["agents"]["a3f9c1"]["conflicts"][0]["status"] = "escalated"
        state.save(data)
    got = negotiate.escalations()
    assert len(got) == 1
    assert got[0]["ref"] == "getUserFromSession"
    assert got[0]["agent"] == "a3f9c1"


def test_resolve_applies_the_verdict_to_every_pair_on_that_ref(repo):
    _pair(repo)
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "a3f9c1", "d")
    state.upsert("c9e1f0", goal="third agent", conflicts=[c],
                 worktree=state.worktree_root())
    n = negotiate.resolve("getUserFromSession", "keep a deprecated shim")
    assert n == 2
    for aid in ("a3f9c1", "c9e1f0"):
        row = state.get_all()[aid]
        assert row["conflicts"][0]["status"] == "resolved"
        assert row["resolved"][0]["agreement"] == "keep a deprecated shim"


def test_an_unanswered_escalation_falls_back_to_first_claim_wins(repo):
    now = _pair(repo)
    with state.locked():
        data = state.load()
        data["agents"]["a3f9c1"]["declared_at"] = 200.0   # later
        data["agents"]["b7d2e0"]["declared_at"] = 100.0   # earlier
        c = data["agents"]["a3f9c1"]["conflicts"][0]
        c["status"] = "escalated"
        c["escalated_at_inv"] = 0
        data["agents"]["a3f9c1"]["inv"] = state.ESCALATE_AFTER_INV
        state.save(data)
    negotiate.check_timers(now)
    row = state.get_all()["a3f9c1"]
    assert row["conflicts"][0]["status"] == "resolved"
    assert "yields" in row["resolved"][-1]["agreement"]
    told = negotiate.drain("a3f9c1")
    assert any("you yield" in m["msg"] for m in told)


def test_an_escalation_within_the_grace_window_is_left_alone(repo):
    now = _pair(repo)
    with state.locked():
        data = state.load()
        c = data["agents"]["a3f9c1"]["conflicts"][0]
        c["status"] = "escalated"
        c["escalated_at_inv"] = 0
        data["agents"]["a3f9c1"]["inv"] = 1
        state.save(data)
    negotiate.check_timers(now)
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] == "escalated"


def test_the_later_declarer_yields(repo):
    _pair(repo)
    with state.locked():
        data = state.load()
        data["agents"]["a3f9c1"]["declared_at"] = 100.0
        data["agents"]["b7d2e0"]["declared_at"] = 200.0
        state.save(data)
    assert negotiate.yield_later_declarer("getUserFromSession") == "b7d2e0"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_negotiate_timers.py -v`
Expected: FAIL — `AttributeError: module 'agentmesh.negotiate' has no attribute 'is_absent'`

- [ ] **Step 3: Write the implementation — append to `agentmesh/negotiate.py`**

```python
def is_absent(row, now):
    return now - row.get("hb", 0) > state.TTL_ABSENT


def _last_offer_inv(agent_id, ref):
    """The recipient's inv when we last messaged them about ref. Kept on the
    conflict so it survives inbox truncation."""
    for c in state.get_all().get(agent_id, {}).get("conflicts", []):
        if c.get("ref") == ref:
            return c.get("waiting_since_inv")
    return None


def check_timers(now=None):
    now = time.time() if now is None else now
    changed, notes, nudges = [], [], []

    with state.locked():
        data = state.prune(state.load(), now)
        agents = data["agents"]
        for aid, row in agents.items():
            for c in row.get("conflicts", []):
                if c.get("status") in ("resolved", "escalated"):
                    continue
                other_id = c.get("with")
                other = agents.get(other_id)

                # 1. absent counterparty: never hold the repo hostage
                if other_id and (other is None or is_absent(other, now)):
                    c["status"] = "resolved"
                    c["detail"] += " | counterparty absent, proceeded"
                    row.setdefault("resolved", []).append({
                        "ref": c["ref"], "with": other_id,
                        "agreement": "counterparty absent; proceeded unilaterally",
                    })
                    if other is not None:
                        notes.append((other_id, aid, c["ref"]))
                    changed.append(c)
                    continue

                # 2. round cap
                if c.get("round", 0) > state.ROUND_CAP:
                    c["status"] = "escalated"
                    c["escalated_at_inv"] = row.get("inv", 0)
                    changed.append(c)
                    continue

                # 3. escalated with no human verdict: the spec's
                #    deterministic fallback, applied automatically
                if c.get("status") == "escalated":
                    continue

                # 4. silence, counted in the recipient's own activity
                waiting = c.get("waiting_since_inv")
                if waiting is not None:
                    elapsed = other.get("inv", 0) - waiting
                    if elapsed >= state.ESCALATE_AFTER_INV:
                        c["status"] = "escalated"
                        c["escalated_at_inv"] = row.get("inv", 0)
                        changed.append(c)
                    elif elapsed >= state.NUDGE_AFTER_INV \
                            and not c.get("nudged"):
                        c["nudged"] = True
                        nudges.append((other_id, aid, c["ref"]))
                        changed.append(c)
        state.save(data)

    changed += _apply_fallback(now)

    for to, frm, ref in notes:
        send(frm, to, "objection", ref,
             f"{frm} proceeded with {ref} while you were absent "
             f"(no activity for over {state.TTL_ABSENT}s). "
             f"Re-read the file before continuing.")
    for to, frm, ref in nudges:
        send(frm, to, "objection", ref,
             f"nudge: {frm} is still blocked on {ref} and needs your reply "
             f"(accept or refuse).")
    return changed


def _apply_fallback(now):
    """An escalated conflict the human never answers cannot stall forever.

    Once the escalated agent has passed ESCALATE_AFTER_INV more gate
    invocations with no verdict, first claim wins by timestamp: the later
    declarer yields and is told to adapt, the earlier declarer proceeds.
    Arbitrary, but predictable beats a stall.
    """
    changed, told = [], []
    with state.locked():
        data = state.prune(state.load(), now)
        agents = data["agents"]
        for aid, row in agents.items():
            for c in row.get("conflicts", []):
                if c.get("status") != "escalated":
                    continue
                waited = row.get("inv", 0) - c.get("escalated_at_inv", 0)
                if waited < state.ESCALATE_AFTER_INV:
                    continue
                pair = [x for x in (aid, c.get("with")) if x in agents]
                if len(pair) < 2:
                    continue
                loser = max(pair,
                            key=lambda a: agents[a].get("declared_at", 0))
                winner = pair[0] if pair[1] == loser else pair[1]
                for holder in pair:
                    for cc in agents[holder].get("conflicts", []):
                        if cc.get("ref") != c["ref"]:
                            continue
                        cc["status"] = "resolved"
                        cc["detail"] += (
                            f" | no human verdict; first claim wins, "
                            f"{loser} yields to {winner}"
                        )
                        changed.append(cc)
                    agents[holder].setdefault("resolved", []).append({
                        "ref": c["ref"], "with": "fallback",
                        "agreement": f"first claim wins; {loser} yields "
                                     f"to {winner}",
                    })
                told.append((loser, winner, c["ref"]))
        state.save(data)

    for loser, winner, ref in told:
        send(winner, loser, "objection", ref,
             f"no human verdict arrived, so the deterministic fallback "
             f"applied: first claim wins and you declared later, so you "
             f"yield on {ref}. Adapt to {winner}'s change.")
    return changed


def mark_waiting(agent_id, ref, recipient_inv):
    """Called after an offer is sent, so silence can be measured."""
    now = time.time()
    with state.locked():
        data = state.prune(state.load(), now)
        for c in data["agents"].get(agent_id, {}).get("conflicts", []):
            if c.get("ref") == ref:
                c["waiting_since_inv"] = recipient_inv
                c["nudged"] = False
        state.save(data)


def escalations():
    out = []
    for aid, row in sorted(state.get_all().items()):
        for c in row.get("conflicts", []):
            if c.get("status") == "escalated":
                entry = dict(c)
                entry["agent"] = aid
                out.append(entry)
    return out


def resolve(ref, decision):
    """A verdict on a symbol applies to every pair contending on it — an
    architectural decision should not need restating per agent pair."""
    now = time.time()
    n = 0
    with state.locked():
        data = state.prune(state.load(), now)
        for row in data["agents"].values():
            for c in row.get("conflicts", []):
                if c.get("ref") == ref and c.get("status") != "resolved":
                    c["status"] = "resolved"
                    row.setdefault("resolved", []).append({
                        "ref": ref, "with": "human", "agreement": decision,
                    })
                    n += 1
        state.save(data)
    return n


def yield_later_declarer(ref):
    """Deterministic fallback. Predictable beats a stall."""
    agents = state.get_all()
    involved = set()
    for aid, row in agents.items():
        for c in row.get("conflicts", []):
            if c.get("ref") == ref:
                involved.add(aid)
                if c.get("with"):
                    involved.add(c["with"])
    if not involved:
        return ""
    return max(involved,
               key=lambda a: agents.get(a, {}).get("declared_at", 0))
```

Also add `declared_at` to `state._blank_row` so the fallback has a timestamp:

```python
        "resolved": [], "state": "active", "hb": now, "inv": 0,
        "declared_at": now,
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_negotiate_timers.py -v`
Expected: all 9 PASS. The nudge/escalate tests require `mark_waiting` to have been called — if they fail, wire `send` to call `mark_waiting` for `kind == "offer"` before returning.

- [ ] **Step 5: Wire `mark_waiting` into `send` for offers**

At the end of `send`, before `return record`:

```python
    if kind == "offer":
        mark_waiting(frm, ref, record["sent_at_inv"])
```

- [ ] **Step 6: Re-run everything**

Run: `python -m pytest tests/ -v`
Expected: all PASS (Tasks 1-8)

- [ ] **Step 7: Commit**

```bash
git add agentmesh/negotiate.py agentmesh/state.py tests/test_negotiate_timers.py
git commit -m "feat: absent-agent, silence, and round-cap timers

An absent counterparty never holds the repo hostage; the blocked agent
proceeds and leaves a note. Silence is measured in the recipient's own
gate invocations, so a long build is not punished."
```

---

### Task 9: CLI — declare, status, reply, resolve, release, hook

**Files:**
- Create: `agentmesh/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `state`, `detect`, `negotiate`, `hooks`.
- Produces:
  - `cli.main(argv: list[str] = None) -> int` — process exit code
  - `cli.cmd_declare(args) -> int`, `cmd_status(args) -> int`, `cmd_reply(args) -> int`, `cmd_resolve(args) -> int`, `cmd_release(args) -> int`, `cmd_hook(args) -> int`
  - `cli.resolve_agent_id(explicit: str = None) -> str` — `--id`, else `$CLAUDE_SESSION_ID[:6]`, else `$AGENTMESH_ID`, else the single active agent in this worktree, else error

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
import json

from agentmesh import cli, negotiate, state


def _run(*argv, env=None, monkeypatch=None):
    if env and monkeypatch:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    return cli.main(list(argv))


def test_declare_creates_a_row(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    assert _run("declare", "--goal", "jwt migration",
                "--globs", "src/auth.py") == 0
    row = state.get_all()["a3f9c1"]
    assert row["goal"] == "jwt migration"
    assert row["globs"] == ["src/auth.py"]
    assert row["worktree"] == state.worktree_root()


def test_declare_accepts_multiple_globs_and_deltas(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    _run("declare", "--goal", "g", "--globs", "src/**", "docs/**",
         "--removes", "getUserFromSession", "--adds", "verifyJwt",
         "--deps", "createSession", "--progress", "half done")
    row = state.get_all()["a3f9c1"]
    assert row["globs"] == ["src/**", "docs/**"]
    assert {"kind": "remove", "symbol": "getUserFromSession"} in row["deltas"]
    assert {"kind": "add", "symbol": "verifyJwt"} in row["deltas"]
    assert row["deps"] == ["createSession"]
    assert row["progress"] == "half done"


def test_declare_is_idempotent_and_recomputes_conflicts(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    state.upsert("b7d2e0", goal="pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    _run("declare", "--goal", "g", "--globs", "src/auth.py",
         "--removes", "getUserFromSession")
    first = state.get_all()["a3f9c1"]["conflicts"]
    _run("declare", "--goal", "g", "--globs", "src/auth.py",
         "--removes", "getUserFromSession")
    second = state.get_all()["a3f9c1"]["conflicts"]
    assert len(first) == len(second) == 1


def test_declare_rejects_a_vague_symbol_with_a_useful_message(repo, capsys,
                                                              monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    rc = _run("declare", "--goal", "g", "--globs", "src/**",
              "--removes", "get")
    assert rc == 1
    assert "Qualify it as" in capsys.readouterr().err


def test_declare_reports_conflicts_on_stdout(repo, capsys, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    state.upsert("b7d2e0", goal="pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    _run("declare", "--goal", "g", "--globs", "src/auth.py",
         "--removes", "getUserFromSession")
    out = capsys.readouterr().out
    assert "INTERFACE_BREAK" in out and "b7d2e0" in out


def test_status_lists_the_roster(repo, capsys, monkeypatch):
    state.upsert("b7d2e0", goal="pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    assert _run("status") == 0
    out = capsys.readouterr().out
    assert "b7d2e0" in out and "pagination" in out


def test_status_json_is_machine_readable(repo, capsys, monkeypatch):
    state.upsert("b7d2e0", goal="pagination", worktree=state.worktree_root())
    _run("status", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["agents"]["b7d2e0"]["goal"] == "pagination"


def test_reply_delivers_a_message(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    wt = state.worktree_root()
    state.upsert("a3f9c1", goal="a", worktree=wt)
    state.upsert("b7d2e0", goal="b", worktree=wt)
    assert _run("reply", "--to", "b7d2e0", "--kind", "offer",
                "--ref", "getUserFromSession", "--msg", "shim?") == 0
    assert negotiate.drain("b7d2e0")[0]["msg"] == "shim?"


def test_reply_rejects_an_unknown_recipient(repo, capsys, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    state.upsert("a3f9c1", goal="a", worktree=state.worktree_root())
    rc = _run("reply", "--to", "nobody", "--kind", "offer", "--ref", "x",
              "--msg", "m")
    assert rc == 1
    assert "no active agent" in capsys.readouterr().err


def test_resolve_records_the_human_verdict(repo, monkeypatch):
    from agentmesh import detect
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "b7d2e0", "d")
    state.upsert("a3f9c1", goal="a", conflicts=[c],
                 worktree=state.worktree_root())
    assert _run("resolve", "--ref", "getUserFromSession",
                "--decision", "keep a shim") == 0
    row = state.get_all()["a3f9c1"]
    assert row["conflicts"][0]["status"] == "resolved"
    assert row["resolved"][0]["agreement"] == "keep a shim"


def test_release_marks_the_agent_done(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    state.upsert("a3f9c1", goal="a", worktree=state.worktree_root())
    assert _run("release") == 0
    assert "a3f9c1" not in state.get_all()


def test_hook_blocks_an_undeclared_write(repo, capsys):
    payload = json.dumps({"session_id": "a3f9c1", "tool_name": "Edit",
                          "tool_input": {"file_path": "src/auth.py"}})
    import io, sys
    sys.stdin = io.StringIO(payload)
    rc = _run("hook", "--event", "PreToolUse")
    assert rc == 2
    assert "agentmesh declare" in capsys.readouterr().err


def test_hook_emits_additional_context_json_on_allow(repo, capsys):
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root())
    negotiate.send("b7d2e0", "a3f9c1", "objection", "getUserFromSession",
                   "I rely on this")
    payload = json.dumps({"session_id": "a3f9c1", "tool_name": "Edit",
                          "tool_input": {"file_path": "src/auth.py"}})
    import io, sys
    sys.stdin = io.StringIO(payload)
    rc = _run("hook", "--event", "PreToolUse")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "I rely on this" in \
        out["hookSpecificOutput"]["additionalContext"]


def test_resolve_agent_id_prefers_explicit_over_env(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "fromenv")
    assert cli.resolve_agent_id("abc123") == "abc123"


def test_resolve_agent_id_falls_back_to_the_only_active_agent(repo,
                                                              monkeypatch):
    monkeypatch.delenv("AGENTMESH_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    state.upsert("solo01", goal="g", worktree=state.worktree_root())
    assert cli.resolve_agent_id() == "solo01"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ImportError: cannot import name 'cli'`

- [ ] **Step 3: Write the implementation**

`agentmesh/cli.py`:

```python
"""One binary, two faces: `agentmesh hook` for hooks, the rest for agents
and humans."""

import argparse
import json
import os
import sys

from . import detect, hooks, negotiate, state


def resolve_agent_id(explicit=None):
    if explicit:
        return explicit
    sid = os.environ.get("CLAUDE_SESSION_ID")
    if sid:
        return sid[:6]
    env = os.environ.get("AGENTMESH_ID")
    if env:
        return env
    here = state.worktree_root()
    mine = [aid for aid, row in state.get_all().items()
            if row.get("worktree") == here]
    if len(mine) == 1:
        return mine[0]
    raise SystemExit(
        "agentmesh: cannot tell which agent you are. Pass --id, or set "
        "AGENTMESH_ID."
    )


def _print_conflicts(conflicts):
    if not conflicts:
        print("agentmesh: declared, no conflicts.")
        return
    blocking = [c for c in conflicts if c.get("status") != "resolved"]
    print(f"agentmesh: declared, {len(conflicts)} conflict(s) "
          f"({len(blocking)} blocking):")
    for c in conflicts:
        flag = " (advisory)" if c.get("status") == "resolved" else ""
        print(f"  [{c['kind']}]{flag} {c['ref'] or '(files)'} with "
              f"{c.get('with') or '(repo-wide)'}: {c['detail']}")


def cmd_declare(args):
    agent_id = resolve_agent_id(args.id)
    deltas = ([{"kind": "remove", "symbol": s} for s in args.removes]
              + [{"kind": "sig", "symbol": s} for s in args.sig]
              + [{"kind": "add", "symbol": s} for s in args.adds])
    try:
        detect.validate_symbols(deltas, args.deps)
    except detect.SymbolTooVague as e:
        print(f"agentmesh: {e}", file=sys.stderr)
        return 1

    fields = {
        "goal": args.goal, "globs": args.globs, "deltas": deltas,
        "deps": args.deps, "worktree": state.worktree_root(),
    }
    if args.progress is not None:
        fields["progress"] = args.progress
    state.upsert(agent_id, **fields)

    agents = state.get_all()
    me = agents[agent_id]
    conflicts = detect.detect_all(me, agents, state.worktree_root(),
                                  my_id=agent_id)
    # preserve status/round on conflicts already under negotiation
    prior = {(c["kind"], c["ref"], c.get("with")): c
             for c in me.get("conflicts", [])}
    for c in conflicts:
        old = prior.get((c["kind"], c["ref"], c.get("with")))
        if old:
            c.update({k: old[k] for k in
                      ("status", "round", "waiting_since_inv", "nudged")
                      if k in old})
    state.upsert(agent_id, conflicts=conflicts)
    _print_conflicts(conflicts)
    return 0


def cmd_status(args):
    negotiate.check_timers()
    agents = state.get_all()
    if args.json:
        print(json.dumps({"agents": agents,
                          "escalations": negotiate.escalations()}, indent=2))
        return 0
    if not agents:
        print("agentmesh: no active agents.")
        return 0
    print(f"agentmesh: {len(agents)} active agent(s)")
    for aid, row in sorted(agents.items()):
        print(f"\n  {aid}  {row.get('goal', '')}")
        print(f"    worktree: {row.get('worktree', '')}")
        if row.get("globs"):
            print(f"    globs:    {', '.join(row['globs'])}")
        if row.get("progress"):
            print(f"    produced: {row['progress']}")
        for c in row.get("conflicts", []):
            print(f"    [{c['status']}] {c['kind']} {c['ref']} "
                  f"with {c.get('with') or '(repo-wide)'}")
    esc = negotiate.escalations()
    if esc:
        print("\nDECISION NEEDED")
        for c in esc:
            print(f"  {c['ref']}: {c['detail']}")
            print(f"    agent {c['agent']} vs {c.get('with') or '(repo)'}")
            print(f"    agentmesh resolve --ref {c['ref']} "
                  f"--decision \"<verdict>\"")
    return 0


def cmd_reply(args):
    agent_id = resolve_agent_id(args.id)
    if args.to not in state.get_all():
        print(f"agentmesh: no active agent {args.to!r}. "
              f"Run `agentmesh status` to see who is here.", file=sys.stderr)
        return 1
    msg = negotiate.send(agent_id, args.to, args.kind, args.ref, args.msg)
    try:
        negotiate.apply(msg)
    except negotiate.ForgedAccept as e:
        print(f"agentmesh: {e}", file=sys.stderr)
        return 1
    print(f"agentmesh: {args.kind} sent to {args.to} re {args.ref}")
    return 0


def cmd_resolve(args):
    n = negotiate.resolve(args.ref, args.decision)
    print(f"agentmesh: verdict recorded on {args.ref}, "
          f"{n} conflict(s) resolved.")
    return 0


def cmd_release(args):
    agent_id = resolve_agent_id(args.id)
    state.upsert(agent_id, state="done")
    print(f"agentmesh: {agent_id} released its claims.")
    return 0


def cmd_hook(args):
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0                       # never break the agent on bad input
    code, out, err = hooks.run(args.event, payload)
    if err:
        print(err, file=sys.stderr)
    if out:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": args.event,
            "additionalContext": out,
        }}))
    return code


def build_parser():
    p = argparse.ArgumentParser(prog="agentmesh")
    p.add_argument("--id", help="agent id (default: session id prefix)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("declare", help="register or update your intent")
    d.add_argument("--goal", required=True)
    d.add_argument("--globs", nargs="*", default=[])
    d.add_argument("--removes", nargs="*", default=[])
    d.add_argument("--sig", nargs="*", default=[])
    d.add_argument("--adds", nargs="*", default=[])
    d.add_argument("--deps", nargs="*", default=[])
    d.add_argument("--progress")
    d.set_defaults(func=cmd_declare)

    s = sub.add_parser("status", help="roster, conflicts, pending decisions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("reply", help="send a negotiation message")
    r.add_argument("--to", required=True)
    r.add_argument("--kind", required=True, choices=negotiate.KINDS)
    r.add_argument("--ref", required=True)
    r.add_argument("--msg", required=True)
    r.set_defaults(func=cmd_reply)

    v = sub.add_parser("resolve", help="record a human verdict")
    v.add_argument("--ref", required=True)
    v.add_argument("--decision", required=True)
    v.set_defaults(func=cmd_resolve)

    rel = sub.add_parser("release", help="drop your claims")
    rel.set_defaults(func=cmd_release)

    h = sub.add_parser("hook", help="internal; reads hook JSON on stdin")
    h.add_argument("--event", required=True)
    h.set_defaults(func=cmd_hook)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not hasattr(args, "id"):
        args.id = None
    return args.func(args)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_cli.py -v`
Expected: all 15 PASS

- [ ] **Step 5: Commit**

```bash
git add agentmesh/cli.py tests/test_cli.py
git commit -m "feat: CLI for declare, status, reply, resolve, release, hook

declare is idempotent and preserves negotiation state across re-runs, so
an agent can widen its claim without losing an in-flight agreement."
```

---

### Task 10: `agentmesh init`

**Files:**
- Modify: `agentmesh/cli.py`
- Create: `agentmesh/install.py`
- Test: `tests/test_install.py`

**Interfaces:**
- Consumes: `state.worktree_root`.
- Produces:
  - `install.HOOK_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "SessionEnd")`
  - `install.MATCHER = "Edit|Write|NotebookEdit|Bash"`
  - `install.hook_config() -> dict` — the `hooks` block to merge
  - `install.merge(existing: dict) -> dict` — idempotent merge; never duplicates an agentmesh entry, never drops a foreign one
  - `install.install(root: str, local: bool = False) -> str` — writes the settings file, returns its path

- [ ] **Step 1: Write the failing test**

`tests/test_install.py`:

```python
import json

from agentmesh import install


def test_hook_config_registers_exactly_four_events():
    cfg = install.hook_config()
    assert set(cfg["hooks"]) == set(install.HOOK_EVENTS)


def test_stop_is_never_registered():
    """Stop fires per response, not per session."""
    assert "Stop" not in install.hook_config()["hooks"]


def test_pretooluse_matches_bash_as_well_as_edit():
    entry = install.hook_config()["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == install.MATCHER
    assert "Bash" in entry["matcher"]


def test_install_writes_project_settings_by_default(repo):
    path = install.install(str(repo))
    assert path.endswith(".claude/settings.json")
    data = json.loads(open(path).read())
    assert "PreToolUse" in data["hooks"]


def test_install_local_writes_settings_local(repo):
    path = install.install(str(repo), local=True)
    assert path.endswith(".claude/settings.local.json")


def test_install_is_idempotent(repo):
    install.install(str(repo))
    path = install.install(str(repo))
    data = json.loads(open(path).read())
    assert len(data["hooks"]["PreToolUse"]) == 1


def test_install_preserves_unrelated_hooks(repo):
    p = repo / ".claude"
    p.mkdir()
    (p / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "my-own-linter"}]}]},
        "permissions": {"allow": ["Bash(ls:*)"]},
    }))
    path = install.install(str(repo))
    data = json.loads(open(path).read())
    cmds = [h["command"] for e in data["hooks"]["PreToolUse"]
            for h in e["hooks"]]
    assert "my-own-linter" in cmds
    assert any("agentmesh" in c for c in cmds)
    assert data["permissions"]["allow"] == ["Bash(ls:*)"]


def test_install_does_not_touch_gitignore(repo):
    """State lives inside .git, so there is nothing to ignore."""
    install.install(str(repo))
    assert not (repo / ".gitignore").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_install.py -v`
Expected: FAIL — `ImportError: cannot import name 'install'`

- [ ] **Step 3: Write the implementation**

`agentmesh/install.py`:

```python
"""`agentmesh init` — the entire onboarding.

No .gitignore edit is needed: state lives inside .git, which is never
tracked.
"""

import json
import os
import pathlib

HOOK_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "SessionEnd")
MATCHER = "Edit|Write|NotebookEdit|Bash"
_MARK = "agentmesh hook"


def _command(event):
    return f"agentmesh hook --event {event}"


def hook_config():
    hooks = {}
    for event in HOOK_EVENTS:
        entry = {"hooks": [{"type": "command", "command": _command(event)}]}
        if event in ("PreToolUse", "PostToolUse"):
            entry["matcher"] = MATCHER
        hooks[event] = [entry]
    return {"hooks": hooks}


def merge(existing):
    out = dict(existing)
    hooks = dict(out.get("hooks", {}))
    for event, entries in hook_config()["hooks"].items():
        current = [
            e for e in hooks.get(event, [])
            if not any(_MARK in h.get("command", "")
                       for h in e.get("hooks", []))
        ]
        hooks[event] = current + entries
    out["hooks"] = hooks
    return out


def install(root, local=False):
    name = "settings.local.json" if local else "settings.json"
    d = pathlib.Path(root) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    existing = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text())
        except json.JSONDecodeError:
            existing = {}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merge(existing), indent=2))
    os.replace(tmp, p)
    return str(p)
```

Then in `agentmesh/cli.py`, add the `init` subcommand:

```python
from . import detect, hooks, install, negotiate, state


def cmd_init(args):
    path = install.install(state.worktree_root(), local=args.local)
    scope = "this developer only" if args.local else "everyone who clones"
    print(f"agentmesh: hooks installed in {path} ({scope}).")
    print("Agents in this repo will now be asked to declare before editing.")
    return 0
```

and register it in `build_parser`, before `return p`:

```python
    i = sub.add_parser("init", help="install the hooks into this repo")
    i.add_argument("--local", action="store_true",
                   help="write settings.local.json instead of settings.json")
    i.set_defaults(func=cmd_init)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_install.py -v`
Expected: all 8 PASS

- [ ] **Step 5: Commit**

```bash
git add agentmesh/install.py agentmesh/cli.py tests/test_install.py
git commit -m "feat: agentmesh init installs the hooks

Idempotent merge that never duplicates an agentmesh entry and never drops
a foreign one. --local writes settings.local.json for opt-in per developer."
```

---

### Task 11: Three-agent integration — the test that proves the product works

**Files:**
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: the whole package through `cli.main` and `hooks.run`.
- Produces: nothing importable.

- [ ] **Step 1: Write the test**

`tests/test_integration.py`:

```python
"""Three agents, one repo, driven through real hook payloads."""

import json

from agentmesh import cli, hooks, negotiate, state


def _declare(agent_id, **kw):
    argv = ["--id", agent_id, "declare", "--goal", kw.pop("goal")]
    for flag, key in (("--globs", "globs"), ("--removes", "removes"),
                      ("--adds", "adds"), ("--deps", "deps")):
        if key in kw:
            argv += [flag, *kw.pop(key)]
    if "progress" in kw:
        argv += ["--progress", kw.pop("progress")]
    assert cli.main(argv) == 0


def _write(agent_id, path):
    return hooks.run("PreToolUse", {
        "session_id": agent_id, "tool_name": "Edit",
        "tool_input": {"file_path": path},
    })


def test_full_remove_objection_accept_cycle(repo):
    # A is migrating auth and will remove a symbol.
    _declare("aaaaaa", goal="migrate auth to JWT", globs=["src/auth.py"],
             removes=["getUserFromSession"], adds=["verifyJwt"])
    # B is working the file that calls it.
    _declare("bbbbbb", goal="add pagination", globs=["src/api.py"])
    # C is elsewhere entirely and must never be involved.
    _declare("cccccc", goal="write docs", globs=["docs/**"])

    # A re-declares now that B is present, so detection sees the contention.
    _declare("aaaaaa", goal="migrate auth to JWT", globs=["src/auth.py"],
             removes=["getUserFromSession"], adds=["verifyJwt"])

    conflicts = state.get_all()["aaaaaa"]["conflicts"]
    assert [c["kind"] for c in conflicts] == ["INTERFACE_BREAK"]
    assert conflicts[0]["with"] == "bbbbbb"

    # A is blocked at its next write.
    code, _, err = _write("aaaaaa", "src/auth.py")
    assert code == hooks.BLOCK
    assert "INTERFACE_BREAK" in err and "bbbbbb" in err

    # C is unaffected — docs/guide.md does not exist yet, which is exactly
    # why the gate must match globs by pattern and not only tracked files.
    assert _write("cccccc", "docs/guide.md")[0] == hooks.ALLOW

    # B objects; the message reaches B's... no: reaches A.
    assert cli.main(["--id", "bbbbbb", "reply", "--to", "aaaaaa",
                     "--kind", "objection", "--ref", "getUserFromSession",
                     "--msg", "src/api.py calls this on every request"]) == 0

    # A is still blocked, and B's objection is waiting in A's inbox.
    assert _write("aaaaaa", "src/auth.py")[0] == hooks.BLOCK
    pending = negotiate.drain("aaaaaa")
    assert [m["kind"] for m in pending] == ["objection"]
    assert "every request" in pending[0]["msg"]

    # A proposes a shim.
    assert cli.main(["--id", "aaaaaa", "reply", "--to", "bbbbbb",
                     "--kind", "offer", "--ref", "getUserFromSession",
                     "--msg", "I will leave a deprecated shim for one release"
                     ]) == 0

    # B receives it on its next tool call.
    code, out, _ = _write("bbbbbb", "src/api.py")
    assert code == hooks.ALLOW
    ctx = out
    assert "deprecated shim" in ctx and "aaaaaa" in ctx

    # B accepts.
    assert cli.main(["--id", "bbbbbb", "reply", "--to", "aaaaaa",
                     "--kind", "accept", "--ref", "getUserFromSession",
                     "--msg", "shim works for me"]) == 0

    # A's gate is now open, and the agreement is recorded.
    row = state.get_all()["aaaaaa"]
    assert row["conflicts"][0]["status"] == "resolved"
    assert row["resolved"][0]["agreement"] == "shim works for me"
    assert _write("aaaaaa", "src/auth.py")[0] == hooks.ALLOW


def test_progress_reaches_a_blocked_agent(repo):
    _declare("aaaaaa", goal="migrate auth", globs=["src/auth.py"],
             progress="verifyJwt(token) added; call sites now pass a raw token")
    _declare("bbbbbb", goal="also touch auth", globs=["src/auth.py"])
    code, _, err = _write("bbbbbb", "src/auth.py")
    assert code == hooks.BLOCK
    assert "raw token" in err          # the output half, not just the intent


def test_agents_in_separate_worktrees_share_files_but_not_interfaces(
        repo, worktree, monkeypatch):
    _declare("aaaaaa", goal="main tree work", globs=["src/auth.py"],
             removes=["getUserFromSession"])
    monkeypatch.chdir(worktree)
    _declare("bbbbbb", goal="worktree work", globs=["src/api.py"])
    monkeypatch.chdir(repo)
    _declare("aaaaaa", goal="main tree work", globs=["src/auth.py"],
             removes=["getUserFromSession"])

    kinds = [c["kind"] for c in state.get_all()["aaaaaa"]["conflicts"]]
    assert "INTERFACE_BREAK" in kinds       # crosses worktrees
    assert "FILE_OVERLAP" not in kinds      # does not


def test_a_crashed_agent_never_holds_the_repo_hostage(repo):
    import time
    _declare("aaaaaa", goal="migrate auth", globs=["src/auth.py"],
             removes=["getUserFromSession"])
    _declare("bbbbbb", goal="pagination", globs=["src/api.py"])
    _declare("aaaaaa", goal="migrate auth", globs=["src/auth.py"],
             removes=["getUserFromSession"])
    assert _write("aaaaaa", "src/auth.py")[0] == hooks.BLOCK

    # B's process dies.
    with state.locked():
        data = state.load()
        data["agents"]["bbbbbb"]["hb"] = time.time() - state.TTL_ABSENT - 1
        state.save(data)

    negotiate.check_timers()
    assert _write("aaaaaa", "src/auth.py")[0] == hooks.ALLOW
    notes = negotiate.drain("bbbbbb")
    assert any("proceeded" in m["msg"] for m in notes)
```

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_integration.py -v`
Expected: PASS. Where a test fails, fix the module, not the test — these four are the product's contract. The `pending == []` assertion in the first test tolerates either drain order; if the gate's drain-on-block behaviour changes, tighten it deliberately rather than loosening it.

- [ ] **Step 3: Run the whole suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: three-agent integration through real hook payloads

Covers the full remove/objection/offer/accept cycle, progress reaching a
blocked agent, the worktree split, and a crashed agent not blocking work."
```

---

### Task 12: README and LICENSE

**Files:**
- Create: `README.md`
- Create: `LICENSE`

**Interfaces:** none.

- [ ] **Step 1: Write the LICENSE**

Fetch the Apache-2.0 text verbatim:

```bash
curl -fsSL https://www.apache.org/licenses/LICENSE-2.0.txt -o LICENSE
head -3 LICENSE   # expect: "                                 Apache License"
```

- [ ] **Step 2: Write the README**

`README.md` must contain, in this order:

1. One-sentence pitch: coordination for 3-7 AI coding agents on one repo.
2. **The problem**, in the spec's terms: file collisions are the symptom; the expensive failure is Agent A removing what Agent B depends on, where both sessions look successful and the repo is broken.
3. **Install**: `pip install agentmesh` then `agentmesh init` (add `--local` to install for yourself only rather than everyone who clones).
4. **How it works**, four bullets: agents declare intent, a blocking `PreToolUse` hook is the gate, `git grep` finds who depends on what, agents negotiate and only deadlocks reach you.
5. **Worktrees**: state lives in `<git-common-dir>/agentmesh`, so agents in separate worktrees coordinate automatically. Files are isolated per worktree; interfaces are not.
6. A **Limitations** section, stated plainly — these are the spec's known ceilings and must not be softened:
   - Enforcement is strong-by-default, not airtight. The Bash scanner is regex over shell strings and misses writes inside invoked scripts and `python -c`. A determined agent can route around the gate; the degraded mode is advisory.
   - Reference detection is `git grep` with word boundaries, not an AST. False positives are suppressed by requiring an intersection with another live declaration, not eliminated.
   - **No security boundary.** Every agent runs as the same OS user with write access to the state directory, and could forge an `accept` from another id. `flock` protects against concurrent corruption, not against a misbehaving agent. Do not deploy agentmesh as a multi-tenant safety mechanism.
   - Symbol-level co-editing of one file is out of scope; claims are file-granular.
   - Duplicate-work detection is a heuristic warning, never a block.
7. **Commands**: the seven-line CLI block from the spec.
8. License line: Apache-2.0.

- [ ] **Step 3: Verify the install path end to end in a scratch repo**

```bash
cd "$(mktemp -d)" && git init -q && echo "x" > a.py && git add -A \
  && git -c user.email=t@e.com -c user.name=T commit -qm init \
  && agentmesh init \
  && agentmesh declare --goal "smoke test" --globs "a.py" \
  && agentmesh status
```

Expected: `init` reports the settings path, `declare` reports no conflicts, `status` lists one agent.

- [ ] **Step 4: Commit**

```bash
git add README.md LICENSE
git commit -m "docs: README and Apache-2.0 license

Limitations section states the enforcement ceiling and the absence of any
security boundary, so nobody deploys this as a multi-tenant guarantee."
```

---

## Done When

- `python -m pytest tests/ -v` is green, including the 20-process race test.
- The scratch-repo smoke sequence in Task 12 Step 3 succeeds.
- `git log --format='%B' | grep -i claude` returns nothing.
