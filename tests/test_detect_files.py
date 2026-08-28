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
