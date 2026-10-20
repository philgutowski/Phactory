"""The builder contract every adapter implements (KTD26, R10, R11, R16).

A builder is whatever writes the code for a card: a Claude Managed Agents
session (U12) or a headless Claude Code CLI session in a container or on the
laptop (U13). An adapter is the class that drives one kind of builder behind
the methods below, so the card workflow never knows which builder it holds.

A primitive is one of the six things any builder needs. Each maps to exactly
one carrier, a method or field that delivers it, and a primitive with no row
is not in the contract:

| Primitive   | Carrier                                                    |
|:-|:-|
| Harness     | `start`                                                    |
| Environment | `poll`, `release`, `delete`, `events`, `schedule`          |
| Tools       | `Handoff.tools`                                            |
| Skills      | `Handoff.rules`                                            |
| Credentials | `Credential`                                               |
| Schedule    | `ResolvedBinding.time_budget_minutes`                      |

Harness is the agent loop itself: start hands it the handoff, the role's
binding (account, model, effort), and the credential, and returns a session
reference, the adapter's own name for the running session. Environment is
where the session runs and what it leaves behind: poll reads its status, and
the terminal status carries the BuilderResult; release archives a provider
session or removes a container; delete removes the provider's record, called
only after the trace is read back (KTD17); events yields the session's trace
shape records from the adapter's own mapper; schedule says how the workflow
waits, remote_session (the workflow polls) or worker_process (the task runs
on a labelled worker and blocks). Tools is the fixed tool list the handoff
names. Skills is the rules text in the handoff. Credentials is a refreshable
object scoped as KTD6 says (a repository wide machine identity, with the host
enforcing the slipway/ branch namespace), injected by the adapter and never
printed in the handoff. Schedule is the role's time budget, which the
workflow enforces as a deadline from the moment the build child starts
(KTD22); no adapter method waits or counts time.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from slipway.config.bindings import ResolvedBinding
from slipway.trace.shape import Record

if TYPE_CHECKING:
    from slipway.builders.handoff import Handoff

# The build outcome a builder can produce (KTD11). halts.py (U10) owns the
# closed set; this is the member a wrong base or a failed session carries.
BUILDER_FAILED = "builder_failed"

RETURNED = "returned"
RETURNED_EMPTY = "returned_empty"


class Schedule(StrEnum):
    REMOTE_SESSION = "remote_session"  # the workflow polls the provider
    WORKER_PROCESS = "worker_process"  # the task runs on a labelled worker and blocks


# The table above as data. The tests hold the docstring and this to each
# other and to the protocol, so a method cannot be added without a row.
PRIMITIVES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Harness", ("start",)),
    ("Environment", ("poll", "release", "delete", "events", "schedule")),
    ("Tools", ("Handoff.tools",)),
    ("Skills", ("Handoff.rules",)),
    ("Credentials", ("Credential",)),
    ("Schedule", ("ResolvedBinding.time_budget_minutes",)),
)


class CredentialError(Exception):
    """A credential could not be minted or refreshed."""


class Credential:
    """A refreshable secret the adapter injects into the session (KTD6).

    name is the operator file entry it was minted from, which the handoff may
    print. The value is never printed: repr and str show the name only, and
    refresh drops the old value before it returns, so nothing on the object
    still holds it. mint is called with no arguments and returns the new
    value and its expiry; for a GitHub App installation token that is one
    hour, and the adapter refreshes it on a fifty minute cadence.
    """

    __slots__ = ("name", "_mint", "_value", "expires_at")

    def __init__(self, name: str, mint: Callable[[], tuple[str, datetime | None]]):
        self.name = name
        self._mint = mint
        self._value: str | None = None
        self.expires_at: datetime | None = None
        self.refresh()

    def current(self) -> str:
        return self._value

    def refresh(self) -> str:
        old = self._value
        value, expires_at = self._mint()
        if not value:
            raise CredentialError(f"credential {self.name} minted an empty value")
        if value == old:
            raise CredentialError(f"credential {self.name} minted the value it replaces")
        self._value, self.expires_at = value, expires_at
        return value

    def due(self, now: datetime, margin: timedelta = timedelta(minutes=10)) -> bool:
        """Whether the value expires within margin of now."""
        return self.expires_at is not None and now + margin >= self.expires_at

    def __repr__(self) -> str:
        return f"Credential(name={self.name!r}, value=...)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a Credential is never serialised")


@dataclass(frozen=True)
class BuilderResult:
    """What a terminal session returns.

    commit is the branch tip the builder pushed, or None when it pushed
    nothing. reason says why the session ended (a provider stop reason, an
    exit code, a budget). provider_ref is the provider's own session or
    container name, cost the provider's reported spend in US dollars when it
    reports one, model and account the binding the session ran under.
    halt_class is set when the build ends returned_empty for a KTD11 class.
    """

    branch: str
    commit: str | None
    reason: str
    provider_ref: str
    cost: float | None = None
    model: str | None = None
    account: str | None = None
    halt_class: str | None = None

    @property
    def outcome(self) -> str:
        return RETURNED if self.commit else RETURNED_EMPTY


@dataclass(frozen=True)
class SessionStatus:
    """poll's answer. A terminal status carries the result; a running one does not."""

    terminal: bool
    result: BuilderResult | None = None
    detail: str | None = None

    def __post_init__(self):
        if self.terminal != (self.result is not None):
            raise ValueError("a terminal status carries a result, a running one does not")


@runtime_checkable
class Builder(Protocol):
    schedule: Schedule

    def start(self, handoff: "Handoff", binding: ResolvedBinding,
              credential: Credential) -> str: ...

    def poll(self, session_ref: str) -> SessionStatus: ...

    def release(self, session_ref: str) -> None: ...

    def delete(self, session_ref: str) -> None: ...

    def events(self, session_ref: str) -> Iterator[Record] | None: ...


PROTOCOL_MEMBERS = ("start", "poll", "release", "delete", "events", "schedule")


def check_base(result: BuilderResult, base: str,
               is_ancestor: Callable[[str, str], bool]) -> BuilderResult:
    """Return result unchanged when its commit descends from base, else returned_empty.

    is_ancestor(base, commit) answers from the project's repository (the
    workflow passes gitread's). A commit equal to base counts as descending,
    and then the builder pushed nothing new, so it is returned_empty too.
    """
    if result.commit is None:
        return result
    if result.commit == base:
        why = f"branch {result.branch} is still at base {base}; nothing was pushed"
    elif not is_ancestor(base, result.commit):
        why = f"commit {result.commit} on {result.branch} does not descend from base {base}"
    else:
        return result
    return replace(result, commit=None, reason=why, halt_class=BUILDER_FAILED)
