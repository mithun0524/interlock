import subprocess
import pytest


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo with a couple of source files and one commit."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.py").write_text(
        "def getUserFromSession(req):\n    return req.session\n"
    )
    (root / "src" / "api.py").write_text(
        "from .auth import getUserFromSession\n\n"
        "def me(req):\n    return getUserFromSession(req)\n"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def worktree(repo, tmp_path):
    """A linked worktree of `repo`, sharing its git common dir."""
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    return wt
