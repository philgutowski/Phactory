"""The closed set of halt classes (KTD11).

A halt class is the name Slipway gives a reason a card stopped short of
landing that a person must look at. The set is closed: a new kind of outcome
becomes a finding attached to a record, never an eighth class, so every
surface (the status page, the landing check, the MCP tools) can rely on
knowing every class it will ever be shown.

The seven classes split by where they occur:

- A verify halt is a wait. The workflow enters the halted state, posts a
  failing landing check on the pull request with the reason (R42), and waits
  for a card.resume event raced with an hourly re verify.
- A build outcome is a reason. The build ends as returned_empty with the class
  as its reason, an ownership terminal state the dispatch verb can reopen, so
  no card is stuck in a wait that only a merge could end.

Each class names the evidence keys its needs you line requires, so a halt
that cannot say what happened is refused when it is built, not when someone
reads it.
"""

from dataclasses import dataclass, field
from enum import StrEnum

# Before verify records merged_card_not_moved the workflow re reads the tracker
# this often for this long, because the host's merged event arrives before the
# tracker's own rule has moved the card (KTD11).
NOT_MOVED_RECHECK_SECONDS = 30
NOT_MOVED_RECHECK_WINDOW_SECONDS = 600

# A verify halt is re verified on this cadence while it waits for card.resume.
REVERIFY_SECONDS = 3600


class Kind(StrEnum):
    VERIFY_HALT = "verify_halt"  # a wait in the halted state
    BUILD_OUTCOME = "build_outcome"  # a reason on returned_empty


@dataclass(frozen=True)
class HaltClass:
    """One member of the closed set: its name, kind, and needs you line."""

    name: str
    kind: Kind
    evidence_keys: tuple[str, ...]
    line: str  # a format string over evidence_keys

    @property
    def is_wait(self) -> bool:
        return self.kind is Kind.VERIFY_HALT


MERGED_CARD_NOT_MOVED = "merged_card_not_moved"
COMMIT_MISMATCH = "commit_mismatch"
TRACKER_UNREADABLE = "tracker_unreadable"
GIT_UNREADABLE = "git_unreadable"
BUILDER_FAILED = "builder_failed"
BUILDER_EXPIRED = "builder_expired"
BINDING_INVALID = "binding_invalid"

CLASSES: dict[str, HaltClass] = {c.name: c for c in (
    HaltClass(
        MERGED_CARD_NOT_MOVED, Kind.VERIFY_HALT, ("card_key", "merged", "tracker_status"),
        "{merged} is on the branch but card {card_key} is still {tracker_status}; "
        "move the card or check the tracker's automation",
    ),
    HaltClass(
        COMMIT_MISMATCH, Kind.VERIFY_HALT, ("landing_path", "relation", "expected", "found"),
        "on {landing_path} the {relation} relation failed: expected {expected}, "
        "found {found}; land with an override only after reading both",
    ),
    HaltClass(
        TRACKER_UNREADABLE, Kind.VERIFY_HALT, ("card_key", "reason"),
        "card {card_key} could not be read from the tracker: {reason}",
    ),
    HaltClass(
        GIT_UNREADABLE, Kind.VERIFY_HALT, ("reason",),
        "the repository could not be read: {reason}",
    ),
    HaltClass(
        BUILDER_FAILED, Kind.BUILD_OUTCOME, ("reason",),
        "the builder failed: {reason}; dispatch again with a reason",
    ),
    HaltClass(
        BUILDER_EXPIRED, Kind.BUILD_OUTCOME, ("budget_seconds",),
        "the build ran past its budget of {budget_seconds} seconds; "
        "dispatch again with a reason",
    ),
    HaltClass(
        BINDING_INVALID, Kind.BUILD_OUTCOME, ("role", "reason"),
        "the {role} binding is not usable: {reason}; fix the project or operator file",
    ),
)}


class HaltError(ValueError):
    """A halt was built with a class outside the set or without its evidence."""


@dataclass(frozen=True)
class Halt:
    """One halt: a class from the set and the evidence that explains it.

    Evidence holds structured fields only (keys, commits, numbers), never a
    raw provider or tracker response body (KTD24).
    """

    name: str
    evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        cls = CLASSES.get(self.name)
        if cls is None:
            raise HaltError(f"{self.name!r} is not a halt class; the set is closed: "
                            f"{', '.join(CLASSES)}")
        missing = [k for k in cls.evidence_keys if k not in self.evidence]
        if missing:
            raise HaltError(f"{self.name} needs evidence {', '.join(missing)}")

    @property
    def cls(self) -> HaltClass:
        return CLASSES[self.name]

    @property
    def kind(self) -> Kind:
        return self.cls.kind

    @property
    def is_wait(self) -> bool:
        return self.cls.is_wait

    def line(self) -> str:
        """The needs you sentence the landing check and the page show."""
        return self.cls.line.format(**{k: self.evidence[k] for k in self.cls.evidence_keys})


def halt(name: str, **evidence) -> Halt:
    return Halt(name, dict(evidence))
