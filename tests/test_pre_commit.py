"""tools/pre-commit takes the picture again on every fifth commit and never stops a commit."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "tools" / "pre-commit"

pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("git") or not shutil.which("sh"),
                                reason="the hook is a shell script that git runs")

# stands in for tools/screenshot.py: a different picture every time it runs
TAKES = "import sys, time\nopen(sys.argv[sys.argv.index('--out') + 1], 'w').write(f'picture {time.time_ns()}')\n"
FAILS = "import sys\nsys.exit('screenshot: no fonts')\n"


def git(repo: Path, *words: str) -> str:
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    # no hooks, signing or identity from the machine the tests run on
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    done = subprocess.run(["git", *words], cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def repo_with_hook(repo: Path, tool: str) -> Path:
    git(repo, "init", "-q", "-b", "main", ".")
    (repo / "tools").mkdir()
    (repo / "tools" / "screenshot.py").write_text(tool)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (repo / ".gitignore").write_text(".venv/\n")
    (repo / "screenshot.png").write_text("the first picture")
    shutil.copy(HOOK, repo / ".git" / "hooks" / "pre-commit")
    return repo


def commit(repo: Path, number: int, *how: str) -> bool:
    """Makes one commit and says whether it changed the picture."""
    (repo / "page.txt").write_text(f"the page at commit {number}")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"commit {number}", *how)
    return "screenshot.png" in git(repo, "show", "--name-only", "--format=", "HEAD").split()


def test_every_fifth_commit_takes_the_picture_along(tmp_path: Path):
    repo = repo_with_hook(tmp_path, TAKES)
    changed = [number for number in range(1, 12) if commit(repo, number)]
    assert changed == [1, 5, 10]  # the first commit adds the picture the repo starts with
    assert git(repo, "status", "--porcelain") == ""


def test_a_picture_that_cannot_be_taken_does_not_stop_the_commit(tmp_path: Path):
    repo = repo_with_hook(tmp_path, FAILS)
    for number in range(1, 6):
        commit(repo, number)
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "5"
    assert (repo / "screenshot.png").read_text() == "the first picture"
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / ".git" / "screenshot-new.png").exists()


def test_a_commit_of_named_files_leaves_the_picture_alone(tmp_path: Path):
    repo = repo_with_hook(tmp_path, TAKES)
    for number in range(1, 5):
        commit(repo, number)
    (repo / "page.txt").write_text("the page at commit 5")
    git(repo, "commit", "-q", "-m", "commit 5", "page.txt")
    assert (repo / "screenshot.png").read_text() == "the first picture"
    assert git(repo, "status", "--porcelain") == ""
