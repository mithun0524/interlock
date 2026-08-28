from interlock import detect, state


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


def test_a_passed_cache_memoises_within_one_call(repo):
    """One detect_all pass should not grep the same symbol twice."""
    cache = {}
    detect.refs_for_symbol("getUserFromSession", str(repo), cache)
    cache["getUserFromSession"] = ["sentinel.py"]
    assert detect.refs_for_symbol("getUserFromSession", str(repo), cache) == [
        "sentinel.py"
    ]


def test_nothing_is_cached_across_calls(repo):
    """Regression: the cache used to be persisted on disk keyed by HEAD. HEAD
    does not move during a session while files change constantly, so it went
    stale within seconds -- and a completion check missed a file created
    mid-session that still referenced a deleted symbol."""
    detect.refs_for_symbol("getUserFromSession", str(repo), None)
    (repo / "late.py").write_text("getUserFromSession(req)\n")
    hits = detect.refs_for_symbol("getUserFromSession", str(repo), None)
    assert "late.py" in hits


def test_no_cache_file_is_written(repo):
    detect.refs_for_symbol("getUserFromSession", str(repo))
    assert not (state.state_dir() / "refcache.json").exists()


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


def test_an_untracked_file_is_visible_to_detection(repo):
    """Regression: found by a live 3-agent run.

    An agent wrote docs/guide.md referencing getUserFromSession while
    another agent deleted it in the same session. Nothing fired, because
    git ls-files and git grep only see tracked content -- and a new file is
    exactly the kind that references what someone else is removing.
    """
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "guide.md").write_text(
        "Endpoints authenticate via getUserFromSession(req).\n"
    )
    hits = detect.refs_for_symbol("getUserFromSession", str(repo), {})
    assert "docs/guide.md" in hits


def test_an_ignored_file_stays_invisible(repo):
    """--untracked must not drag in build output or vendored trees."""
    (repo / ".gitignore").write_text("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "bundle.js").write_text("getUserFromSession()\n")
    hits = detect.refs_for_symbol("getUserFromSession", str(repo), {})
    assert not any(h.startswith("build/") for h in hits)


def test_untracked_dependent_file_raises_interface_break(repo):
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "guide.md").write_text("uses getUserFromSession(req)\n")
    me = _row(deltas=[{"kind": "remove", "symbol": "getUserFromSession"}],
              globs=["src/**"])
    them = {"ccc111": _row(globs=["docs/**"], goal="write docs")}
    got = detect.interface_conflicts(me, them, str(repo))
    assert [c["kind"] for c in got] == [detect.INTERFACE_BREAK]
    assert "docs/guide.md" in got[0]["detail"]
