"""U22's runner records against the local Postgres named by SLIPWAY_TEST_DSN.

The unit tests drive the cron and the evaluator over FakeRecords; these run
the same paths over PostgresRecords, so the SQL and migration 005 are held
to what the fakes promise. Each test gets its own schema, as in
test_records.py, and skips without the variable (a failure under CI).
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from _runner_fakes import A, B, C, FakeApi, FakeScripts, operator
from _runner_fakes import instance as runner_instance
from slipway.records import migrate, store
from slipway.runner import instances, rollback
from slipway.runner.instances import Finding, PostgresRecords, RunnerPorts
from slipway.workflow import runner_track

T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


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


class Ports:
    """RunnerPorts over Postgres with fake scripts and API, and a hand moved clock."""

    def __init__(self, conn, head=A):
        self.records = PostgresRecords(conn)
        self.scripts, self.api = FakeScripts(), FakeApi()
        self.head, self.now = head, T0
        self.operator = operator(runner_instance("vps", "vps"))

    @property
    def ports(self):
        return RunnerPorts(self.records, self.scripts, self.api, lambda r: self.head,
                           lambda: self.operator, lambda: self.now)


def gate_run(conn, project, run_id, started_at, *classes):
    """A gate run written as observation writes it, with its findings."""
    gid = store.record_gate_run(conn, project, "github", run_id, "vps", started_at=started_at)
    row = conn.execute("SELECT id, project_id, instance, runner_build_id FROM gate_runs "
                       "WHERE id = %s", (gid,)).fetchone()
    run = dict(zip(("id", "project_id", "instance", "runner_build_id"), row, strict=True))
    commit = conn.execute("SELECT runner_commit FROM runner_builds WHERE id = %s",
                          (run["runner_build_id"],)).fetchone()[0]
    records = PostgresRecords(conn)
    for name in classes:
        records.add_finding(Finding(name, f"{name}:{gid}", commit, project_id=project,
                                    gate_run_id=gid, runner_build_id=run["runner_build_id"]))
    return run


def test_ae11_over_postgres_rolls_b_on_then_back_and_rejects_it(conn):
    project = store.ensure_project(conn, "example")
    w = Ports(conn)
    assert runner_track.reconcile(w.ports)["instances"]["vps"]["outcome"] == "swapped"
    gate_run(conn, project, "1", T0 + timedelta(minutes=10))

    w.head, w.now = B, T0 + timedelta(hours=1)
    assert runner_track.reconcile(w.ports)["instances"]["vps"]["outcome"] == "swapped"
    b = w.records.active_build("vps")
    assert b.runner_commit == B
    assert w.records.previous_build(b).runner_commit == A

    red = gate_run(conn, project, "2", w.now + timedelta(minutes=1))
    assert rollback.evaluate(w.ports, red)["outcome"] == rollback.NO_RUNNER_FINDING

    bad = gate_run(conn, project, "3", w.now + timedelta(minutes=2), rollback.UNSUPPORTED_STEP)
    w.now += timedelta(minutes=10)
    first = rollback.evaluate(w.ports, bad)
    again = gate_run(conn, project, "4", b.activated_at + timedelta(minutes=3), rollback.LOG_GAP)
    second = rollback.evaluate(w.ports, again)

    assert first["outcome"] == rollback.ROLLED_BACK
    assert second["outcome"] == rollback.ALREADY_ROLLED_BACK
    assert second["related_finding"] == first["finding_id"]
    assert w.records.active_build("vps").runner_commit == A
    assert w.records.rejected(B)
    states = conn.execute("SELECT runner_commit, state FROM runner_builds ORDER BY id").fetchall()
    assert states == [(A, "previous"), (B, "rejected"), (A, "active")]
    # A gate run after the rollback is attributed to the restored A.
    later = gate_run(conn, project, "5", w.now + timedelta(minutes=1))
    assert later["runner_build_id"] == w.records.active_build("vps").id

    w.now += timedelta(hours=1)
    assert runner_track.reconcile(w.ports)["skipped"] == "rejected"
    w.head = C
    assert runner_track.reconcile(w.ports)["instances"]["vps"]["outcome"] == "swapped"
    assert w.records.previous_build(w.records.active_build("vps")).runner_commit == A


def test_a_build_failure_is_a_deployment_finding_with_no_project(conn):
    w = Ports(conn, head=B)
    w.scripts.failing.add(("build", "vps"))

    out = runner_track.reconcile(w.ports)

    assert out["instances"]["vps"]["outcome"] == "build_red"
    row = conn.execute("SELECT project_id, class, runner_commit, excerpt FROM findings").fetchone()
    assert row[:3] == (None, instances.BUILD_RED, B) and "zig build test failed" in row[3]
    fingerprint = f"{instances.BUILD_RED}:vps:{B}"
    assert w.records.has_open_finding(fingerprint)
    conn.execute("UPDATE findings SET state = 'closed', closed_at = now()")
    assert not w.records.has_open_finding(fingerprint)
    assert w.records.active_build("vps") is None


def test_activate_is_a_compare_and_set(conn):
    records = PostgresRecords(conn)
    first = records.activate("vps", A, None, T0)

    assert records.activate("vps", B, None, T0 + timedelta(minutes=1)) is None
    assert records.activate("vps", B, first + 99, T0 + timedelta(minutes=1)) is None
    second = records.activate("vps", B, first, T0 + timedelta(minutes=2))
    assert second is not None
    assert records.roll_back(first, A, T0 + timedelta(minutes=3)) is None  # not active
    assert records.roll_back(second, A, T0 + timedelta(minutes=3)) is not None
    assert records.roll_back(second, A, T0 + timedelta(minutes=4)) is None  # already done
