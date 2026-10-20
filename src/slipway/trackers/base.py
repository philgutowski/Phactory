"""What every tracker adapter returns, and the interface they share (KTD8, R34).

A tracker is where a project's cards live: GitHub Projects or Jira. An
adapter is the whole of what Slipway knows about one tracker, and it only
reads (R34). The board moves by the tracker's own automation on host events
Slipway causes (KTD27), so no method here writes.

The interface every adapter implements:

    ready()                -> (cards, reason)   open cards carrying the lane label
    read(key)              -> Card              one card, with labels and body
    status(key)            -> TrackerStatus     the card's status, and whether it is terminal
    closing_reference(key) -> ClosingReference  the tokens the tracker's automation keys on

ready returns candidates, not a verdict: the ready rule itself is
slipway.cards.contract.ready, which the caller applies to each card. A read
that fails returns a reason instead of raising, because "the tracker could
not be read" and "nothing is ready" lead to different places. A status read
that fails is a skip with halt class tracker_unreadable (KTD11): verify
treats it as unknown, never as landed and never as not landed.

Every adapter takes an injectable transport, a run callable for gh and an
opener for urllib, so tests answer from fixtures and never reach a network.
"""

from dataclasses import dataclass

# Every gh call and every HTTP request is bounded by this many seconds.
NETWORK_TIMEOUT_SECONDS = 30

# The halt class a skipped read carries (KTD11). halts.py (U10) owns the
# closed set; this is the one member a tracker read can produce.
TRACKER_UNREADABLE = "tracker_unreadable"


@dataclass(frozen=True)
class Card:
    """One card as a tracker returns it.

    key is the tracker's own name for it: the issue number on GitHub, the
    issue key (such as ABC-12) on Jira. status is the board column on GitHub
    Projects and the workflow status on Jira; None when the tracker gave none.
    skipped carries the reason when the read failed, and the other fields are
    then empty.
    """

    key: str
    title: str = ""
    body: str = ""
    status: str | None = None
    labels: tuple[str, ...] = ()
    skipped: str | None = None


@dataclass(frozen=True)
class TrackerStatus:
    """A card's status and whether it is terminal (done) on its board."""

    status: str | None
    terminal: bool
    skipped: str | None = None

    @property
    def halt_class(self) -> str | None:
        return TRACKER_UNREADABLE if self.skipped else None


def skipped(reason: str) -> TrackerStatus:
    """The status of a read that could not be completed."""
    return TrackerStatus(status=None, terminal=False, skipped=str(reason))


@dataclass(frozen=True)
class ClosingReference:
    """What a pull request carries so the tracker's own automation finds the card.

    title_token goes in the pull request title, body_line in its body, and
    branch_token in the branch name. A tracker that keys on none of one of
    these leaves it None.
    """

    title_token: str | None = None
    body_line: str | None = None
    branch_token: str | None = None


# The public surface of every adapter. The tests assert no other public
# method exists, so a closeout, filing, or comment search method cannot creep
# back in from the origin files.
INTERFACE = ("candidates", "ready", "read", "status", "closing_reference")
