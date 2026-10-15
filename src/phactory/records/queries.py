"""Reads over Phactory's records, the ones service composes into status and tools.

Each function takes a psycopg connection and returns plain dicts, so a caller
needs no knowledge of the schema. None of them writes.
"""

from datetime import datetime

from psycopg.rows import dict_row

# KTD4: the in flight files fact counts cards from dispatched to merged.
IN_FLIGHT_STATES = ("dispatched", "building", "returned", "pr_open", "awaiting_approval",
                    "queued", "gate_running", "gate_red", "merged")

_LATEST_BUILDS = """
SELECT DISTINCT ON (b.project_id, b.card_key)
       p.slug AS project, b.project_id, b.card_key, b.id AS build_id, b.state, s.bucket,
       b.pr_number, b.updated_at
FROM builds b
JOIN projects p ON p.id = b.project_id
JOIN states s ON s.name = b.state
{where}
ORDER BY b.project_id, b.card_key, b.id DESC
"""


def _all(conn, query, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def _one(conn, query, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchone()


def status_buckets(conn, project_id: int | None = None) -> dict[str, dict[str, list[dict]]]:
    """Each card's latest build, grouped by project slug then by page bucket.

    With project_id, only that project; without, every project.
    """
    where, params = ("WHERE b.project_id = %s", (project_id,)) if project_id else ("", ())
    out: dict[str, dict[str, list[dict]]] = {}
    for row in _all(conn, _LATEST_BUILDS.format(where=where), params):
        out.setdefault(row["project"], {}).setdefault(row["bucket"], []).append(row)
    return out


def card_detail(conn, project_id: int, card_key: str) -> dict | None:
    """The card with every build, newest first, each with transitions, attempts, and scores."""
    card = _one(conn, "SELECT * FROM cards WHERE project_id = %s AND card_key = %s",
                (project_id, card_key))
    if card is None:
        return None
    builds = _all(conn, "SELECT * FROM builds WHERE project_id = %s AND card_key = %s "
                        "ORDER BY id DESC", (project_id, card_key))
    for build in builds:
        build["transitions"] = _all(conn, "SELECT * FROM transitions WHERE build_id = %s "
                                          "ORDER BY seq", (build["id"],))
        build["attempts"] = _all(conn, "SELECT * FROM attempts WHERE build_id = %s ORDER BY id",
                                 (build["id"],))
        build["scores"] = _all(conn, "SELECT * FROM scores WHERE build_id = %s ORDER BY kind",
                               (build["id"],))
    card["builds"] = builds
    return card


def events_since(conn, project_id: int, cursor: int = 0, limit: int = 100) -> dict:
    """Transitions of one project with id strictly greater than cursor, in id order.

    The result carries rows, the cursor to pass next time, and truncated,
    which is True when the cursor is below the project's events floor: rows
    the caller never saw may have been pruned.
    """
    floor = _one(conn, "SELECT events_floor FROM projects WHERE id = %s", (project_id,))
    if floor is None:
        raise LookupError(f"no project {project_id}")
    rows = _all(conn,
                "SELECT t.id, t.build_id, b.card_key, t.seq, t.from_state, t.to_state, t.actor, "
                "t.evidence, t.at FROM transitions t JOIN builds b ON b.id = t.build_id "
                "WHERE t.project_id = %s AND t.id > %s ORDER BY t.id LIMIT %s",
                (project_id, cursor, limit))
    return {
        "rows": rows,
        "cursor": rows[-1]["id"] if rows else cursor,
        "truncated": cursor < floor["events_floor"],
    }


def in_flight_files(conn, project_id: int) -> list[dict]:
    """Cards from dispatched to merged with their declared paths.

    paths is None for a card that declared none (KTD5 "not declared").
    """
    return _all(conn,
                "SELECT b.card_key, b.id AS build_id, b.state, c.paths FROM builds b "
                "JOIN cards c ON c.project_id = b.project_id AND c.card_key = b.card_key "
                "WHERE b.project_id = %s AND b.state = ANY(%s) ORDER BY b.id",
                (project_id, list(IN_FLIGHT_STATES)))


def open_findings(conn, project_id: int) -> list[dict]:
    return _all(conn, "SELECT * FROM findings WHERE project_id = %s AND state = 'open' "
                      "ORDER BY id", (project_id,))


def stale_claims(conn, older_than: datetime) -> list[dict]:
    """Claimed builds with no run id, last touched before older_than (KTD3).

    Watch retries these rather than claiming the card again.
    """
    return _all(conn, "SELECT * FROM builds WHERE state = 'claimed' AND run_id IS NULL "
                      "AND updated_at < %s ORDER BY id", (older_than,))


def latest_gate_run(conn, project_id: int, instance: str, runner_commit: str) -> dict | None:
    """This project's latest gate run on instance attributed to a runner build of runner_commit."""
    return _one(conn,
                "SELECT g.*, r.runner_commit FROM gate_runs g "
                "JOIN runner_builds r ON r.id = g.runner_build_id "
                "WHERE g.project_id = %s AND g.instance = %s AND r.runner_commit = %s "
                "ORDER BY g.started_at DESC, g.id DESC LIMIT 1",
                (project_id, instance, runner_commit))


def runner_build_active_at(conn, instance: str, at: datetime) -> dict | None:
    """The runner build whose activation interval on instance contains the instant at."""
    return _one(conn,
                "SELECT * FROM runner_builds WHERE instance = %s AND activated_at <= %s "
                "AND (deactivated_at IS NULL OR deactivated_at > %s) "
                "ORDER BY activated_at DESC LIMIT 1",
                (instance, at, at))
