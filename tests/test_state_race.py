import json
import subprocess
import sys

from agentmesh import state

WRITER = """
import sys
from agentmesh import state
state.upsert(sys.argv[1], goal="agent " + sys.argv[1])
"""


def test_twenty_concurrent_writers_lose_nothing(repo, tmp_path):
    script = tmp_path / "writer.py"
    script.write_text(WRITER)
    ids = [f"ag{i:04d}" for i in range(20)]
    procs = [
        subprocess.Popen([sys.executable, str(script), aid], cwd=repo)
        for aid in ids
    ]
    for p in procs:
        assert p.wait() == 0

    raw = (state.state_dir() / "declarations.json").read_text()
    json.loads(raw)                      # never corrupt
    assert set(state.get_all()) == set(ids)   # never a lost write
