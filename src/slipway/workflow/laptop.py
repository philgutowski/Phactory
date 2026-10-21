"""The laptop worker's entry point (U30): `python -m slipway.workflow.laptop`.

The laptop worker is the Hatchet worker on the operator's laptop that runs
laptop_build, and only that, for every project whose project file names a
builder_worker (KTD12). worker.laptop_worker registers it; this module builds
what it runs from the laptop's own environment and starts it, the way
worker.main starts the VPS worker (U29).

The laptop never reads a project file or the operator file. What it needs
of the project comes in the task's input (card.laptop_project): the slug,
the host and repository, clone or worktree, and the names of the
environment variables that hold the git token and, in the binding, the
account token. Everything that belongs to the machine comes from these
variables, which Slipway names and the laptop's environment file holds
(deploy/laptop/README.md):

    SLIPWAY_WORKER_LABEL    the label of this machine's entry in the
                            operator file's builder_workers
    SLIPWAY_SESSION_USER    the second user each session runs as
    SLIPWAY_SESSIONS        the folder session directories live in, one
                            folder per project under it
    SLIPWAY_SESSION_PATH    the PATH a session gets
    SLIPWAY_BROWSERS        the folder Playwright's browsers are installed
                            in, which headless_browser needs
    SLIPWAY_DESKTOP_SERVER  the computer use MCP server entry, a JSON object
                            with command, args, and env, which desktop needs
    SLIPWAY_CHECKOUTS       a JSON object from project slug to the path of
                            that project's shared checkout, which checkout
                            worktree needs

The first four are required: the worker refuses to start, naming each one
unset. The last three are needed only by a project that uses them, so a
build that needs one the machine lacks is refused at its start, naming the
variable, before any session or folder exists, and ends returned_empty with
that reason. A checkout path is the machine's, never the project file's (R1).

The git credential needs no helper of the worker's own: LaptopBuilder
writes each session's token to that session's run/token and points the
session's git at it (builders/cli.py).
"""

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from slipway.builders.base import BUILDER_FAILED, BuilderResult, Credential
from slipway.builders.cli import GitAccess, LaptopBuilder, LaptopReturn, default_run, laptop_build
from slipway.builders.handoff import Handoff
from slipway.builders.tools import ordered
from slipway.config.bindings import ResolvedBinding
from slipway.config.operator import Host
from slipway.config.project import BuilderTool, Checkout
from slipway.workflow.worker import GIT_HOSTS, GIT_USERNAMES, SESSIONS, laptop_worker

LABEL = "SLIPWAY_WORKER_LABEL"
SESSION_USER = "SLIPWAY_SESSION_USER"
SESSION_PATH = "SLIPWAY_SESSION_PATH"
BROWSERS = "SLIPWAY_BROWSERS"
DESKTOP_SERVER = "SLIPWAY_DESKTOP_SERVER"
CHECKOUTS = "SLIPWAY_CHECKOUTS"
REQUIRED = (LABEL, SESSION_USER, SESSIONS, SESSION_PATH)

# The machine variable each builder tool needs. operator_browser needs none:
# it is a flag on the session's command line.
TOOL_VARIABLES = {BuilderTool.HEADLESS_BROWSER.value: BROWSERS,
                  BuilderTool.DESKTOP.value: DESKTOP_SERVER}


class MachineError(ValueError):
    """A machine variable is unset or unreadable; the message names it, never its value."""


class Refused(ValueError):
    """A build this machine cannot run as asked; the message names what is missing."""


@dataclass(frozen=True)
class Machine:
    """This laptop's setup, read once from its environment at start."""

    label: str
    session_user: str
    sessions: Path
    path: str
    browsers: str | None = None
    desktop_server: Mapping | None = None
    checkouts: Mapping[str, Path] = field(default_factory=dict)


def missing(environ: Mapping[str, str]) -> list[str]:
    return [name for name in REQUIRED if not environ.get(name)]


def _json_object(environ: Mapping[str, str], name: str) -> dict | None:
    text = environ.get(name, "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise MachineError(f"{name}: not JSON ({error.msg})") from None
    if not isinstance(value, dict):
        raise MachineError(f"{name}: not a JSON object")
    return value


def read_machine(environ: Mapping[str, str]) -> Machine:
    """The Machine from environ; raises MachineError naming the first bad variable."""
    unset = missing(environ)
    if unset:
        raise MachineError(f"unset: {', '.join(unset)}")
    desktop = _json_object(environ, DESKTOP_SERVER)
    if desktop is not None and not isinstance(desktop.get("command"), str):
        raise MachineError(f"{DESKTOP_SERVER}: names no command")
    checkouts = _json_object(environ, CHECKOUTS) or {}
    for slug, path in checkouts.items():
        if not isinstance(path, str) or not path:
            raise MachineError(f"{CHECKOUTS}: the entry for {slug!r} is not a path")
    return Machine(environ[LABEL], environ[SESSION_USER], Path(environ[SESSIONS]),
                   environ[SESSION_PATH], environ.get(BROWSERS) or None, desktop,
                   {slug: Path(path) for slug, path in checkouts.items()})


def check_tools(machine: Machine, tools) -> None:
    """Refuse a granted tool this machine has not set up, naming its variable."""
    present = {BROWSERS: machine.browsers, DESKTOP_SERVER: machine.desktop_server}
    for tool in ordered(tools):
        variable = TOOL_VARIABLES.get(tool)
        if variable and not present[variable]:
            raise Refused(f"{tool} is granted, but this laptop has no {variable} set")


def builder_for(machine: Machine, project: Mapping, environ: Mapping[str, str],
                run=default_run) -> LaptopBuilder:
    """The LaptopBuilder for one build, in a clone or a worktree as the project says."""
    if not project.get("slug"):
        raise Refused("the task names no project; the VPS worker is older than this laptop "
                      "worker")
    slug = project["slug"]
    checkout = Checkout(project.get("checkout") or Checkout.CLONE)
    shared = None
    if checkout is Checkout.WORKTREE:
        shared = machine.checkouts.get(slug)
        if shared is None:
            raise Refused(f"checkout worktree needs the shared checkout of {slug!r} in "
                          f"{CHECKOUTS}, which names none")
    host = Host(project["host"])
    git = GitAccess(f"https://{GIT_HOSTS[host]}/{project['repository']}.git",
                    GIT_USERNAMES[host])
    return LaptopBuilder(machine.sessions / slug, git, machine.session_user, machine.path,
                         run=run, environ=environ, checkout=checkout, shared=shared,
                         desktop_server=machine.desktop_server,
                         browsers_path=machine.browsers)


def git_credential(project: Mapping, environ: Mapping[str, str]) -> Credential:
    """The project's git token from the variable the task names, never refreshed.

    The operator file's host credentials are static tokens in the
    environment, as on the VPS (worker.host_credentials).
    """
    entry = project.get("credential") or {}
    name, variable = entry.get("name", ""), entry.get("env", "")
    if not variable or not environ.get(variable):
        raise Refused(f"host_credentials.{name}.env: environment variable "
                      f"{variable or '(none named)'} is unset on this laptop")
    return Credential(name, lambda: (environ[variable], None))


def refused(handoff: Handoff, binding: ResolvedBinding, reason: str) -> LaptopReturn:
    """A build that never started: returned_empty with the reason, as laptop_build does."""
    result = BuilderResult(handoff.branch, None, f"the session did not start: {reason}", "",
                           model=binding.model, account=binding.account.name,
                           halt_class=BUILDER_FAILED)
    return LaptopReturn(result, "", 0)


def session_runner(machine: Machine, environ: Mapping[str, str], run=default_run,
                   build: Callable = laptop_build) -> Callable:
    """card.LaptopPorts.run: check the build can run here, then run its session."""

    def session(handoff: Handoff, binding: ResolvedBinding, project: Mapping) -> LaptopReturn:
        try:
            check_tools(machine, handoff.builder_tools)
            if not environ.get(binding.account.env):
                raise Refused(f"accounts.{binding.account.name}.env: environment variable "
                              f"{binding.account.env} is unset on this laptop")
            builder = builder_for(machine, project, environ, run)
            credential = git_credential(project, environ)
        except Refused as error:
            return refused(handoff, binding, str(error))
        return build(builder, handoff, binding, credential)

    return session


def main(argv: list[str] | None = None, environ=None, hatchet: Callable | None = None) -> int:
    environ = os.environ if environ is None else environ
    try:
        machine = read_machine(environ)
    except MachineError as error:
        print(f"slipway laptop worker: {error}", file=sys.stderr)
        return 2
    if hatchet is None:
        from hatchet_sdk import Hatchet as hatchet
    worker = laptop_worker(hatchet(), machine.label, session_runner(machine, environ))
    worker.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["CHECKOUTS", "DESKTOP_SERVER", "Machine", "MachineError", "REQUIRED", "Refused",
           "builder_for", "check_tools", "git_credential", "main", "read_machine",
           "session_runner"]
