"""Auto-objection and imperative block messages.

Both come from a live 3-agent run in which every agent, when blocked, chose
to ask its human instead of running `interlock reply` -- despite the message
naming that command. Zero messages were exchanged across the whole run.
"""

from interlock import cli, detect, hooks, negotiate, state


def _payload(sid="a3f9c1", path="src/auth.py"):
    return {"session_id": sid, "tool_name": "Edit",
            "tool_input": {"file_path": path}}


def _declared_pair(repo, monkeypatch):
    """B claims the file that calls getUserFromSession; A declares removing it."""
    state.upsert("b7d2e0", goal="add pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    monkeypatch.setenv("INTERLOCK_ID", "a3f9c1")
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
    monkeypatch.setenv("INTERLOCK_ID", "a3f9c1")
    cli.main(["declare", "--goal", "also auth", "--globs", "src/auth.py"])
    kinds = {c["kind"] for c in state.get_all()["a3f9c1"]["conflicts"]}
    assert detect.FILE_OVERLAP in kinds
    assert negotiate.drain("a3f9c1") == []


def test_god_symbol_gets_no_auto_objection(repo, monkeypatch):
    """No counterparty to object on behalf of."""
    monkeypatch.setattr(state, "GOD_SYMBOL_REFS", 0)
    monkeypatch.setenv("INTERLOCK_ID", "a3f9c1")
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
    assert msg.count("interlock reply") == 1     # exactly one command offered
    assert "ask" not in msg.lower()              # never suggest escalating


def test_claim_block_gives_one_imperative_next_step(repo):
    wt = state.worktree_root()
    state.upsert("a3f9c1", goal="mine", globs=["src/**"], worktree=wt)
    state.upsert("b7d2e0", goal="patch auth", globs=["src/auth.py"],
                 worktree=wt)
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "Next step:" in msg
    assert msg.count("interlock reply") == 1


def test_god_symbol_block_directs_to_the_human_verdict(repo):
    """The one case where involving the human IS the next step."""
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root(),
                 conflicts=[dict(detect.conflict(detect.GOD_SYMBOL, "widelyUsed",
                                                 "", "900 references"),
                                 status="escalated")])
    code, msg = hooks.gate(_payload())
    assert code == hooks.BLOCK
    assert "interlock resolve" in msg
    assert "interlock reply" not in msg


def test_resolve_refuses_when_invoked_from_inside_an_agent(repo, capsys,
                                                           monkeypatch):
    """Found live: an agent ran `interlock resolve` to unblock itself, which
    skips the negotiation entirely -- a bigger bypass than the forged accept
    that IS guarded. A guardrail, not security: the trust model has no
    boundary between agents, but this closes the path an agent will take."""
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "b7d2e0", "d")
    state.upsert("a3f9c1", goal="a", conflicts=[c],
                 worktree=state.worktree_root())
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("INTERLOCK_ALLOW_AGENT_RESOLVE", raising=False)

    rc = cli.main(["resolve", "--ref", "getUserFromSession",
                   "--decision", "I unblock myself"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "reply --kind offer" in err
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] != "resolved"


def test_resolve_works_for_a_human_shell(repo, monkeypatch):
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "b7d2e0", "d")
    state.upsert("a3f9c1", goal="a", conflicts=[c],
                 worktree=state.worktree_root())
    for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["resolve", "--ref", "getUserFromSession",
                     "--decision", "keep a shim"]) == 0
    assert state.get_all()["a3f9c1"]["conflicts"][0]["status"] == "resolved"


def test_the_escape_hatch_allows_resolve_in_automation(repo, monkeypatch):
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "b7d2e0", "d")
    state.upsert("a3f9c1", goal="a", conflicts=[c],
                 worktree=state.worktree_root())
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("INTERLOCK_ALLOW_AGENT_RESOLVE", "1")
    assert cli.main(["resolve", "--ref", "getUserFromSession",
                     "--decision", "CI decided"]) == 0


def test_the_gate_never_suggests_resolve_for_a_negotiable_conflict(repo):
    """The hint must not teach agents the bypass."""
    state.upsert("b7d2e0", goal="pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    state.upsert("a3f9c1", goal="jwt", globs=["src/auth.py"],
                 worktree=state.worktree_root(),
                 conflicts=[detect.conflict(detect.INTERFACE_BREAK,
                                            "getUserFromSession", "b7d2e0",
                                            "referenced in src/api.py")])
    _, msg = hooks.gate(_payload())
    assert "interlock resolve" not in msg


def test_the_offer_prompt_defines_accept_as_unconditional(repo):
    """Found live: an agent accepted with 'go ahead once I confirm tests pass
    -- will ping shortly'. accept is binary in the model, so that was recorded
    as unconditional, the gate opened, and the ping never came.

    accept stays binary. The prompt now says so, and routes a precondition to
    refuse -- where it keeps the gate shut, which is the safe direction."""
    msgs = [{"from": "b7d2e0", "to": "a3f9c1", "kind": "offer",
             "ref": "getUserFromSession", "msg": "I will leave a shim"}]
    text = negotiate.format_for_agent(msgs)
    assert "no preconditions" in text
    assert "--kind refuse" in text


def test_the_objection_prompt_is_unchanged_by_that(repo):
    msgs = [{"from": "b7d2e0", "to": "a3f9c1", "kind": "objection",
             "ref": "getUserFromSession", "msg": "I rely on this"}]
    text = negotiate.format_for_agent(msgs)
    assert "--kind offer" in text
    assert "no preconditions" not in text


def test_reply_help_states_what_accept_means():
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            cli.build_parser().parse_args(["reply", "--help"])
        except SystemExit:
            pass
    help_text = buf.getvalue()
    assert "unconditional" in help_text.lower()
