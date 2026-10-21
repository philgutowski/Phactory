"""The operator's reads and upkeep, beside the two write verbs (U21, R25, KTD23).

The CLI's verbs are thin clients of the service layer. dispatch and land live
in verbs.py and compose and across in status.py; this module holds the three
the CLI needs that had no service function yet, so the CLI still imports
nothing below service:

status(ports, project, actor)
    compose for one project, or across every project when project is None,
    with one audit row naming who asked. A subject that is neither the
    operator nor on the project's allowed_subjects is refused.

events(ports, project, cursor, limit) and tail(ports, project, actor, ...)
    The project's transitions after a cursor, oldest first. A transition is
    one row the card workflow writes each time a build changes state, with
    the actor and the evidence. A cursor is the id of the last transition
    the caller has seen, so the next read starts after it. tail is events
    with one audit row; a caller that follows the stream audits the first
    read with tail and polls with events, so a long watch writes one row.

migrate(ports, actor, database)
    Applies the pending record migrations (records.migrate) and writes one
    audit row once they are in. A refused migration writes none, since the
    audit table may be what failed to arrive.

An audit row needs a project. The two calls that cover every project,
status with no project and migrate, are written under EVERY_PROJECT.
"""

from slipway.records.migrate import MigrationError
from slipway.service.status import across, compose
from slipway.service.verbs import REFUSED, Ports, audit, scrubbed

# The audit row's project for a call that covers every project.
EVERY_PROJECT = "*"

EVENTS = "events"
MIGRATED = "migrated"
# How many transitions one read returns at most.
EVENTS_LIMIT = 100


def may_read(ports: Ports, project, actor: str) -> bool:
    """Whether actor may read project: the operator, or a subject the project allows."""
    return actor == ports.operator().operator_subject or actor in project.allowed_subjects


def _brief(result: dict) -> dict:
    """What a status audit row keeps: the outcome and the counts, not every card."""
    return {key: result[key] for key in ("outcome", "reason", "counts") if key in result}


def status(ports: Ports, project: str | None, actor: str, client: str | None = None) -> dict:
    """compose for one project, or across all of them; one audit row either way."""
    if project is None:
        result = across(ports, actor)
        audit(ports, EVERY_PROJECT, actor, "status", {"project": None}, _brief(result), client)
        return result
    found = ports.project(project)
    if not may_read(ports, found, actor):
        result = {"project": found.slug, "outcome": REFUSED,
                  "reason": f"subject {actor} may not read project {found.slug}"}
    else:
        result = compose(ports, found.slug)
    audit(ports, found.slug, actor, "status", {"project": found.slug}, _brief(result), client)
    return result


def _event(row: dict) -> dict:
    evidence = row.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    said = evidence.get("line") or evidence.get("reason")
    return {"id": row["id"], "key": row["card_key"], "build_id": row["build_id"],
            "from": row.get("from_state"), "to": row["to_state"], "actor": row.get("actor"),
            "at": row.get("at"), "said": scrubbed(str(said)) if said else None}


def events(ports: Ports, project: str, cursor: int = 0, limit: int = EVENTS_LIMIT) -> dict:
    """The project's transitions after cursor, oldest first, and the cursor to pass next."""
    found = ports.project(project)
    page = ports.records.events_since(ports.records.project_id(found.slug), int(cursor),
                                      int(limit))
    return {"project": found.slug, "outcome": EVENTS,
            "events": [_event(row) for row in page["rows"]], "cursor": page["cursor"],
            "truncated": page["truncated"]}


def tail(ports: Ports, project: str, actor: str, cursor: int = 0, limit: int = EVENTS_LIMIT,
         client: str | None = None) -> dict:
    """events, refused for a subject that may not read the project, with one audit row."""
    found = ports.project(project)
    arguments = {"project": found.slug, "cursor": int(cursor), "limit": int(limit)}
    if not may_read(ports, found, actor):
        result = {"project": found.slug, "outcome": REFUSED,
                  "reason": f"subject {actor} may not read project {found.slug}"}
        audit(ports, found.slug, actor, "watch", arguments, result, client)
        return result
    result = events(ports, found.slug, cursor, limit)
    audit(ports, found.slug, actor, "watch", arguments,
          {"outcome": EVENTS, "count": len(result["events"]), "cursor": result["cursor"]},
          client)
    return result


def migrate(ports: Ports, actor: str, database: str | None,
            client: str | None = None) -> dict:
    """Apply the pending migrations, for the operator alone."""
    if actor != ports.operator().operator_subject:
        return {"outcome": REFUSED, "reason": "only the operator migrates the records"}
    try:
        applied = ports.records.migrate(database)
    except MigrationError as error:
        return {"outcome": REFUSED, "reason": str(error)}
    result = {"outcome": MIGRATED, "applied": list(applied)}
    audit(ports, EVERY_PROJECT, actor, "migrate", {"database": database}, result, client)
    return result


__all__ = ["EVENTS", "EVERY_PROJECT", "MIGRATED", "events", "may_read", "migrate", "status",
           "tail"]
