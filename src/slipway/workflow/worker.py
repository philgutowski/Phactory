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

    vps_worker(...)     every task except laptop_build, the watch cron, and
                        the runner tracking cron when its scripts are set up
    laptop_worker(...)  laptop_build only, under the label the operator file
                        gives the builder worker (KTD12), with no records
                        connection and no operator file

Stop either with SIGTERM for a deploy: the engine lets running children
finish first (the spike's step 5).

The VPS worker's entry point is `python -m slipway.workflow.worker` (U29),
which main() below serves. It reads, besides SLIPWAY_HOME,
SLIPWAY_DATABASE_URL, and the Hatchet client's own variables:

    SLIPWAY_BUILDER_IMAGE     the tag built from deploy/builder.Dockerfile
    SLIPWAY_BUILDER_NETWORK   the isolated builder network's name
    SLIPWAY_BUILDER_USER      the uid:gid builder containers run as
    SLIPWAY_SESSIONS          the writable folder builder sessions live in;
                              the same path on the VPS and in the worker,
                              because docker resolves bind mounts on the VPS

and refuses to start, naming the variable, while one is unset. It also
points git at a credential helper, a small program git asks for a
password: `python -m slipway.workflow.worker credential` answers with the
host credential of the project whose repository git is reading, so the
git cache never holds a token in a URL.
"""

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from slipway import gitread
from slipway import verify as verify_module
from slipway.builders.base import BuilderResult, Credential, Schedule
from slipway.builders.handoff import InFlightBuild
from slipway.config import ConfigError, home
from slipway.config.bindings import ResolvedBinding, resolve_binding
from slipway.config.operator import Host, load_operator
from slipway.config.project import load_project
from slipway.hosts.base import resolve_host_credential
from slipway.records import queries, store
from slipway.service import verbs
from slipway.service.engine import WORKFLOW, HatchetEngine
from slipway.workflow import card, events, runner_track, watch
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

# One tracker factory for the workflow and the service layer, so the two cannot drift.
tracker_for = verbs.tracker_for


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
    workflows.append(watch.register(
        hatchet, lambda: verbs.real_ports(connect(), HatchetEngine(hatchet)),
        close=lambda ports: ports.records.conn.close()))
    # The runner tracking cron, only where the deployment set its scripts up (U22).
    if os.environ.get(runner_track.SCRIPTS_ENV):
        runner_track.check_operator()  # a missing setting fails at boot, not an hour later
        workflows.append(runner_track.register(
            hatchet, lambda: runner_track.real_ports(connect),
            close=lambda ports: ports.records.conn.close()))
    return hatchet.worker("slipway-worker", slots=slots, workflows=workflows)


def laptop_worker(hatchet, label: str, run: Callable, slots: int = 1):
    """The laptop worker: laptop_build only, under label (KTD12)."""
    tasks = register(hatchet, make_ports=lambda: None,
                     laptop_ports=lambda: card.LaptopPorts(run, WallClock()))
    return hatchet.worker("slipway-laptop", slots=slots, labels={LABEL_KEY: label},
                          workflows=[tasks[card.LAPTOP_BUILD]])


# The entry point (U29).

BUILDER_IMAGE = "SLIPWAY_BUILDER_IMAGE"
BUILDER_NETWORK = "SLIPWAY_BUILDER_NETWORK"
BUILDER_USER = "SLIPWAY_BUILDER_USER"
SESSIONS = "SLIPWAY_SESSIONS"
DATABASE_URL = "SLIPWAY_DATABASE_URL"
REQUIRED = (DATABASE_URL, BUILDER_IMAGE, BUILDER_NETWORK, BUILDER_USER, SESSIONS)

# The user name each host expects beside a token over HTTPS. These are the
# hosts' own conventions, not a project's.
GIT_USERNAMES = {Host.GITHUB: "x-access-token", Host.BITBUCKET: "x-token-auth"}


class LaptopScheduled:
    """The builder port's answer for a project built on the laptop (KTD12).

    The VPS worker only reads its schedule: worker_process sends the build
    to the laptop worker through laptop_trigger, which runs the real
    LaptopBuilder there.
    """

    schedule = Schedule.WORKER_PROCESS


def remote_of(project) -> str:
    return f"https://{GIT_HOSTS[project.host]}/{project.repository}.git"


def container_builders(environ: Mapping[str, str]) -> Callable[[Any, ResolvedBinding], Any]:
    """card.Ports.builder for the VPS: a container per card, or the laptop's schedule."""
    from slipway.builders.cli import ContainerBuilder, GitAccess

    def builder(project, binding: ResolvedBinding):
        if project.builder_worker:
            return LaptopScheduled()
        git = GitAccess(remote_of(project), GIT_USERNAMES[project.host])
        return ContainerBuilder(Path(environ[SESSIONS]) / project.slug, git,
                                environ[BUILDER_IMAGE], environ[BUILDER_NETWORK],
                                environ[BUILDER_USER], environ=environ)

    return builder


def host_credentials(environ: Mapping[str, str]) -> Callable[[Any], Credential]:
    """card.Ports.credential: the project's host credential as a builder Credential.

    The operator file's host credentials are static tokens in the
    environment, so the credential has no expiry and is never refreshed
    (builders.base.Credential, follow up #54).
    """

    def credential(project) -> Credential:
        def mint():
            return resolve_host_credential(project, load_operator(), environ).token, None

        return Credential(project.host_credential, mint)

    return credential


def credential_answer(request: Mapping[str, str], environ: Mapping[str, str],
                      root: Path | None = None) -> dict[str, str]:
    """git's credential helper protocol: the request's host and path, the answer's login.

    git sends protocol, host, and path (with credential.useHttpPath set);
    the answer is the project's host credential when exactly one project
    file names that repository on that host, and nothing otherwise, which
    git reads as "no credential here".
    """
    if request.get("protocol") != "https":
        return {}
    repository = request.get("path", "").removesuffix(".git").strip("/")
    for host, address in GIT_HOSTS.items():
        if request.get("host") != address:
            continue
        for slug in verbs.configured_slugs(root):
            try:
                project = load_project(slug, root)
            except ConfigError:
                continue
            if project.host is host and project.repository == repository:
                found = resolve_host_credential(project, load_operator(root), environ)
                return {"username": GIT_USERNAMES[host], "password": found.token}
    return {}


def credential_helper(argv: list[str], stdin, stdout, environ: Mapping[str, str]) -> int:
    """`python -m slipway.workflow.worker credential get`: answer git on stdout."""
    if argv[-1:] != ["get"]:
        return 0  # store and erase: nothing is kept, so nothing to do
    request = dict(line.rstrip("\n").split("=", 1) for line in stdin if "=" in line)
    try:
        answer = credential_answer(request, environ)
    except Exception as error:  # git then fails the fetch, naming no value
        print(f"slipway credential helper: {type(error).__name__}", file=sys.stderr)
        return 1
    for name, value in answer.items():
        stdout.write(f"{name}={value}\n")
    return 0


def git_helper_config(executable: str = sys.executable) -> dict[str, str]:
    """The environment that points every git call at the credential helper."""
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": f"!{executable} -m slipway.workflow.worker credential",
        "GIT_CONFIG_KEY_1": "credential.useHttpPath",
        "GIT_CONFIG_VALUE_1": "true",
    }


def missing(environ: Mapping[str, str]) -> list[str]:
    return [name for name in REQUIRED if not environ.get(name)]


def main(argv: list[str] | None = None, environ=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    if argv[:1] == ["credential"]:
        return credential_helper(argv, sys.stdin, sys.stdout, environ)
    unset = missing(environ)
    if unset:
        print(f"slipway worker: unset: {', '.join(unset)}", file=sys.stderr)
        return 2
    import psycopg
    from hatchet_sdk import Hatchet

    environ.update(git_helper_config())
    dsn = environ[DATABASE_URL]
    worker = vps_worker(Hatchet(), lambda: psycopg.connect(dsn, autocommit=True),
                        container_builders(environ), host_credentials(environ))
    worker.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["CacheGit", "HatchetSteps", "LABEL_KEY", "LaptopScheduled", "PostgresRecords",
           "WallClock", "container_builders", "credential_answer", "git_helper_config",
           "host_credentials", "laptop_worker", "main", "real_ports", "register",
           "task_options", "vps_worker"]
