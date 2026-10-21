"""U21's CLI over the real records in Postgres.

The unit tests drive the CLI on in memory records; this file runs watch,
status, and migrate through service.verbs.PostgresRecords, so the two record
reads U21 adds (events_since and migrate) run their SQL in the gate too.
Each test gets its own schema, as in test_service_records.py.
"""

import os
import uuid

import psycopg
import pytest

from _service_fakes import OPERATOR, SLUG, world
from slipway import cli
from slipway.records import migrate, store
from slipway.service import verbs

pytestmark = pytest.mark.integration


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
def run(tmp_path, conn):
    found = world(tmp_path)
    ports = verbs.Ports(**{**found.ports.__dict__, "records": verbs.PostgresRecords(conn)})
    out: list[str] = []

    def run(*argv) -> tuple[int, list[str]]:
        out.clear()
        wiring = cli.Wiring(root=found.root, service=lambda: ports, onboard=None,
                            write=out.append)
        return cli.main(list(argv), wiring), list(out)

    return run


def _audit(conn):
    return conn.execute("SELECT project_slug, subject, client_name, tool FROM audit ORDER BY id"
                        ).fetchall()


def test_watch_prints_the_transitions_after_the_cursor(run, conn):
    project_id = store.ensure_project(conn, SLUG)
    build = store.claim(conn, project_id, "EX-1", OPERATOR, None, None, "Card EX-1").build_id
    assert store.record_transition(conn, build, "claimed", "dispatched", "workflow",
                                   {"reason": "the builder started"}).recorded

    code, out = run("watch", SLUG)

    assert code == 0
    assert len(out) == 2
    assert "EX-1  entered claimed" in out[0]
    assert "EX-1  claimed to dispatched  by workflow: the builder started" in out[1]
    first = int(out[0].split()[0].lstrip("#"))
    code, out = run("watch", SLUG, "--since", str(first))
    assert len(out) == 1 and "claimed to dispatched" in out[0]
    assert _audit(conn)[-1] == (SLUG, OPERATOR, cli.CLIENT, "watch")


def test_status_reads_postgres_and_is_audited(run, conn):
    code, out = run("status", SLUG)

    assert code == 0
    assert out[0] == "Nothing is in flight."
    assert "Needs you (0)" in out
    assert _audit(conn) == [(SLUG, OPERATOR, cli.CLIENT, "status")]


def test_migrate_refuses_another_database_and_finds_the_ledger_current(run, conn):
    code, out = run("migrate")
    assert code == 1
    assert out[0].startswith("Refused: refusing to migrate database")
    assert _audit(conn) == []

    database = conn.execute("SELECT current_database()").fetchone()[0]
    code, out = run("migrate", "--database", database)
    assert code == 0
    assert out == ["Applied: nothing, the ledger is current"]
    assert _audit(conn) == [("*", OPERATOR, cli.CLIENT, "migrate")]
