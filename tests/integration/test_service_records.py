"""U25's verbs, status, and watch over the real records in Postgres.

The unit tests drive the verbs through in memory records; this file runs the
same scenarios through service.verbs.PostgresRecords on the schema the
migrations build, so the SQL the fakes stand in for is exercised too. Each
test gets its own schema, as in test_records.py. The engine and the tracker
stay fakes.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from _service_fakes import MANAGER, OPERATOR, SLUG, ready_card, world
from slipway.records import migrate, store
from slipway.service import status, verbs
from slipway.workflow import watch

pytestmark = pytest.mark.integration

HALT = {"halt": "commit_mismatch", "line": "commit mismatch: the merge is not the confirmed one",
        "evidence": {"expected": "a" * 40, "found": "c" * 40}}


@pytest.fixture
def conn():
    dsn = os.environ.get("SLIPWAY_TEST_DSN", "")
    if not dsn:
        pytest.skip("SLIPWAY_TEST_DSN is not set")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(dsn, autocommit=True, client_encoding="utf8")
    admin.execute(f"CREATE SCHEMA {schema}")
    c = psycopg.connect(dsn, autocommit=True, client_encoding="utf8",
                        options=f"-c search_path={schema}")
    try:
        migrate.apply(c)
        yield c
    finally:
        c.close()
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


@pytest.fixture
def w(tmp_path, conn):
    found = world(tmp_path, [ready_card("EX-1")])
    found.engine.records = None  # attach_run is the workflow's; tests write it by hand
    found.clock.now = datetime.now(UTC)
    ports = verbs.Ports(**{**found.ports.__dict__, "records": verbs.PostgresRecords(conn)})
    found.ports = ports
    return found


def _walk(conn, build_id, *states):
    current = conn.execute("SELECT state FROM builds WHERE id = %s", (build_id,)).fetchone()[0]
    for state, evidence in states:
        assert store.record_transition(conn, build_id, current, state, "test", evidence).recorded
        current = state


def _audit(conn):
    return conn.execute("SELECT subject, tool, arguments, result FROM audit ORDER BY id"
                        ).fetchall()


def test_three_dispatches_make_one_claim_one_run_three_audit_rows(w, conn):
    results = [verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR) for _ in range(3)]

    assert [r["outcome"] for r in results] == [verbs.DISPATCHED, verbs.OWNED, verbs.OWNED]
    assert len({r["build_id"] for r in results}) == 1
    assert conn.execute("SELECT count(*) FROM builds").fetchone()[0] == 1
    assert len(w.engine.starts) == 1
    rows = _audit(conn)
    assert [(s, t) for s, t, _, _ in rows] == [(OPERATOR, "dispatch")] * 3
    assert rows[1][3]["outcome"] == verbs.OWNED


def test_the_daily_cap_counts_todays_claims(w, conn):
    project_id = store.ensure_project(conn, SLUG)
    cap = w.ports.project(SLUG).daily_dispatch_cap
    for n in range(cap):
        claim = store.claim(conn, project_id, f"EX-{n + 10}", "test")
        _walk(conn, claim.build_id, ("abandoned", {}))

    result = verbs.dispatch(w.ports, SLUG, "EX-1", OPERATOR)

    assert result["outcome"] == verbs.REFUSED and result["cap"] == cap


def test_land_refusal_is_audited_and_the_operator_override_writes_nothing(w, conn):
    project_id = store.ensure_project(conn, SLUG)
    build_id = store.claim(conn, project_id, "EX-1", "test").build_id
    _walk(conn, build_id, ("dispatched", {}), ("building", {}), ("returned", {}),
          ("pr_open", {}), ("awaiting_approval", {}), ("queued", {}), ("gate_running", {}),
          ("merged", {}), ("verifying", {}), ("halted", HALT))
    conn.execute("UPDATE builds SET pr_number = 7 WHERE id = %s", (build_id,))
    transitions = conn.execute("SELECT count(*) FROM transitions").fetchone()[0]

    refused = verbs.land(w.ports, SLUG, "EX-1", MANAGER, override=True, reason="please")
    pushed = verbs.land(w.ports, SLUG, "EX-1", OPERATOR, override=True, reason="squashed")

    assert refused["outcome"] == verbs.REFUSED and refused["halt"]["class"] == "commit_mismatch"
    assert refused["link"].endswith("/pull/7")
    assert pushed["outcome"] == verbs.PUSHED and len(w.engine.pushes) == 1
    assert [(s, t) for s, t, _, _ in _audit(conn)] == [(MANAGER, "land")]
    assert conn.execute("SELECT count(*) FROM transitions").fetchone()[0] == transitions


def test_compose_agrees_with_the_card_query(w, conn):
    project_id = store.ensure_project(conn, SLUG)
    for key in ("EX-1", "EX-2", "EX-3"):
        _walk(conn, store.claim(conn, project_id, key, "test").build_id,
              ("dispatched", {}), ("building", {}))
    _walk(conn, store.claim(conn, project_id, "EX-4", "test").build_id,
          ("dispatched", {}), ("building", {}), ("returned", {}), ("pr_open", {}))
    halted = store.claim(conn, project_id, "EX-5", "test").build_id
    _walk(conn, halted, ("dispatched", {}), ("building", {}), ("returned", {}),
          ("pr_open", {}), ("awaiting_approval", {}), ("queued", {}), ("gate_running", {}),
          ("merged", {}), ("verifying", {}), ("halted", HALT))

    found = status.compose(w.ports, SLUG)

    assert found["counts"] == {"being_built": 3, "waiting_to_land": 1, "needs_you": 1,
                               "landed_today": 0}
    assert "3 being built" in found["spoken"] and "1 waiting to land" in found["spoken"]
    assert "EX-5 is halted: commit mismatch" in found["spoken"]
    records = w.ports.records
    for entry in found["cards"]:
        latest = records.card(project_id, entry["key"])["builds"][0]
        assert (entry["build_id"], entry["state"]) == (latest["id"], latest["state"])


def test_watch_retries_a_stale_claim_without_claiming_again(w, conn):
    project_id = store.ensure_project(conn, SLUG)
    build_id = store.claim(conn, project_id, "EX-1", "watch").build_id
    conn.execute("UPDATE builds SET updated_at = now() - interval '6 minutes' WHERE id = %s",
                 (build_id,))

    out = watch.cycle(w.ports, SLUG)

    assert out["retried"] == [{"key": "EX-1", "build_id": build_id, "started": True,
                               "run_id": "run-1"}]
    assert out["dispatched"] == []
    assert conn.execute("SELECT count(*) FROM builds").fetchone()[0] == 1
    assert w.engine.starts == [(SLUG, "EX-1", build_id)]


def test_a_fresh_claim_is_not_retried(w, conn):
    project_id = store.ensure_project(conn, SLUG)
    store.claim(conn, project_id, "EX-1", "watch")
    w.clock.now = datetime.now(UTC) + timedelta(seconds=30)

    assert watch.cycle(w.ports, SLUG)["retried"] == []


def test_compose_reads_scores_and_open_findings_from_postgres(w, conn):
    """U20: the page's landed row and findings come from the scores and findings tables."""
    project_id = store.ensure_project(conn, SLUG)
    landed = store.claim(conn, project_id, "EX-6", "test").build_id
    _walk(conn, landed, ("dispatched", {}), ("building", {}), ("returned", {}),
          ("pr_open", {}), ("awaiting_approval", {}), ("queued", {}), ("gate_running", {}),
          ("merged", {}), ("verifying", {}), ("landed", {}), ("scored", {}))
    store.record_score(conn, landed, "deterministic",
                       {"total": {"verdict": "pass", "passed": 3, "failed": 0, "of": 3}})
    conn.execute("INSERT INTO findings (project_id, build_id, class, fingerprint, excerpt) "
                 "VALUES (%s, %s, 'runner_step', 'fp-1', 'step 4 failed')",
                 (project_id, landed))
    conn.execute("INSERT INTO findings (project_id, class, fingerprint, state) "
                 "VALUES (%s, 'runner_step', 'fp-2', 'closed')", (project_id,))

    found = status.compose(w.ports, SLUG)

    assert found["counts"]["landed_today"] == 1
    [row] = found["landed"]
    assert row["key"] == "EX-6"
    assert row["scores"] == {"deterministic": "pass, 3 of 3", "judged": None}
    assert [(f["fingerprint"], f["excerpt"]) for f in found["findings"]] == [
        ("fp-1", "step 4 failed")]
