"""U14 against real engines: the records in Postgres, then one card through a local Hatchet.

Two kinds of test live here.

The first runs whole cards through the fake engine of tests/_card_fakes.py
with the real records store underneath (worker.PostgresRecords over the
local Postgres named by SLIPWAY_TEST_DSN), so every transition the workflow
makes is checked by the database's own allowed edges, row lock, and compare
and set, migration 004 included. It needs only Postgres, so it runs in the
gate.

The second is the operator run smoke KTD19 names: one card through a local
Hatchet engine with a fake builder and a fake host, asserting the transition
log. It needs a running engine, so it is skipped without SLIPWAY_TEST_HATCHET
and marked operator_smoke, which tests/_gate.py leaves out of the rule that
a skipped integration test fails under CI. To run it, start hatchet-lite as
docs/explainers/u14-card-workflow.md shows, then:

    export HATCHET_CLIENT_TOKEN=<token> HATCHET_CLIENT_TLS_STRATEGY=none
    export HATCHET_CLIENT_HOST_PORT=localhost:7077 SLIPWAY_TEST_HATCHET=1
    CI=true python -m pytest -q tests/integration/test_workflow_live.py

The smoke starts the worker as a subprocess of this file (python
test_workflow_live.py worker ...), because a Hatchet worker owns its process.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from _card_fakes import FakeHost, World, parked, run, sha
from slipway.hosts.base import MERGED
from slipway.records import migrate, store
from slipway.workflow import card, events, worker

ROOT = Path(__file__).resolve().parents[2]


class PgRecords(worker.PostgresRecords):
    """PostgresRecords plus the reads World's helpers and the assertions use."""

    def claim(self, project: str, key: str) -> int:
        project_id = store.ensure_project(self.conn, project)
        return store.claim(self.conn, project_id, key, "watch", "ready").build_id

    @property
    def builds(self):
        records = self

        class View:
            def __getitem__(self, build_id):
                return records.build(build_id).to_json()

        return View()

    def path(self, build_id) -> list[str]:
        rows = self.conn.execute("SELECT to_state FROM transitions WHERE build_id = %s "
                                 "ORDER BY seq", (build_id,)).fetchall()
        return [row[0] for row in rows]

    def evidence(self, build_id, state) -> dict:
        return self.entered(build_id, state)[1]


def schema_dsn():
    dsn = os.environ.get("SLIPWAY_TEST_DSN", "")
    if not dsn:
        pytest.skip("SLIPWAY_TEST_DSN is not set")
    return dsn


@pytest.fixture
def connect():
    """Connections into a fresh schema with the migrations applied, dropped afterwards."""
    dsn = schema_dsn()
    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(dsn, autocommit=True, client_encoding="utf8")
    admin.execute(f"CREATE SCHEMA {schema}")
    opened = []

    def _connect():
        conn = psycopg.connect(dsn, autocommit=True, client_encoding="utf8",
                               options=f"-c search_path={schema}")
        opened.append(conn)
        return conn

    migrate.apply(_connect())
    _connect.dsn, _connect.schema = dsn, schema
    try:
        yield _connect
    finally:
        for conn in opened:
            conn.close()
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


# Whole cards on the real records.

def test_a_card_lands_and_scores_with_every_transition_on_an_allowed_edge(connect, tmp_path):
    records = PgRecords(connect())
    world = World(tmp_path, records=records)
    build_id = world.card("EX-20")

    async def scenario():
        task = world.start(build_id)
        await parked(world, build_id)
        world.host.change(records.build(build_id).pr_number, state=MERGED, merged=True,
                          merge_commit=sha(50))
        world.engine.push(events.PR_MERGED, build_id)
        return await task

    assert run(scenario)["outcome"] == "scored"
    assert records.path(build_id) == ["claimed", "dispatched", "building", "returned",
                                      "pr_open", "merged", "verifying", "landed", "scored"]
    row = records.build(build_id)
    assert row.run_id == "run-1"
    assert row.branch == f"slipway/EX-20-{build_id}"
    assert row.pr_number == 101 and row.confirmed_commit == sha(1)
    assert row.session_ref == "session-EX-20-1"
    attempts = records.conn.execute("SELECT model, ended_at IS NOT NULL, tools_granted "
                                    "FROM attempts WHERE build_id = %s",
                                    (build_id,)).fetchall()
    assert attempts == [("model-large", True, ["headless_browser"])]


def test_the_edges_migration_004_adds_carry_real_cards(connect, tmp_path):
    records = PgRecords(connect())
    world = World(tmp_path, records=records)
    taken_back = world.card("EX-21")
    world.tracker.cards["EX-21"] = replace(world.tracker.cards["EX-21"], status="Backlog")
    unbound = world.card("EX-22")
    path = world.home / "projects" / "example.toml"

    async def scenario():
        return await world.start(taken_back)

    assert run(scenario)["outcome"] == "abandoned"
    assert records.path(taken_back) == ["claimed", "abandoned"]

    path.write_text(path.read_text().replace('account = "max-sub"', 'account = "nobody"'))

    async def unbound_run():
        return await world.start(unbound, "run-2")

    assert run(unbound_run)["outcome"] == "returned_empty"
    assert records.path(unbound) == ["claimed", "returned_empty"]
    assert records.evidence(unbound, "returned_empty")["halt"] == "binding_invalid"


def test_a_polled_merge_is_written_once_to_host_events(connect, tmp_path):
    records = PgRecords(connect())
    world = World(tmp_path, records=records)
    build_id = world.card("EX-23")

    async def scenario():
        task = world.start(build_id)
        await parked(world, build_id)
        world.host.change(records.build(build_id).pr_number, state=MERGED, merged=True,
                          merge_commit=sha(51))
        world.engine.push(events.SLEEP, build_id)
        return await task

    assert run(scenario)["outcome"] == "scored"
    rows = records.conn.execute("SELECT source, kind, merge_commit FROM host_events").fetchall()
    assert rows == [("poll", events.PR_MERGED, sha(51))]


# The operator run smoke: one card through a local Hatchet.

class FileHost(FakeHost):
    """FakeHost whose pull requests read merged once the test writes the state file."""

    def __init__(self, state: Path):
        super().__init__()
        self.state = state

    def pull_request(self, number):
        pr = super().pull_request(number)
        if self.state.exists() and json.loads(self.state.read_text()).get("merged"):
            return replace(pr, state=MERGED, merged=True, merge_commit=sha(60))
        return pr


def serve(home: str, dsn: str, schema: str, state: str, key: str) -> None:
    """The worker subprocess: the real task declarations over fakes and Postgres."""
    from hatchet_sdk import Hatchet

    world = World(Path(state).parent / "worker")
    world.home = Path(home)  # the ports read world.home when they run
    world.host = FileHost(Path(state))
    world.tracker.add(key)

    def make_ports():
        conn = psycopg.connect(dsn, autocommit=True, client_encoding="utf8",
                               options=f"-c search_path={schema}")
        return replace(world.ports, records=worker.PostgresRecords(conn))

    hatchet = Hatchet()
    tasks = worker.register(hatchet, make_ports,
                            close=lambda ports: ports.records.conn.close())
    workflows = [t for name, t in tasks.items() if name != card.LAPTOP_BUILD]
    hatchet.worker("slipway-smoke", slots=10, workflows=workflows).start()


@pytest.mark.operator_smoke
def test_one_card_through_a_local_hatchet(connect, tmp_path):
    if not os.environ.get("SLIPWAY_TEST_HATCHET"):
        pytest.skip("SLIPWAY_TEST_HATCHET is not set; this is an operator run smoke")
    from slipway.service.engine import HatchetEngine

    world = World(tmp_path)  # writes the config files the worker reads
    records = PgRecords(connect())
    key = f"EX-{uuid.uuid4().hex[:6]}"
    build_id = records.claim("example", key)
    state = tmp_path / "host.json"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")])}
    proc = subprocess.Popen(
        [sys.executable, __file__, "worker", str(world.home), connect.dsn, connect.schema,
         str(state), key], env=env, cwd=ROOT)
    try:
        engine = HatchetEngine()
        run_id = engine.start_card("example", key, build_id)
        wait_for(lambda: records.build(build_id).state == "pr_open", 90)
        assert records.build(build_id).run_id == run_id
        time.sleep(5)  # let the parent park in its wait
        state.write_text(json.dumps({"merged": True}))
        engine.push(events.PR_MERGED, build_id, {"pr_number": records.build(build_id).pr_number})
        wait_for(lambda: records.build(build_id).state == "scored", 90)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
    assert records.path(build_id) == ["claimed", "dispatched", "building", "returned",
                                      "pr_open", "merged", "verifying", "landed", "scored"]
    actors = records.conn.execute("SELECT DISTINCT actor FROM transitions WHERE build_id = %s "
                                  "AND seq > 0", (build_id,)).fetchall()
    assert actors == [(card.ACTOR,)]


def wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("the card did not get there in time")
        time.sleep(0.5)


if __name__ == "__main__" and sys.argv[1:2] == ["worker"]:
    serve(*sys.argv[2:7])
