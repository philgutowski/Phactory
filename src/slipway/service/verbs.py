"""The two write verbs every surface calls: dispatch and land (U25, KTD3, KTD11).

A verb is one action a person or an agent asks Slipway to take. The MCP
tools, the CLI, and the watch cron all call these functions, so a rule lives
here once and every surface keeps it the same way (KTD23).

dispatch(ports, project, key, actor, reason)
    Builds one card. It refuses, with a reason the caller can act on:
    a card that is not ready, such as one without the project's lane label
    (the refusal names the allowed labels); a card whose latest build still
    owns it, returning that build; a card whose last build ended as
    returned_empty or abandoned when no reason is given, since those reopen
    only with a reason (KTD3); and a project at its daily dispatch cap, with
    the cap named. Otherwise it claims the card and starts the card
    workflow's run. The run writes its own id onto the build (KTD3), so a
    run the engine started twice, or a start that failed after the claim,
    still leaves one live run per build: watch retries a claim that never
    got a run, and never claims the card again.

land(ports, project, key, actor, override, reason)
    Pushes card.resume into the card's run. Without override that asks a
    halted or gate_red build for a fresh look; the hourly re verify does the
    same on its own. With override it asks the run to land a commit_mismatch
    halt by hand, which only the operator's subject may do, with a reason.
    Any other caller gets the halt and its host link back, and the refusal
    is audited. land never runs verify itself: the workflow does, inside the
    run (KTD11).

Ownership, in one rule: at most one build per card is outside the ownership
terminal set (scored, abandoned, returned_empty). records.store.claim
enforces it with a partial unique index, so when two callers race, the
database decides and the loser gets the winner's build back (AE9).

The audit table holds one row per dispatch call and one per refused land,
each with the caller's subject, the tool, the redacted arguments (KTD24),
and the result. A land that pushes writes nothing here: its record is the
transition the run makes, which carries the actor and the reason.

Ports is what the verbs reach: the records, the engine, the two config
files, the tracker, and a clock. real_ports builds them from SLIPWAY_HOME
over one Postgres connection; the tests pass fakes.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, Protocol

from psycopg.rows import dict_row

from slipway.cards.contract import ready as ready_rule
from slipway.config import home
from slipway.config.bindings import BindingInvalid
from slipway.config.operator import Host, Operator, load_operator
from slipway.config.project import PROJECTS, Project, Tracker, load_project
from slipway.halts import COMMIT_MISMATCH
from slipway.records import queries, store
from slipway.records.store import Claim, canonical_json
from slipway.redact import scrub
from slipway.service.engine import CARD_RESUME, Engine
from slipway.trackers.base import Card

# The outcomes a verb returns, so a surface can branch without parsing text.
DISPATCHED = "dispatched"  # claimed and the run started
OWNED = "owned"  # another build already owns the card; that build is returned
REFUSED = "refused"  # nothing was claimed or pushed; reason says why
PUSHED = "pushed"  # card.resume was pushed into the run
WAITING = "waiting"  # the build is not in a state land can act on; nothing changed
NOT_FOUND = "not_found"  # the card has no build in this project

TERMINAL = store.OWNERSHIP_TERMINAL
# The two terminal states a card leaves only through dispatch with a reason (KTD3).
REOPEN_NEEDS_REASON = frozenset({"returned_empty", "abandoned"})
# The states whose run waits on card.resume (KTD11; card.resume in gate_red
# asks for a fresh read of the pull request).
RESUMABLE = frozenset({"halted", "gate_red"})

# Where each host shows a pull request. These are the hosts' own addresses,
# not a project's; the project file names the repository.
PULL_REQUEST_LINKS = {
    Host.GITHUB: "https://github.com/{repository}/pull/{number}",
    Host.BITBUCKET: "https://bitbucket.org/{repository}/pull-requests/{number}",
}


class Records(Protocol):
    """The reads and writes the service layer makes on Slipway's records."""

    def project_id(self, slug: str) -> int: ...

    def claim(self, project_id: int, key: str, actor: str, reason: str | None,
              paths: list[str] | None, title: str | None) -> Claim: ...

    def latest(self, project_id: int, key: str) -> dict | None: ...

    def latest_builds(self, project_id: int) -> list[dict]: ...

    def claimed_since(self, project_id: int, since: datetime) -> int: ...

    def entered(self, build_id: int, state: str) -> dict | None: ...

    def card(self, project_id: int, key: str) -> dict | None: ...

    def stale_claims(self, project_id: int, older_than: datetime) -> list[dict]: ...

    def audit(self, slug: str, subject: str, tool: str, arguments: dict, result: dict,
              client: str | None = None) -> None: ...

    def snapshot(self): ...


@dataclass(frozen=True)
class Ports:
    records: Records
    engine: Engine
    project: Callable[[str], Project]
    operator: Callable[[], Operator]
    tracker: Callable[[Project], Any]
    slugs: Callable[[], list[str]]
    clock: Callable[[], datetime]


def host_link(project: Project, pr_number: int | None) -> str | None:
    """The pull request's page on the host, or None before one is open."""
    if not pr_number:
        return None
    return PULL_REQUEST_LINKS[project.host].format(repository=project.repository,
                                                   number=pr_number)


def day_start(now: datetime) -> datetime:
    """Midnight UTC of now's day: the daily dispatch cap counts from here."""
    return datetime.combine(now.astimezone(UTC).date(), time(), tzinfo=UTC)


def other_lanes(ports: Ports, project: Project) -> tuple[str, ...]:
    """The lane labels of every other configured project.

    The note card #47 carried onto this card from the review of #45.

    The ready rule only catches a card labelled for two lanes when it is
    told the other lanes' words, since a project file names its own alone.
    A project file that fails to load is skipped: its lane is unknown, and
    one bad file must not stop every other project's dispatch.
    """
    lanes = []
    for slug in ports.slugs():
        if slug == project.slug:
            continue
        try:
            lane = ports.project(slug).lane_label
        except ValueError:
            continue
        if lane.lower() != project.lane_label.lower():
            lanes.append(lane)
    return tuple(sorted(set(lanes)))


def _build(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {"build_id": row["build_id"], "state": row["state"]}


# dispatch.

def dispatch(ports: Ports, project: str, key: str, actor: str, reason: str | None = None,
             client: str | None = None) -> dict:
    """Claim the card and start its run, or say why not. Every call is audited."""
    found = ports.project(project)
    try:
        card = ports.tracker(found).read(key)
    except (BindingInvalid, ValueError) as error:
        # A tracker credential that does not resolve, or an operator file that
        # fails validation (ConfigError), is refused like an unreadable
        # tracker, so this call still writes its one audit row.
        card = Card(str(key), skipped=str(error))
    result = dispatch_card(ports, found, card, actor, reason, other_lanes(ports, found))
    audit(ports, found.slug, actor, "dispatch", {"key": str(key), "reason": reason}, result,
           client)
    return result


def dispatch_card(ports: Ports, project: Project, card, actor: str, reason: str | None,
                  lanes: tuple[str, ...]) -> dict:
    """dispatch for a card already read from the tracker; watch calls this directly.

    It writes no audit row, so its caller decides: dispatch audits every
    call, and watch audits the dispatches it makes.
    """
    key = str(card.key)
    out = {"project": project.slug, "key": key}
    reason = (reason or "").strip() or None
    # Ownership first: once a build owns the card the board has moved it out
    # of the ready status, and the caller still needs to learn which build,
    # even when the tracker cannot be read.
    project_id = ports.records.project_id(project.slug)
    latest = ports.records.latest(project_id, key)
    if latest and latest["state"] not in TERMINAL:
        return {**out, "outcome": OWNED, **_build(latest),
                "reason": f"build {latest['build_id']} owns card {key} in {latest['state']}"}
    if card.skipped:
        return {**out, "outcome": REFUSED,
                "reason": f"the tracker could not be read: {scrub(card.skipped)}"}
    readiness = ready_rule(card, project, lanes)
    if not readiness.ready:
        why = readiness.reason or (f"card {key} is not in {project.ready_status} "
                                   f"(it is in {card.status or 'no status'})")
        return {**out, "outcome": REFUSED, "reason": why,
                "allowed_labels": [project.lane_label]}
    if latest and latest["state"] in REOPEN_NEEDS_REASON and not reason:
        return {**out, "outcome": REFUSED, **_build(latest),
                "reason": f"card {key}'s last build ended {latest['state']}; "
                          "dispatch it again with a reason"}
    taken = ports.records.claimed_since(project_id, day_start(ports.clock()))
    if taken >= project.daily_dispatch_cap:
        return {**out, "outcome": REFUSED, "cap": project.daily_dispatch_cap, "taken": taken,
                "reason": f"project {project.slug} has dispatched {taken} builds today (UTC), "
                          f"its daily_dispatch_cap of {project.daily_dispatch_cap}"}
    claim = ports.records.claim(project_id, key, actor, reason,
                                list(readiness.block.paths) or None, card.title or None)
    if not claim.won:
        return {**out, "outcome": OWNED, "build_id": claim.build_id, "state": claim.state,
                "reason": f"build {claim.build_id} owns card {key} in {claim.state}"}
    started = start(ports, project.slug, key, claim.build_id)
    return {**out, "outcome": DISPATCHED, "build_id": claim.build_id, "state": claim.state,
            **started}


def start(ports: Ports, slug: str, key: str, build_id: int) -> dict:
    """Start the card workflow's run for a claimed build.

    A start that fails leaves the claim with no run id, which watch retries
    after one interval (KTD3); the caller learns it here and need not retry.
    """
    try:
        return {"started": True, "run_id": ports.engine.start_card(slug, key, build_id)}
    except Exception as error:  # any engine failure leaves the claim for watch to retry
        return {"started": False, "start_error": scrub(str(error))}


# land.

def land(ports: Ports, project: str, key: str, actor: str, override: bool = False,
         reason: str | None = None, client: str | None = None) -> dict:
    """Push card.resume into the card's run, or say why not (KTD11)."""
    found = ports.project(project)
    key = str(key)
    reason = (reason or "").strip() or None
    out = {"project": found.slug, "key": key}
    latest = ports.records.latest(ports.records.project_id(found.slug), key)
    arguments = {"key": key, "override": bool(override), "reason": reason}

    def refuse(why: str, **extra) -> dict:
        result = {**out, "outcome": REFUSED, "reason": why, **extra}
        audit(ports, found.slug, actor, "land", arguments, result, client)
        return result

    if override and not reason:
        return refuse("an override needs a reason")
    if latest is None:
        return {**out, "outcome": NOT_FOUND, "reason": f"card {key} has no build"}
    held = {**_build(latest), "link": host_link(found, latest.get("pr_number"))}
    if latest["state"] == "halted":
        held["halt"] = halt_of(ports, latest["build_id"])
    if override:
        if actor != ports.operator().operator_subject:
            return refuse("only the operator may land with an override", **held)
        held_by = (held.get("halt") or {}).get("class")
        if held_by != COMMIT_MISMATCH:
            return refuse(f"an override lands a {COMMIT_MISMATCH} halt only", **held)
    elif latest["state"] not in RESUMABLE:
        return {**out, "outcome": WAITING, **held,
                "reason": f"build {latest['build_id']} is {latest['state']}; "
                          "land acts on halted and gate_red only"}
    payload = {"actor": actor, "reason": reason, "override": bool(override)}
    ports.engine.push(CARD_RESUME, latest["build_id"], payload)
    return {**out, "outcome": PUSHED, **held}


def halt_of(ports: Ports, build_id: int) -> dict | None:
    """The halt a build entered halted with: its class, its needs you line, its evidence."""
    evidence = ports.records.entered(build_id, "halted")
    if evidence is None:
        return None
    return {"class": evidence.get("halt"), "line": evidence.get("line"),
            "evidence": evidence.get("evidence") or {}}


def scrubbed(value):
    """value with every string inside it passed through redact.scrub (KTD24)."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrubbed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrubbed(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrubbed(v) for v in value)
    return value


def audit(ports: Ports, slug: str, actor: str, tool: str, arguments: dict, result: dict,
          client: str | None = None) -> None:
    """One audit row, its arguments and result scrubbed first (KTD24).

    A result can carry text Slipway did not write, such as a tracker's own
    error, so both sides are scrubbed, not only the caller's free text.
    """
    ports.records.audit(slug, actor, tool, scrubbed(arguments), scrubbed(result), client)


# The records over Postgres.

class PostgresRecords:
    """Records over one autocommit psycopg connection (records.store, records.queries)."""

    def __init__(self, conn):
        self.conn = conn

    def _one(self, query: str, params=()) -> dict | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(query, params).fetchone()

    def project_id(self, slug: str) -> int:
        return store.ensure_project(self.conn, slug)

    def claim(self, project_id, key, actor, reason, paths, title) -> Claim:
        return store.claim(self.conn, project_id, key, actor, reason, paths, title)

    def latest(self, project_id: int, key: str) -> dict | None:
        return self._one(
            "SELECT id AS build_id, state, run_id, pr_number, created_at, updated_at "
            "FROM builds WHERE project_id = %s AND card_key = %s ORDER BY id DESC LIMIT 1",
            (project_id, key))

    def latest_builds(self, project_id: int) -> list[dict]:
        buckets = queries.status_buckets(self.conn, project_id)
        rows = [row for project in buckets.values() for bucket in project.values()
                for row in bucket]
        return sorted(rows, key=lambda row: row["build_id"])

    def claimed_since(self, project_id: int, since: datetime) -> int:
        return self._one("SELECT count(*) AS n FROM builds WHERE project_id = %s "
                         "AND created_at >= %s", (project_id, since))["n"]

    def entered(self, build_id: int, state: str) -> dict | None:
        row = self._one("SELECT evidence FROM transitions WHERE build_id = %s "
                        "AND to_state = %s ORDER BY seq DESC LIMIT 1", (build_id, state))
        return None if row is None else row["evidence"]

    def card(self, project_id: int, key: str) -> dict | None:
        return queries.card_detail(self.conn, project_id, key)

    def stale_claims(self, project_id: int, older_than: datetime) -> list[dict]:
        return [row for row in queries.stale_claims(self.conn, older_than)
                if row["project_id"] == project_id]

    def audit(self, slug, subject, tool, arguments, result, client=None) -> None:
        self.conn.execute(
            "INSERT INTO audit (project_slug, subject, client_name, tool, arguments, result) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (slug, subject, client, tool, canonical_json(arguments),
             canonical_json(result)))

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Every read inside sees one snapshot of the records, so counts and lists agree.

        SET TRANSACTION must open its transaction, so this needs the
        autocommit connection real_ports passes, with no transaction open.
        """
        with self.conn.transaction():
            self.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield


# The real ports, from SLIPWAY_HOME.

def tracker_for(project: Project):
    if project.tracker is Tracker.GITHUB_PROJECTS:
        from slipway.trackers.github import GitHubTracker

        return GitHubTracker(project)
    from slipway.trackers.jira import JiraTracker

    return JiraTracker(project)


def configured_slugs(root: Path | None = None) -> list[str]:
    """Every project with a file under SLIPWAY_HOME/projects, by slug."""
    folder = (root or home()) / PROJECTS
    return sorted(path.stem for path in folder.glob("*.toml"))


def real_ports(conn, engine: Engine, root: Path | None = None) -> Ports:
    """Ports over one connection, reading both config files on every call."""
    root = root or home()
    return Ports(
        records=PostgresRecords(conn),
        engine=engine,
        project=lambda slug: load_project(slug, root),
        operator=lambda: load_operator(root),
        tracker=tracker_for,
        slugs=lambda: configured_slugs(root),
        clock=lambda: datetime.now(UTC),
    )


__all__ = ["DISPATCHED", "NOT_FOUND", "OWNED", "PUSHED", "Ports", "PostgresRecords", "REFUSED",
           "Records", "WAITING", "audit", "configured_slugs", "day_start", "dispatch",
           "dispatch_card", "halt_of", "host_link", "land", "other_lanes", "real_ports",
           "scrubbed", "start"]
