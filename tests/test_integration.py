"""Three agents, one repo, driven through real hook payloads."""

from agentmesh import cli, hooks, negotiate, state


def _declare(agent_id, **kw):
    argv = ["--id", agent_id, "declare", "--goal", kw.pop("goal")]
    for flag, key in (("--globs", "globs"), ("--removes", "removes"),
                      ("--adds", "adds"), ("--deps", "deps")):
        if key in kw:
            argv += [flag, *kw.pop(key)]
    if "progress" in kw:
        argv += ["--progress", kw.pop("progress")]
    assert cli.main(argv) == 0


def _write(agent_id, path):
    return hooks.run("PreToolUse", {
        "session_id": agent_id, "tool_name": "Edit",
        "tool_input": {"file_path": path},
    })


def test_full_remove_objection_accept_cycle(repo):
    # A is migrating auth and will remove a symbol.
    _declare("aaaaaa", goal="migrate auth to JWT", globs=["src/auth.py"],
             removes=["getUserFromSession"], adds=["verifyJwt"])
    # B is working the file that calls it.
    _declare("bbbbbb", goal="add pagination", globs=["src/api.py"])
    # C is elsewhere entirely and must never be involved.
    _declare("cccccc", goal="write docs", globs=["docs/**"])

    # A re-declares now that B is present, so detection sees the contention.
    _declare("aaaaaa", goal="migrate auth to JWT", globs=["src/auth.py"],
             removes=["getUserFromSession"], adds=["verifyJwt"])

    conflicts = state.get_all()["aaaaaa"]["conflicts"]
    assert [c["kind"] for c in conflicts] == ["INTERFACE_BREAK"]
    assert conflicts[0]["with"] == "bbbbbb"

    # A is blocked at its next write.
    code, _, err = _write("aaaaaa", "src/auth.py")
    assert code == hooks.BLOCK
    assert "INTERFACE_BREAK" in err and "bbbbbb" in err

    # C is unaffected — docs/guide.md does not exist yet, which is exactly
    # why the gate must match globs by pattern and not only tracked files.
    assert _write("cccccc", "docs/guide.md")[0] == hooks.ALLOW

    # B objects.
    assert cli.main(["--id", "bbbbbb", "reply", "--to", "aaaaaa",
                     "--kind", "objection", "--ref", "getUserFromSession",
                     "--msg", "src/api.py calls this on every request"]) == 0

    # A is still blocked, and B's objection is waiting in A's inbox.
    assert _write("aaaaaa", "src/auth.py")[0] == hooks.BLOCK
    pending = negotiate.drain("aaaaaa")
    assert [m["kind"] for m in pending] == ["objection"]
    assert "every request" in pending[0]["msg"]

    # A proposes a shim.
    assert cli.main(["--id", "aaaaaa", "reply", "--to", "bbbbbb",
                     "--kind", "offer", "--ref", "getUserFromSession",
                     "--msg", "I will leave a deprecated shim for one release"
                     ]) == 0

    # B receives it on its next tool call.
    code, out, _ = _write("bbbbbb", "src/api.py")
    assert code == hooks.ALLOW
    assert "deprecated shim" in out and "aaaaaa" in out

    # B accepts.
    assert cli.main(["--id", "bbbbbb", "reply", "--to", "aaaaaa",
                     "--kind", "accept", "--ref", "getUserFromSession",
                     "--msg", "shim works for me"]) == 0

    # A's gate is now open, and the agreement is recorded.
    row = state.get_all()["aaaaaa"]
    assert row["conflicts"][0]["status"] == "resolved"
    assert row["resolved"][0]["agreement"] == "shim works for me"
    assert _write("aaaaaa", "src/auth.py")[0] == hooks.ALLOW


def test_progress_reaches_a_blocked_agent(repo):
    _declare("aaaaaa", goal="migrate auth", globs=["src/auth.py"],
             progress="verifyJwt(token) added; call sites now pass a raw token")
    _declare("bbbbbb", goal="also touch auth", globs=["src/auth.py"])
    code, _, err = _write("bbbbbb", "src/auth.py")
    assert code == hooks.BLOCK
    assert "raw token" in err          # the output half, not just the intent


def test_agents_in_separate_worktrees_share_files_but_not_interfaces(
        repo, worktree, monkeypatch):
    _declare("aaaaaa", goal="main tree work", globs=["src/auth.py"],
             removes=["getUserFromSession"])
    monkeypatch.chdir(worktree)
    _declare("bbbbbb", goal="worktree work", globs=["src/api.py"])
    monkeypatch.chdir(repo)
    _declare("aaaaaa", goal="main tree work", globs=["src/auth.py"],
             removes=["getUserFromSession"])

    kinds = [c["kind"] for c in state.get_all()["aaaaaa"]["conflicts"]]
    assert "INTERFACE_BREAK" in kinds       # crosses worktrees
    assert "FILE_OVERLAP" not in kinds      # does not


def test_a_crashed_agent_never_holds_the_repo_hostage(repo):
    import time
    _declare("aaaaaa", goal="migrate auth", globs=["src/auth.py"],
             removes=["getUserFromSession"])
    _declare("bbbbbb", goal="pagination", globs=["src/api.py"])
    _declare("aaaaaa", goal="migrate auth", globs=["src/auth.py"],
             removes=["getUserFromSession"])
    assert _write("aaaaaa", "src/auth.py")[0] == hooks.BLOCK

    # B's process dies.
    with state.locked():
        data = state.load()
        data["agents"]["bbbbbb"]["hb"] = time.time() - state.TTL_ABSENT - 1
        state.save(data)

    negotiate.check_timers()
    assert _write("aaaaaa", "src/auth.py")[0] == hooks.ALLOW
    notes = negotiate.drain("bbbbbb")
    assert any("proceeded" in m["msg"] for m in notes)
