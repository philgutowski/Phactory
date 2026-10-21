"""U30 test scenarios for the laptop worker's entry point.

No test starts a worker, a session, sudo, or claude. The entry is given a
fake Hatchet that records what is registered, and the session runner a fake
build that records the LaptopBuilder it was handed.
"""

import plistlib
import re
from dataclasses import asdict
from pathlib import Path

import pytest

from slipway.builders.base import BUILDER_FAILED, RETURNED_EMPTY, BuilderResult
from slipway.builders.cli import LaptopBuilder, LaptopReturn
from slipway.builders.handoff import Handoff
from slipway.config.bindings import ResolvedBinding
from slipway.config.operator import Account, AccountKind, Host, HostCredential
from slipway.config.project import Checkout
from slipway.workflow import card, laptop, worker

ROOT = Path(__file__).resolve().parents[2]
PLIST = ROOT / "deploy" / "laptop" / "com.slipway.worker.plist"
README = ROOT / "deploy" / "laptop" / "README.md"
EXPLAINER = ROOT / "docs" / "explainers" / "u30-laptop-worker.md"

ACCOUNT_TOKEN = "sk-ant-oat01-" + "Q" * 40
GIT_TOKEN = "ghs_" + "A" * 36
DESKTOP = '{"command": "/opt/bin/computer-use", "args": ["--stdio"]}'
SHARED = "/Users/operator/code/example-app"

ENVIRON = {
    "SLIPWAY_WORKER_LABEL": "example-laptop",
    "SLIPWAY_SESSION_USER": "slipway-session",
    "SLIPWAY_SESSIONS": "/Users/Shared/slipway/sessions",
    "SLIPWAY_SESSION_PATH": "/opt/bin:/usr/bin:/bin",
    "SLIPWAY_BROWSERS": "/Users/Shared/slipway/browsers",
    "SLIPWAY_DESKTOP_SERVER": DESKTOP,
    "SLIPWAY_CHECKOUTS": f'{{"example-app": "{SHARED}"}}',
    "EXAMPLE_ACCOUNT_TOKEN": ACCOUNT_TOKEN,
    "EXAMPLE_GIT_TOKEN": GIT_TOKEN,
}

BINDING = ResolvedBinding("builder", Account("example", AccountKind.SUBSCRIPTION,
                                             "EXAMPLE_ACCOUNT_TOKEN"),
                          "example-model", "high", 60)


def project(checkout="worktree", slug="example-app", env="EXAMPLE_GIT_TOKEN") -> dict:
    return {"slug": slug, "host": "github", "repository": "example-org/example-app",
            "checkout": checkout, "credential": {"name": "example-app", "env": env}}


def handoff(tools=("headless_browser",)) -> Handoff:
    return Handoff("EX-12", "0" * 40, "slipway/EX-12-hello-page", "rules", (), (), "done",
                   "example-app", "refs/slipway/log/EX-12", "text", tuple(tools))


class FakeHatchet:
    """Records what the laptop worker registers and the worker it asks for."""

    def __init__(self):
        self.tasks, self.workers = {}, []

    class stubs:
        @staticmethod
        def task(**options):
            return ("stub", options["name"])

    def task(self, **options):
        def wrap(fn):
            self.tasks[options["name"]] = fn
            return fn
        return wrap

    def durable_task(self, **options):
        def wrap(fn):
            self.tasks[options["name"]] = fn
            return fn
        return wrap

    def worker(self, name, slots, labels=None, workflows=()):
        class Worker:
            started = False

            def start(self):
                Worker.started = True

        found = Worker()
        self.workers.append({"name": name, "slots": slots, "labels": labels,
                             "workflows": list(workflows), "worker": found})
        return found


class Built:
    """A stand in for cli.laptop_build: records the builder and the credential."""

    def __init__(self):
        self.calls = []

    def __call__(self, builder, handoff, binding, credential):
        self.calls.append((builder, credential))
        return LaptopReturn(BuilderResult(handoff.branch, "c" * 40, "end_turn", "session"),
                            "", 0)


def started_with(environ, monkeypatch):
    """Run main with a fake Hatchet and a recording build; return both."""
    built = Built()
    runner = laptop.session_runner
    monkeypatch.setattr(laptop, "session_runner",
                        lambda machine, env: runner(machine, env, build=built))
    hatchet = FakeHatchet()
    assert laptop.main([], dict(environ), hatchet=lambda: hatchet) == 0
    return hatchet, built


# With every variable set.

def test_the_entry_builds_a_laptop_builder_from_the_environment_and_registers_only_laptop_build(
        monkeypatch):
    hatchet, built = started_with(ENVIRON, monkeypatch)

    [registered] = hatchet.workers
    assert registered["labels"] == {worker.LABEL_KEY: "example-laptop"}
    assert registered["workflows"] == [hatchet.tasks[card.LAPTOP_BUILD]]
    assert registered["worker"].started

    # The registered task runs a session through the entry's own runner.
    from hatchet_sdk import EmptyModel

    input = {"build_id": 7, "handoff": asdict(handoff(("headless_browser", "desktop"))),
             "binding": card._binding_json(BINDING), "budget_seconds": 3600,
             "project": project()}
    out = hatchet.tasks[card.LAPTOP_BUILD](EmptyModel(**input), None)
    assert out["result"]["commit"] == "c" * 40

    [(builder, credential)] = built.calls
    assert isinstance(builder, LaptopBuilder)
    assert builder.session_user == "slipway-session"
    assert builder.root == Path("/Users/Shared/slipway/sessions/example-app")
    assert builder.path == "/opt/bin:/usr/bin:/bin"
    assert builder.browsers_path == "/Users/Shared/slipway/browsers"
    assert builder.desktop_server == {"command": "/opt/bin/computer-use", "args": ["--stdio"]}
    assert builder.checkout is Checkout.WORKTREE and builder.shared == Path(SHARED)
    assert builder.git.remote == "https://github.com/example-org/example-app.git"
    assert builder.git.username == "x-access-token"
    assert credential.name == "example-app" and credential.current() == GIT_TOKEN


def test_the_vps_sends_the_laptop_what_it_needs_of_the_project_and_no_value():
    class Project:
        slug, host, repository = "example-app", Host.GITHUB, "example-org/example-app"
        host_credential, checkout = "example-app", Checkout.WORKTREE

    class Operator:
        host_credentials = {"example-app": HostCredential("example-app", Host.GITHUB, "app",
                                                          "EXAMPLE_GIT_TOKEN")}

    assert card.laptop_project(Project(), Operator()) == project()


# With a required variable unset.

@pytest.mark.parametrize("name", ["SLIPWAY_WORKER_LABEL", "SLIPWAY_SESSION_USER"])
def test_the_entry_refuses_to_start_naming_the_unset_variable(name, capsys):
    environ = {k: v for k, v in ENVIRON.items() if k != name}
    hatchet = FakeHatchet()
    assert laptop.main([], environ, hatchet=lambda: hatchet) == 2
    err = capsys.readouterr().err
    assert name in err
    assert hatchet.workers == []


def test_the_entry_names_every_unset_variable_and_refuses_an_unreadable_one(capsys):
    assert laptop.main([], {}, hatchet=FakeHatchet) == 2
    err = capsys.readouterr().err
    for name in laptop.REQUIRED:
        assert name in err
    assert laptop.main([], {**ENVIRON, "SLIPWAY_CHECKOUTS": "[1, 2]"}, hatchet=FakeHatchet) == 2
    assert "SLIPWAY_CHECKOUTS: not a JSON object" in capsys.readouterr().err
    assert laptop.main([], {**ENVIRON, "SLIPWAY_DESKTOP_SERVER": "{"},
                       hatchet=FakeHatchet) == 2
    err = capsys.readouterr().err
    assert "SLIPWAY_DESKTOP_SERVER: not JSON" in err and "computer-use" not in err


# Clone and worktree.

def runner_for(environ):
    built = Built()
    machine = laptop.read_machine(environ)
    return laptop.session_runner(machine, environ, build=built), built


def test_a_worktree_project_missing_from_the_checkout_map_is_refused_at_the_start_of_its_build():
    environ = {k: v for k, v in ENVIRON.items() if k != "SLIPWAY_CHECKOUTS"}
    session, built = runner_for(environ)
    back = session(handoff(), BINDING, project("worktree"))
    assert built.calls == []
    assert back.result.outcome == RETURNED_EMPTY and back.result.halt_class == BUILDER_FAILED
    assert "SLIPWAY_CHECKOUTS" in back.result.reason and "example-app" in back.result.reason

    other = session(handoff(), BINDING, project("worktree", slug="other-app"))
    assert built.calls == [] and "other-app" in other.result.reason


def test_a_clone_project_needs_no_checkout_entry():
    environ = {k: v for k, v in ENVIRON.items() if k != "SLIPWAY_CHECKOUTS"}
    session, built = runner_for(environ)
    back = session(handoff(), BINDING, project("clone", slug="other-app"))
    assert back.result.commit == "c" * 40
    [(builder, _)] = built.calls
    assert builder.checkout is Checkout.CLONE and builder.shared is None
    assert builder.root == Path("/Users/Shared/slipway/sessions/other-app")


# Builder tools the machine has not set up.

def test_a_project_granting_desktop_without_a_desktop_server_is_refused_naming_the_variable():
    environ = {k: v for k, v in ENVIRON.items() if k != "SLIPWAY_DESKTOP_SERVER"}
    session, built = runner_for(environ)
    back = session(handoff(("headless_browser", "desktop")), BINDING, project())
    assert built.calls == []
    assert back.result.outcome == RETURNED_EMPTY and back.result.halt_class == BUILDER_FAILED
    assert "desktop" in back.result.reason and "SLIPWAY_DESKTOP_SERVER" in back.result.reason

    # A project granting only the browser builds on the same machine.
    assert session(handoff(), BINDING, project()).result.commit == "c" * 40


def test_a_headless_browser_without_a_browsers_folder_is_refused_naming_the_variable():
    environ = {k: v for k, v in ENVIRON.items() if k != "SLIPWAY_BROWSERS"}
    session, built = runner_for(environ)
    back = session(handoff(), BINDING, project())
    assert built.calls == [] and "SLIPWAY_BROWSERS" in back.result.reason
    # operator_browser needs nothing of the machine.
    assert session(handoff(("operator_browser",)), BINDING,
                   project()).result.commit == "c" * 40


def test_an_unset_token_is_refused_by_its_variable_name_never_its_value():
    session, built = runner_for({k: v for k, v in ENVIRON.items() if k != "EXAMPLE_GIT_TOKEN"})
    back = session(handoff(), BINDING, project())
    assert built.calls == [] and "EXAMPLE_GIT_TOKEN" in back.result.reason

    session, built = runner_for({k: v for k, v in ENVIRON.items()
                                 if k != "EXAMPLE_ACCOUNT_TOKEN"})
    back = session(handoff(), BINDING, project())
    assert built.calls == [] and "EXAMPLE_ACCOUNT_TOKEN" in back.result.reason

    session, built = runner_for(ENVIRON)
    back = session(handoff(), BINDING, {})
    assert built.calls == [] and "names no project" in back.result.reason
    assert GIT_TOKEN not in back.result.reason and ACCOUNT_TOKEN not in back.result.reason


# The launch file and the README.

def test_the_launch_file_runs_the_entry_and_reads_the_environment_file_the_readme_names():
    plist = plistlib.loads(PLIST.read_bytes())
    command = plist["ProgramArguments"][-1]
    assert "exec python -m slipway.workflow.laptop" in command
    assert "SLIPWAY_WORKER_COMMAND" not in PLIST.read_text()
    [env_file] = re.findall(r'\. "(\$HOME/[^"]+)"', command)
    readme = README.read_text()
    assert f"`{env_file}`" in readme
    for name in (*laptop.REQUIRED, laptop.BROWSERS, laptop.DESKTOP_SERVER, laptop.CHECKOUTS):
        assert f"`{name}`" in readme


@pytest.mark.parametrize("path", [README, EXPLAINER, PLIST,
                                  ROOT / "src" / "slipway" / "workflow" / "laptop.py"],
                         ids=lambda p: p.name)
def test_no_dashes_as_punctuation(path):
    text = path.read_text()
    assert chr(0x2014) not in text and chr(0x2013) not in text
    if path == PLIST:
        assert "--" not in text
    if path.suffix == ".md":
        prose = re.sub(r"```.*?```", "", text, flags=re.S)
        assert "--" not in re.sub(r"`[^`]*`", "", prose)
