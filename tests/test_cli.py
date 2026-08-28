import json

from agentmesh import cli, negotiate, state


def _run(*argv):
    return cli.main(list(argv))


def test_declare_creates_a_row(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    assert _run("declare", "--goal", "jwt migration",
                "--globs", "src/auth.py") == 0
    row = state.get_all()["a3f9c1"]
    assert row["goal"] == "jwt migration"
    assert row["globs"] == ["src/auth.py"]
    assert row["worktree"] == state.worktree_root()


def test_declare_accepts_multiple_globs_and_deltas(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    _run("declare", "--goal", "g", "--globs", "src/**", "docs/**",
         "--removes", "getUserFromSession", "--adds", "verifyJwt",
         "--deps", "createSession", "--progress", "half done")
    row = state.get_all()["a3f9c1"]
    assert row["globs"] == ["src/**", "docs/**"]
    assert {"kind": "remove", "symbol": "getUserFromSession"} in row["deltas"]
    assert {"kind": "add", "symbol": "verifyJwt"} in row["deltas"]
    assert row["deps"] == ["createSession"]
    assert row["progress"] == "half done"


def test_declare_is_idempotent_and_recomputes_conflicts(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    state.upsert("b7d2e0", goal="pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    _run("declare", "--goal", "g", "--globs", "src/auth.py",
         "--removes", "getUserFromSession")
    first = state.get_all()["a3f9c1"]["conflicts"]
    _run("declare", "--goal", "g", "--globs", "src/auth.py",
         "--removes", "getUserFromSession")
    second = state.get_all()["a3f9c1"]["conflicts"]
    assert len(first) == len(second) == 1


def test_declare_rejects_a_vague_symbol_with_a_useful_message(repo, capsys,
                                                              monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    rc = _run("declare", "--goal", "g", "--globs", "src/**",
              "--removes", "get")
    assert rc == 1
    assert "Qualify it as" in capsys.readouterr().err


def test_declare_reports_conflicts_on_stdout(repo, capsys, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    state.upsert("b7d2e0", goal="pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    _run("declare", "--goal", "g", "--globs", "src/auth.py",
         "--removes", "getUserFromSession")
    out = capsys.readouterr().out
    assert "INTERFACE_BREAK" in out and "b7d2e0" in out


def test_status_lists_the_roster(repo, capsys):
    state.upsert("b7d2e0", goal="pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())
    assert _run("status") == 0
    out = capsys.readouterr().out
    assert "b7d2e0" in out and "pagination" in out


def test_status_json_is_machine_readable(repo, capsys):
    state.upsert("b7d2e0", goal="pagination", worktree=state.worktree_root())
    _run("status", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["agents"]["b7d2e0"]["goal"] == "pagination"


def test_reply_delivers_a_message(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    wt = state.worktree_root()
    state.upsert("a3f9c1", goal="a", worktree=wt)
    state.upsert("b7d2e0", goal="b", worktree=wt)
    assert _run("reply", "--to", "b7d2e0", "--kind", "offer",
                "--ref", "getUserFromSession", "--msg", "shim?") == 0
    assert negotiate.drain("b7d2e0")[0]["msg"] == "shim?"


def test_reply_rejects_an_unknown_recipient(repo, capsys, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    state.upsert("a3f9c1", goal="a", worktree=state.worktree_root())
    rc = _run("reply", "--to", "nobody", "--kind", "offer", "--ref", "x",
              "--msg", "m")
    assert rc == 1
    assert "no active agent" in capsys.readouterr().err


def test_resolve_records_the_human_verdict(repo):
    from agentmesh import detect
    c = detect.conflict(detect.INTERFACE_BREAK, "getUserFromSession",
                        "b7d2e0", "d")
    state.upsert("a3f9c1", goal="a", conflicts=[c],
                 worktree=state.worktree_root())
    assert _run("resolve", "--ref", "getUserFromSession",
                "--decision", "keep a shim") == 0
    row = state.get_all()["a3f9c1"]
    assert row["conflicts"][0]["status"] == "resolved"
    assert row["resolved"][0]["agreement"] == "keep a shim"


def test_release_marks_the_agent_done(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    state.upsert("a3f9c1", goal="a", worktree=state.worktree_root())
    assert _run("release") == 0
    assert "a3f9c1" not in state.get_all()


def test_hook_blocks_an_undeclared_write(repo, capsys, monkeypatch):
    import io
    payload = json.dumps({"session_id": "a3f9c1", "tool_name": "Edit",
                          "tool_input": {"file_path": "src/auth.py"}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc = _run("hook", "--event", "PreToolUse")
    assert rc == 2
    assert "agentmesh declare" in capsys.readouterr().err


def test_hook_emits_additional_context_json_on_allow(repo, capsys,
                                                     monkeypatch):
    import io
    state.upsert("a3f9c1", goal="g", globs=["src/**"],
                 worktree=state.worktree_root())
    negotiate.send("b7d2e0", "a3f9c1", "objection", "getUserFromSession",
                   "I rely on this")
    payload = json.dumps({"session_id": "a3f9c1", "tool_name": "Edit",
                          "tool_input": {"file_path": "src/auth.py"}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc = _run("hook", "--event", "PreToolUse")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "I rely on this" in \
        out["hookSpecificOutput"]["additionalContext"]


def test_resolve_agent_id_prefers_explicit_over_env(repo, monkeypatch):
    monkeypatch.setenv("AGENTMESH_ID", "fromenv")
    assert cli.resolve_agent_id("abc123") == "abc123"


def test_resolve_agent_id_falls_back_to_the_only_active_agent(repo,
                                                              monkeypatch):
    monkeypatch.delenv("AGENTMESH_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    state.upsert("solo01", goal="g", worktree=state.worktree_root())
    assert cli.resolve_agent_id() == "solo01"


def test_resolve_agent_id_reads_the_env_var_claude_code_actually_exports(
        repo, monkeypatch):
    """Regression: the code originally looked for CLAUDE_SESSION_ID, which
    Claude Code does not export. The real name is CLAUDE_CODE_SESSION_ID."""
    monkeypatch.delenv("AGENTMESH_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "a3f9c1de-dead-beef")
    assert cli.resolve_agent_id() == "a3f9c1"


def test_declare_writes_state_exactly_once(repo, monkeypatch):
    """Regression: found by a live 3-agent run.

    declare used to upsert fields, then upsert conflicts. Between those two
    writes the declaration was live advertising no conflicts, so another
    agent's gate could read it and sail through.
    """
    monkeypatch.setenv("AGENTMESH_ID", "a3f9c1")
    state.upsert("b7d2e0", goal="pagination", globs=["src/api.py"],
                 worktree=state.worktree_root())

    real_upsert = state.upsert
    calls = []

    def counting_upsert(agent_id, **fields):
        if agent_id == "a3f9c1":
            calls.append(fields)
        return real_upsert(agent_id, **fields)

    monkeypatch.setattr(state, "upsert", counting_upsert)
    monkeypatch.setattr(cli.state, "upsert", counting_upsert)
    cli.main(["declare", "--goal", "g", "--globs", "src/auth.py",
              "--removes", "getUserFromSession"])

    assert len(calls) == 1, f"declare wrote {len(calls)} times"
    assert "conflicts" in calls[0] and "globs" in calls[0]
    # and the single write carries the detected conflict
    assert calls[0]["conflicts"][0]["kind"] == "INTERFACE_BREAK"
