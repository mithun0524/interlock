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
