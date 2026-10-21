"""Runner tracking: the hourly cron that rolls the runner's main onto each instance (U22, R37).

Runner instances track the runner repository's default branch (KTD20). Once
an hour reconcile() does this:

1. Read the default branch head from the coordinates in the operator file.
2. Skip it if a rejected runner_builds row names it: the rollback evaluator
   (slipway.runner.rollback) rejected it, and only a newer head is tried.
3. Leave alone every instance already on the head. Group the rest by host,
   since a binary built on the VPS does not run on the laptop.
4. On each host, build the head and run the runner's own tests through the
   deploy/runner scripts, in a sandbox container on that host. A failure
   writes a runner_build_red finding naming the host and commit, leaves
   the host's instances where they are, and is not retried for that commit;
   a newer head is.
5. For each instance on a host that passed, ask the runner's read API
   whether any runner process under the instance's name prefix is busy. A
   busy instance waits for the next hour. An idle one, every process online
   and waiting or offline between jobs, is swapped: the swap script
   repoints the path the supervisor launches next, then a compare and set
   closes the active row's interval and opens the head's, at one instant,
   so a gate run is attributed to exactly one build by its start time.

If the compare and set loses, because the evaluator rolled the instance
back in the same moment, the swap script is run again for whichever build
the records now say is active, so the binary always follows the record.

Nothing here builds or runs the runner's code (KTD18). The scripts do, on
the runner's host; this module only calls them and writes the records.
"""

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from slipway.config.operator import load_operator
from slipway.runner.instances import (
    BUILD_RED,
    PostgresRecords,
    ReadApi,
    RunnerPorts,
    ShellScripts,
    busy_under,
    finding,
    git_head,
)

TASK = "runner_track"
CRON = "0 * * * *"
EXECUTION = timedelta(minutes=50)

# Where the deploy/runner scripts live on every host, and how to reach each
# host that is not the worker's own, as JSON: {"<host>": ["ssh", "<target>"]}.
# Without SCRIPTS_ENV the cron is not registered.
SCRIPTS_ENV = "SLIPWAY_RUNNER_SCRIPTS"
REMOTES_ENV = "SLIPWAY_RUNNER_REMOTES"

# Outcomes per instance.
CURRENT = "current"
BUILD_RED_BEFORE = "build_red_before"
BUILD_RED_NOW = "build_red"
API_UNREACHABLE = "api_unreachable"
BUSY = "busy"
SWAP_ERROR = "swap_error"
LOST_RACE = "lost_race"
SWAPPED = "swapped"


def reconcile(ports: RunnerPorts) -> dict:
    """One tracking pass over every runner instance in the operator file."""
    operator = ports.operator()
    repository = operator.runner_repository
    if repository is None or not operator.runner_instances:
        return {"skipped": "the operator file names no runner repository or instances"}
    records = ports.records
    head = ports.head(repository)
    out: dict[str, Any] = {"head": head, "instances": {}}
    if records.rejected(head):
        out["skipped"] = "rejected"
        return out

    pending: dict[str, list] = {}
    for name, instance in sorted(operator.runner_instances.items()):
        active = records.active_build(name)
        if active is not None and active.runner_commit == head:
            out["instances"][name] = {"outcome": CURRENT}
        else:
            pending.setdefault(instance.host, []).append((instance, active))

    for host, members in pending.items():
        fingerprint = f"{BUILD_RED}:{host}:{head}"
        if records.has_finding(fingerprint):
            for instance, _ in members:
                out["instances"][instance.name] = {"outcome": BUILD_RED_BEFORE}
            continue
        result = ports.scripts.build(host, repository, head)
        if result.ok:
            result = ports.scripts.test(host, head)
        if not result.ok:
            finding_id = records.add_finding(finding(BUILD_RED, fingerprint, head,
                                                     result.output))
            for instance, _ in members:
                out["instances"][instance.name] = {"outcome": BUILD_RED_NOW,
                                                   "finding_id": finding_id}
            continue
        # Asked after the build, which can take minutes, so the answer is fresh.
        try:
            runners = list(ports.api.runners())
        except Exception as error:  # an unreachable API never means idle
            runners = None
            out["api"] = str(error)
        for instance, active in members:
            if runners is None:
                out["instances"][instance.name] = {"outcome": API_UNREACHABLE}
                continue
            out["instances"][instance.name] = _swap(ports, host, instance.name, instance.prefix,
                                                    active, head, runners)
    return out


def _swap(ports: RunnerPorts, host: str, name: str, prefix: str, active, head: str,
          runners: list) -> dict:
    busy = busy_under(prefix, runners)
    if busy:
        return {"outcome": BUSY, "busy": busy}
    swapped = ports.scripts.swap(host, name, head)
    if not swapped.ok:
        return {"outcome": SWAP_ERROR, "output": swapped.output[-500:]}
    new_id = ports.records.activate(name, head, active.id if active else None, ports.now())
    if new_id is None:
        current = ports.records.active_build(name)
        if current is not None:
            ports.scripts.swap(host, name, current.runner_commit)
        return {"outcome": LOST_RACE,
                "active": current.runner_commit if current is not None else None}
    return {"outcome": SWAPPED, "runner_build_id": new_id,
            "previous": active.runner_commit if active else None}


def real_ports(conn, environ: Mapping[str, str] | None = None) -> RunnerPorts | None:
    """RunnerPorts from the environment and SLIPWAY_HOME, or None when not deployed."""
    environ = os.environ if environ is None else environ
    directory = environ.get(SCRIPTS_ENV)
    if not directory:
        return None
    operator = load_operator()
    if not operator.runner_read_api:
        raise ValueError("the operator file names no runner_read_api")
    remotes = json.loads(environ.get(REMOTES_ENV) or "{}")
    return RunnerPorts(
        records=PostgresRecords(conn),
        scripts=ShellScripts(directory, remotes),
        api=ReadApi(operator.runner_read_api),
        head=git_head,
        operator=load_operator,
        now=lambda: datetime.now(UTC),
    )


def register(hatchet, make_ports: Callable[[], RunnerPorts],
             close: Callable[[RunnerPorts], None] | None = None):
    """Declare the runner tracking cron on hatchet and return the task."""
    from hatchet_sdk import Context, EmptyModel

    def runner_track(input: EmptyModel, ctx: Context) -> dict:
        ports = make_ports()
        try:
            return reconcile(ports)
        finally:
            if close:
                close(ports)

    return hatchet.task(name=TASK, on_crons=[CRON], input_validator=EmptyModel,
                        execution_timeout=EXECUTION, retries=0)(runner_track)


__all__ = ["CRON", "EXECUTION", "TASK", "real_ports", "reconcile", "register"]
