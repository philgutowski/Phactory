"""The git calls verify needs, read only toward the project (KTD8).

Trimmed from native-relay's skills/relay/scripts/relay/gitread.py. That
repository is not in this checkout, so the lifted names (run, GitError,
rev_parse, fetch, log_oneline, log_messages, default_branch) are rewritten to
the behaviour the plan names. tree_of, is_ancestor, merge_tree, and the cache
helpers are new Slipway code.

Nothing here writes to the project's remote. The only writes are to
Slipway's own bare cache: fetched objects, branch refs mirrored from the
remote, and the held refs under HELD_REF_PREFIX that keep a confirmed head
alive after the host deletes its branch.

A tree is git's snapshot of every file at a commit, identified by its own
hash, so two commits with different hashes can carry the same tree. That is
what verify compares on the auto merge path.

Slipway executes no project code (KTD18). Every repository this module
touches is a bare cache under SLIPWAY_HOME: a bare repository has no
working checkout, so no filter, hook, or attribute from the project ever
runs, and git is started with hooks pointed at nothing for good measure.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from slipway.redact import scrub

# Every git call is bounded by this many seconds; a fetch gets longer.
GIT_TIMEOUT_SECONDS = 60
FETCH_TIMEOUT_SECONDS = 300

# Arguments every call carries: no hooks, and never a credential prompt.
_SAFE = ("-c", "core.hooksPath=" + os.devnull)
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}

_HEX = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

# A namespace in the cache that mirrors nothing on the remote, so a fetch
# with prune never deletes what lives under it.
HELD_REF_PREFIX = "refs/slipway/held/"


@dataclass
class GitError(Exception):
    """A git call failed. Messages are scrubbed, since a remote URL may hold a token."""

    command: tuple[str, ...]
    returncode: int | None
    stderr: str

    def __str__(self) -> str:
        code = "timed out" if self.returncode is None else f"exited {self.returncode}"
        return scrub(f"git {' '.join(self.command)} {code}: {self.stderr.strip()}")


def run(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> str:
    """Run one git command in repo and return its standard output."""
    try:
        proc = subprocess.run(
            ["git", *_SAFE, *args], cwd=repo, env={**os.environ, **_ENV},
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise GitError(tuple(args), None, f"no answer in {timeout} seconds") from None
    except OSError as exc:
        raise GitError(tuple(args), None, str(exc)) from None
    if proc.returncode != 0:
        raise GitError(tuple(args), proc.returncode, proc.stderr)
    return proc.stdout


def rev_parse(repo: Path, ref: str) -> str:
    """The commit id ref names; GitError when it names no commit."""
    return run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()


def tree_of(repo: Path, commit: str) -> str:
    """The tree id of commit."""
    return run(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}").strip()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """Whether ancestor is reachable from descendant (a commit is its own ancestor)."""
    try:
        run(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    except GitError as exc:
        if exc.returncode == 1:
            return False
        raise
    return True


def merge_tree(repo: Path, ours: str, theirs: str) -> str | None:
    """The tree git builds by merging theirs into ours, or None on a conflict.

    This computes the merge in the object store alone, with no checkout.
    """
    try:
        out = run(repo, "merge-tree", "--write-tree", "--no-messages", ours, theirs)
    except GitError as exc:
        if exc.returncode == 1:
            return None
        raise
    return out.split("\n", 1)[0].strip()


def first_parent(repo: Path, commit: str) -> str | None:
    """The first parent of commit, or None for a root commit."""
    parents = run(repo, "rev-list", "--parents", "-n", "1", commit).split()
    return parents[1] if len(parents) > 1 else None


def log_oneline(repo: Path, rev_range: str) -> list[tuple[str, str]]:
    """(commit, subject) for every commit in rev_range, newest first."""
    out = run(repo, "log", "--format=%H%x09%s", rev_range)
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if line]


def log_messages(repo: Path, rev_range: str) -> list[tuple[str, str]]:
    """(commit, full message) for every commit in rev_range, newest first."""
    out = run(repo, "log", "-z", "--format=%H%x09%B", rev_range)
    found = []
    for record in out.split("\0"):
        record = record.strip("\n")
        if record:
            commit, _, message = record.partition("\t")
            found.append((commit, message.strip()))
    return found


def default_branch(repo: Path) -> str:
    """The branch the remote calls its default.

    A bare cache made by clone points HEAD at the remote's default branch; a
    working clone records it as origin/HEAD. Either answers.
    """
    for args in (("symbolic-ref", "--short", "refs/remotes/origin/HEAD"),
                 ("symbolic-ref", "--short", "HEAD")):
        try:
            name = run(repo, *args).strip()
        except GitError:
            continue
        return name.removeprefix("origin/")
    raise GitError(("symbolic-ref",), 1, "no default branch is recorded")


def fetch(repo: Path, remote: str, *refspecs: str, prune: bool = False) -> None:
    """Fetch refspecs (branch refspecs, remote ref names, or bare commit ids) into repo.

    With prune, a local ref that a refspec's destination pattern covers and
    the remote no longer has is deleted, so branches deleted on merge do not
    pile up in the cache.
    """
    run(repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
        *(("--prune",) if prune else ()), remote, *refspecs, timeout=FETCH_TIMEOUT_SECONDS)


def cache_path(home: Path, project: str) -> Path:
    """Where the bare cache for one project lives under SLIPWAY_HOME."""
    return Path(home) / "cache" / "git" / f"{project}.git"


def refresh(cache: Path, remote: str, commits=(), refs=()) -> str:
    """Bring a bare cache up to date with remote and return its default branch.

    The first call clones bare. Later calls fetch every branch with prune, so
    a branch the host deleted leaves the cache too. A commit in commits not
    yet present, such as a confirmed head whose branch was deleted on merge,
    is looked for in two more places, in this order:

    1. refs: remote ref names the host still advertises for the pull request
       after its branch is gone. The ref layout belongs to the host, so the
       caller passes it in from a host adapter; this module names none. They
       are fetched for their objects only and stored under no local ref.
    2. The commit's bare id. A host that refuses an id it does not advertise
       fails this fetch.

    A commit still missing after both raises GitError, which the caller
    records as git_unreadable. hold, called at confirm time, avoids needing
    either: it pins the head while its branch still exists.
    """
    cache = Path(cache)
    if not (cache / "HEAD").is_file():
        cache.parent.mkdir(parents=True, exist_ok=True)
        run(cache.parent, "clone", "--quiet", "--bare", "--no-tags", remote, cache.name,
            timeout=FETCH_TIMEOUT_SECONDS)
    else:
        fetch(cache, remote, "+refs/heads/*:refs/heads/*", prune=True)
    missing = _missing(cache, commits)
    if missing and refs:
        try:
            fetch(cache, remote, *refs)
        except GitError:
            pass  # the id fetch below is the last word, and raises if it fails too
        missing = _missing(cache, missing)
    if missing:
        fetch(cache, remote, *missing)
    return default_branch(cache)


def hold(cache: Path, remote: str, branch: str, commit: str) -> str:
    """Refresh the cache at confirm time, pin commit there, and return the held ref.

    The workflow calls this when it confirms a branch head, while the branch
    still exists on the remote. The refresh fetches that branch with every
    other, so commit arrives as an advertised object, which every host
    serves. A ref under HELD_REF_PREFIX then keeps it in the cache after the
    host deletes the branch on merge: refresh's prune only touches
    refs/heads/, and garbage collection never drops a commit a ref points
    at. A later refresh finds it present and fetches nothing by id.

    Holding the same branch again moves the ref to the new head. release
    drops it once the card is finished.
    """
    refresh(cache, remote)
    if not _present(cache, commit):
        raise GitError(("hold", branch), 1, f"{commit} is not on the remote after a refresh")
    ref = HELD_REF_PREFIX + branch
    run(cache, "update-ref", ref, commit)
    return ref


def release(cache: Path, branch: str) -> None:
    """Drop the held ref for branch; a branch never held is not an error."""
    ref = HELD_REF_PREFIX + branch
    try:
        run(cache, "rev-parse", "--verify", "--quiet", ref)
    except GitError:
        return
    run(cache, "update-ref", "-d", ref)


def _missing(repo: Path, commits) -> list[str]:
    return [c for c in commits if c and not _present(repo, c)]


def _present(repo: Path, commit: str) -> bool:
    if not _HEX.fullmatch(commit):
        return False
    try:
        run(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    except GitError:
        return False
    return True
