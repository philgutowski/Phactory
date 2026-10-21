"""The slipway terminal, first slice (U21, R25, R33, KTD23).

    slipway onboard <slug>
    slipway dispatch <project> <key> [--reason TEXT]
    slipway status [<project>]
    slipway land <project> <key> [--override --reason TEXT]
    slipway watch <project> [--since ID] [--limit N] [--follow] [--every SECONDS]
    slipway migrate [--database NAME]

Each takes --json before the verb to print the service's result as JSON.

The CLI is a surface: one of the front ends a person reaches Slipway
through, beside the MCP tools (U18) and the status page (U20). Every verb
here is a thin client of the same service function the MCP tools call, so a
rule lives in service once and the terminal keeps it the same way. The CLI
imports service, config, and onboard only, never mcp or workflow (KTD23).

The actor is the subject a verb records as the one who asked. The terminal
is the operator's, so every verb passes the operator's subject from the
operator file (config.operator) as the actor: dispatch's audit row, land's
card.resume payload and refusals, onboard's answers, and the audit rows of
status, watch, and migrate all carry it. The audit rows also name the client
as CLIENT, so a reader can tell the terminal from an agent.

What each verb calls:

    onboard    onboard.terminal.run, U19's question loop over the state machine
    dispatch   service.verbs.dispatch
    status     service.operate.status: status.compose for one project, or
               status.across for every project, grouped as the page groups
               them (needs you, being built, waiting to land, landed today,
               open findings)
    land       service.verbs.land; --override lands a commit mismatch halt
               and needs --reason
    watch      service.operate.tail, then service.operate.events every
               --every seconds with --follow, until Ctrl C
    migrate    service.operate.migrate, which applies records.migrate

Exit codes: 0 when the verb did its work or found nothing to do, 1 when the
service refused it or the card has no build, 2 for a usage or configuration
error. dispatch and land refusals carry what to do next, such as the allowed
labels for a card with none.

main takes a Wiring, so the tests drive the entry point with fakes:
real_wiring reads SLIPWAY_HOME for the config files and SLIPWAY_DSN for the
records, and starts runs through the Hatchet engine.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from slipway.config import home
from slipway.config.bindings import BindingInvalid
from slipway.config.operator import load_operator
from slipway.onboard import machine, terminal
from slipway.service import operate, verbs

PROG = "slipway"
# The client name the audit rows carry for a call made from the terminal.
CLIENT = "slipway-cli"
DSN_VARIABLE = terminal.DSN_VARIABLE

OK, REFUSED_EXIT, USAGE = 0, 1, 2
# The default seconds between reads of watch --follow.
EVERY = 5.0

# The page's groups, in the page's order, with its titles and empty lines (U20).
GROUPS = (
    ("needs_you", "Needs you", "Nothing needs you."),
    ("being_built", "Being built", "Nothing is being built."),
    ("waiting_to_land", "Waiting to land", "Nothing is waiting to land."),
    ("landed", "Landed today", "Nothing landed today."),
    ("findings", "Open findings", "No open findings."),
)


class Unusable(Exception):
    """The CLI cannot run the verb as configured, such as with no database named."""


@dataclass
class Wiring:
    """What the CLI reaches: the config root, the service ports, onboard's ports, and IO.

    service and onboard are called only when a verb needs them, so onboard
    runs with no database named and a typo costs no connection.
    """

    root: Path
    service: Callable[[], verbs.Ports]
    onboard: Callable[[str], machine.Ports]
    read: Callable[[str], str] = input
    write: Callable[[str], None] = print
    sleep: Callable[[float], None] = time.sleep


def real_wiring() -> Wiring:
    root = home()

    def service() -> verbs.Ports:
        dsn = os.environ.get(DSN_VARIABLE, "").strip()
        if not dsn:
            raise Unusable(f"set {DSN_VARIABLE} to the records database's connection string")
        import psycopg

        from slipway.service.engine import HatchetEngine

        try:
            conn = psycopg.connect(dsn, autocommit=True)
        except psycopg.Error as error:
            raise Unusable(f"the records database could not be reached: {error}") from None
        return verbs.real_ports(conn, HatchetEngine(), root)

    return Wiring(root=root, service=service,
                  onboard=lambda slug: terminal.real_ports(slug, root))


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog=PROG, description="Run Slipway from the terminal.")
    top.add_argument("--json", action="store_true", help="print the result as JSON")
    verb = top.add_subparsers(dest="verb", required=True, metavar="verb")

    p = verb.add_parser("onboard", help="onboard a project, one question at a time")
    p.add_argument("slug", help="the project's short name, as in its MCP URL")

    p = verb.add_parser("dispatch", help="claim a card and start its build")
    p.add_argument("project", help="the project's slug")
    p.add_argument("key", help="the card's key on the tracker, such as EX-12 or 24")
    p.add_argument("--reason", help="why; needed to reopen a card that ended empty or abandoned")

    p = verb.add_parser("status", help="what is going on, grouped as the page groups it")
    p.add_argument("project", nargs="?", help="one project's slug; every project when left out")

    p = verb.add_parser("land", help="ask a halted or gate red build to land")
    p.add_argument("project", help="the project's slug")
    p.add_argument("key", help="the card's key on the tracker")
    p.add_argument("--override", action="store_true",
                   help="land a commit mismatch halt by hand; needs --reason")
    p.add_argument("--reason", help="why; required with --override")

    p = verb.add_parser("watch", help="print the project's events as they happen")
    p.add_argument("project", help="the project's slug")
    p.add_argument("--since", type=int, default=0, metavar="ID",
                   help="start after this event id (default: the beginning)")
    p.add_argument("--limit", type=int, default=operate.EVENTS_LIMIT, metavar="N",
                   help="at most this many events per read")
    p.add_argument("--follow", action="store_true", help="keep reading until Ctrl C")
    p.add_argument("--every", type=float, default=EVERY, metavar="SECONDS",
                   help="seconds between reads with --follow")

    p = verb.add_parser("migrate", help="apply the pending record migrations")
    p.add_argument("--database", default="slipway",
                   help="refuse unless the connection's database has this name")
    return top


# What each verb prints.

def _dump(result, write) -> None:
    write(json.dumps(result, indent=2, sort_keys=True, default=str))


def _refused(result: dict, write) -> int:
    write(f"Refused: {result['reason']}")
    if result.get("allowed_labels"):
        write(f"  Allowed labels: {', '.join(result['allowed_labels'])}")
    halt = result.get("halt") or {}
    if halt.get("line"):
        write(f"  Halt: {halt['line']}")
    if result.get("link"):
        write(f"  {result['link']}")
    return REFUSED_EXIT


def show_dispatch(result: dict, write) -> int:
    outcome = result["outcome"]
    if outcome == verbs.REFUSED:
        return _refused(result, write)
    if outcome == verbs.OWNED:
        write(f"Already owned: {result['reason']}.")
        return OK
    write(f"Dispatched {result['key']} as build {result['build_id']}.")
    if result.get("started"):
        write(f"  Run {result['run_id']} started.")
    else:
        write(f"  The run did not start ({result.get('start_error')}); "
              "watch starts it again within one interval.")
    return OK


def show_land(result: dict, write) -> int:
    outcome = result["outcome"]
    if outcome == verbs.REFUSED:
        return _refused(result, write)
    if outcome == verbs.NOT_FOUND:
        write(f"Not found: {result['reason']}.")
        return REFUSED_EXIT
    if outcome == verbs.WAITING:
        write(f"Nothing to land: {result['reason']}.")
    else:
        write(f"Asked build {result['build_id']} ({result['state']}) to land.")
    if result.get("link"):
        write(f"  {result['link']}")
    return OK


def _rows(found: dict) -> dict[str, list[str]]:
    """Each group's lines for one project, from status.compose's result."""

    def card(entry: dict) -> str:
        parts = [entry["key"], entry["state"].replace("_", " "), entry.get("age") or "",
                 entry.get("link") or ""]
        return "  ".join(part for part in parts if part)

    rows = {name: [] for name, _, _ in GROUPS}
    for entry in found["needs_you"]:
        rows["needs_you"] += [card(entry), f"    {entry['line']}",
                              f"    What to do: {entry['what']}"]
    for entry in found["cards"]:
        if entry["bucket"] in ("being_built", "waiting_to_land"):
            rows[entry["bucket"]].append(card(entry))
    for entry in found.get("landed", []):
        scores = entry["scores"]
        rows["landed"] += [card(entry), "    Deterministic: "
                           f"{scores.get('deterministic') or 'not scored yet'}. Judged: "
                           f"{scores.get('judged') or 'not scored yet'}."]
    for entry in found.get("findings", []):
        rows["findings"].append("  ".join(
            part for part in (entry["class"], entry["fingerprint"], entry.get("age") or "")
            if part))
        if entry.get("excerpt"):
            rows["findings"].append(f"    {entry['excerpt']}")
    return rows


def _counted(found: dict) -> dict[str, int]:
    return {"needs_you": len(found["needs_you"]),
            "being_built": sum(1 for c in found["cards"] if c["bucket"] == "being_built"),
            "waiting_to_land": sum(1 for c in found["cards"]
                                   if c["bucket"] == "waiting_to_land"),
            "landed": len(found.get("landed", [])), "findings": len(found.get("findings", []))}


def show_project(found: dict, write, indent: str = "") -> None:
    rows, counts = _rows(found), _counted(found)
    for name, title, empty in GROUPS:
        write(f"{indent}{title} ({counts[name]})")
        for line in rows[name] or [empty]:
            write(f"{indent}  {line}")


def show_status(result: dict, write) -> int:
    if result.get("outcome") == verbs.REFUSED:
        return _refused(result, write)
    write(result["spoken"])
    if "projects" not in result:
        write("")
        show_project(result, write)
        return OK
    for bad in result["unreadable"]:
        write(f"Could not read {bad['project']}: {bad['error']}")
    for found in result["projects"]:
        write("")
        write(f"{found['project']}: {found['spoken']}")
        show_project(found, write, "  ")
    return OK


def show_events(result: dict, write) -> None:
    if result.get("truncated"):
        write("Some events before this cursor were pruned.")
    for event in result["events"]:
        change = (f"{event['from']} to {event['to']}" if event.get("from")
                 else f"entered {event['to']}")
        parts = [f"#{event['id']}", str(event.get("at") or ""), event["key"], change]
        if event.get("actor"):
            parts.append(f"by {event['actor']}")
        line = "  ".join(part for part in parts if part)
        write(f"{line}: {event['said']}" if event.get("said") else line)


def show_migrate(result: dict, write) -> int:
    if result["outcome"] == verbs.REFUSED:
        return _refused(result, write)
    applied = result["applied"]
    write("Applied: " + (", ".join(applied) if applied else "nothing, the ledger is current"))
    return OK


# The verbs.

def _onboard(wiring: Wiring, args, actor: str) -> int:
    return terminal.run(wiring.onboard(args.slug), args.slug, actor, wiring.read, wiring.write)


def _dispatch(wiring: Wiring, args, actor: str):
    result = verbs.dispatch(wiring.service(), args.project, args.key, actor, args.reason,
                            client=CLIENT)
    return result, show_dispatch


def _status(wiring: Wiring, args, actor: str):
    return operate.status(wiring.service(), args.project, actor, CLIENT), show_status


def _land(wiring: Wiring, args, actor: str):
    result = verbs.land(wiring.service(), args.project, args.key, actor, args.override,
                        args.reason, client=CLIENT)
    return result, show_land


def _watch(wiring: Wiring, args, actor: str) -> int:
    ports, write = wiring.service(), wiring.write
    emit = (lambda result: _dump(result, write)) if args.json else (
        lambda result: show_events(result, write))
    result = operate.tail(ports, args.project, actor, args.since, args.limit, CLIENT)
    if result["outcome"] == verbs.REFUSED:
        if args.json:
            _dump(result, write)
            return REFUSED_EXIT
        return _refused(result, write)
    emit(result)
    try:
        while args.follow:
            wiring.sleep(args.every)
            result = operate.events(ports, args.project, result["cursor"], args.limit)
            if result["events"] or result["truncated"]:
                emit(result)
    except KeyboardInterrupt:
        pass
    return OK


def _migrate(wiring: Wiring, args, actor: str):
    return operate.migrate(wiring.service(), actor, args.database, CLIENT), show_migrate


VERBS = {"onboard": _onboard, "dispatch": _dispatch, "status": _status, "land": _land,
         "watch": _watch, "migrate": _migrate}
# The outcomes that exit non zero when --json prints the result instead.
NOT_DONE = frozenset({verbs.REFUSED, verbs.NOT_FOUND})


def main(argv: list[str] | None = None, wiring: Wiring | None = None) -> int:
    """Run one verb; the exit code is as the module says."""
    args = parser().parse_args(argv)
    wiring = wiring or real_wiring()
    write = wiring.write
    try:
        actor = load_operator(wiring.root).operator_subject
        done = VERBS[args.verb](wiring, args, actor)
    except (Unusable, ValueError, BindingInvalid) as error:
        print(f"{PROG} {args.verb}: {error}", file=sys.stderr)
        return USAGE
    if isinstance(done, int):
        return done
    result, show = done
    if args.json:
        _dump(result, write)
        return REFUSED_EXIT if result.get("outcome") in NOT_DONE else OK
    return show(result, write)


if __name__ == "__main__":
    sys.exit(main())
