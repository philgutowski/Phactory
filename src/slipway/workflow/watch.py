"""Watch: the five minute cron that dispatches ready cards (U25, KTD3, KTD21).

A cron is a task the engine starts on a clock, here every five minutes. One
cycle walks every configured project and, for each, does two things:

1. Dispatch. It reads the project's ready cards from the tracker and
   dispatches each one no build owns, with actor watch, through the same
   service.verbs.dispatch_card the dispatch verb uses (KTD21: route at
   filing, nobody routes). A card whose latest build is outside the
   ownership terminal set is skipped, so a card in awaiting_approval, or
   any other live state, is never claimed twice. A card whose last build
   ended returned_empty or abandoned is skipped too: those reopen only
   through the dispatch verb with a reason (KTD3). The cycle stops at the
   project's daily dispatch cap and says so.
2. Retry. A claimed build with no run id, untouched for longer than one
   interval, is a claim whose start never reached the engine. Watch starts
   its run again, never claims the card again (KTD3); if the first start
   did reach the engine after all, the run's attach_run step ends one of
   the two as a no op.

Halted builds are not touched here. Their hourly re verify is the card
workflow's own sleep, raced against card.resume inside the run (KTD11).

Every dispatch watch makes writes an audit row under the subject watch, so
the record says who caused it (R8). One project failing (a bad project
file, an unreadable tracker) is reported in the cycle's result and does
not stop the others.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from slipway.cards.contract import ready as ready_rule
from slipway.service import verbs

ACTOR = "watch"
TASK = "watch"
INTERVAL = timedelta(minutes=5)
CRON = "*/5 * * * *"
# A cycle is bounded well inside its interval, so two never pile up.
EXECUTION = timedelta(minutes=4)


def cycle(ports: verbs.Ports, slug: str) -> dict:
    """One watch cycle for one project: dispatch what is ready, retry stale claims."""
    project = ports.project(slug)
    records = ports.records
    project_id = records.project_id(project.slug)
    out: dict[str, Any] = {"project": project.slug, "dispatched": [], "skipped": [],
                           "retried": []}

    stale = records.stale_claims(project_id, ports.clock() - INTERVAL)
    for row in stale:
        started = verbs.start(ports, project.slug, row["card_key"], row["id"])
        out["retried"].append({"key": row["card_key"], "build_id": row["id"], **started})

    cards, reason = ports.tracker(project).ready()
    if reason:
        out["tracker"] = reason
        return out
    lanes = verbs.other_lanes(ports, project)
    latest = {row["card_key"]: row for row in records.latest_builds(project_id)}
    for card in cards:
        key = str(card.key)
        held = latest.get(key)
        if held and (held["state"] not in verbs.TERMINAL
                     or held["state"] in verbs.REOPEN_NEEDS_REASON):
            out["skipped"].append({"key": key, "build_id": held["build_id"],
                                   "state": held["state"]})
            continue
        readiness = ready_rule(card, project, lanes)
        if not readiness.ready:
            # A card outside the ready status is silent; one with a reason
            # (two lane labels, a broken block) is named so it gets fixed.
            if readiness.reason:
                out["skipped"].append({"key": key, "reason": readiness.reason})
            continue
        result = verbs.dispatch_card(ports, project, card, ACTOR, None, lanes)
        if result["outcome"] == verbs.REFUSED and "cap" in result:
            out["cap"] = result["reason"]
            break
        verbs.audit(ports, project.slug, ACTOR, "dispatch", {"key": key, "reason": None}, result)
        if result["outcome"] == verbs.DISPATCHED:
            out["dispatched"].append({"key": key, "build_id": result["build_id"],
                                      "started": result["started"]})
        else:
            out["skipped"].append({"key": key, "build_id": result.get("build_id"),
                                   "state": result.get("state")})
    return out


def run(ports: verbs.Ports) -> dict:
    """One watch cycle over every configured project."""
    results = []
    for slug in ports.slugs():
        try:
            results.append(cycle(ports, slug))
        except Exception as error:  # one project's failure never stops the others
            results.append({"project": slug, "error": str(error)})
    return {"projects": results}


def register(hatchet, make_ports: Callable[[], verbs.Ports],
             close: Callable[[verbs.Ports], None] | None = None):
    """Declare the watch cron on hatchet and return the task."""
    from hatchet_sdk import Context, EmptyModel

    def watch(input: EmptyModel, ctx: Context) -> dict:
        ports = make_ports()
        try:
            return run(ports)
        finally:
            if close:
                close(ports)

    return hatchet.task(name=TASK, on_crons=[CRON], input_validator=EmptyModel,
                        execution_timeout=EXECUTION, retries=0)(watch)


__all__ = ["ACTOR", "CRON", "INTERVAL", "TASK", "cycle", "register", "run"]
