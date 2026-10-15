"""U7 test scenarios against the local Postgres named by PHACTORY_TEST_DSN.

Each test gets its own schema (a namespace of tables inside the database),
with the connection's search_path pointing at it, applies the migrations
there, and drops it afterwards, so tests never see each other's rows. Without
the variable every test skips, and tests/_gate.py turns that skip into a
failure under CI.
"""

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

from phactory.records import migrate, queries, store


@pytest.fixture
def connect():
    dsn = os.environ.get("PHACTORY_TEST_DSN", "")
    if not dsn:
        pytest.skip("PHACTORY_TEST_DSN is not set")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    # A database made by initdb under the C locale is SQL_ASCII, and psycopg
    # then returns text as bytes. Asking for UTF8 on the client side makes
    # every test read str whatever locale the server was initialised under.
    admin = psycopg.connect(dsn, autocommit=True, client_encoding="utf8")
    admin.execute(f"CREATE SCHEMA {schema}")
    opened = []

    def _connect():
        conn = psycopg.connect(dsn, autocommit=True, client_encoding="utf8",
                                options=f"-c search_path={schema}")
        opened.append(conn)
        return conn

    try:
        yield _connect
    finally:
        for conn in opened:
            conn.close()
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


@pytest.fixture
def conn(connect):
    c = connect()
    migrate.apply(c)
    return c


@pytest.fixture
def project(conn):
    return store.ensure_project(conn, "example")


def _walk(conn, build_id, *states, actor="worker"):
    current = conn.execute("SELECT state FROM builds WHERE id = %s", (build_id,)).fetchone()[0]
    for state in states:
        result = store.record_transition(conn, build_id, current, state, actor)
        assert result.recorded, (current, state)
        current = state


# Migrations.

def test_ledger_holds_one_row_per_file_and_second_apply_is_a_no_op(connect):
    c = connect()
    assert migrate.apply(c) == ["001"]
    assert migrate.apply(c) == []
    rows = c.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    files = migrate.discover()
    assert rows == [(m.version, m.name, m.checksum) for m in files]


def test_modified_001_is_refused_with_the_checksum_named(connect, tmp_path):
    c = connect()
    migrate.apply(c)
    original = (migrate.SCHEMA_DIR / "001_init.sql").read_text()
    (tmp_path / "001_init.sql").write_text(original + "\n/* edited */\n")
    with pytest.raises(migrate.ChecksumMismatch) as caught:
        migrate.apply(c, schema_dir=tmp_path)
    recorded = migrate.discover()[0].checksum
    assert recorded in str(caught.value)
    assert c.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_no_transaction_file_builds_a_concurrent_index(connect, tmp_path):
    c = connect()
    (tmp_path / "001_t.sql").write_text("CREATE TABLE t (a int);\n")
    (tmp_path / "002_idx.sql").write_text(
        f"{migrate.NO_TRANSACTION_MARKER}\nCREATE INDEX CONCURRENTLY t_a ON t (a);\n")
    assert migrate.apply(c, schema_dir=tmp_path) == ["001", "002"]
    assert c.execute("SELECT count(*) FROM pg_indexes WHERE indexname = 't_a'").fetchone()[0]


def test_failed_file_leaves_no_ledger_row(connect, tmp_path):
    c = connect()
    (tmp_path / "001_bad.sql").write_text("CREATE TABLE t (a int);\nSELECT nope;\n")
    with pytest.raises(psycopg.Error):
        migrate.apply(c, schema_dir=tmp_path)
    assert c.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
    assert c.execute("SELECT to_regclass('t')").fetchone()[0] is None


def test_wrong_database_is_refused(connect):
    with pytest.raises(migrate.WrongDatabase):
        migrate.apply(connect(), database="not_this_one")


# Transitions.

def test_dispatched_then_building_records_two_transitions(conn, project):
    build = store.claim(conn, project, "C-1", "watch").build_id
    first = store.record_transition(conn, build, "claimed", "dispatched", "watch",
                                    evidence={"branch": "phactory/c-1"},
                                    claims={"branch": "phactory/c-1"})
    second = store.record_transition(conn, build, "dispatched", "building", "worker")
    assert (first.seq, second.seq) == (1, 2)
    rows = conn.execute("SELECT seq, from_state, to_state, actor FROM transitions "
                        "WHERE build_id = %s AND seq > 0 ORDER BY seq", (build,)).fetchall()
    assert rows == [(1, "claimed", "dispatched", "watch"), (2, "dispatched", "building", "worker")]
    state, branch = conn.execute("SELECT state, branch FROM builds WHERE id = %s",
                                 (build,)).fetchone()
    assert (state, branch) == ("building", "phactory/c-1")


def test_edge_not_allowed_is_refused_by_the_database(conn, project):
    build = store.claim(conn, project, "C-1", "watch").build_id
    with pytest.raises(errors.CheckViolation):
        store.record_transition(conn, build, "claimed", "merged", "worker")
    # Neither a direct update of the build nor a direct transition row gets past it.
    with pytest.raises(errors.CheckViolation):
        conn.execute("UPDATE builds SET state = 'merged' WHERE id = %s", (build,))
    with pytest.raises(errors.ForeignKeyViolation):
        conn.execute("INSERT INTO transitions (project_id, build_id, seq, from_state, to_state, "
                     "actor) VALUES (%s, %s, 9, 'claimed', 'merged', 'x')", (project, build))
    assert conn.execute("SELECT state, last_seq FROM builds WHERE id = %s",
                        (build,)).fetchone() == ("claimed", 0)


def test_replayed_transition_is_already_past(conn, project):
    build = store.claim(conn, project, "C-1", "watch").build_id
    first = store.record_transition(conn, build, "claimed", "dispatched", "watch")
    again = store.record_transition(conn, build, "claimed", "dispatched", "watch")
    assert first.recorded and again.already_past
    assert again.state == "dispatched"
    count = conn.execute("SELECT count(*) FROM transitions WHERE build_id = %s AND "
                         "to_state = 'dispatched'", (build,)).fetchone()[0]
    assert count == 1


def test_unknown_claim_column_is_refused(conn, project):
    build = store.claim(conn, project, "C-1", "watch").build_id
    with pytest.raises(ValueError, match="state"):
        store.record_transition(conn, build, "claimed", "dispatched", "w", claims={"state": "x"})


# Claims and run ids.

def test_two_claims_from_two_connections_yield_one_build(connect, conn, project):
    other = connect()
    results = {}
    with conn.transaction():
        results["a"] = store.claim(conn, project, "C-1", "watch", reason="ready")
        thread = threading.Thread(
            target=lambda: results.__setitem__("b", store.claim(other, project, "C-1", "verb")))
        thread.start()
        thread.join(0.5)
        assert thread.is_alive(), "the second claim should wait on the first transaction"
    thread.join(10)
    a, b = results["a"], results["b"]
    assert a.won and not b.won
    assert b.build_id == a.build_id and b.state == "claimed"
    assert conn.execute("SELECT count(*) FROM builds").fetchone()[0] == 1


def test_terminal_build_lets_a_new_claim_in(conn, project):
    first = store.claim(conn, project, "C-1", "watch").build_id
    _walk(conn, first, "dispatched", "returned_empty")
    second = store.claim(conn, project, "C-1", "operator", reason="re dispatch")
    assert second.won and second.build_id != first


def test_second_attach_run_keeps_the_first(conn, project):
    build = store.claim(conn, project, "C-1", "watch").build_id
    first = store.attach_run(conn, build, "run-1")
    second = store.attach_run(conn, build, "run-2")
    assert first.attached and first.run_id == "run-1"
    assert not second.attached and second.run_id == "run-1"


# Events.

def test_cursor_reader_sees_every_row_once_in_order(connect, conn, project):
    writers = [connect(), connect()]
    builds = [store.claim(conn, project, f"C-{i}", "watch").build_id for i in range(2)]
    path = ["dispatched", "building", "returned", "pr_open", "awaiting_approval", "queued",
            "gate_running", "gate_red"]
    loop = ["queued", "gate_running", "gate_red"]
    errors_seen = []

    def write(c, build):
        try:
            current = "claimed"
            for state in path + loop * 5:
                assert store.record_transition(c, build, current, state, "worker").recorded
                current = state
        except BaseException as exc:  # surfaced in the main thread below
            errors_seen.append(exc)

    threads = [threading.Thread(target=write, args=(c, b))
               for c, b in zip(writers, builds, strict=True)]
    for t in threads:
        t.start()
    reader, seen, cursor = connect(), [], 0

    def poll():
        nonlocal cursor
        page = queries.events_since(reader, project, cursor, limit=7)
        assert not page["truncated"]
        seen.extend(r["id"] for r in page["rows"])
        cursor = page["cursor"]
        return page["rows"]

    while any(t.is_alive() for t in threads):
        poll()
    for t in threads:
        t.join()
    while poll():
        pass
    assert not errors_seen, errors_seen
    expected = [r[0] for r in conn.execute(
        "SELECT id FROM transitions WHERE project_id = %s ORDER BY id", (project,)).fetchall()]
    assert seen == expected
    assert len(seen) == len(set(seen))


def test_cursor_below_the_floor_is_truncated(conn, project):
    store.claim(conn, project, "C-1", "watch")
    conn.execute("UPDATE projects SET events_floor = 1000 WHERE id = %s", (project,))
    assert queries.events_since(conn, project, 5)["truncated"]
    assert not queries.events_since(conn, project, 1000)["truncated"]


def _merged(project, delivery):
    return {"project_id": project, "host": "github", "delivery_id": delivery,
            "repository": "owner/repo", "pr_number": 7, "kind": "pr.merged",
            "merge_commit": "a" * 40, "head_commit": "b" * 40}


def test_same_merged_event_twice_yields_one_row(conn, project):
    first = store.ingest_host_event(conn, "webhook", _merged(project, "d-1"))
    second = store.ingest_host_event(conn, "webhook", _merged(project, "d-1"))
    assert first.inserted and not second.inserted
    assert second.event_id == first.event_id
    # The poll path finding the same merge later is deduplicated on the merge commit.
    polled = store.ingest_host_event(conn, "poll", _merged(project, None))
    assert not polled.inserted and polled.event_id == first.event_id
    assert conn.execute("SELECT count(*) FROM host_events").fetchone()[0] == 1


def test_poll_path_writes_a_byte_equal_row(connect, project):
    webhook_conn = connect()
    migrate.apply(webhook_conn)
    poll_conn = connect()
    poll_conn.execute("CREATE SCHEMA IF NOT EXISTS poll_side")
    poll_conn.execute("SET search_path = poll_side")
    try:
        migrate.apply(poll_conn)
        poll_project = store.ensure_project(poll_conn, "example")
        assert poll_project == project
        store.ingest_host_event(webhook_conn, "webhook", _merged(project, None))
        store.ingest_host_event(poll_conn, "poll", _merged(project, None))
        columns = ("host, delivery_id, repository, pr_number, kind, merge_commit, "
                   "head_commit, normalised")
        a = webhook_conn.execute(f"SELECT source, {columns} FROM host_events").fetchone()
        b = poll_conn.execute(f"SELECT source, {columns} FROM host_events").fetchone()
        assert (a[0], b[0]) == ("webhook", "poll")
        assert a[1:] == b[1:]
        assert a[-1].encode() == b[-1].encode()
    finally:
        poll_conn.execute("DROP SCHEMA poll_side CASCADE")


# Runner builds and gate runs.

def test_gate_run_before_a_swap_resolves_to_the_previous_build(conn, project):
    t0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    old = store.activate_runner_build(conn, "vps", "c" * 40, t0)
    swap = t0 + timedelta(hours=1)
    new = store.activate_runner_build(conn, "vps", "d" * 40, swap)
    before = store.record_gate_run(conn, project, "github", "100", "vps",
                                   started_at=swap - timedelta(minutes=5),
                                   completed_at=swap + timedelta(minutes=5))
    after = store.record_gate_run(conn, project, "github", "101", "vps",
                                  started_at=swap + timedelta(minutes=1))
    attributed = dict(conn.execute("SELECT id, runner_build_id FROM gate_runs").fetchall())
    assert attributed == {before: old, after: new}
    assert queries.runner_build_active_at(conn, "vps", swap - timedelta(seconds=1))["id"] == old
    assert queries.runner_build_active_at(conn, "vps", swap)["id"] == new
    assert queries.latest_gate_run(conn, project, "vps", "c" * 40)["id"] == before
    assert queries.latest_gate_run(conn, project, "vps", "d" * 40)["id"] == after
    states = dict(conn.execute("SELECT id, state FROM runner_builds").fetchall())
    assert states == {old: "previous", new: "active"}


def test_same_gate_run_written_twice_is_one_row(conn, project):
    started = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    first = store.record_gate_run(conn, project, "github", "100", "vps", started_at=started)
    second = store.record_gate_run(conn, project, "github", "100", "vps", started_at=started,
                                   conclusion="success")
    assert first == second
    assert conn.execute("SELECT conclusion FROM gate_runs").fetchone()[0] == "success"


# Queries and retention.

def test_status_buckets_and_in_flight_files(conn, project):
    other = store.ensure_project(conn, "second")
    a = store.claim(conn, project, "C-1", "watch", paths=["src/a.py"]).build_id
    store.claim(conn, project, "C-2", "watch")
    _walk(conn, a, "dispatched", "building")
    store.claim(conn, other, "C-9", "watch")
    across = queries.status_buckets(conn)
    assert set(across) == {"example", "second"}
    mine = queries.status_buckets(conn, project)["example"]
    assert [r["card_key"] for r in mine["being_built"]] == ["C-1", "C-2"]
    flight = queries.in_flight_files(conn, project)
    assert [(r["card_key"], r["paths"]) for r in flight] == [("C-1", ["src/a.py"])]


def test_card_detail_and_open_findings(conn, project):
    build = store.claim(conn, project, "C-1", "watch").build_id
    _walk(conn, build, "dispatched")
    conn.execute("INSERT INTO findings (project_id, build_id, class, fingerprint) "
                 "VALUES (%s, %s, 'runner', 'fp')", (project, build))
    detail = queries.card_detail(conn, project, "C-1")
    assert [t["to_state"] for t in detail["builds"][0]["transitions"]] == ["claimed",
                                                                           "dispatched"]
    assert [f["fingerprint"] for f in queries.open_findings(conn, project)] == ["fp"]
    assert queries.card_detail(conn, project, "nope") is None


def test_stale_claim_is_listed_for_retry(conn, project):
    build = store.claim(conn, project, "C-1", "watch").build_id
    future = datetime.now(UTC) + timedelta(minutes=1)
    assert [r["id"] for r in queries.stale_claims(conn, future)] == [build]
    store.attach_run(conn, build, "run-1")
    assert queries.stale_claims(conn, future) == []


def test_deleting_a_project_cascades_but_keeps_audit(conn, project):
    store.claim(conn, project, "C-1", "watch")
    conn.execute("INSERT INTO audit (project_slug, subject, tool) "
                 "VALUES ('example', 's', 'status')")
    conn.execute("DELETE FROM projects WHERE id = %s", (project,))
    for table in ("cards", "builds", "transitions"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM audit").fetchone()[0] == 1
