"""The events the card workflow waits on, and how a wait's answer is read.

A durable wait parks the card workflow until an event arrives or a sleep
ends, whichever is first (the spike's or group). Every wait in the workflow
is the same race:

    any of WAIT_KINDS for this build   raced with   a sleep of WAKE_SECONDS

WAIT_KINDS are the six host event kinds hosts.webhooks normalises, plus
card.resume, which the land verb pushes (service.engine). Each condition is
filtered by filter_expression(build_id), a CEL expression (the small
expression language Hatchet evaluates against the event payload) that keeps
another build's events from waking this one. The sleep is the fallback poll:
when it wins, the workflow re reads the pull request itself and turns what
it sees into the event the host would have sent (poll_event), so a lost
webhook costs at most an hour.

A wait's answer comes back from the engine as {"CREATE": {data key: [payload,
...]}}, where the data key is the condition's readable data key: the event
kind for an event, SLEEP for the sleep. read_wake turns that into a Wake.

Every event is a wake up, never evidence (KTD10): the payload says what to
look at, and the workflow re reads the pull request before it records
anything.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from slipway.hosts.base import CLOSED, MERGED, PullRequest
from slipway.hosts.webhooks import (
    CHECK_COMPLETED,
    KINDS,
    PR_CLOSED,
    PR_DEQUEUED,
    PR_MERGED,
    PR_SYNCHRONIZE,
    QUEUE_REQUESTED,
)
from slipway.service.engine import BUILD_FIELD, CARD_RESUME

HOST_KINDS = KINDS
WAIT_KINDS = (*HOST_KINDS, CARD_RESUME)
# The readable data key of the racing sleep, and how long it sleeps. A woken
# sleep is the poll path (KTD10) and, in a halted wait, the hourly re verify
# (KTD11).
SLEEP = "sleep"
WAKE_SECONDS = 3600

# The events that reach the gate_observed seam.
GATE_KINDS = (CHECK_COMPLETED, QUEUE_REQUESTED, PR_DEQUEUED)

__all__ = [
    "CARD_RESUME", "CHECK_COMPLETED", "GATE_KINDS", "HOST_KINDS", "PR_CLOSED", "PR_DEQUEUED",
    "PR_MERGED", "PR_SYNCHRONIZE", "QUEUE_REQUESTED", "SLEEP", "WAIT_KINDS", "WAKE_SECONDS",
    "Wake", "filter_expression", "poll_event", "read_wake",
]


@dataclass(frozen=True)
class Wake:
    """What ended a wait: an event kind with its payload, or the sleep."""

    kind: str
    payload: dict = field(default_factory=dict)

    @property
    def slept(self) -> bool:
        return self.kind == SLEEP

    def to_json(self) -> dict:
        return {"kind": self.kind, "payload": dict(self.payload)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Wake":
        return cls(str(data["kind"]), dict(data.get("payload") or {}))


def filter_expression(build_id: int) -> str:
    """The CEL filter that matches an event pushed for this build only."""
    return f"input.{BUILD_FIELD} == {int(build_id)}"


def read_wake(answer: Mapping[str, Any]) -> Wake:
    """The Wake inside an engine's wait answer.

    The answer holds one matched condition. A payload that is not a mapping
    (a sleep's own record) is kept as an empty payload.
    """
    matched = answer.get("CREATE") or {}
    if not matched:
        raise ValueError(f"a wait answer with no matched condition: {sorted(answer)}")
    key, items = next(iter(matched.items()))
    first = items[0] if isinstance(items, list) and items else items
    payload = dict(first) if isinstance(first, Mapping) else {}
    return Wake(SLEEP if key == SLEEP else str(key), payload)


def poll_event(pr: PullRequest, confirmed: str | None, repository: str, host: str
               ) -> dict | None:
    """The normalised event a re read pull request implies, or None when nothing changed.

    merged and closed read off the state; a head that is not the confirmed
    commit is a synchronize. The mapping has the shape hosts.webhooks.Event
    normalises, with no delivery id, which is what ingest_host_event expects
    of the poll path.
    """
    if pr.state == MERGED or pr.merged:
        kind = PR_MERGED
    elif pr.state == CLOSED:
        kind = PR_CLOSED
    elif confirmed and pr.head and pr.head != confirmed:
        kind = PR_SYNCHRONIZE
    else:
        return None
    return {
        "host": host, "delivery_id": None, "repository": repository, "pr_number": pr.number,
        "kind": kind, "merge_commit": pr.merge_commit, "head_commit": pr.head,
        "tested_commit": None, "tested_head": None, "check_name": None, "check_state": None,
    }
