"""Completion checking, and telling a blocked agent the block is terminal.

Both come from a live run that ended with a BROKEN repo while every agent
exited 0. Agent A removed getUserFromSession, was blocked from the call sites
by B's claim, said "waiting on bbbbbb to release src/api.py ... will resume
automatically in ~2 min", and exited. `claude -p` is single-shot: there was no
resuming. B released the file afterwards. Both believed the other had it.

Without interlock A would simply have edited the file and left a consistent
repo, so the block caused the breakage the tool exists to prevent.
"""

import time

from interlock import cli, detect, hooks, state


def _agent_removing_the_symbol(claims):
    state.upsert("a3f9c1", goal="jwt migration", globs=claims,
                 deltas=[{"kind": "remove", "symbol": "getUserFromSession"}],
                 worktree=state.worktree_root())


def test_session_end_flags_a_removal_whose_call_sites_remain(repo):
    """src/api.py still calls it and a3f9c1 does not claim that file."""
    _agent_removing_the_symbol(["src/auth.py"])
    code, out, err = hooks.run("SessionEnd", {"session_id": "a3f9c1"})
    assert code == hooks.ALLOW
    assert "getUserFromSession" in err
    assert "src/api.py" in err
    assert "incomplete" in err.lower()


def test_the_incomplete_record_outlives_the_session(repo):
    """A done row is normally pruned at once. One carrying unfinished work
    must survive so `status` can show a human what was abandoned."""
    _agent_removing_the_symbol(["src/auth.py"])
    hooks.run("SessionEnd", {"session_id": "a3f9c1"})
    row = state.get_all().get("a3f9c1")
    assert row is not None
    assert row["state"] == "done"
    assert "getUserFromSession" in str(row["incomplete"])


def test_an_incomplete_row_holds_no_claims(repo):
    """It lingers for visibility only; it must not block anybody."""
    _agent_removing_the_symbol(["src/auth.py"])
    hooks.run("SessionEnd", {"session_id": "a3f9c1"})
    other = {"goal": "next agent", "globs": ["src/auth.py"], "deltas": [],
             "deps": [], "touched": [], "worktree": state.worktree_root(),
             "state": "active"}
    assert detect.file_conflicts(other, state.get_all(), str(repo),
                                 my_id="zzz999") == []


def test_a_removal_with_every_call_site_claimed_is_complete(repo):
    _agent_removing_the_symbol(["src/**"])
    _, _, err = hooks.run("SessionEnd", {"session_id": "a3f9c1"})
    assert err == ""
    assert "a3f9c1" not in state.get_all()          # pruned as usual


def test_an_agent_with_no_deltas_is_never_flagged(repo):
    state.upsert("a3f9c1", goal="tidy", globs=["src/**"],
                 worktree=state.worktree_root())
    _, _, err = hooks.run("SessionEnd", {"session_id": "a3f9c1"})
    assert err == ""


def test_incomplete_rows_are_pruned_once_stale(repo):
    _agent_removing_the_symbol(["src/auth.py"])
    hooks.run("SessionEnd", {"session_id": "a3f9c1"})
    now = time.time()
    with state.locked():
        data = state.load()
        data["agents"]["a3f9c1"]["hb"] = now - state.TTL_PRUNE - 1
        state.save(data)
    assert "a3f9c1" not in state.get_all()


def test_status_surfaces_abandoned_work(repo, capsys):
    _agent_removing_the_symbol(["src/auth.py"])
    hooks.run("SessionEnd", {"session_id": "a3f9c1"})
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out
    assert "getUserFromSession" in out


def test_a_claim_block_says_it_will_not_clear(repo):
    """A blocked agent must not treat the gate as a queue. It waited for a
    release that arrived after its own process exited."""
    wt = state.worktree_root()
    state.upsert("a3f9c1", goal="mine", globs=["src/**"], worktree=wt)
    state.upsert("b7d2e0", goal="patch auth", globs=["src/auth.py"],
                 worktree=wt)
    _, msg = hooks.gate({"session_id": "a3f9c1", "tool_name": "Edit",
                         "tool_input": {"file_path": "src/auth.py"}})
    assert "will not clear" in msg
    assert "do not wait" in msg.lower()


def test_the_gate_runs_the_timers_when_the_agent_is_blocked(repo):
    """Regression: check_timers was only ever called from `interlock status`,
    so the absent-counterparty release, the nudge, the escalation and the
    first-claim-wins fallback were all dead code in the hook path.

    Observed live: an agent waited for a counterparty that had exited, and
    nothing released it because nothing was watching.
    """
    from interlock import detect
    wt = state.worktree_root()
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "b7d2e0", "referenced in src/api.py")
    state.upsert("a3f9c1", goal="jwt", globs=["src/auth.py"], worktree=wt,
                 conflicts=[c])
    # counterparty exists but its heartbeat is long stale: it is absent
    state.upsert("b7d2e0", goal="gone", globs=["src/api.py"], worktree=wt)
    with state.locked():
        data = state.load()
        data["agents"]["b7d2e0"]["hb"] = time.time() - state.TTL_ABSENT - 1
        state.save(data)

    code, _ = hooks.gate({"session_id": "a3f9c1", "tool_name": "Edit",
                          "tool_input": {"file_path": "src/auth.py"}})
    assert code == hooks.ALLOW, "absent counterparty should have released it"
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] == "resolved"


def test_the_gate_does_not_run_the_timers_when_unblocked(repo):
    """Only pay the cost while blocked."""
    calls = []
    from interlock import negotiate
    real = negotiate.check_timers
    try:
        negotiate.check_timers = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        state.upsert("a3f9c1", goal="g", globs=["src/**"],
                     worktree=state.worktree_root())
        hooks.gate({"session_id": "a3f9c1", "tool_name": "Edit",
                    "tool_input": {"file_path": "src/auth.py"}})
    finally:
        negotiate.check_timers = real
    assert calls == []
