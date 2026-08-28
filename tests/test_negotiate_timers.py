import time

from interlock import detect, negotiate, state


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
