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
