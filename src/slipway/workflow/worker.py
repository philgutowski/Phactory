"""The Hatchet worker: the card workflow's tasks, and the real ports they reach.

A worker is the process that connects out to the engine over gRPC and runs
the tasks it registered (docs/spikes/hatchet.md). card.py holds what each
task does; this module holds the three things that make those functions run
on Hatchet:

1. register(hatchet, make_ports) declares one Hatchet task per child in
   card.CHILDREN, with the concurrency key, timeouts, and retries the table
   gives, and the durable parent card_workflow with no key (KTD4). Each task
   body calls make_ports() for a fresh set of ports, so every step reads the
   project and operator files as they are when it runs, and closes them when
   it returns.
2. HatchetSteps adapts Hatchet's two primitives, start a child and wait, to
   card.Engine, so the parent body in card.py runs unchanged on the engine
   and in the tests' fake.
3. RealPorts builds card.Ports from SLIPWAY_HOME: the records store over
   Postgres, the tracker and host adapters, the builder adapters, the git
   cache, and verify.

Two workers, two registrations:

    vps_worker(...)     every task except laptop_build
    laptop_worker(...)  laptop_build only, under the label the operator file
                        gives the builder worker (KTD12), with no records
                        connection and no operator file

Stop either with SIGTERM for a deploy: the engine lets running children
finish first (the spike's step 5).
"""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from slipway import gitread
from slipway import verify as verify_module
from slipway.builders.base import BuilderResult
from slipway.builders.handoff import InFlightBuild
from slipway.config import home
from slipway.config.bindings import ResolvedBinding, resolve_binding
from slipway.config.operator import Host, load_operator
from slipway.config.project import Tracker, load_project
from slipway.hosts.base import resolve_host_credential
from slipway.records import queries, store
from slipway.service.engine import WORKFLOW
from slipway.workflow import card, events
from slipway.workflow.post_landed import load as load_seams

# The label key a laptop worker registers its builder worker label under.
LABEL_KEY = "builder_worker"

# Where each host serves git over HTTPS. These are the hosts' own addresses,
# not a project's: the project file names the repository.
GIT_HOSTS = {Host.GITHUB: "github.com", Host.BITBUCKET: "bitbucket.org"}


# Records over Postgres.

BUILD_COLUMNS = ("id", "project_id", "card_key", "state", "last_seq", "run_id", "branch",
                 "session_ref", "pr_number", "check_run_id", "confirmed_commit",
                 "tested_commit")


class PostgresRecords:
    """card.Records over one autocommit psycopg connection (records.store)."""

    def __init__(self, conn):
        self.conn = conn

    def build(self, build_id: int) -> card.Build:
        row = self.conn.execute(
            f"SELECT {', '.join('b.' + c for c in BUILD_COLUMNS)}, p.slug FROM builds b "
            "JOIN projects p ON p.id = b.project_id WHERE b.id = %s", (build_id,)).fetchone()
        if row is None:
            raise LookupError(f"no build {build_id}")
        values = dict(zip(BUILD_COLUMNS, row[:-1], strict=True))
        return card.Build(project=row[-1], **values)

    def attach_run(self, build_id: int, run_id: str):
        return store.attach_run(self.conn, build_id, run_id)

    def transition(self, build_id, from_state, to_state, actor, evidence, claims,
                   expected_seq):
        return store.record_transition(self.conn, build_id, from_state, to_state, actor,
                                       evidence, claims, expected_seq)

    def entered(self, build_id: int, state: str):
        row = self.conn.execute(
            "SELECT at, evidence FROM transitions WHERE build_id = %s AND to_state = %s "
            "ORDER BY seq DESC LIMIT 1", (build_id, state)).fetchone()
        if row is None:
            return None
        evidence = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        return row[0], evidence

    def slots_taken(self, project_id: int) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM builds WHERE project_id = %s AND state = ANY(%s)",
            (project_id, list(card.SLOT_STATES))).fetchone()[0]

    def in_flight(self, project_id: int) -> list[InFlightBuild]:
        return [InFlightBuild(row["card_key"], row["state"], tuple(row["paths"] or ()))
                for row in queries.in_flight_files(self.conn, project_id)]

    def attempts(self, build_id: int) -> int:
        return self.conn.execute("SELECT count(*) FROM attempts WHERE build_id = %s",
                                 (build_id,)).fetchone()[0]

    def start_attempt(self, build_id: int, binding: ResolvedBinding) -> int:
        return self.conn.execute(
            "INSERT INTO attempts (project_id, build_id, account, model, effort) "
            "SELECT project_id, id, %s, %s, %s FROM builds WHERE id = %s RETURNING id",
            (binding.account.name, binding.model, binding.effort, build_id)).fetchone()[0]

    def end_attempt(self, build_id: int, result: BuilderResult) -> None:
        self.conn.execute(
            "UPDATE attempts SET ended_at = now(), cost_usd = COALESCE(%s, cost_usd), "
            "model = COALESCE(%s, model), account = COALESCE(%s, account) "
            "WHERE id = (SELECT max(id) FROM attempts WHERE build_id = %s) "
            "AND ended_at IS NULL",
            (result.cost, result.model, result.account, build_id))

    def record_score(self, build_id: int, kind: str, result: dict) -> int:
        return store.record_score(self.conn, build_id, kind, result)

    def ingest_poll(self, project_id: int, normalised: dict) -> bool:
        return store.ingest_host_event(self.conn, "poll",
                                       {**normalised, "project_id": project_id}).inserted


# Git through Slipway's own cache. Nothing here runs project code (KTD18).

@dataclass
class CacheGit:
    """card.Git over gitread, one bare cache per project under SLIPWAY_HOME."""

    root: Path
    remote_for: Callable[[Any], str] = field(
        default=lambda project: f"https://{GIT_HOSTS[project.host]}/{project.repository}.git")

    def _cache(self, project) -> Path:
        return gitread.cache_path(self.root, project.slug)

    def base(self, project) -> str:
        cache = self._cache(project)
        branch = gitread.refresh(cache, self.remote_for(project))
        return gitread.rev_parse(cache, f"refs/heads/{branch}")

    def is_ancestor(self, project, ancestor: str, descendant: str) -> bool:
        cache = self._cache(project)
        gitread.refresh(cache, self.remote_for(project), commits=(descendant,))
        return gitread.is_ancestor(cache, ancestor, descendant)

    def hold(self, project, branch: str, commit: str) -> None:
        gitread.hold(self._cache(project), self.remote_for(project), branch, commit)

    def cache(self, project, commits: tuple[str, ...]) -> Path:
        cache = self._cache(project)
        gitread.refresh(cache, self.remote_for(project), commits=commits)
        return cache


class WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)


# The real ports, from SLIPWAY_HOME.

def tracker_for(project):
    if project.tracker is Tracker.GITHUB_PROJECTS:
        from slipway.trackers.github import GitHubTracker

        return GitHubTracker(project)
    from slipway.trackers.jira import JiraTracker

    return JiraTracker(project)


def host_for(project, environ: Mapping[str, str] | None = None):
    credential = resolve_host_credential(project, load_operator(),
                                         os.environ if environ is None else environ)
    if project.host is Host.GITHUB:
        from slipway.hosts.github import GitHubHost

        return GitHubHost(project, credential)
    from slipway.hosts.bitbucket import BitbucketHost

    return BitbucketHost(project, credential)


def worker_label(project) -> str:
    from slipway.builders.cli import worker_label as label_of

    return label_of(project, load_operator())


def real_ports(conn, builder: Callable[[Any, ResolvedBinding], Any],
               credential: Callable[[Any], Any], laptop: Callable[[dict, str], dict],
               seams=None) -> card.Ports:
    """card.Ports for the VPS worker over one connection.

    builder and credential are passed in: which builder adapters a deployment
    offers, and how it mints their credentials, is the deploy's to say (U12,
    U13), and both read only the project and operator files.
    """
    root = home()
    return card.Ports(
        records=PostgresRecords(conn),
        project=lambda slug: load_project(slug, root),
        operator=lambda: load_operator(root),
        binding=lambda slug, role: resolve_binding(slug, role, root),
        tracker=tracker_for,
        host=host_for,
        builder=builder,
        credential=credential,
        worker_label=worker_label,
        laptop=laptop,
        git=CacheGit(root),
        verify=verify_module.verify,
        clock=WallClock(),
        seams=seams if seams is not None else load_seams(),
    )


# Hatchet.

class HatchetSteps:
    """card.Engine over a Hatchet DurableContext and the registered child tasks."""

    def __init__(self, ctx, tasks: Mapping[str, Any]):
        self.ctx = ctx
        self.tasks = tasks

    async def child(self, name: str, input: dict) -> dict:
        from hatchet_sdk import EmptyModel

        return await self.tasks[name].aio_run(EmptyModel(**input))

    async def wait(self, signal: str, build_id: int) -> events.Wake:
        from hatchet_sdk import SleepCondition, UserEventCondition, or_

        expression = events.filter_expression(build_id)
        answer = await self.ctx.aio_wait_for(
            signal,
            or_(*(UserEventCondition(event_key=kind, expression=expression)
                  for kind in events.WAIT_KINDS),
                SleepCondition(timedelta(seconds=events.WAKE_SECONDS),
                               readable_data_key=events.SLEEP)),
        )
        return events.read_wake(answer)


def task_options(child: card.Child) -> dict:
    """The keyword arguments hatchet.task takes for one child, from card.CHILDREN."""
    from hatchet_sdk import ConcurrencyExpression, ConcurrencyLimitStrategy

    options = {"name": child.name, "execution_timeout": child.execution,
               "schedule_timeout": child.schedule, "retries": child.retries}
    if child.key:
        max_runs = child.max_runs
        options["concurrency"] = ConcurrencyExpression(
            expression=f"input.{child.key}",
            max_runs=f"input.{max_runs}" if isinstance(max_runs, str) else max_runs,
            limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
        )
    return options


def register(hatchet, make_ports: Callable[[], Any], close: Callable[[Any], None] = None,
             laptop_ports: Callable[[], card.LaptopPorts] | None = None) -> dict[str, Any]:
    """Declare every task on hatchet; return them by name, the parent under WORKFLOW.

    make_ports() gives the ports one child execution uses, and close(ports)
    releases them afterwards. laptop_ports, when given, registers
    laptop_build too; a VPS worker passes none and only triggers it.
    """
    from hatchet_sdk import Context, DurableContext, EmptyModel

    tasks: dict[str, Any] = {}

    def child_task(name: str, body):
        def run(input: EmptyModel, ctx: Context) -> dict:
            ports = make_ports()
            try:
                return body(ports, input.model_dump())
            finally:
                if close:
                    close(ports)

        run.__name__ = name
        return hatchet.task(input_validator=EmptyModel, **task_options(card.CHILDREN[name]))(run)

    for name, body in card.BODIES.items():
        tasks[name] = child_task(name, body)

    laptop = card.CHILDREN[card.LAPTOP_BUILD]
    if laptop_ports is not None:
        def laptop_run(input: EmptyModel, ctx: Context) -> dict:
            return card.laptop_build(laptop_ports(), input.model_dump())

        tasks[card.LAPTOP_BUILD] = hatchet.task(input_validator=EmptyModel,
                                                **task_options(laptop))(laptop_run)
    else:
        tasks[card.LAPTOP_BUILD] = hatchet.stubs.task(name=laptop.name,
                                                      input_validator=EmptyModel)

    @hatchet.durable_task(name=WORKFLOW, input_validator=EmptyModel,
                          execution_timeout=card.PARENT_EXECUTION,
                          schedule_timeout=card.PARENT_SCHEDULE)
    async def card_workflow(input: EmptyModel, ctx: DurableContext) -> dict:
        data = input.model_dump()
        return await card.card_workflow(HatchetSteps(ctx, tasks), data["project"], data["key"],
                                        data["build_id"], ctx.workflow_run_id)

    tasks[WORKFLOW] = card_workflow
    return tasks


def laptop_trigger(tasks: Mapping[str, Any]) -> Callable[[dict, str], dict]:
    """card.Ports.laptop: run laptop_build on the worker carrying label, and wait.

    The child key is made from the build id, so a build child the engine
    runs again reaches the laptop run the first try started instead of
    starting a second session on the laptop.
    """

    def run(input: dict, label: str) -> dict:
        from hatchet_sdk import DesiredWorkerLabel, EmptyModel, TriggerWorkflowOptions

        options = TriggerWorkflowOptions(
            desired_worker_label={LABEL_KEY: DesiredWorkerLabel(value=label, required=True)})
        out = tasks[card.LAPTOP_BUILD].run(EmptyModel(**input), options=options,
                                           child_key=f"{card.LAPTOP_BUILD}-{input['build_id']}")
        # A stub task hands its output back as a model, not a mapping.
        return out.model_dump() if hasattr(out, "model_dump") else dict(out)

    return run


def vps_worker(hatchet, connect: Callable[[], Any], builder, credential, slots: int = 20):
    """The VPS worker: every task but laptop_build, each child on its own connection."""
    tasks: dict[str, Any] = {}

    def make_ports():
        return real_ports(connect(), builder, credential, laptop_trigger(tasks))

    tasks.update(register(hatchet, make_ports, close=lambda ports: ports.records.conn.close()))
    workflows = [t for name, t in tasks.items() if name != card.LAPTOP_BUILD]
    return hatchet.worker("slipway-worker", slots=slots, workflows=workflows)


def laptop_worker(hatchet, label: str, run: Callable, slots: int = 1):
    """The laptop worker: laptop_build only, under label (KTD12)."""
    tasks = register(hatchet, make_ports=lambda: None,
                     laptop_ports=lambda: card.LaptopPorts(run, WallClock()))
    return hatchet.worker("slipway-laptop", slots=slots, labels={LABEL_KEY: label},
                          workflows=[tasks[card.LAPTOP_BUILD]])


__all__ = ["CacheGit", "HatchetSteps", "LABEL_KEY", "PostgresRecords",
           "WallClock", "laptop_worker", "real_ports", "register", "task_options",
           "vps_worker"]
