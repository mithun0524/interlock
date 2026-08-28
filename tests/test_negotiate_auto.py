"""Auto-objection and imperative block messages.

Both come from a live 3-agent run in which every agent, when blocked, chose
to ask its human instead of running `agentmesh reply` -- despite the message
naming that command. Zero messages were exchanged across the whole run.
"""

from agentmesh import cli, detect, hooks, negotiate, state


def _payload(sid="a3f9c1", path="src/auth.py"):
    return {"session_id": sid, "tool_name": "Edit",
            "tool_input": {"file_path": path}}


def _declared_pair(repo, monkeypatch):
    """B claims the file that calls getUserFromSession; A declares removing it."""
    state.upsert("b7d2e0", goal="add pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    assert cli.main(["declare", "--goal", "jwt migration",
                     "--globs", "src/auth.py",
                     "--removes", "getUserFromSession"]) == 0


def test_interface_break_queues_an_objection_without_the_other_agent_acting(
        repo, monkeypatch):
    """The dependency is a fact in the code, so the tool can state it. B never
    has to run a command for the negotiation to start."""
    _declared_pair(repo, monkeypatch)
    msgs = negotiate.drain("a3f9c1")
    assert [m["kind"] for m in msgs] == ["objection"]
    assert msgs[0]["from"] == "b7d2e0"
    assert msgs[0]["ref"] == "getUserFromSession"
    assert "src/api.py" in msgs[0]["msg"]


def test_the_auto_objection_marks_the_conflict_negotiating(repo, monkeypatch):
    _declared_pair(repo, monkeypatch)
    c = state.get_all()["a3f9c1"]["conflicts"][0]
    assert c["status"] == "negotiating"
    assert c["auto_objected"] is True


def test_it_is_not_sent_twice_when_declare_is_re_run(repo, monkeypatch):
    _declared_pair(repo, monkeypatch)
    negotiate.drain("a3f9c1")
    cli.main(["declare", "--goal", "jwt migration", "--globs", "src/auth.py",
              "--removes", "getUserFromSession"])
    assert negotiate.drain("a3f9c1") == []


def test_file_overlap_gets_no_auto_objection(repo, monkeypatch):
    """A shared file is a scheduling matter, not a stated dependency -- the
    tool must not manufacture an objection nobody felt."""
    state.upsert("b7d2e0", goal="patch auth", globs=["src/auth.py"],
                 worktree=state.worktree_root())
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    cli.main(["declare", "--goal", "also auth", "--globs", "src/auth.py"])
    kinds = {c["kind"] for c in state.get_all()["a3f9c1"]["conflicts"]}
    assert detect.FILE_OVERLAP in kinds
    assert negotiate.drain("a3f9c1") == []


def test_god_symbol_gets_no_auto_objection(repo, monkeypatch):
    """No counterparty to object on behalf of."""
    monkeypatch.setattr(state, "GOD_SYMBOL_REFS", 0)
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    cli.main(["declare", "--goal", "g", "--globs", "src/auth.py",
              "--removes", "getUserFromSession"])
    assert negotiate.drain("a3f9c1") == []


def test_conflict_block_gives_one_imperative_next_step(repo):
    """Not a menu. A menu is what made agents pick 'ask the human'."""
    state.upsert("b7d2e0", goal="pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    state.upsert("a3f9c1", goal="jwt", globs=["src/auth.py"],
                 worktree=state.worktree_root(),
                 conflicts=[detect.conflict(detect.INTERFACE_BREAK,
                                            "getUserFromSession", "b7d2e0",
                                            "referenced in src/api.py")])
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "Next step:" in msg
    assert msg.count("agentmesh reply") == 1     # exactly one command offered
    assert "ask" not in msg.lower()              # never suggest escalating


def test_claim_block_gives_one_imperative_next_step(repo):
    wt = state.worktree_root()
    state.upsert("a3f9c1", goal="mine", globs=["src/**"], worktree=wt)
    state.upsert("b7d2e0", goal="patch auth", globs=["src/auth.py"],
                 worktree=wt)
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "Next step:" in msg
    assert msg.count("agentmesh reply") == 1


def test_god_symbol_block_directs_to_the_human_verdict(repo):
    """The one case where involving the human IS the next step."""
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root(),
                 conflicts=[dict(detect.conflict(detect.GOD_SYMBOL, "widelyUsed",
                                                 "", "900 references"),
                                 status="escalated")])
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "agentmesh resolve" in msg
    assert "agentmesh reply" not in msg
