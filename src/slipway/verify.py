"""Decide whether a merged pull request landed, from git and the tracker alone.

Derived from native-relay's skills/relay/scripts/relay/verify.py. That
repository is not in this checkout, so Verdict, _check, _skip, _finish, and
hand_landing are rewritten to the behaviour the plan names (KTD8, U10).

Verify runs after the host says a pull request merged. The host's event is a
wake up, never evidence (KTD10), so verify reads the repository itself and
takes the card's tracker status from the caller. These checks decide, each
recorded under a name no other check shares:

1. new_commit_since_baseline: the merged commit is on the branch, after the
   commit the build started from.
2. The merged commit relates to the commit Slipway confirmed when it opened
   the pull request, by the relation of the landing path (below). Commit
   equality alone is never the test. A queue path records two checks,
   merged_equals_tested and tested_head_equals_confirmed (the second is a
   skip when the first fails); every other path records one,
   confirmed_landed.
3. card_terminal: the tracker shows the card done.

hand_landing, a commit between baseline and merged whose message names the
card key, is recorded as evidence and never gates, because a host merge
commit names the pull request number and not the card.

The relation per landing path, as measured in docs/spikes/merge-paths.md:

    merge_queue, bitbucket_queue  merged equals the tested commit, and the
                                  tested commit's recorded pull request head
                                  equals the confirmed commit
    auto_merge_strict             tree of merged equals tree of confirmed,
                                  under merge commit, squash, and rebase
    plain_merge                   tree of merged equals the tree of merging
                                  confirmed into merged's first parent

plain_merge is a repository with auto merge on but no required up to date
check: the branch may be behind when it lands, so the trees differ by
whatever reached the branch meanwhile, and the relation folds that in.

The outcome is one of:

    landed            every check passed
    partial_landing   the commit landed and the card did not move
                      (halt merged_card_not_moved)
    halted            a commit check failed (halt commit_mismatch) or git
                      could not be read (halt git_unreadable)
    blocked           the commits landed but the tracker could not be read;
                      no class here, the caller names tracker_unreadable from
                      the tracker status it passed in
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from slipway import gitread
from slipway.config.project import LandingPath
from slipway.halts import COMMIT_MISMATCH, GIT_UNREADABLE, MERGED_CARD_NOT_MOVED, Halt, halt
from slipway.trackers.base import TrackerStatus

PASS, FAIL, SKIP = "pass", "fail", "skip"

# The names of the checks that relate the confirmed commit to the merged one.
# A queue path takes two, every other path one; no name appears twice.
MERGED_EQUALS_TESTED = "merged_equals_tested"
TESTED_HEAD_EQUALS_CONFIRMED = "tested_head_equals_confirmed"
CONFIRMED_LANDED = "confirmed_landed"

# The relation each landing path is checked by. config owns the list of
# paths; a path with no entry here is refused, and a unit test fails when
# this table and LandingPath disagree.
TESTED_COMMIT = "tested_commit"
TREE_EQUALS_CONFIRMED = "tree_equals_confirmed"
TREE_EQUALS_CONFIRMED_MERGED_INTO_PARENT = "tree_equals_confirmed_merged_into_parent"
RELATIONS = {
    LandingPath.MERGE_QUEUE: TESTED_COMMIT,
    LandingPath.BITBUCKET_QUEUE: TESTED_COMMIT,
    LandingPath.AUTO_MERGE_STRICT: TREE_EQUALS_CONFIRMED,
    LandingPath.PLAIN_MERGE: TREE_EQUALS_CONFIRMED_MERGED_INTO_PARENT,
}

LANDED = "landed"
PARTIAL_LANDING = "partial_landing"
HALTED = "halted"
BLOCKED = "blocked"


@dataclass(frozen=True)
class Check:
    name: str
    result: str  # pass, fail, or skip
    detail: str = ""


@dataclass(frozen=True)
class Verdict:
    """What verify decided, the checks behind it, and the evidence it read."""

    outcome: str
    checks: tuple[Check, ...]
    evidence: dict = field(default_factory=dict)
    halt: Halt | None = None

    @property
    def landed(self) -> bool:
        return self.outcome == LANDED

    @property
    def halt_class(self) -> str | None:
        return self.halt.name if self.halt else None

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)


class _Run:
    """The checks and evidence of one verify call, in the order taken."""

    def __init__(self):
        self.checks: list[Check] = []
        self.evidence: dict = {}
        self.mismatch: dict | None = None  # the first failed commit relation

    def _check(self, name, ok, detail="", **mismatch):
        self.checks.append(Check(name, PASS if ok else FAIL, detail))
        if not ok and mismatch and self.mismatch is None:
            self.mismatch = mismatch
        return ok

    def _skip(self, name, detail):
        self.checks.append(Check(name, SKIP, detail))

    def _finish(self, landing_path, card_key, tracker_status, merged) -> Verdict:
        checks, evidence = tuple(self.checks), self.evidence
        if self.mismatch is not None:
            found = halt(COMMIT_MISMATCH, landing_path=landing_path, **self.mismatch)
            return Verdict(HALTED, checks, evidence, found)
        terminal = next(c for c in checks if c.name == "card_terminal")
        if terminal.result == SKIP:
            return Verdict(BLOCKED, checks, evidence)
        if terminal.result == FAIL:
            found = halt(MERGED_CARD_NOT_MOVED, card_key=card_key, merged=merged,
                         tracker_status=tracker_status.status)
            return Verdict(PARTIAL_LANDING, checks, evidence, found)
        return Verdict(LANDED, checks, evidence)


def key_pattern(card_key: str) -> re.Pattern[str]:
    """How a commit message names a card: #13 for a number, ABC-12 for a Jira key."""
    key = str(card_key).strip().lstrip("#")
    if key.isdigit():
        return re.compile(rf"(?<![\w&])#{key}\b")
    return re.compile(rf"(?<![\w-]){re.escape(key)}\b", re.IGNORECASE)


def hand_landing(repo: Path, baseline: str, merged: str, card_key: str) -> str | None:
    """The newest commit in baseline..merged whose message names the card, or None."""
    pattern = key_pattern(card_key)
    for commit, message in gitread.log_messages(repo, f"{baseline}..{merged}"):
        if pattern.search(message):
            return commit
    return None


def verify(repo, baseline, confirmed, tested, merged, card_key,
           tracker_status: TrackerStatus, landing_path, *, tested_head=None,
           branch=None) -> Verdict:
    """Decide landed, partial landing, or halt for one merged pull request.

    repo is a bare cache already refreshed by gitread.refresh, holding the
    confirmed commit (gitread.hold pins it at confirm time). tested and
    tested_head are the queue's tested commit and the pull request head
    recorded with it (KTD7); both are None off a queue path. branch defaults
    to the repository's default branch.
    """
    if landing_path not in RELATIONS:
        names = sorted(path.value for path in RELATIONS)
        raise ValueError(f"{landing_path!r} is not a landing path: {names}")
    landing_path = LandingPath(landing_path).value
    relation = RELATIONS[landing_path]
    repo = Path(repo)
    run = _Run()
    run.evidence.update(landing_path=landing_path, baseline=baseline, confirmed=confirmed,
                        tested=tested, tested_head=tested_head, merged=merged,
                        card_key=str(card_key))
    try:
        branch = branch or gitread.default_branch(repo)
        tip = gitread.rev_parse(repo, f"refs/heads/{branch}")
        run.evidence.update(branch=branch, branch_tip=tip)
        _commits(repo, run, baseline, confirmed, tested, tested_head, merged, tip,
                 relation, branch)
        run.evidence["hand_landing"] = hand_landing(repo, baseline, merged, card_key)
    except gitread.GitError as exc:
        run._skip("git", str(exc))
        found = halt(GIT_UNREADABLE, reason=str(exc))
        return Verdict(HALTED, tuple(run.checks), run.evidence, found)

    run.evidence["tracker_status"] = tracker_status.status
    if tracker_status.skipped:
        run._skip("card_terminal", tracker_status.skipped)
    else:
        run._check("card_terminal", tracker_status.terminal,
                   f"card {card_key} is {tracker_status.status}")
    return run._finish(landing_path, card_key, tracker_status, merged)


def _commits(repo, run, baseline, confirmed, tested, tested_head, merged, tip,
             relation, branch):
    on_branch = gitread.is_ancestor(repo, merged, tip)
    after_baseline = merged != baseline and gitread.is_ancestor(repo, baseline, merged)
    run._check("new_commit_since_baseline", on_branch and after_baseline,
               f"{merged} on {branch} after {baseline}",
               relation="new_commit_since_baseline",
               expected=f"a commit on {branch} after {baseline}", found=merged)

    if relation == TESTED_COMMIT:
        # Two checks, each under its own name, so reading a check by name
        # can never find a pass that sits beside the failure that halted.
        if run._check(MERGED_EQUALS_TESTED, merged == tested,
                      f"merged {merged}, tested {tested}",
                      relation=MERGED_EQUALS_TESTED, expected=tested, found=merged):
            run._check(TESTED_HEAD_EQUALS_CONFIRMED, tested_head == confirmed,
                       f"tested pull request head {tested_head}, confirmed {confirmed}",
                       relation=TESTED_HEAD_EQUALS_CONFIRMED, expected=confirmed,
                       found=tested_head)
        else:
            run._skip(TESTED_HEAD_EQUALS_CONFIRMED,
                      f"not checked: merged {merged} is not the tested commit")
        return

    merged_tree = gitread.tree_of(repo, merged)
    if relation == TREE_EQUALS_CONFIRMED:
        expected = gitread.tree_of(repo, confirmed)
    else:
        parent = gitread.first_parent(repo, merged)
        expected = parent and gitread.merge_tree(repo, parent, confirmed)
    run.evidence.update(merged_tree=merged_tree, expected_tree=expected)
    run._check(CONFIRMED_LANDED, merged_tree == expected,
               f"tree of {merged} is {merged_tree}, expected {expected}",
               relation=relation, expected=expected, found=merged_tree)
