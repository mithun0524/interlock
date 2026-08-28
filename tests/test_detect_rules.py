import pytest

from interlock import detect, state


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


def test_two_agents_creating_the_same_new_file_collide(repo):
    me = _row(globs=["src/brand_new.py"], worktree="/repo")
    them = {"b7d2e0": _row(globs=["src/brand_new.py"], worktree="/repo")}
    got = detect.add_collisions(me, them, str(repo))
    assert [c["kind"] for c in got] == [detect.ADD_COLLISION]
    assert got[0]["ref"] == "src/brand_new.py"


def test_new_file_collision_does_not_cross_worktrees(repo):
    me = _row(globs=["src/brand_new.py"], worktree="/repo")
    them = {"b7d2e0": _row(globs=["src/brand_new.py"],
                           worktree="/elsewhere/wt-a")}
    assert detect.add_collisions(me, them, str(repo)) == []


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
