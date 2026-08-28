import time

from interlock import detect, hooks, state


def _payload(sid="a3f9c1", tool="Edit", path="src/auth.py"):
    return {"session_id": sid, "cwd": ".", "tool_name": tool,
            "tool_input": {"file_path": path}}


def test_undeclared_agent_is_blocked_with_instructions(repo):
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "interlock declare" in msg


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
    assert "re-declare" in msg.lower()


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


def test_the_declare_hint_carries_the_agents_own_id(repo):
    """Regression: found by a live 3-agent run.

    Without its id in the hint, an agent invents one (`claude-main`), the
    gate keys off session_id[:6] (`a3f9c1`), the two never match, and the
    gate answers 'declare your intent' forever to an agent that already
    declared. Silent and total.
    """
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "--id a3f9c1" in msg


def test_the_redeclare_hint_also_carries_the_id(repo):
    state.upsert("a3f9c1", goal="g", globs=["docs/**"],
                 worktree=state.worktree_root())
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "--id a3f9c1" in msg


def test_session_start_hint_carries_the_id(repo):
    _, out, _ = hooks.run("SessionStart", {"session_id": "a3f9c1xxxx"})
    assert "--id a3f9c1" in out
