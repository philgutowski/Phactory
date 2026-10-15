"""Writes to Phactory's records: claims, transitions, host events, runner builds.

Every function takes an autocommit psycopg connection and runs its own
transaction with conn.transaction(), so it commits or rolls back as a whole.
Called inside a transaction the caller opened, it becomes a savepoint and
commits with the caller's.

Lock order, the same in every function that takes both, so two writers never
deadlock: first the project's advisory lock, then the build's row lock.

The project advisory lock is a transaction level lock keyed on the project
id. Every write that inserts a transitions row holds it, so within one
project transitions.id values are assigned and committed in order, which is
what lets events_since(project, cursor) read with a strictly greater cursor
and never skip a row that commits late.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import sql

OWNERSHIP_TERMINAL = frozenset({"scored", "abandoned", "returned_empty"})
PROJECT_LOCK_SPACE = 0x70726F6A  # first half of the two key advisory lock: "this is a project"

# The claim columns a transition may write (KTD16, claim before act).
CLAIM_COLUMNS = frozenset({
    "branch", "session_ref", "pr_number", "check_run_id", "trace_id",
    "confirmed_commit", "tested_commit", "runner_commit",
})

HOST_EVENT_SOURCES = frozenset({"webhook", "poll"})
# The normalised event fields host_events stores as columns. The whole
# normalised mapping is also stored as canonical JSON text.
HOST_EVENT_FIELDS = ("host", "delivery_id", "repository", "pr_number", "kind",
                     "merge_commit", "head_commit")


@dataclass(frozen=True)
class Claim:
    build_id: int
    state: str
    won: bool
    run_id: str | None


@dataclass(frozen=True)
class Transition:
    """The outcome of record_transition.

    recorded is False when the build had already left from_state, which is
    what a replayed step sees; state is then the build's current state.
    """

    recorded: bool
    state: str
    seq: int | None

    @property
    def already_past(self) -> bool:
        return not self.recorded


@dataclass(frozen=True)
class Attach:
    attached: bool
    run_id: str


@dataclass(frozen=True)
class Ingest:
    inserted: bool
    event_id: int


def canonical_json(value: Any) -> str:
    """One text form for a mapping, so equal events are byte equal rows."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def lock_project(conn, project_id: int) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (PROJECT_LOCK_SPACE, project_id))


def ensure_project(conn, slug: str) -> int:
    """Return the id of the project with this slug, creating the row if needed."""
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO projects (slug) VALUES (%s) ON CONFLICT (slug) DO NOTHING RETURNING id",
            (slug,)).fetchone()
        if row:
            return row[0]
        return conn.execute("SELECT id FROM projects WHERE slug = %s", (slug,)).fetchone()[0]


def claim(conn, project_id: int, card_key: str, actor: str, reason: str | None = None,
          paths: list[str] | None = None, title: str | None = None) -> Claim:
    """Claim a card for a new build, or return the build that already owns it (KTD3).

    The insert and the claim transition (seq 0, to claimed) commit together.
    When another build outside the ownership terminal set exists, nothing is
    written and the result carries that build's id and state with won False.
    The partial unique index decides a race the advisory lock did not.
    """
    with conn.transaction():
        lock_project(conn, project_id)
        conn.execute(
            "INSERT INTO cards (project_id, card_key, title, paths) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (project_id, card_key) DO UPDATE SET "
            "title = COALESCE(EXCLUDED.title, cards.title), "
            "paths = COALESCE(EXCLUDED.paths, cards.paths)",
            (project_id, card_key, title, None if paths is None else json.dumps(paths)))
        row = conn.execute(
            "INSERT INTO builds (project_id, card_key, state, claimed_by, claim_reason) "
            "VALUES (%s, %s, 'claimed', %s, %s) "
            "ON CONFLICT (project_id, card_key) "
            "WHERE state NOT IN ('scored', 'abandoned', 'returned_empty') DO NOTHING "
            "RETURNING id",
            (project_id, card_key, actor, reason)).fetchone()
        if row:
            conn.execute(
                "INSERT INTO transitions (project_id, build_id, seq, from_state, to_state, "
                "actor, evidence) VALUES (%s, %s, 0, NULL, 'claimed', %s, %s)",
                (project_id, row[0], actor, canonical_json({"reason": reason})))
            return Claim(row[0], "claimed", True, None)
        build_id, state, run_id = conn.execute(
            "SELECT id, state, run_id FROM builds WHERE project_id = %s AND card_key = %s "
            "AND state NOT IN ('scored', 'abandoned', 'returned_empty')",
            (project_id, card_key)).fetchone()
        return Claim(build_id, state, False, run_id)


def attach_run(conn, build_id: int, run_id: str) -> Attach:
    """Write the engine's run id onto the build once (KTD3).

    A compare and set on a NULL run id: the first caller wins, a later caller
    gets attached False and the run id already there, and exits as a no op.
    """
    with conn.transaction():
        row = conn.execute(
            "UPDATE builds SET run_id = %s, updated_at = now() "
            "WHERE id = %s AND run_id IS NULL RETURNING run_id",
            (run_id, build_id)).fetchone()
        if row:
            return Attach(True, row[0])
        existing = conn.execute("SELECT run_id FROM builds WHERE id = %s", (build_id,)).fetchone()
        if existing is None:
            raise LookupError(f"no build {build_id}")
        return Attach(False, existing[0])


def record_transition(conn, build_id: int, from_state: str, to_state: str, actor: str,
                      evidence: dict | None = None, claims: dict | None = None) -> Transition:
    """Move a build from from_state to to_state and write the transition row.

    The build update is a compare and set on from_state: when the build is
    no longer there, as on a replayed step, nothing is written and the result
    is already past. Otherwise seq is the build's last_seq plus one, taken
    under the row lock the update holds, and the claim columns in claims are
    written in the same transaction. An edge not in allowed_edges is refused
    by the database (the trigger on builds and the foreign key on
    transitions), which raises psycopg.errors.IntegrityError or CheckViolation.

    evidence holds structured fields only (keys, commits, numbers) and is
    expected to have passed the redaction module before it reaches here.
    """
    claims = dict(claims or {})
    unknown = set(claims) - CLAIM_COLUMNS
    if unknown:
        raise ValueError(f"not claim columns: {sorted(unknown)}")
    with conn.transaction():
        found = conn.execute("SELECT project_id FROM builds WHERE id = %s",
                             (build_id,)).fetchone()
        if found is None:
            raise LookupError(f"no build {build_id}")
        project_id = found[0]
        lock_project(conn, project_id)
        assignments = [sql.SQL("state = %s"), sql.SQL("last_seq = last_seq + 1"),
                       sql.SQL("updated_at = now()")]
        values: list[Any] = [to_state]
        for column in sorted(claims):
            assignments.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
            values.append(claims[column])
        query = sql.SQL("UPDATE builds SET {} WHERE id = %s AND state = %s "
                        "RETURNING last_seq").format(sql.SQL(", ").join(assignments))
        row = conn.execute(query, [*values, build_id, from_state]).fetchone()
        if row is None:
            current = conn.execute("SELECT state FROM builds WHERE id = %s",
                                   (build_id,)).fetchone()[0]
            return Transition(False, current, None)
        seq = row[0]
        conn.execute(
            "INSERT INTO transitions (project_id, build_id, seq, from_state, to_state, actor, "
            "evidence) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (project_id, build_id, seq, from_state, to_state, actor,
             canonical_json(evidence or {})))
        return Transition(True, to_state, seq)


def ingest_host_event(conn, source: str, normalised: dict) -> Ingest:
    """Write one normalised host event, deduplicated (KTD10).

    The webhook endpoint and the poll path both call this, so the row depends
    only on the normalised mapping and the source. normalised carries
    project_id plus HOST_EVENT_FIELDS; delivery_id is None for a polled
    event. An event already present by (host, delivery id), or by (project,
    pull request, kind, merge commit), is not written again and the result
    names the existing row.
    """
    if source not in HOST_EVENT_SOURCES:
        raise ValueError(f"unknown host event source {source!r}")
    event = {field: normalised.get(field) for field in HOST_EVENT_FIELDS}
    project_id = normalised["project_id"]
    body = canonical_json(normalised)
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO host_events (project_id, source, host, delivery_id, repository, "
            "pr_number, kind, merge_commit, head_commit, normalised) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (project_id, source, event["host"], event["delivery_id"], event["repository"],
             event["pr_number"], event["kind"], event["merge_commit"], event["head_commit"],
             body)).fetchone()
        if row:
            return Ingest(True, row[0])
        existing = conn.execute(
            "SELECT id FROM host_events WHERE (host = %s AND delivery_id = %s) "
            "OR (project_id = %s AND pr_number = %s AND kind = %s AND merge_commit = %s) "
            "ORDER BY id LIMIT 1",
            (event["host"], event["delivery_id"], project_id, event["pr_number"],
             event["kind"], event["merge_commit"])).fetchone()
        return Ingest(False, existing[0])


def activate_runner_build(conn, instance: str, runner_commit: str, at: datetime) -> int:
    """Make runner_commit the active build on instance from the instant at.

    The build active until now becomes previous with deactivated_at set to
    the same instant, so the two activation intervals meet without a gap.
    Returns the new row's id.
    """
    with conn.transaction():
        conn.execute(
            "UPDATE runner_builds SET state = 'previous', deactivated_at = %s "
            "WHERE instance = %s AND state = 'active'", (at, instance))
        return conn.execute(
            "INSERT INTO runner_builds (instance, runner_commit, state, activated_at) "
            "VALUES (%s, %s, 'active', %s) RETURNING id",
            (instance, runner_commit, at)).fetchone()[0]


def record_gate_run(conn, project_id: int, host: str, host_run_id: str, instance: str,
                    started_at: datetime, head_commit: str | None = None,
                    conclusion: str | None = None,
                    completed_at: datetime | None = None) -> int:
    """Write a gate run once per (host, host run id) and return its id.

    runner_build_id is the runner build whose activation interval contains
    started_at on that instance (KTD20). A repeat write fills a conclusion
    the first write did not have.
    """
    with conn.transaction():
        return conn.execute(
            "INSERT INTO gate_runs (project_id, host, host_run_id, instance, runner_build_id, "
            "head_commit, conclusion, started_at, completed_at) "
            "VALUES (%s, %s, %s, %s, ("
            "  SELECT id FROM runner_builds WHERE instance = %s AND activated_at <= %s "
            "  AND (deactivated_at IS NULL OR deactivated_at > %s) "
            "  ORDER BY activated_at DESC LIMIT 1"
            "), %s, %s, %s, %s) "
            "ON CONFLICT (host, host_run_id) DO UPDATE SET "
            "conclusion = COALESCE(gate_runs.conclusion, EXCLUDED.conclusion), "
            "completed_at = COALESCE(gate_runs.completed_at, EXCLUDED.completed_at) "
            "RETURNING id",
            (project_id, host, host_run_id, instance, instance, started_at, started_at,
             head_commit, conclusion, started_at, completed_at)).fetchone()[0]
