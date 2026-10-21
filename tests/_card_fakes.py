"""Fakes for the card workflow (U14), shared by its unit and integration tests.

FakeEngine stands in for Hatchet's two primitives, start a child and wait for
an event or a sleep. It honours each child's concurrency key from
card.CHILDREN the way the engine does (the U4 spike measured it), runs each
child body in a thread as a worker would, and wakes every parent waiting on a
build when an event for that build is pushed. FakeRecords keeps the records
in memory and refuses any transition the migrations do not allow, reading
the allowed edges from the schema files themselves so the fake cannot drift
from the database. World holds a temporary SLIPWAY_HOME with the real config
fixtures, the fakes, and a FakeEngine over them; the integration test passes
its own records to run the same card against Postgres.
"""

import asyncio
import re
import threading
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from slipway.builders.base import BuilderResult, Schedule, SessionStatus
from slipway.builders.cli import LaptopReturn
from slipway.config.bindings import resolve_binding
from slipway.config.operator import load_operator
from slipway.config.project import load_project
from slipway.halts import COMMIT_MISMATCH, MERGED_CARD_NOT_MOVED, halt
from slipway.hosts.base import (
    AUTO_MERGE_ARMED,
    OPEN,
    PINNED,
    Branch,
    BranchConflict,
    CheckPost,
    Landing,
    PullRequest,
)
from slipway.records.store import Attach, Transition
from slipway.trackers.base import Card, ClosingReference, TrackerStatus
from slipway.verify import FAIL, HALTED, LANDED, PARTIAL_LANDING, PASS, Check, Verdict
from slipway.workflow import card
from slipway.workflow.events import Wake
from slipway.workflow.post_landed import Registry

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "config"
SCHEMA = ROOT / "src" / "slipway" / "records" / "schema"

BASE = "b" * 40
OPERATOR = "user_operator_example"
BODY = ('Add the page.\n\n```slipway\n{"good": "The page renders.", "paths": ["src/page.py"], '
        '"test": "tests/test_page.py", "lane": "cloud"}\n```\n')


def sha(n: int) -> str:
    return f"{n:040x}"


# Records.

def allowed_edges() -> set[tuple[str, str]]:
    """Every (from, to) pair the migrations insert into allowed_edges."""
    edges = set()
    for path in sorted(SCHEMA.glob("*.sql")):
        for statement in re.findall(r"INSERT INTO allowed_edges.*?;", path.read_text(), re.S):
            edges.update(re.findall(r"\('(\w+)', '(\w+)'\)", statement))
    return edges


EDGES = allowed_edges()


class Crash(Exception):
    """A worker lost between a host call and the record that follows it."""


class FakeRecords:
    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.RLock()
        self.builds: dict[int, dict] = {}
        self.transitions: list[dict] = []
        self.attempt_rows: list[dict] = []
        self.polled: list[tuple] = []
        self.projects: dict[str, int] = {}
        self.crash_before: set[str] = set()

    def claim(self, project: str, key: str) -> int:
        with self.lock:
            project_id = self.projects.setdefault(project, len(self.projects) + 1)
            build_id = len(self.builds) + 1
            self.builds[build_id] = {
                "id": build_id, "project": project, "project_id": project_id, "card_key": key,
                "state": "claimed", "last_seq": 0, "run_id": None, "branch": None,
                "session_ref": None, "pr_number": None, "check_run_id": None,
                "confirmed_commit": None, "tested_commit": None}
            self.transitions.append({"build_id": build_id, "seq": 0, "from": None,
                                     "to": "claimed", "evidence": {},
                                     "at": self.clock.now()})
            return build_id

    def build(self, build_id):
        with self.lock:
            return card.Build(**self.builds[build_id])

    def attach_run(self, build_id, run_id):
        with self.lock:
            row = self.builds[build_id]
            if row["run_id"] is None:
                row["run_id"] = run_id
                return Attach(True, run_id)
            return Attach(False, row["run_id"])

    def transition(self, build_id, from_state, to_state, actor, evidence, claims,
                   expected_seq):
        with self.lock:
            if to_state in self.crash_before:
                self.crash_before.discard(to_state)
                raise Crash(to_state)
            row = self.builds[build_id]
            if row["state"] != from_state or (expected_seq is not None
                                              and row["last_seq"] != expected_seq):
                return Transition(False, row["state"], None)
            if (from_state, to_state) not in EDGES:
                raise AssertionError(f"edge {from_state} -> {to_state} is not allowed")
            row["state"] = to_state
            row["last_seq"] += 1
            row.update(claims)
            self.transitions.append({"build_id": build_id, "seq": row["last_seq"],
                                     "from": from_state, "to": to_state, "actor": actor,
                                     "evidence": evidence, "at": self.clock.now()})
            return Transition(True, to_state, row["last_seq"])

    def entered(self, build_id, state):
        with self.lock:
            rows = [t for t in self.transitions if t["build_id"] == build_id and t["to"] == state]
            return (rows[-1]["at"], rows[-1]["evidence"]) if rows else None

    def slots_taken(self, project_id):
        with self.lock:
            return sum(1 for b in self.builds.values()
                       if b["project_id"] == project_id and b["state"] in card.SLOT_STATES)

    def in_flight(self, project_id):
        return []

    def attempts(self, build_id):
        with self.lock:
            return sum(1 for a in self.attempt_rows if a["build_id"] == build_id)

    def start_attempt(self, build_id, binding, tools_granted=()):
        with self.lock:
            self.attempt_rows.append({"build_id": build_id, "model": binding.model,
                                      "tools_granted": list(tools_granted)})
            return len(self.attempt_rows)

    def end_attempt(self, build_id, result):
        pass

    def ingest_poll(self, project_id, normalised):
        # The store's own key for the poll path: the same change is one row.
        key = (project_id, normalised["kind"], normalised["pr_number"],
               normalised["head_commit"], normalised["merge_commit"])
        if any(seen == key for seen, _ in self.polled):
            return False
        self.polled.append((key, normalised))
        return True

    # Reads for the assertions.

    def path(self, build_id) -> list[str]:
        return [t["to"] for t in self.transitions if t["build_id"] == build_id]

    def evidence(self, build_id, state) -> dict:
        return self.entered(build_id, state)[1]


# Adapters.

class FakeClock:
    def __init__(self, advance=True):
        self.at = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
        self.advance = advance
        self.lock = threading.Lock()
        self.slept: list[float] = []

    def now(self):
        with self.lock:
            return self.at

    def sleep(self, seconds):
        self.slept.append(seconds)
        if self.advance:
            self.forward(seconds)
        else:
            threading.Event().wait(0.002)

    def forward(self, seconds):
        with self.lock:
            self.at += timedelta(seconds=seconds)


class FakeTracker:
    def __init__(self):
        self.cards: dict[str, Card] = {}
        self.statuses: dict[str, list[TrackerStatus]] = defaultdict(list)

    def add(self, key):
        self.cards[key] = Card(key=key, title=f"Card {key}", body=BODY, status="Ready",
                               labels=("cloud",))

    def read(self, key):
        return self.cards[key]

    def status(self, key):
        queued = self.statuses[key]
        return queued.pop(0) if queued else TrackerStatus("Done", True)

    def closing_reference(self, key):
        return ClosingReference(title_token=key, branch_token=key)


class FakeHost:
    def __init__(self):
        self.lock = threading.Lock()
        self.branches: dict[str, str] = {}
        self.prs: dict[int, PullRequest] = {}
        self.by_branch: dict[str, int] = {}
        self.created = 0
        self.checks: list[CheckPost] = []
        self.crash_after_open = False

    def create_branch(self, name, sha):
        with self.lock:
            found = self.branches.get(name)
            if found and found != sha:
                raise BranchConflict(name, sha, found)
            self.branches[name] = sha
            return Branch(name, sha, found is None)

    def commit_on_remote(self, branch, sha):
        return self.branches.get(branch) == sha

    def open_pull_request(self, branch, title, reference, body=""):
        with self.lock:
            if branch not in self.by_branch:
                self.created += 1
                number = 100 + self.created
                self.by_branch[branch] = number
                self.prs[number] = PullRequest(number, OPEN, False, self.branches[branch],
                                               head_ref=branch, title=title,
                                               url=f"https://host.example/pr/{number}")
            pr = self.prs[self.by_branch[branch]]
        if self.crash_after_open:
            self.crash_after_open = False
            raise Crash("after open")
        return pr

    def post_check(self, sha, name, state, url=None, summary=""):
        post = CheckPost(sha, name, state, str(len(self.checks) + 1))
        self.checks.append(replace(post, url=summary))
        return post

    def enqueue(self, pr, landing=None):
        return AUTO_MERGE_ARMED

    def pull_request(self, number):
        return self.prs[number]

    def probe_landing(self, project):
        return Landing("auto_merge_strict", "squash", PINNED, PINNED)

    # Moves the tests make on the host's behalf.

    def change(self, number, **fields):
        self.prs[number] = replace(self.prs[number], **fields)

    def checks_on(self, sha):
        return [(c.name, c.state) for c in self.checks if c.sha == sha]


class FakeBuilder:
    """A remote session. Each session ends after polls_to_finish polls, or when released."""

    def __init__(self, world, schedule=Schedule.REMOTE_SESSION):
        self.world = world
        self.schedule = schedule
        self.starts: list[tuple[str, str]] = []
        self.sessions: dict[str, tuple[str, str]] = {}
        self.polls: Counter = Counter()
        self.polls_to_finish = 1
        self.gates: dict[str, threading.Event] = {}
        self.push = True
        self.never_finish = False

    def start(self, handoff, binding, credential):
        self.starts.append((handoff.branch, binding.model))
        session_ref = f"session-{handoff.card_key}-{len(self.starts)}"
        self.sessions[session_ref] = (handoff.card_key, handoff.branch)
        return session_ref

    def poll(self, session_ref):
        key, branch = self.sessions[session_ref]
        gate = self.gates.get(key)
        if gate is not None and not gate.is_set():
            return SessionStatus(False, detail="running")
        self.polls[session_ref] += 1
        if self.never_finish or self.polls[session_ref] < self.polls_to_finish:
            return SessionStatus(False, detail="running")
        return SessionStatus(True, self.world.result(branch, session_ref, self.push))

    def release(self, session_ref):
        pass

    def delete(self, session_ref):
        pass

    def events(self, session_ref):
        return None


class World:
    """One temporary SLIPWAY_HOME, the fakes, and a FakeEngine over them."""

    def __init__(self, tmp_path, advance=True, projects=None, records=None):
        self.home = tmp_path / "home"
        (self.home / "projects").mkdir(parents=True)
        (self.home / "operator.toml").write_text((FIXTURES / "operator.toml").read_text())
        text = (FIXTURES / "project.toml").read_text()
        for slug, cap in (projects or {"example": 3}).items():
            self.write_project(slug, text.replace("build_cap = 3", f"build_cap = {cap}")
                               .replace("example-org/example-app", f"example-org/{slug}"))
        self.clock = FakeClock(advance)
        self.records = records if records is not None else FakeRecords(self.clock)
        self.tracker = FakeTracker()
        self.host = FakeHost()
        self.builder = FakeBuilder(self)
        self.verdicts: list[str] = []
        self.verify_calls = 0
        self.seams = Registry()
        self.ports = card.Ports(
            records=self.records,
            project=lambda slug: load_project(slug, self.home),
            operator=lambda: load_operator(self.home),
            binding=lambda slug, role: resolve_binding(slug, role, self.home),
            tracker=lambda project: self.tracker,
            host=lambda project: self.host,
            builder=lambda project, binding: self.builder,
            credential=lambda project: "credential",
            worker_label=lambda project: "laptop-builder",
            laptop=self.laptop,
            git=FakeGit(),
            verify=self.verify,
            clock=self.clock,
            seams=self.seams,
        )
        self.engine = FakeEngine(self.ports)
        self.laptop_queue_seconds = 0
        self.commits = 0

    def write_project(self, slug, text):
        (self.home / "projects" / f"{slug}.toml").write_text(text)

    def result(self, branch, provider_ref, push=True):
        if not push:
            return BuilderResult(branch, None, "exit 0, nothing pushed", provider_ref)
        self.commits += 1
        commit = sha(self.commits)
        self.host.branches[branch] = commit
        return BuilderResult(branch, commit, "end_turn", provider_ref, cost=1.5)

    def laptop(self, input, label):
        # The task sits queued until a worker with the label appears.
        self.clock.forward(self.laptop_queue_seconds)

        def run(handoff, binding, project):
            result = self.result(handoff.branch, "laptop-session")
            return LaptopReturn(result, '{"type": "user"}\n', 0)

        return card.laptop_build(card.LaptopPorts(run, self.clock), input)

    def verify(self, cache, baseline, confirmed, tested, merged, key, status, landing_path,
               tested_head=None):
        self.verify_calls += 1
        if self.verdicts and self.verdicts[0] == COMMIT_MISMATCH:
            self.verdicts.pop(0)
            found = halt(COMMIT_MISMATCH, landing_path=landing_path,
                         relation="tree_equals_confirmed", expected="tree-1", found="tree-2")
            return Verdict(HALTED, (Check("confirmed_landed", FAIL),), {}, found)
        if status.terminal:
            return Verdict(LANDED, (Check("card_terminal", PASS),), {})
        found = halt(MERGED_CARD_NOT_MOVED, card_key=key, merged=merged,
                     tracker_status=status.status)
        return Verdict(PARTIAL_LANDING, (Check("card_terminal", FAIL),), {}, found)

    def card(self, key, project="example"):
        self.tracker.add(key)
        return self.records.claim(project, key)

    def start(self, build_id, run_id="run-1", project="example"):
        key = self.records.builds[build_id]["card_key"]
        return asyncio.ensure_future(
            card.card_workflow(self.engine, project, key, build_id, run_id))

    def state(self, build_id):
        return self.records.builds[build_id]["state"]

    def pr_of(self, build_id):
        return self.records.builds[build_id]["pr_number"]


class FakeGit:
    def __init__(self):
        self.held: list[tuple[str, str]] = []

    def base(self, project):
        return BASE

    def is_ancestor(self, project, ancestor, descendant):
        return True

    def hold(self, project, branch, commit):
        self.held.append((branch, commit))

    def cache(self, project, commits):
        return "cache"


class FakeEngine:
    """Hatchet's start a child and wait, in process."""

    def __init__(self, ports):
        self.ports = ports
        self.waiters: dict[int, list[asyncio.Future]] = defaultdict(list)
        self.limits: dict[tuple, asyncio.Semaphore] = {}
        self.active: Counter = Counter()
        self.peak: Counter = Counter()
        self.started: list[tuple[str, int]] = []

    async def child(self, name, input):
        spec = card.CHILDREN[name]
        group = None
        if spec.key:
            limit = input[spec.max_runs] if isinstance(spec.max_runs, str) else spec.max_runs
            group = (name, input[spec.key])
            gate = self.limits.setdefault(group, asyncio.Semaphore(limit))
            await gate.acquire()
            self.active[group] += 1
            self.peak[group] = max(self.peak[group], self.active[group])
        try:
            self.started.append((name, input["build_id"]))
            return await asyncio.to_thread(card.BODIES[name], self.ports, dict(input))
        finally:
            if group:
                self.active[group] -= 1
                self.limits[group].release()

    async def wait(self, signal, build_id):
        future = asyncio.get_running_loop().create_future()
        self.waiters[build_id].append(future)
        try:
            return await future
        finally:
            self.waiters[build_id].remove(future)

    def push(self, kind, build_id, **payload):
        """One event wakes every parent waiting on the build."""
        for future in list(self.waiters[build_id]):
            if not future.done():
                future.set_result(Wake(kind, {**payload, "build_id": build_id}))

    def parked(self, build_id) -> int:
        return len(self.waiters[build_id])


async def until(predicate, seconds=10):
    """Let the loop run until predicate() holds."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.002)


def run(coroutine_factory):
    return asyncio.run(coroutine_factory())


def drive(world, build_id):
    """Run one card's parent to its end, with no events pushed."""
    async def go():
        return await world.start(build_id)
    return run(go)


async def parked(world, build_id):
    await until(lambda: world.engine.parked(build_id) == 1)
