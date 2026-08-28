import json

from agentmesh import install


def test_hook_config_registers_exactly_four_events():
    cfg = install.hook_config()
    assert set(cfg["hooks"]) == set(install.HOOK_EVENTS)


def test_stop_is_never_registered():
    """Stop fires per response, not per session."""
    assert "Stop" not in install.hook_config()["hooks"]


def test_pretooluse_matches_bash_as_well_as_edit():
    entry = install.hook_config()["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == install.MATCHER
    assert "Bash" in entry["matcher"]


def test_install_writes_project_settings_by_default(repo):
    path = install.install(str(repo))
    assert path.endswith(".claude/settings.json")
    data = json.loads(open(path).read())
    assert "PreToolUse" in data["hooks"]


def test_install_local_writes_settings_local(repo):
    path = install.install(str(repo), local=True)
    assert path.endswith(".claude/settings.local.json")


def test_install_is_idempotent(repo):
    install.install(str(repo))
    path = install.install(str(repo))
    data = json.loads(open(path).read())
    assert len(data["hooks"]["PreToolUse"]) == 1


def test_install_preserves_unrelated_hooks(repo):
    p = repo / ".claude"
    p.mkdir()
    (p / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "my-own-linter"}]}]},
        "permissions": {"allow": ["Bash(ls:*)"]},
    }))
    path = install.install(str(repo))
    data = json.loads(open(path).read())
    cmds = [h["command"] for e in data["hooks"]["PreToolUse"]
            for h in e["hooks"]]
    assert "my-own-linter" in cmds
    assert any("agentmesh" in c for c in cmds)
    assert data["permissions"]["allow"] == ["Bash(ls:*)"]


def test_install_does_not_touch_gitignore(repo):
    """State lives inside .git, so there is nothing to ignore."""
    install.install(str(repo))
    assert not (repo / ".gitignore").exists()
