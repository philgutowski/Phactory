"""Throwaway git repositories for tests.

The plan describes this file as lifted from native-relay's tests/_repo.py.
That repository is not in this checkout, so this is a fresh file written to
the behaviour U5 names: make_repo yields a working clone whose origin is a
bare repository on disk and whose origin/HEAD points at the default branch,
which is what a fresh clone of a real host repository looks like.

Every git call runs with the user's global and system config ignored and a
fixed identity, so a developer's own git settings never change a result.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BRANCH = "main"

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd: Path, *args: str) -> str:
    """Run git in cwd and return its stripped standard output."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class Repo:
    work: Path
    origin: Path
    default_branch: str

    def git(self, *args: str) -> str:
        return git(self.work, *args)

    def commit(self, path: str, content: str, message: str) -> str:
        """Write one file, commit it, and return the new commit id."""
        target = self.work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git("add", path)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


def make_repo(root: Path, default_branch: str = DEFAULT_BRANCH) -> Repo:
    """Create root/origin.git (bare) and root/work (a clone with one commit pushed)."""
    origin = root / "origin.git"
    work = root / "work"
    origin.mkdir(parents=True)
    git(origin, "init", "-q", "--bare", "-b", default_branch)
    work.mkdir()
    git(work, "init", "-q", "-b", default_branch)
    git(work, "remote", "add", "origin", str(origin))
    repo = Repo(work=work, origin=origin, default_branch=default_branch)
    repo.commit("README.md", "seed\n", "seed")
    git(work, "push", "-q", "-u", "origin", default_branch)
    git(work, "remote", "set-head", "origin", default_branch)
    return repo
