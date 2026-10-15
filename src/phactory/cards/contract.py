"""The card contract: the fenced phactory block and the ready rule (KTD5, R9).

A card's body carries one fenced code block whose info string is phactory,
holding a JSON object (the card template, .github/ISSUE_TEMPLATE/card.md,
shows it):

    ```phactory
    {"good": "...", "paths": ["..."], "test": "...", "lane": "cloud"}
    ```

good is the written definition of done the card is scored against, paths the
files the card may touch, test the test that proves it, and lane the lane the
card was filed for. An absent field is not an error: absent paths contributes
nothing to in flight files until push, absent good records "no rubric", and
absent test records "none named". An empty string or empty list counts as
absent, since the template ships with empty values. A block that is present
but not a JSON object of the right types is an error, and an error card is
not ready.

check_card (U16) shares parse, so a card that passes check_card parses here.

ready(card, project) is the ready rule: the card sits in the project's ready
status, carries exactly one lane label and it is the project's, and its block
parsed (absent fields recorded). A card outside the ready status, or in
another lane, is not ready and silent: nothing is wrong with it, it is just
not this project's to build now. A card in the ready status whose lane or
block is wrong is not ready with a reason, because someone has to fix it.

This module reads cards through their attributes (key, body, status, labels)
and imports no tracker, so the trackers and the contract stay independent.
"""

import json
import re
from dataclasses import dataclass

# The three fields a card should carry. lane is read too but is not counted
# as absent, since the lane label is what dispatch keys on (R9).
FIELDS = ("good", "paths", "test")

# A fenced block opened by three or more backticks with the info string
# phactory, closed by a fence of the same length on its own line.
BLOCK = re.compile(
    r"^(?P<fence>`{3,})[ \t]*phactory[ \t]*\r?\n(?P<json>.*?)^(?P=fence)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class Block:
    """A parsed phactory block. error is set when the block is malformed."""

    good: str | None = None
    paths: tuple[str, ...] = ()
    test: str | None = None
    lane: str | None = None
    absent: tuple[str, ...] = FIELDS
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class Readiness:
    """The ready rule's answer. reason is None when not ready is silent."""

    ready: bool
    reason: str | None = None
    block: Block | None = None


def _fail(message: str) -> Block:
    return Block(absent=(), error=message)


def parse(body: str | None) -> Block:
    """Parse the phactory block out of a card body."""
    found = list(BLOCK.finditer(body or ""))
    if not found:
        return Block()
    if len(found) > 1:
        return _fail(f"the body holds {len(found)} phactory blocks; keep one")
    try:
        data = json.loads(found[0].group("json"))
    except ValueError as error:
        return _fail(f"the phactory block is not valid JSON: {error}")
    if not isinstance(data, dict):
        return _fail("the phactory block must be a JSON object")

    def text(name: str) -> str | None:
        value = data.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string, got {type(value).__name__}")
        return value.strip() or None

    try:
        good, test, lane = text("good"), text("test"), text("lane")
        paths = data.get("paths") or []
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise ValueError("paths must be a list of strings")
        paths = tuple(p.strip() for p in paths if p.strip())
    except ValueError as error:
        return _fail(f"the phactory block's {error}")

    values = {"good": good, "paths": paths, "test": test}
    absent = tuple(name for name in FIELDS if not values[name])
    return Block(good=good, paths=paths, test=test, lane=lane, absent=absent)


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def ready(card, project, other_lanes=()) -> Readiness:
    """Apply the ready rule to one card for one project.

    other_lanes names the lane labels that are not this project's (such as
    a lane run by hand), so a card carrying both is caught as ambiguous. The
    project's own lane_label is always a lane.
    """
    if not _same(card.status, project.ready_status):
        return Readiness(False)
    lanes = {project.lane_label.lower(), *(name.lower() for name in other_lanes)}
    carried = sorted({label for label in card.labels if label.lower() in lanes})
    if len(carried) > 1:
        return Readiness(
            False, f"card {card.key} carries {len(carried)} lane labels ({', '.join(carried)}); "
            "keep one",
        )
    if not carried:
        return Readiness(False, f"card {card.key} carries no lane label")
    if not _same(carried[0], project.lane_label):
        return Readiness(False)
    block = parse(card.body)
    if not block.ok:
        return Readiness(False, f"card {card.key}: {block.error}", block)
    if block.lane and not _same(block.lane, project.lane_label):
        return Readiness(
            False,
            f"card {card.key} is labelled {carried[0]} but its block names lane {block.lane}",
            block,
        )
    return Readiness(True, None, block)
