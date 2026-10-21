"""U13 test scenarios for the headless CLI adapter and the laptop worker.

No test launches docker, sudo, or claude: those calls go to a FakeRun. Git
calls in the side reference and credential helper tests run for real against
bare repositories on disk, which the network guard allows.
"""

import json
import os
import plistlib
import subprocess
import threading
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from _fakes import FakeRun
from _repo import git
from slipway.builders.base import (
    BUILDER_FAILED,
    RETURNED,
    RETURNED_EMPTY,
    Builder,
    Credential,
    CredentialError,
    Schedule,
)
from slipway.builders.cli import (
    CREDENTIAL_HELPER,
    PERMISSION_MODE,
    SESSION_SCRIPT,
    TOKEN_VARIABLE,
    WORKTREE_SCRIPT,
    ContainerBuilder,
    GitAccess,
    LaptopBuilder,
    LaptopReturn,
    default_run,
    laptop_build,
    map_transcript,
    push_side_reference,
    scrub_record,
    worker_label,
)
from slipway.builders.handoff import render
from slipway.config.bindings import BindingInvalid, ResolvedBinding
from slipway.config.operator import Account, AccountKind, BuilderWorker, parse_operator
from slipway.config.project import parse_project
from slipway.redact import MARK
from slipway.trace.shape import (
    ACTIONS_FILE,
    TRACE_FILE,
    TRANSCRIPT_FILE,
    Command,
    Connection,
    FileChange,
    ModelRequest,
    ToolCall,
    load_jsonl,
    trace_of,
)
from slipway.trackers.base import Card
from test_imports import SRC, edges_of, layer

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "config"
DOCKERFILE = ROOT / "deploy" / "builder.Dockerfile"
PLIST = ROOT / "deploy" / "laptop" / "com.slipway.worker.plist"
EXPLAINER = ROOT / "docs" / "explainers" / "u13-cli-builder.md"

BASE = "0123456789abcdef0123456789abcdef01234567"
PUSHED = "89abcdef0123456789abcdef0123456789abcdef"
ACCOUNT_TOKEN = "sk-ant-oat01-" + "Q" * 40
GIT_TOKENS = ["ghs_" + c * 36 for c in "ABCD"]
REMOTE = "https://code-host.example.test/example-org/example-app.git"
NETWORK = "builders-isolated"
IMAGE = "builder:test"
USER = "10001:10001"
T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", f"credential.helper={CREDENTIAL_HELPER}"]

BODY = ('Add a hello page.\n\n```slipway\n{"good": "It renders.", '
        '"paths": ["src/hello.py"], "test": "tests/test_hello.py"}\n```\n')


class Recorder(FakeRun):
    """FakeRun that also keeps the keyword arguments of every call."""

    def __init__(self):
        super().__init__()
        self.kwargs: list[dict] = []

    def __call__(self, argv, check=True, **kwargs):
        self.kwargs.append(kwargs)
        return super().__call__(argv, check=check)

    def call(self, first: str) -> tuple[list[str], dict]:
        index = next(i for i, a in enumerate(self.calls) if a[0] == first)
        return self.calls[index], self.kwargs[index]


class Hybrid(Recorder):
    """docker and sudo go to the script; git runs for real on local paths."""

    def __call__(self, argv, check=True, **kwargs):
        if argv[0] == "git":
            self.calls.append(list(argv))
            self.kwargs.append(kwargs)
            return default_run(argv, check=check, env=kwargs.get("env"),
                               input=kwargs.get("input"))
        return super().__call__(argv, check=check, **kwargs)


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at


def minting(values, lifetime=timedelta(hours=1), clock=None):
    items = iter(values)

    def mint():
        start = clock() if clock else T0
        return next(items), (start + lifetime if lifetime else None)
    return mint


@pytest.fixture
def project():
    return parse_project(Path("example.toml"),
                         tomllib.loads((FIXTURES / "project.toml").read_text()))


@pytest.fixture
def operator():
    return parse_operator(Path("operator.toml"),
                          tomllib.loads((FIXTURES / "operator.toml").read_text()))


@pytest.fixture
def binding(operator):
    return ResolvedBinding("builder", operator.accounts["max-sub"], "model-large", "high", 90)


def handoff(project, key="EX-12"):
    card = Card(key=key, title="Add the hello page", body=BODY, status="Ready")
    return render(project, card, BASE, f"slipway/{key}-hello-page")


ENVIRON = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/worker",
    "SLIPWAY_ACCOUNT_MAX_SUB": ACCOUNT_TOKEN,
    "SLIPWAY_DATABASE_URL": "postgresql://slipway:pw@db/slipway",
    "HATCHET_CLIENT_TOKEN": "hatchet-client-token-value-0123456789",
    "SLIPWAY_HOME": "/srv/slipway",
}


def container(tmp_path, run, clock=None, remote=REMOTE):
    return ContainerBuilder(tmp_path / "sessions", GitAccess(remote, "x-access-token"), IMAGE,
                            NETWORK, USER, run=run, environ=ENVIRON, now=clock or Clock())


def mounts(argv):
    found = []
    for i, word in enumerate(argv):
        if word == "--mount":
            fields = dict(p.split("=", 1) for p in argv[i + 1].split(",") if "=" in p)
            found.append(fields)
    return found


def env_args(argv):
    return [argv[i + 1] for i, w in enumerate(argv) if w == "--env"]


def inspect_reply(run, ref, running=False, code=0):
    state = {"Status": "running" if running else "exited", "Running": running,
             "ExitCode": code, "OOMKilled": False, "Error": ""}
    run.reply(["docker", "inspect", "--format", "{{json .State}}", ref], json.dumps(state))


def write_transcript(builder, ref, text):
    top = builder.dir(ref)
    session_id = json.loads((top / "state.json").read_text())["session_id"]
    path = top / "workspace" / "config" / "projects" / "-workspace-repo" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(text)


# Covers AE3: two cards on one account share nothing.

def test_two_cards_on_one_account_share_nothing(tmp_path, project, binding):
    run = Recorder()
    run.reply(["docker", "run"], "container-id\n")
    builder = container(tmp_path, run)
    cred = lambda: Credential("example-app", minting(GIT_TOKENS))  # noqa: E731
    first = builder.start(handoff(project, "EX-12"), binding, cred())
    second = builder.start(handoff(project, "EX-13"), binding, cred())
    again = builder.start(handoff(project, "EX-12"), binding, cred())

    names = {first, second, again}
    assert len(names) == 3
    runs = [c for c in run.calls if c[:2] == ["docker", "run"]]
    assert [r[r.index("--name") + 1] for r in runs] == [first, second, again]
    sources = [m["source"] for r in runs for m in mounts(r)]
    assert len(sources) == len(set(sources)) == 6
    for a in sources:
        for b in sources:
            assert a == b or not (a.startswith(b + "/") or b.startswith(a + "/"))
    for ref in names:
        config = builder.dir(ref) / "workspace" / "config"
        assert config.is_dir() and list(config.iterdir()) == []
        repo = builder.dir(ref) / "workspace" / "repo"
        assert repo.is_dir() and list(repo.iterdir()) == []
    # Each session clones into its own workspace, the one its own mount gives.
    for r in runs:
        assert "SLIPWAY_REPO=/workspace/repo" in env_args(r)
        assert SESSION_SCRIPT == r[-1] and 'git clone' in r[-1]


def test_container_invocation_baseline(tmp_path, project, binding):
    run = Recorder()
    run.reply(["docker", "run"], "container-id\n")
    builder = container(tmp_path, run)
    builder.start(handoff(project), binding, Credential("example-app", minting(GIT_TOKENS)))
    argv, kwargs = run.call("docker")
    text = " ".join(argv)

    assert "docker.sock" not in text and "/var/run/docker" not in text
    assert "--privileged" not in argv
    assert argv[argv.index("--user") + 1] == USER
    assert argv[argv.index("--network") + 1] == NETWORK
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert {m["target"] for m in mounts(argv)} == {"/workspace", "/run/slipway"}
    assert any("readonly" in a and "/run/slipway" in a for a in argv)
    assert "CLAUDE_CONFIG_DIR=/workspace/config" in env_args(argv)
    # The token is named on the command line and carried by the client's environment.
    variable = TOKEN_VARIABLE[AccountKind.SUBSCRIPTION]
    assert variable in env_args(argv)
    assert ACCOUNT_TOKEN not in text and all(t not in text for t in GIT_TOKENS)
    assert kwargs["env"][variable] == ACCOUNT_TOKEN
    assert "SLIPWAY_DATABASE_URL" not in kwargs["env"]
    assert "HATCHET_CLIENT_TOKEN" not in kwargs["env"]
    # The git credential is in a token file, never in the remote URL.
    assert f"SLIPWAY_REMOTE={REMOTE}" in env_args(argv)
    token = builder.dir(argv[argv.index("--name") + 1]) / "run" / "token"
    assert token.read_text() == GIT_TOKENS[0]
    assert token.stat().st_mode & 0o777 == 0o600


def test_session_script_shape():
    assert "claude -p" in SESSION_SCRIPT
    assert "--bare" not in SESSION_SCRIPT.replace("# `--bare`", "")
    assert "$SLIPWAY_PERMISSION_MODE" in SESSION_SCRIPT
    assert PERMISSION_MODE == "bypassPermissions"
    assert '< "$SLIPWAY_HANDOFF"' in SESSION_SCRIPT


def test_api_key_account_uses_the_api_key_variable(tmp_path, project, binding):
    run = Recorder()
    run.reply(["docker", "run"])
    builder = container(tmp_path, run)
    api = replace(binding, account=Account("api-main", AccountKind.API_KEY,
                                           "SLIPWAY_ACCOUNT_MAX_SUB"))
    builder.start(handoff(project), api, Credential("example-app", minting(GIT_TOKENS)))
    argv, kwargs = run.call("docker")
    assert "ANTHROPIC_API_KEY" in env_args(argv)
    assert kwargs["env"]["ANTHROPIC_API_KEY"] == ACCOUNT_TOKEN


def test_unset_account_token_is_binding_invalid(tmp_path, project, binding):
    builder = ContainerBuilder(tmp_path, GitAccess(REMOTE, "x"), IMAGE, NETWORK, USER,
                               run=Recorder(), environ={})
    with pytest.raises(BindingInvalid) as raised:
        builder.start(handoff(project), binding, Credential("example-app",
                                                             minting(GIT_TOKENS)))
    assert raised.value.halt_class == "binding_invalid"


@pytest.mark.parametrize("user,network", [("0:0", NETWORK), ("root", NETWORK),
                                          (USER, "host"), (USER, "bridge")])
def test_container_refuses_root_and_shared_networks(tmp_path, user, network):
    with pytest.raises(ValueError):
        ContainerBuilder(tmp_path, GitAccess(REMOTE, "x"), IMAGE, network, user)


def test_remote_with_a_credential_is_refused():
    with pytest.raises(ValueError):
        GitAccess("https://user:secret@code-host.example.test/repo.git", "x")


# The end of a container session.

def started(tmp_path, project, binding, clock=None, tokens=GIT_TOKENS, lifetime=timedelta(hours=1)):
    run = Recorder()
    run.reply(["docker", "run"])
    builder = container(tmp_path, run, clock)
    cred = Credential("example-app", minting(tokens, lifetime, clock))
    ref = builder.start(handoff(project), binding, cred)
    return builder, run, ref


def test_exit_with_no_branch_pushed_is_returned_empty(tmp_path, project, binding):
    builder, run, ref = started(tmp_path, project, binding)
    inspect_reply(run, ref, code=3)
    run.reply([*GIT, "ls-remote", REMOTE], f"{BASE}\trefs/heads/slipway/EX-12-hello-page\n")
    status = builder.poll(ref)
    assert status.terminal
    result = status.result
    assert result.outcome == RETURNED_EMPTY and result.commit is None
    assert "code 3" in result.reason and "nothing was pushed" in result.reason
    assert result.halt_class == BUILDER_FAILED
    assert result.provider_ref == ref and result.model == "model-large"
    assert result.account == "max-sub"
    # A terminal session answers from its record without asking docker again.
    count = len(run.calls)
    assert builder.poll(ref).result == result and len(run.calls) == count


def test_exit_with_no_branch_on_the_remote_is_returned_empty(tmp_path, project, binding):
    builder, run, ref = started(tmp_path, project, binding)
    inspect_reply(run, ref, code=1)
    run.reply([*GIT, "ls-remote", REMOTE], "")
    result = builder.poll(ref).result
    assert result.outcome == RETURNED_EMPTY and "code 1" in result.reason


def test_exit_with_a_pushed_branch_returns_commit_and_cost(tmp_path, project, binding):
    builder, run, ref = started(tmp_path, project, binding)
    out = builder.dir(ref) / "workspace" / "out" / "result.json"
    out.write_text(json.dumps({"type": "result", "subtype": "success",
                               "total_cost_usd": 1.25}))
    inspect_reply(run, ref, code=0)
    run.reply([*GIT, "ls-remote", REMOTE], f"{PUSHED}\trefs/heads/slipway/EX-12-hello-page\n")
    result = builder.poll(ref).result
    assert result.outcome == RETURNED and result.commit == PUSHED
    assert result.cost == 1.25 and result.halt_class is None
    assert "success" in result.reason


def test_running_session_is_not_terminal(tmp_path, project, binding):
    builder, run, ref = started(tmp_path, project, binding)
    inspect_reply(run, ref, running=True)
    status = builder.poll(ref)
    assert not status.terminal and status.result is None


def test_release_removes_the_container_and_its_files(tmp_path, project, binding):
    builder, run, ref = started(tmp_path, project, binding)
    run.reply(["docker", "rm"])
    builder.release(ref)
    assert ["docker", "rm", "--force", "--volumes", ref] in run.calls
    assert not builder.dir(ref).exists()
    builder.delete(ref)


# The transcript, scrubbed, on the side reference.

def transcript_lines(secret_values):
    git_token, account_token = secret_values
    return "\n".join(json.dumps(line) for line in [
        {"type": "user", "timestamp": "2026-10-15T09:00:00.000Z",
         "message": {"role": "user", "content": "the handoff"}},
        {"type": "assistant", "timestamp": "2026-10-15T09:00:02.000Z",
         "message": {"id": "msg_1", "model": "model-large", "role": "assistant",
                     "stop_reason": "tool_use",
                     "usage": {"input_tokens": 1200, "output_tokens": 340,
                               "cache_read_input_tokens": 800},
                     "content": [{"type": "text", "text": "I will run the tests."}]}},
        {"type": "assistant", "timestamp": "2026-10-15T09:00:03.000Z",
         "message": {"id": "msg_1", "model": "model-large", "role": "assistant",
                     "usage": {"input_tokens": 1200, "output_tokens": 340},
                     "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                  "input": {"command": f"git push https://x:{git_token}@h/r"}}]}},
        {"type": "user", "timestamp": "2026-10-15T09:00:05.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_1",
              "content": f"env shows {account_token}"}]}},
        {"type": "assistant", "timestamp": "2026-10-15T09:00:06.000Z",
         "message": {"id": "msg_2", "model": "model-large", "role": "assistant",
                     "usage": {"input_tokens": 1500, "output_tokens": 90},
                     "content": [
                         {"type": "tool_use", "id": "toolu_2", "name": "Write",
                          "input": {"file_path": "/workspace/repo/src/hello.py",
                                    "content": "print('hi')"}},
                         {"type": "tool_use", "id": "toolu_3", "name": "WebFetch",
                          "input": {"url": "https://pypi.org/simple/x/", "prompt": "p"}},
                         {"type": "tool_use", "id": "toolu_4", "name": "Bash",
                          "input": {"command": "pytest -q"}}]}},
        {"type": "user", "timestamp": "2026-10-15T09:00:07.000Z",
         "toolUseResult": {"type": "create"},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_2", "content": "written"}]}},
        {"type": "user", "timestamp": "2026-10-15T09:00:08.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_3",
              "content": [{"type": "text", "text": "index"}]}]}},
        {"type": "user", "timestamp": "2026-10-15T09:00:09.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_4", "is_error": True,
              "content": "1 failed"}]}},
    ]) + "\n"


def bare_remote(tmp_path, branch="slipway/EX-12-hello-page"):
    """A bare remote with the card's branch at a base commit, and that commit id."""
    work = tmp_path / "seed"
    work.mkdir()
    git(work, "init", "-q", "-b", branch)
    (work / "README.md").write_text("seed\n")
    git(work, "add", "README.md")
    git(work, "commit", "-q", "-m", "seed")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(work, "push", "-q", str(remote), f"HEAD:refs/heads/{branch}")
    return remote, work, git(work, "rev-parse", "HEAD")


def test_transcript_is_scrubbed_before_the_side_reference_commit(tmp_path, project, binding):
    remote, work, seed = bare_remote(tmp_path)
    # The builder pushed its own action log, carrying a token, before it finished.
    (work / ACTIONS_FILE).write_text(json.dumps({"type": "command", "line": GIT_TOKENS[0]}))
    git(work, "checkout", "-q", "--orphan", "log")
    git(work, "rm", "-q", "--cached", "README.md")
    git(work, "add", ACTIONS_FILE)
    git(work, "commit", "-q", "-m", "builder log")
    builder_log = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", str(remote), "HEAD:refs/slipway/log/EX-12")

    run = Hybrid()
    run.reply(["docker", "run"])
    builder = ContainerBuilder(tmp_path / "sessions", GitAccess(str(remote), "x-access-token"),
                               IMAGE, NETWORK, USER, run=run, environ=ENVIRON, now=Clock())
    cred = Credential("example-app", minting(GIT_TOKENS))
    ref = builder.start(handoff(project), binding, cred)
    write_transcript(builder, ref, transcript_lines((GIT_TOKENS[0], ACCOUNT_TOKEN)))
    inspect_reply(run, ref, code=0)
    result = builder.poll(ref).result
    assert result.commit == seed  # the remote branch tip, read by ls-remote

    # Scrubbed already on the worker's disk, before any commit.
    on_disk = builder.transcript(ref)
    assert ACCOUNT_TOKEN not in on_disk and GIT_TOKENS[0] not in on_disk
    assert MARK in on_disk

    commit = builder.push_log(ref, 7)
    reference = "refs/slipway/log/EX-12"
    assert git(tmp_path, "--git-dir", str(remote), "rev-parse", reference) == commit
    assert git(tmp_path, "--git-dir", str(remote), "rev-parse", f"{commit}^") == builder_log
    assert git(tmp_path, "--git-dir", str(remote), "log", "-1", "--format=%s", commit) == \
        "slipway log EX-12 build 7"
    names = git(tmp_path, "--git-dir", str(remote), "ls-tree", "--name-only", commit).split()
    assert names == sorted([ACTIONS_FILE, TRACE_FILE, TRANSCRIPT_FILE])
    for name in names:
        text = git(tmp_path, "--git-dir", str(remote), "show", f"{commit}:{name}")
        assert ACCOUNT_TOKEN not in text
        assert all(t not in text for t in GIT_TOKENS)
    trace = git(tmp_path, "--git-dir", str(remote), "show", f"{commit}:{TRACE_FILE}")
    records = load_jsonl(trace)
    assert [type(r).__name__ for r in records][:3] == ["ModelRequest", "ToolCall", "Command"]
    # The token file the helper read is gone once the push is done.
    assert not (builder.dir(ref) / "log" / "token").exists()


def test_side_reference_without_a_builder_log_has_no_parent(tmp_path):
    remote, _, _ = bare_remote(tmp_path)
    cred = Credential("example-app", minting(GIT_TOKENS))
    commit = push_side_reference(default_run, tmp_path / "log", GitAccess(str(remote), "x"),
                                 cred, "EX-12", 3, [], "line\n", environ=ENVIRON)
    parents = git(tmp_path, "--git-dir", str(remote), "rev-list", "--parents", "-1", commit)
    assert parents == commit
    names = git(tmp_path, "--git-dir", str(remote), "ls-tree", "--name-only", commit).split()
    assert names == [TRACE_FILE, TRANSCRIPT_FILE]


# The credential across the hour.

def test_build_crossing_the_hour_pushes_with_the_rewritten_token(tmp_path, project, binding):
    remote, _, _ = bare_remote(tmp_path)
    clock = Clock()
    run = Hybrid()
    run.reply(["docker", "run"])
    builder = ContainerBuilder(tmp_path / "sessions", GitAccess(str(remote), "x-access-token"),
                               IMAGE, NETWORK, USER, run=run, environ=ENVIRON, now=clock)
    cred = Credential("example-app", minting(GIT_TOKENS, clock=clock))
    ref = builder.start(handoff(project), binding, cred)
    token = builder.dir(ref) / "run" / "token"
    inspect_reply(run, ref, running=True)

    clock.at = T0 + timedelta(minutes=20)
    builder.poll(ref)
    assert token.read_text() == GIT_TOKENS[0]

    clock.at = T0 + timedelta(minutes=55)
    builder.poll(ref)
    assert token.read_text() == GIT_TOKENS[1]
    assert token.stat().st_mode & 0o777 == 0o600

    # The helper git runs inside the session now answers with the new token.
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
           "SLIPWAY_TOKEN_FILE": str(token), "SLIPWAY_GIT_USERNAME": "x-access-token"}
    filled = subprocess.run(["git", "-c", f"credential.helper={CREDENTIAL_HELPER}",
                             "credential", "fill"],
                            input="protocol=https\nhost=code-host.example.test\n\n", env=env,
                            capture_output=True, text=True, check=True).stdout
    assert f"password={GIT_TOKENS[1]}" in filled and GIT_TOKENS[0] not in filled
    assert "username=x-access-token" in filled

    # And the push after the hour succeeds with the refreshed credential.
    inspect_reply(run, ref, code=0)
    builder.poll(ref)
    commit = builder.push_log(ref, 1)
    assert git(tmp_path, "--git-dir", str(remote), "rev-parse",
               "refs/slipway/log/EX-12") == commit
    # Both tokens the session ever held are scrubbed from what it writes.
    assert set(GIT_TOKENS[:2]) <= set(builder.injected(ref))


def test_static_credential_is_never_refreshed(tmp_path, project, binding):
    clock = Clock()
    builder, run, ref = started(tmp_path, project, binding, clock, tokens=["ATCTT" + "s" * 30],
                                lifetime=None)
    inspect_reply(run, ref, running=True)
    for hours in (1, 2, 5):
        clock.at = T0 + timedelta(hours=hours)
        assert not builder.poll(ref).terminal
    assert (builder.dir(ref) / "run" / "token").read_text() == "ATCTT" + "s" * 30


# A refresh that fails mid build (follow up #67). A rotating token minted
# unchanged raises CredentialError; the build ends as a halt with that reason.

def test_container_raises_a_failed_refresh_once_and_then_halts(tmp_path, project, binding):
    clock = Clock()
    builder, run, ref = started(tmp_path, project, binding, clock,
                                tokens=[GIT_TOKENS[0], GIT_TOKENS[0]])
    inspect_reply(run, ref, running=True)
    clock.at = T0 + timedelta(minutes=55)
    with pytest.raises(CredentialError, match="minted the value it replaces"):
        builder.poll(ref)
    # Every later tick answers from the record: no second raise, no docker call.
    count = len(run.calls)
    for minutes in (56, 57, 70):
        clock.at = T0 + timedelta(minutes=minutes)
        status = builder.poll(ref)
        assert status.terminal and len(run.calls) == count
    result = status.result
    assert result.outcome == RETURNED_EMPTY and result.halt_class == BUILDER_FAILED
    assert "credential refresh failed" in result.reason
    assert "minted the value it replaces" in result.reason
    assert GIT_TOKENS[0] not in result.reason


def test_laptop_build_halts_when_the_refresh_thread_fails(tmp_path, project, binding):
    run = Recorder()
    builder = LaptopBuilder(tmp_path / "sessions", GitAccess(REMOTE, "x-access-token"),
                            "slipway-session", "/opt/bin:/usr/bin:/bin", run=run,
                            environ=ENVIRON, now=Clock(T0 + timedelta(minutes=55)),
                            refresh_seconds=0.01)
    minted = threading.Event()
    calls = []

    def mint():
        # The first mint starts the session; the second is the refresh,
        # which hands back the same rotating token.
        calls.append(1)
        if len(calls) > 1:
            minted.set()
        return GIT_TOKENS[0], T0 + timedelta(hours=1)

    def session(argv, check=True, **kwargs):
        if argv[0] == "sudo" and "/bin/sh" in argv:
            # The session runs until the refresh thread has failed.
            assert minted.wait(5)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return Recorder.__call__(run, argv, check=check, **kwargs)

    run.reply([*GIT, "ls-remote", REMOTE], f"{PUSHED}\trefs/heads/slipway/EX-12-hello-page\n")
    run.reply(["sudo", "--non-interactive", "--user", "slipway-session", "--", "rm"])
    builder.run = session
    back = laptop_build(builder, handoff(project), binding, Credential("example-app", mint))
    result = back.result
    # A pushed branch does not count: the session lost its credential.
    assert result.outcome == RETURNED_EMPTY and result.commit is None
    assert result.halt_class == BUILDER_FAILED
    assert "credential refresh failed" in result.reason
    assert "minted the value it replaces" in result.reason and "code 0" in result.reason
    assert GIT_TOKENS[0] not in result.reason


# The laptop.

def test_laptop_task_routes_to_the_operator_files_label(project, operator):
    assert worker_label(project, operator) == operator.builder_workers["laptop"].label
    other = replace(operator, builder_workers={
        "laptop": BuilderWorker("laptop", "a-label-the-operator-chose", "laptop")})
    assert worker_label(project, other) == "a-label-the-operator-chose"
    with pytest.raises(BindingInvalid):
        worker_label(replace(project, builder_worker=None), operator)
    with pytest.raises(BindingInvalid):
        worker_label(project, replace(operator, builder_workers={}))


def laptop(tmp_path, run):
    return LaptopBuilder(tmp_path / "sessions", GitAccess(REMOTE, "x-access-token"),
                         "slipway-session", "/opt/bin:/usr/bin:/bin", run=run,
                         environ=ENVIRON, now=Clock())


def test_laptop_session_environment_carries_no_control_plane_secret(tmp_path, project,
                                                                     binding):
    run = Recorder()
    run.reply(["sudo"])
    run.reply([*GIT, "ls-remote", REMOTE], f"{PUSHED}\trefs/heads/slipway/EX-12-hello-page\n")
    builder = laptop(tmp_path, run)
    assert builder.schedule is Schedule.WORKER_PROCESS
    ref = builder.start(handoff(project), binding, Credential("example-app",
                                                              minting(GIT_TOKENS)))
    argv, kwargs = run.call("sudo")
    env = kwargs["env"]
    assert argv[:4] == ["sudo", "--non-interactive", "--user", "slipway-session"]
    kept = next(a for a in argv if a.startswith("--preserve-env="))
    for name in ("SLIPWAY_DATABASE_URL", "HATCHET_CLIENT_TOKEN", "SLIPWAY_HOME"):
        assert name not in env and name not in kept
    for value in (ENVIRON["SLIPWAY_DATABASE_URL"], ENVIRON["HATCHET_CLIENT_TOKEN"],
                  ENVIRON["SLIPWAY_HOME"]):
        assert value not in json.dumps(env) and value not in " ".join(argv)
    assert "operator.toml" not in json.dumps(env)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ACCOUNT_TOKEN
    assert ACCOUNT_TOKEN not in " ".join(argv)
    assert env["PATH"] == "/opt/bin:/usr/bin:/bin"
    assert env["CLAUDE_CONFIG_DIR"] == str(builder.dir(ref) / "workspace" / "config")
    assert set(kept.split("=", 1)[1].split(",")) == set(env)
    token = builder.dir(ref) / "run" / "token"
    assert token.stat().st_mode & 0o777 == 0o640  # the session user reads it by group
    assert builder.poll(ref).result.commit == PUSHED


def test_laptop_build_returns_result_and_scrubbed_transcript(tmp_path, project, binding):
    run = Recorder()
    builder = laptop(tmp_path, run)

    def session(argv, check=True, **kwargs):
        if argv[0] == "sudo" and "/bin/sh" in argv:
            ref = next(p.name for p in (tmp_path / "sessions").iterdir())
            write_transcript(builder, ref, transcript_lines((GIT_TOKENS[0], ACCOUNT_TOKEN))
                             + "not json\n")
            return subprocess.CompletedProcess(argv, 2, "", "")
        return Recorder.__call__(run, argv, check=check, **kwargs)

    run.reply([*GIT, "ls-remote", REMOTE], "")
    run.reply(["sudo", "--non-interactive", "--user", "slipway-session", "--", "rm"])
    builder.run = session
    back = laptop_build(builder, handoff(project), binding,
                        Credential("example-app", minting(GIT_TOKENS)))
    assert back.result.outcome == RETURNED_EMPTY and "code 2" in back.result.reason
    assert ACCOUNT_TOKEN not in back.transcript and GIT_TOKENS[0] not in back.transcript
    assert back.skipped == 1
    assert LaptopReturn.from_json(json.loads(json.dumps(back.to_json()))) == back
    assert list((tmp_path / "sessions").iterdir()) == []


# The transcript mapper.

def test_transcript_maps_to_shape_records():
    mapped = map_transcript(transcript_lines(("ghs_x", "tok")))
    assert mapped.skipped == 0
    kinds = [type(r) for r in mapped.records]
    assert kinds == [ModelRequest, ToolCall, Command, ModelRequest, ToolCall, FileChange,
                     ToolCall, Connection, ToolCall, Command]
    first = mapped.records[0]
    assert first.response_id == "msg_1" and first.input_tokens == 1200
    assert first.cache_read_tokens == 800 and first.finish_reason == "tool_use"
    assert first.end_ns > first.start_ns
    bash = mapped.records[1]
    assert bash.name == "Bash" and bash.call_id == "toolu_1" and bash.error is None
    assert "env shows" in bash.result and bash.end_ns > bash.start_ns
    assert mapped.records[2].exit_code == 0
    write = mapped.records[5]
    assert write.action == "created" and write.path.endswith("src/hello.py")
    fetch = mapped.records[7]
    assert fetch.address == "pypi.org" and fetch.port == 443
    failed = mapped.records[8]
    assert failed.error == "1 failed"
    assert mapped.records[9].exit_code is None
    # The records build a trace whose tool events land on their spans.
    trace = trace_of("cli", "EX-12", "slipway/EX-12-hello-page", BASE, mapped.records,
                     trace_id="4bf92f3577b34da6a3ce929d0e0e4736")
    assert len(trace.spans) == 1 + 2 + 4
    json.dumps(trace.otlp())


def test_a_line_that_is_not_json_is_skipped_and_counted():
    good = transcript_lines(("ghs_x", "tok"))
    text = "{not json\n" + good + "[1, 2]\n\n" + "\"a string\"\n"
    mapped = map_transcript(text)
    assert mapped.skipped == 3
    assert mapped.records == map_transcript(good).records


def test_tool_use_without_a_result_is_an_error_call():
    line = json.dumps({"type": "assistant", "timestamp": "2026-10-15T09:00:00Z",
                       "message": {"id": "m", "model": "model-large", "content": [
                           {"type": "tool_use", "id": "t", "name": "Read",
                            "input": {"file_path": "a"}}]}})
    records = map_transcript(line).records
    assert isinstance(records[1], ToolCall) and records[1].error


def test_events_scrub_every_string_field(tmp_path, project, binding):
    builder, run, ref = started(tmp_path, project, binding)
    write_transcript(builder, ref, transcript_lines((GIT_TOKENS[0], ACCOUNT_TOKEN)))
    inspect_reply(run, ref, code=0)
    run.reply([*GIT, "ls-remote", REMOTE], "")
    builder.poll(ref)
    records = list(builder.events(ref))
    assert records
    for record in records:
        dumped = json.dumps(record.__dict__)
        assert ACCOUNT_TOKEN not in dumped and GIT_TOKENS[0] not in dumped


def test_scrub_record_covers_every_string_field():
    secret = "s" * 20
    for record in (
        ModelRequest(model=secret, start_ns=0, end_ns=1, input_tokens=0, output_tokens=0,
                     provider=secret, response_id=secret, finish_reason=secret),
        ToolCall(name=secret, call_id=secret, start_ns=0, end_ns=1, arguments=secret,
                 result=secret, error=secret),
        FileChange(path=secret, action=secret, time_ns=0, tool_call_id=secret),
        Command(command_line=secret, time_ns=0, tool_call_id=secret),
        Connection(address=secret, time_ns=0, url=secret, tool_call_id=secret),
    ):
        assert secret not in json.dumps(scrub_record(record, [secret]).__dict__)


# The contract, the imports, the deploy files.

def test_both_adapters_satisfy_the_protocol(tmp_path):
    a = ContainerBuilder(tmp_path, GitAccess(REMOTE, "x"), IMAGE, NETWORK, USER)
    b = LaptopBuilder(tmp_path, GitAccess(REMOTE, "x"), "slipway-session", "/usr/bin")
    assert isinstance(a, Builder) and a.schedule is Schedule.REMOTE_SESSION
    assert isinstance(b, Builder) and b.schedule is Schedule.WORKER_PROCESS


def test_cli_imports_only_what_builders_may():
    allowed = {"builders", "cards", "trackers", "config", "redact", "trace"}
    path = SRC / "slipway" / "builders" / "cli.py"
    reached = {layer(e.imported) for e in edges_of(SRC, path)}
    assert reached <= allowed, reached


def test_dockerfile_baseline():
    text = DOCKERFILE.read_text()
    lines = [line.strip() for line in text.splitlines()]
    user = [line for line in lines if line.startswith("USER ")]
    assert user == ["USER ${BUILDER_UID}:${BUILDER_GID}"]
    assert "ARG BASE_IMAGE" in lines and "ARG CLAUDE_CODE_VERSION" in lines
    assert 'test "${BUILDER_UID}" != 0' in text
    assert "docker.sock" not in text
    for line in lines:
        if line.startswith(("ENV", "ARG")):
            assert "TOKEN" not in line and "API_KEY" not in line


def test_launchd_plist_supervises_the_worker():
    plist = plistlib.loads(PLIST.read_bytes())
    assert plist["Label"] == "com.slipway.worker"
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["UserName"] == "SLIPWAY_WORKER_USER"  # replaced at install
    command = plist["ProgramArguments"][-1]
    assert "$SLIPWAY_WORKER_COMMAND" in command and ".slipway-worker.env" in command
    assert "EnvironmentVariables" not in plist


@pytest.mark.parametrize("path", [DOCKERFILE, PLIST, EXPLAINER,
                                  SRC / "slipway" / "builders" / "cli.py"],
                         ids=lambda p: p.name)
def test_no_dashes_as_punctuation(path):
    text = path.read_text()
    assert chr(0x2014) not in text and chr(0x2013) not in text
    if path == PLIST:
        assert "--" not in text
    for line in text.splitlines():
        prose = line.strip().startswith("#") or path.suffix == ".md"
        if prose:
            for word in line.split():
                if "--" in word:
                    assert word.strip("`(),.").startswith("--"), line


# U26: the worktree checkout. The session script runs for real under /bin/sh,
# with a stand in claude on PATH that commits, pushes, and meets the other
# session twice, before and after it lists the worktrees, so both exist at
# once while each looks. Only sudo is stripped.

FAKE_CLAUDE = """#!/bin/sh
cat > /dev/null
echo "work for $SLIPWAY_BRANCH" > work.txt
git add work.txt
git commit --quiet -m "work for $SLIPWAY_BRANCH"
git push --quiet
meet() {{
  mkdir -p "{barrier}/$1"
  touch "{barrier}/$1/$(echo "$SLIPWAY_BRANCH" | tr / _)"
  n=0
  while [ "$(ls "{barrier}/$1" | wc -l)" -lt 2 ] && [ $n -lt 400 ]; do
    sleep 0.05; n=$((n+1))
  done
}}
meet started
git worktree list --porcelain > "$SLIPWAY_OUT/worktrees.txt"
meet listed
echo '{{"subtype": "success", "total_cost_usd": 0.25}}'
"""


def as_session_user(argv, check=True, **kwargs):
    """Run what sudo would run, as this user: the session script or the rm."""
    if argv[0] == "sudo":
        argv = argv[argv.index("--") + 1:]
    return default_run(argv, check=check, env=kwargs.get("env"), input=kwargs.get("input"))


def shared_checkout(tmp_path, branches):
    """A bare remote with main and each card branch at one base, and a dirty clone of it."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    shared = tmp_path / "shared"
    git(tmp_path, "clone", "-q", str(remote), str(shared))
    (shared / "README.md").write_text("seed\n")
    git(shared, "add", "README.md")
    git(shared, "commit", "-q", "-m", "seed")
    git(shared, "push", "-q", "origin", "HEAD:refs/heads/main")
    for branch in branches:
        git(shared, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    # Another session's work in progress, which must survive both builds.
    (shared / "README.md").write_text("seed, edited by the operator\n")
    (shared / "notes.txt").write_text("not committed\n")
    return remote, shared, git(shared, "rev-parse", "HEAD")


def test_two_laptop_builds_run_in_two_worktrees_and_leave_the_shared_checkout_alone(
        tmp_path, project, binding):
    keys = ("XY-12", "XY-13")
    branches = [f"{key}-fix-export" for key in keys]
    remote, shared, base = shared_checkout(tmp_path, branches)
    before = (git(shared, "branch", "--show-current"), git(shared, "status", "--porcelain"),
              git(shared, "branch", "--list"))
    bin_dir, barrier = tmp_path / "bin", tmp_path / "barrier"
    bin_dir.mkdir()
    barrier.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(FAKE_CLAUDE.format(barrier=barrier))
    claude.chmod(0o755)
    calls: list[list[str]] = []

    def run(argv, check=True, **kwargs):
        calls.append(list(argv))
        return as_session_user(argv, check=check, **kwargs)

    builder = LaptopBuilder(tmp_path / "sessions", GitAccess(str(remote), "x-access-token"),
                            "slipway-session", f"{bin_dir}:/usr/bin:/bin", run=run,
                            environ=ENVIRON, now=Clock(), checkout="worktree", shared=shared)
    backs: dict[str, LaptopReturn] = {}
    worktrees: dict[str, str] = {}
    real_release = builder.release

    def release(session_ref):
        # Read what the session saw before release removes it.
        out = builder.dir(session_ref) / "workspace" / "out" / "worktrees.txt"
        worktrees[builder._state(session_ref)["branch"]] = out.read_text()
        real_release(session_ref)

    builder.release = release

    def build(key, branch):
        card = Card(key=key, title="Fix export", body=BODY, status="Ready")
        backs[key] = laptop_build(builder, render(project, card, base, branch), binding,
                                  Credential("example-app", minting(GIT_TOKENS)))

    pairs = list(zip(keys, branches, strict=True))
    threads = [threading.Thread(target=build, args=pair) for pair in pairs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    for key, branch in pairs:
        result = backs[key].result
        assert result.outcome == RETURNED, result.reason
        assert result.commit == git(remote, "rev-parse", f"refs/heads/{branch}") != base
        # Each session saw its own worktree and the other's beside it.
        listed = worktrees[branch]
        assert listed.count("worktree ") == 3
        assert f"branch refs/heads/{branch}" in listed
    after = (git(shared, "branch", "--show-current"), git(shared, "status", "--porcelain"),
             git(shared, "branch", "--list"))
    assert after == before
    assert before[0] == "main"
    assert git(shared, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert list((tmp_path / "sessions").iterdir()) == []
    # Every git call on the shared checkout runs as the session user, so the
    # worktree's files and its record are the session's to write and remove.
    on_shared = [argv for argv in calls if str(shared) in argv]
    assert {argv[argv.index("-C") + 2] for argv in on_shared} >= {
        "rev-parse", "fetch", "worktree", "branch"}
    for argv in on_shared:
        assert argv[:4] == ["sudo", "--non-interactive", "--user", "slipway-session"], argv


def test_a_branch_the_shared_checkout_already_has_is_neither_reset_nor_deleted(
        tmp_path, project, binding):
    branch = "XY-14-fix-export"
    remote, shared, base = shared_checkout(tmp_path, [branch])
    git(shared, "branch", branch, base)  # the operator's own local branch of that name
    builder = LaptopBuilder(tmp_path / "sessions", GitAccess(str(remote), "x-access-token"),
                            "slipway-session", "/usr/bin:/bin", run=as_session_user,
                            environ=ENVIRON, now=Clock(), checkout="worktree", shared=shared)
    card = Card(key="XY-14", title="Fix export", body=BODY, status="Ready")
    back = laptop_build(builder, render(project, card, base, branch), binding,
                        Credential("example-app", minting(GIT_TOKENS)))
    assert back.result.outcome == RETURNED_EMPTY
    assert git(shared, "rev-parse", f"refs/heads/{branch}") == base


def test_a_worktree_checkout_needs_the_shared_path(tmp_path):
    with pytest.raises(ValueError, match="shared checkout"):
        LaptopBuilder(tmp_path, GitAccess(REMOTE, "x-access-token"), "slipway-session",
                      "/usr/bin:/bin", checkout="worktree")


def test_the_worktree_script_differs_from_the_clone_script_only_in_the_checkout():
    assert "git clone" not in WORKTREE_SCRIPT
    assert "--force" not in WORKTREE_SCRIPT and "reset" not in WORKTREE_SCRIPT
    assert WORKTREE_SCRIPT.endswith(SESSION_SCRIPT.split('cd "$SLIPWAY_REPO"\n')[1])
