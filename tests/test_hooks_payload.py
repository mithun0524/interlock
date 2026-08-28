import pytest

from interlock import hooks

WRITES = [
    ("cat > src/auth.py <<'EOF'\nx\nEOF", ["src/auth.py"]),
    ("echo hi >> src/log.txt", ["src/log.txt"]),
    ("sed -i '' 's/a/b/' src/auth.py", ["src/auth.py"]),
    ("sed -i.bak 's/a/b/' src/auth.py", ["src/auth.py"]),
    ("tee src/out.txt < in.txt", ["src/out.txt"]),
    ("mv src/a.py src/b.py", ["src/a.py", "src/b.py"]),
    ("cp src/a.py src/b.py", ["src/b.py"]),
    ("rm -f src/gone.py", ["src/gone.py"]),
    ("patch -p1 < fix.diff", ["fix.diff"]),
    ("git apply fix.diff", ["fix.diff"]),
    ("git checkout -- src/auth.py", ["src/auth.py"]),
    ("git restore src/auth.py", ["src/auth.py"]),
    ("python3 -c 'x' && echo y > src/z.txt", ["src/z.txt"]),
]

READS = [
    "grep -rn getUserFromSession src/",
    "cat src/auth.py",
    "git status --short",
    "ls -la src/",
    "python -m pytest tests/ -v",
    "echo 'a > b'",
]


@pytest.mark.parametrize("cmd,expected", WRITES)
def test_write_shapes_are_detected(cmd, expected):
    assert sorted(hooks.targets_from_bash(cmd)) == sorted(expected)


@pytest.mark.parametrize("cmd", READS)
def test_reads_are_never_treated_as_writes(cmd):
    assert hooks.targets_from_bash(cmd) == []


def test_edit_tool_target():
    payload = {"tool_name": "Edit",
               "tool_input": {"file_path": "/repo/src/auth.py"}}
    assert hooks.payload_targets(payload) == ["/repo/src/auth.py"]


def test_write_tool_target():
    payload = {"tool_name": "Write",
               "tool_input": {"file_path": "/repo/src/new.py"}}
    assert hooks.payload_targets(payload) == ["/repo/src/new.py"]


def test_notebook_edit_target():
    payload = {"tool_name": "NotebookEdit",
               "tool_input": {"notebook_path": "/repo/nb.ipynb"}}
    assert hooks.payload_targets(payload) == ["/repo/nb.ipynb"]


def test_bash_tool_routes_through_the_scanner():
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "echo x > src/a.txt"}}
    assert hooks.payload_targets(payload) == ["src/a.txt"]


def test_unknown_tool_has_no_targets():
    assert hooks.payload_targets({"tool_name": "Grep",
                                  "tool_input": {"pattern": "x"}}) == []


def test_agent_id_is_the_session_prefix():
    assert hooks.agent_id({"session_id": "a3f9c1de-dead-beef"}) == "a3f9c1"
