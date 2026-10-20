"""Verify landed, partial landing, and halt, on real git in temporary repositories.

Each test builds a work clone and its bare origin with make_repo, plays the
host's part by merging on main the way the landing path would, pushes, and
then refreshes a bare cache from the origin, exactly as the workflow
refreshes a cache from the host. No test reads a real remote; a remote is
always a path on disk.

The cache reaches the origin through a file:// URL, not a bare path. Given a
bare path, git clone copies the origin's whole object store, unreachable
commits included, so a deleted branch's head would already be in the cache
and no fetch by id would ever be tried. A file:// URL makes git talk to the
origin the way it talks to a host, sending only what the origin serves.
"""

import os
from pathlib import Path

import pytest

from _nonet import NetworkReached
from _repo import git, make_repo
from slipway import gitread, verify
from slipway.halts import COMMIT_MISMATCH, GIT_UNREADABLE, MERGED_CARD_NOT_MOVED
from slipway.trackers.base import TRACKER_UNREADABLE, TrackerStatus, skipped

DONE = TrackerStatus("Done", True)
IN_PROGRESS = TrackerStatus("In progress", False)
CARD = "13"
BRANCH = "slipway/u13-demo"
PULL_REF = "refs/pull/7/head"  # what a GitHub shaped host adapter would pass in


def refuse_unadvertised_ids(origin: Path, monkeypatch) -> None:
    """Make origin serve only the commits its refs advertise, as some hosts do.

    The three uploadpack settings are git's switches for serving a commit by
    id that no ref advertises, reachable or not. git's protocol version 2
    does not enforce them, so every git call in the test is also pinned to
    version 0 through git's environment config, and the origin then answers
    "Server does not allow request for unadvertised object".
    """
    for key in ("allowTipSHA1InWant", "allowReachableSHA1InWant", "allowAnySHA1InWant"):
        git(origin, "config", f"uploadpack.{key}", "false")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.version")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "0")


class Landing:
    """One card's branch and the commits verify is handed after it merges."""

    def __init__(self, tmp_path: Path, behind: bool = False):
        self.repo = make_repo(tmp_path / "host")
        self.remote = self.repo.origin.as_uri()
        self.cache = gitread.cache_path(tmp_path / "home", "demo")
        self.baseline = self.repo.git("rev-parse", "HEAD")
        r = self.repo
        r.git("checkout", "-q", "-b", BRANCH)
        r.commit("src/a.py", "a = 1\n", "add a")
        self.confirmed = r.commit("src/b.py", "b = 2\n", "add b")
        r.git("push", "-q", "origin", BRANCH)
        r.git("checkout", "-q", "main")
        if behind:
            r.commit("other.txt", "someone else landed\n", "sibling card")
        self.tested = None
        self.tested_head = None

    def land(self, method: str) -> str:
        """Merge the branch on main by method, push main, delete the branch."""
        r = self.repo
        if method == "merge":
            r.git("merge", "-q", "--no-ff", "-m", "Merge pull request #7", BRANCH)
        elif method == "squash":
            r.git("merge", "-q", "--squash", BRANCH)
            r.git("commit", "-q", "-m", "U13 demo (#7)")
        elif method == "rebase":
            # The host replays the commits with itself as committer, so the
            # replayed commits get new ids even when nothing landed meanwhile.
            os.environ["GIT_COMMITTER_DATE"] = "2001-01-01T00:00:00Z"
            try:
                r.git("cherry-pick", f"main..{BRANCH}")
            finally:
                del os.environ["GIT_COMMITTER_DATE"]
        else:
            raise AssertionError(method)
        return self._push()

    def queue(self, head: str | None = None) -> str:
        """Build the queue's tested commit from main plus the head, land it."""
        r = self.repo
        self.tested_head = head or self.confirmed
        r.git("checkout", "-q", "-b", "queue/main-pr-7", "main")
        r.git("merge", "-q", "--no-ff", "-m", "Merge pull request #7", self.tested_head)
        self.tested = r.git("rev-parse", "HEAD")
        r.git("checkout", "-q", "main")
        r.git("merge", "-q", "--ff-only", self.tested)
        return self._push()

    def _push(self) -> str:
        r = self.repo
        r.git("push", "-q", "origin", "main")
        r.git("push", "-q", "origin", f":{BRANCH}")
        return r.git("rev-parse", "main")

    def hold(self) -> str:
        """What the workflow does at confirm time, while the branch still exists."""
        return gitread.hold(self.cache, self.remote, BRANCH, self.confirmed)

    def verify(self, merged, status=DONE, path=verify.AUTO_MERGE_STRICT, refs=(), **kw):
        gitread.refresh(self.cache, self.remote, commits=(self.confirmed,), refs=refs)
        kw.setdefault("tested", self.tested)
        kw.setdefault("tested_head", self.tested_head)
        tested = kw.pop("tested")
        return verify.verify(self.cache, self.baseline, self.confirmed, tested, merged,
                             CARD, status, path, **kw)


# Covers AE2.
def test_merged_on_main_card_still_in_progress_is_partial_landing(tmp_path):
    card = Landing(tmp_path)
    merged = card.land("squash")
    v = card.verify(merged, status=IN_PROGRESS)
    assert v.outcome == verify.PARTIAL_LANDING
    assert v.halt_class == MERGED_CARD_NOT_MOVED
    assert v.halt.is_wait
    assert v.check("new_commit_since_baseline").result == verify.PASS
    assert v.check("card_terminal").result == verify.FAIL
    assert "In progress" in v.halt.line()


def test_queue_path_landed_without_a_commit_naming_the_key(tmp_path):
    card = Landing(tmp_path)
    merged = card.queue()
    v = card.verify(merged, path=verify.MERGE_QUEUE)
    assert v.outcome == verify.LANDED and v.landed
    assert v.halt is None
    assert v.evidence["hand_landing"] is None
    assert v.evidence["landing_path"] == verify.MERGE_QUEUE
    assert v.check(verify.MERGED_EQUALS_TESTED).result == verify.PASS
    assert v.check(verify.TESTED_HEAD_EQUALS_CONFIRMED).result == verify.PASS
    assert v.check(verify.CONFIRMED_LANDED) is None
    assert_names_unique(v)


def assert_names_unique(v):
    """A page reads a check by name, so no name may stand for two results."""
    names = [c.name for c in v.checks]
    assert len(names) == len(set(names)), names


def test_queue_path_merged_differs_from_tested(tmp_path):
    card = Landing(tmp_path)
    tested = card.queue()
    merged = card.repo.commit("late.txt", "pushed after the queue\n", "late")
    card.repo.git("push", "-q", "origin", "main")
    v = card.verify(merged, path=verify.MERGE_QUEUE, tested=tested)
    assert v.outcome == verify.HALTED
    assert v.halt_class == COMMIT_MISMATCH
    assert v.halt.evidence["relation"] == verify.MERGED_EQUALS_TESTED
    assert v.halt.evidence["expected"] == tested
    assert v.halt.evidence["found"] == merged
    assert tested in v.halt.line() and merged in v.halt.line()
    assert v.check(verify.MERGED_EQUALS_TESTED).result == verify.FAIL
    assert v.check(verify.TESTED_HEAD_EQUALS_CONFIRMED).result == verify.SKIP
    assert_names_unique(v)


def test_queue_path_tested_head_differs_from_confirmed(tmp_path):
    card = Landing(tmp_path)
    r = card.repo
    r.git("checkout", "-q", BRANCH)
    later = r.commit("src/c.py", "c = 3\n", "pushed after confirm")
    r.git("checkout", "-q", "main")
    merged = card.queue(head=later)
    v = card.verify(merged, path=verify.BITBUCKET_QUEUE)
    assert v.halt_class == COMMIT_MISMATCH
    assert v.halt.evidence["relation"] == verify.TESTED_HEAD_EQUALS_CONFIRMED
    assert v.halt.evidence["found"] == later
    assert v.halt.evidence["expected"] == card.confirmed
    # The halted verdict's pass and fail sit under different names, so a page
    # asking for the failing relation by name finds the failure.
    assert v.check(verify.MERGED_EQUALS_TESTED).result == verify.PASS
    assert v.check(verify.TESTED_HEAD_EQUALS_CONFIRMED).result == verify.FAIL
    assert_names_unique(v)


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_auto_merge_strict_landed_under_each_method(tmp_path, method):
    card = Landing(tmp_path)
    merged = card.land(method)
    assert merged != card.confirmed
    v = card.verify(merged)
    assert v.outcome == verify.LANDED
    assert v.evidence["landing_path"] == verify.AUTO_MERGE_STRICT
    assert v.evidence["merged_tree"] == v.evidence["expected_tree"]


def test_auto_merge_strict_tree_differs(tmp_path):
    card = Landing(tmp_path)
    card.land("squash")
    merged = card.repo.commit("src/b.py", "b = 999\n", "changed on main")
    card.repo.git("push", "-q", "origin", "main")
    v = card.verify(merged)
    assert v.outcome == verify.HALTED
    assert v.halt_class == COMMIT_MISMATCH
    assert v.halt.evidence["relation"] == "tree_equals_confirmed"


def test_auto_merge_strict_on_a_branch_that_was_behind_is_a_mismatch(tmp_path):
    # The strict relation must not paper over a branch that was not up to date.
    card = Landing(tmp_path, behind=True)
    merged = card.land("merge")
    assert card.verify(merged).halt_class == COMMIT_MISMATCH


@pytest.mark.parametrize("method", ["merge", "squash"])
def test_plain_merge_folds_in_what_landed_meanwhile(tmp_path, method):
    card = Landing(tmp_path, behind=True)
    merged = card.land(method)
    v = card.verify(merged, path=verify.PLAIN_MERGE)
    assert v.outcome == verify.LANDED
    assert v.check("confirmed_landed").detail
    assert v.evidence["merged_tree"] != gitread.tree_of(card.cache, card.confirmed)


def test_tracker_read_fails_is_a_blocking_skip_with_no_class(tmp_path):
    card = Landing(tmp_path)
    merged = card.land("squash")
    status = skipped("GitHub GraphQL budget exhausted")
    v = card.verify(merged, status=status)
    assert v.outcome == verify.BLOCKED
    assert not v.landed
    assert v.halt_class is None
    assert v.check("card_terminal").result == verify.SKIP
    assert status.halt_class == TRACKER_UNREADABLE  # the caller names it


def test_commit_mismatch_beats_an_unreadable_tracker(tmp_path):
    card = Landing(tmp_path)
    tested = card.queue()
    v = card.verify(card.baseline, status=skipped("down"), path=verify.MERGE_QUEUE,
                    tested=tested)
    assert v.halt_class == COMMIT_MISMATCH
    assert v.halt.evidence["relation"] == "new_commit_since_baseline"


def test_unknown_commit_is_git_unreadable(tmp_path):
    card = Landing(tmp_path)
    card.land("squash")
    v = card.verify("0" * 40)
    assert v.outcome == verify.HALTED
    assert v.halt_class == GIT_UNREADABLE
    assert v.check("git").result == verify.SKIP


def test_hand_landing_is_evidence_when_a_message_names_the_card(tmp_path):
    card = Landing(tmp_path)
    card.repo.git("checkout", "-q", BRANCH)
    card.confirmed = card.repo.commit("src/d.py", "d = 4\n", "finish\n\nCloses #13")
    card.repo.git("push", "-q", "origin", BRANCH)
    card.repo.git("checkout", "-q", "main")
    merged = card.land("rebase")
    v = card.verify(merged)
    assert v.landed
    assert v.evidence["hand_landing"] == merged


def test_unknown_landing_path_is_refused(tmp_path):
    with pytest.raises(ValueError, match="landing path"):
        verify.verify(tmp_path, "a", "b", None, "c", CARD, DONE, "fast_forward")


@pytest.mark.parametrize(("key", "text", "hit"), [
    ("13", "Closes #13", True),
    ("13", "Closes #130", False),
    ("13", "see issue 13", False),
    ("ABC-12", "abc-12: fix the page", True),
    ("ABC-12", "XABC-12 and ABC-123", False),
])
def test_key_pattern(key, text, hit):
    assert bool(verify.key_pattern(key).search(text)) is hit


# gitread


def test_refresh_makes_a_bare_cache_and_fetches_a_deleted_branch_head(tmp_path):
    card = Landing(tmp_path)
    card.land("squash")
    branch = gitread.refresh(card.cache, card.remote, commits=(card.confirmed,))
    assert branch == "main"
    assert gitread.run(card.cache, "rev-parse", "--is-bare-repository").strip() == "true"
    assert gitread.rev_parse(card.cache, card.confirmed) == card.confirmed
    later = card.repo.commit("e.txt", "e\n", "later")
    card.repo.git("push", "-q", "origin", "main")
    gitread.refresh(card.cache, card.remote)
    assert gitread.rev_parse(card.cache, "refs/heads/main") == later


@pytest.mark.parametrize("method", ["squash", "rebase"])
def test_squash_landed_card_verifies_against_an_origin_that_refuses_unadvertised_ids(
        tmp_path, monkeypatch, method):
    card = Landing(tmp_path)
    refuse_unadvertised_ids(card.repo.origin, monkeypatch)
    card.hold()
    merged = card.land(method)
    # Squash and rebase land new commits, and the host deleted the branch,
    # so nothing on the origin advertises the confirmed head any more. A
    # cache that did not hold it at confirm time cannot get it by id.
    with pytest.raises(gitread.GitError, match="unadvertised"):
        gitread.refresh(tmp_path / "unheld.git", card.remote, commits=(card.confirmed,))
    v = card.verify(merged)
    assert v.outcome == verify.LANDED, v.checks
    assert v.evidence["merged_tree"] == v.evidence["expected_tree"]


def test_refresh_fetches_a_host_pull_ref_when_the_origin_refuses_ids(tmp_path, monkeypatch):
    card = Landing(tmp_path)
    refuse_unadvertised_ids(card.repo.origin, monkeypatch)
    # A GitHub shaped host keeps the pull request head under a ref of its own
    # after the branch is deleted; the host adapter names that ref.
    card.repo.git("push", "-q", "origin", f"{card.confirmed}:{PULL_REF}")
    merged = card.land("squash")
    fetched = []
    real_fetch = gitread.fetch

    def recording_fetch(repo, remote, *refspecs, **kw):
        fetched.append(refspecs)
        return real_fetch(repo, remote, *refspecs, **kw)

    monkeypatch.setattr(gitread, "fetch", recording_fetch)
    v = card.verify(merged, refs=(PULL_REF,))
    assert v.outcome == verify.LANDED, v.checks
    # The head came in through the named ref; no fetch ever asked for its id.
    assert fetched == [(PULL_REF,)]
    assert not any(card.confirmed in spec for specs in fetched for spec in specs)
    assert not gitread.run(card.cache, "for-each-ref", "refs/pull/").strip()


def test_a_pull_ref_the_host_lacks_falls_back_to_the_id(tmp_path):
    card = Landing(tmp_path)
    merged = card.land("squash")
    v = card.verify(merged, refs=("refs/pull/99/head",))
    assert v.outcome == verify.LANDED, v.checks


def test_refresh_prunes_deleted_branches_and_keeps_held_heads(tmp_path):
    card = Landing(tmp_path)
    held = card.hold()
    assert held == gitread.HELD_REF_PREFIX + BRANCH
    assert gitread.rev_parse(card.cache, f"refs/heads/{BRANCH}") == card.confirmed
    card.land("squash")
    gitread.refresh(card.cache, card.remote)
    with pytest.raises(gitread.GitError):
        gitread.rev_parse(card.cache, f"refs/heads/{BRANCH}")
    assert gitread.rev_parse(card.cache, held) == card.confirmed
    gitread.release(card.cache, BRANCH)
    with pytest.raises(gitread.GitError):
        gitread.rev_parse(card.cache, held)
    gitread.release(card.cache, BRANCH)  # releasing twice is not an error


def test_hold_refuses_a_commit_the_remote_does_not_have(tmp_path):
    card = Landing(tmp_path)
    with pytest.raises(gitread.GitError, match="not on the remote"):
        gitread.hold(card.cache, card.remote, BRANCH, "0" * 40)


def test_log_reads_and_default_branch_of_a_work_clone(tmp_path):
    repo = make_repo(tmp_path)
    base = repo.git("rev-parse", "HEAD")
    head = repo.commit("x.txt", "x\n", "subject line\n\nbody line")
    assert gitread.default_branch(repo.work) == "main"
    assert gitread.log_oneline(repo.work, f"{base}..{head}") == [(head, "subject line")]
    assert gitread.log_messages(repo.work, f"{base}..{head}") == [
        (head, "subject line\n\nbody line")]
    assert gitread.first_parent(repo.work, head) == base
    assert gitread.first_parent(repo.work, base) is None


def test_git_error_names_the_command_and_scrubs_tokens(tmp_path):
    repo = make_repo(tmp_path)
    token = "ghp_" + "A" * 36
    with pytest.raises(gitread.GitError) as caught:
        gitread.rev_parse(repo.work, f"refs/heads/{token}")
    assert "rev-parse" in str(caught.value)
    with pytest.raises(gitread.GitError) as caught:
        gitread.fetch(repo.work, str(tmp_path / f"missing-{token}"), "main")
    assert token not in str(caught.value)


def test_a_network_remote_is_refused_in_tests(tmp_path):
    with pytest.raises(NetworkReached):
        gitread.refresh(tmp_path / "cache.git", "https://example.invalid/repo.git")
