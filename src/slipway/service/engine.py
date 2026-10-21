"""The engine client facade: start a card's run, push an event into it (KTD23).

The engine is Hatchet (KTD2), the server that keeps every run's durable log.
service may not import workflow (KTD23), so this module never sees the card
workflow's code. It names the workflow by its registered name and speaks to
the engine through its client alone, and the workflow side reads the names
it shares from here, so the two cannot drift apart.

Two calls, in the order a card uses them:

    start_card(project, key, build_id) -> run id
        Starts one card_workflow run. The caller has already claimed the
        build row (KTD3): the run's first step writes its own run id onto
        that row, so this call never writes a record, and a second start for
        the same build exits as a no op inside the workflow.

    push(kind, build_id, payload)
        Pushes one event. The workflow's waits are filtered on the build id
        the payload carries, so an event wakes the runs of that build only.
        A host event is a wake up, never evidence (KTD10): the workflow re
        reads the pull request before it records anything, which is why the
        payload may be the normalised event as it came.

card.resume is the event the land verb (U25) pushes to end a halted wait;
its payload carries the actor, the reason, and override when the operator
accepts a commit mismatch by hand (KTD11).

HatchetEngine builds its client lazily, on the first call, so importing this
module needs no token and never opens a connection.
"""

from collections.abc import Mapping
from typing import Any, Protocol

# The registered name of the durable card workflow, and the field its input
# and every event payload carry the build id under.
WORKFLOW = "card_workflow"
BUILD_FIELD = "build_id"

# The event the land verb pushes to end a halted or gate_red wait (KTD11).
CARD_RESUME = "card.resume"


class Engine(Protocol):
    def start_card(self, project: str, key: str, build_id: int) -> str: ...

    def push(self, kind: str, build_id: int, payload: Mapping[str, Any]) -> None: ...


def card_input(project: str, key: str, build_id: int) -> dict:
    """The card workflow's input: the project slug, the card key, the build id."""
    return {"project": project, "key": str(key), BUILD_FIELD: int(build_id)}


def event_payload(build_id: int, payload: Mapping[str, Any]) -> dict:
    """An event payload with the build id the workflow's waits filter on."""
    return {**payload, BUILD_FIELD: int(build_id)}


def push_host_event(engine: Engine, build_id: int, normalised: Mapping[str, Any]) -> None:
    """Push a normalised host event (hosts.webhooks.Event.normalised) under its own kind."""
    engine.push(normalised["kind"], build_id, normalised)


class HatchetEngine:
    """Engine over a hatchet_sdk client. client may be passed in; else one is built."""

    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from hatchet_sdk import Hatchet

            self._client = Hatchet()
        return self._client

    def start_card(self, project: str, key: str, build_id: int) -> str:
        details = self.client.runs.create(WORKFLOW, card_input(project, key, build_id))
        return details.run.metadata.id

    def push(self, kind: str, build_id: int, payload: Mapping[str, Any]) -> None:
        self.client.event.push(kind, event_payload(build_id, payload))
