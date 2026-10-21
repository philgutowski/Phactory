"""Fakes for the service layer (U25), shared by test_verbs and test_watch.

FakeServiceRecords keeps builds, transitions, and audit rows in memory and
answers service.verbs.Records the way PostgresRecords does over the real
schema: claim honours the one owner rule (KTD3) and the bucket of each state
is read from the migration that defines it, so the fake cannot drift from the
database. FakeEngine records every start and push. FakeTracker answers ready
and read from a dict of cards. world() puts the config fixtures under a
temporary SLIPWAY_HOME and returns Ports over all three.
"""

import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from slipway.config.operator import load_operator
from slipway.config.project import load_project
from slipway.records.store import OWNERSHIP_TERMINAL, Claim
from slipway.service.verbs import Ports, configured_slugs
from slipway.trackers.base import Card

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / "fixtures" / "config"
SCHEMA = ROOT / "src" / "slipway" / "records" / "schema"

OPERATOR = "user_operator_example"
MANAGER = "agent_manager_example"
SLUG = "example"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def body(lane: str = "cloud") -> str:
    return ('Add the page.\n\n```slipway\n{"good": "The page renders.", '
            f'"paths": ["src/page.py"], "test": "tests/test_page.py", "lane": "{lane}"}}\n```\n')


def ready_card(key: str, labels=("cloud",), status: str = "Ready") -> Card:
    return Card(key, f"Card {key}", body(), status, tuple(labels))


def state_buckets() -> dict[str, str]:
    """Each state's page bucket, as 001_init.sql inserts it."""
    text = (SCHEMA / "001_init.sql").read_text()
    insert = re.search(r"INSERT INTO states.*?;", text, re.S).group(0)
    return dict(re.findall(r"\('(\w+)', '(\w+)'", insert))


BUCKETS = state_buckets()


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeServiceRecords:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.projects: dict[str, int] = {}
        self.builds: dict[int, dict] = {}
        self.transitions: list[dict] = []
        self.audit_rows: list[dict] = []
        self.claims: list[dict] = []
        self.snapshots = 0

    def project_id(self, slug):
        return self.projects.setdefault(slug, len(self.projects) + 1)

    def _owner(self, project_id, key):
        for row in self.builds.values():
            if (row["project_id"], row["card_key"]) == (project_id, key) and (
                    row["state"] not in OWNERSHIP_TERMINAL):
                return row
        return None

    def seed(self, key, state, project=SLUG, pr_number=None, run_id="run", at=None,
             evidence=None, created=None):
        """A build already in state, as earlier runs would have left it."""
        project_id = self.project_id(project)
        build_id = len(self.builds) + 1
        at = at or self.clock()
        self.builds[build_id] = {
            "build_id": build_id, "id": build_id, "project_id": project_id, "card_key": key,
            "state": state, "run_id": run_id, "pr_number": pr_number,
            "created_at": created or at, "updated_at": at}
        self.transitions.append({"build_id": build_id, "to": state, "evidence": evidence or {}})
        return build_id

    def claim(self, project_id, key, actor, reason, paths, title):
        held = self._owner(project_id, key)
        if held:
            return Claim(held["build_id"], held["state"], False, held["run_id"])
        build_id = len(self.builds) + 1
        now = self.clock()
        self.builds[build_id] = {
            "build_id": build_id, "id": build_id, "project_id": project_id, "card_key": key,
            "state": "claimed", "run_id": None, "pr_number": None, "created_at": now,
            "updated_at": now}
        self.claims.append({"build_id": build_id, "key": key, "actor": actor,
                            "reason": reason, "paths": paths, "title": title})
        self.transitions.append({"build_id": build_id, "to": "claimed",
                                 "evidence": {"reason": reason}})
        return Claim(build_id, "claimed", True, None)

    def latest(self, project_id, key):
        rows = [r for r in self.builds.values()
                if (r["project_id"], r["card_key"]) == (project_id, key)]
        return dict(max(rows, key=lambda r: r["build_id"])) if rows else None

    def latest_builds(self, project_id):
        keys = sorted({r["card_key"] for r in self.builds.values()
                       if r["project_id"] == project_id})
        rows = [self.latest(project_id, key) for key in keys]
        for row in rows:
            row["bucket"] = BUCKETS[row["state"]]
        return sorted(rows, key=lambda r: r["build_id"])

    def claimed_since(self, project_id, since):
        return sum(1 for r in self.builds.values()
                   if r["project_id"] == project_id and r["created_at"] >= since)

    def entered(self, build_id, state):
        rows = [t for t in self.transitions if t["build_id"] == build_id and t["to"] == state]
        return rows[-1]["evidence"] if rows else None

    def card(self, project_id, key):
        builds = sorted((dict(r) for r in self.builds.values()
                         if (r["project_id"], r["card_key"]) == (project_id, key)),
                        key=lambda r: r["build_id"], reverse=True)
        return {"card_key": key, "builds": builds} if builds else None

    def stale_claims(self, project_id, older_than):
        return [dict(r) for r in self.builds.values()
                if r["project_id"] == project_id and r["state"] == "claimed"
                and r["run_id"] is None and r["updated_at"] < older_than]

    def audit(self, slug, subject, tool, arguments, result, client=None):
        self.audit_rows.append({"project_slug": slug, "subject": subject, "tool": tool,
                                "arguments": arguments, "result": result, "client": client})

    @contextmanager
    def snapshot(self):
        self.snapshots += 1
        yield


@dataclass
class FakeEngine:
    starts: list[tuple] = field(default_factory=list)
    pushes: list[tuple] = field(default_factory=list)
    fail_start: bool = False
    records: FakeServiceRecords | None = None

    def start_card(self, project, key, build_id):
        if self.fail_start:
            raise ConnectionError("engine unreachable")
        self.starts.append((project, key, build_id))
        run_id = f"run-{len(self.starts)}"
        if self.records is not None:
            # The run's attach_run step, which writes its own id (KTD3).
            row = self.records.builds[build_id]
            if row["run_id"] is None:
                row["run_id"] = run_id
        return run_id

    def push(self, kind, build_id, payload):
        self.pushes.append((kind, build_id, dict(payload)))


class FakeTracker:
    def __init__(self, cards=(), reason=None):
        self.cards = {str(c.key): c for c in cards}
        self.reason = reason
        self.reads: list[str] = []

    def ready(self):
        if self.reason:
            return [], self.reason
        return list(self.cards.values()), None

    def read(self, key):
        self.reads.append(str(key))
        found = self.cards.get(str(key))
        return found or Card(str(key), skipped=f"no card {key}")


@dataclass
class World:
    root: Path
    clock: Clock
    records: FakeServiceRecords
    engine: FakeEngine
    tracker: FakeTracker
    ports: Ports

    def later(self, delta: timedelta) -> None:
        self.clock.now += delta


def world(tmp_path: Path, cards=(), lanes: dict[str, str] | None = None) -> World:
    """Ports over the fixture config at SLUG, plus one project per entry of lanes.

    lanes maps another project's slug to its lane label, for the two lane rule.
    """
    root = tmp_path / "home"
    (root / "projects").mkdir(parents=True)
    shutil.copy(CONFIG / "operator.toml", root / "operator.toml")
    text = (CONFIG / "project.toml").read_text()
    (root / "projects" / f"{SLUG}.toml").write_text(text)
    for slug, lane in (lanes or {}).items():
        (root / "projects" / f"{slug}.toml").write_text(
            text.replace('lane_label = "cloud"', f'lane_label = "{lane}"'))
    clock = Clock()
    records = FakeServiceRecords(clock)
    engine = FakeEngine(records=records)
    tracker = FakeTracker(cards)
    ports = Ports(records=records, engine=engine,
                  project=lambda slug: load_project(slug, root),
                  operator=lambda: load_operator(root),
                  tracker=lambda project: tracker,
                  slugs=lambda: configured_slugs(root), clock=clock)
    return World(root, clock, records, engine, tracker, ports)
