"""U27 test scenarios: what a builder may drive beyond the shell.

No test starts docker, sudo, claude, or a browser: those calls go to the
recorders of tests/unit/test_cli_builder.py, and the workflow runs on the
fakes in tests/_card_fakes.py, as in U13 and U14.
"""

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from test_cli_builder import (
    GIT,
    GIT_TOKENS,
    PUSHED,
    REMOTE,
    Recorder,
    binding,  # noqa: F401 (a fixture)
    container,
    env_args,
    handoff,
    laptop,
    minting,
    operator,  # noqa: F401 (a fixture)
)
from test_onboard import (
    BITBUCKET,
    OPERATOR,
    SLUG,
    no_processes,  # noqa: F401 (an autouse fixture)
    world,  # noqa: F401 (a fixture)
)

from _card_fakes import World as CardWorld
from _card_fakes import parked, run
from slipway.builders.base import Credential, Schedule
from slipway.builders.cli import SESSION_SCRIPT, WORKTREE_SCRIPT, LaptopReturn
from slipway.builders.handoff import TOOLS
from slipway.builders.tools import (
    BROWSERS_VARIABLE,
    CHROME_FLAG,
    DESKTOP_SERVER,
    IMAGE_BROWSERS,
    PLAYWRIGHT_COMMAND,
    PLAYWRIGHT_SERVER,
    ToolRefused,
    grant,
)
from slipway.config import ConfigError
from slipway.config.project import DEFAULT_BUILDER_TOOLS, parse_project
from slipway.onboard import machine
from slipway.onboard.questions import BY_ID, QUESTIONS
from slipway.workflow import card

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "config" / "project.toml"
DOCKERFILE = ROOT / "deploy" / "builder.Dockerfile"
EXPLAINER = ROOT / "docs" / "explainers" / "u27-builder-tools.md"
DESKTOP = {"command": "/opt/computer-use/server", "args": ["--display", "main"]}


def project_from(text: str):
    return parse_project(Path("example.toml"), tomllib.loads(text))


def container_text(tools: str | None) -> str:
    """The fixture project with no builder_worker, so its builds run in a container."""
    text = FIXTURE.read_text().replace('builder_worker = "laptop"\n', "")
    if tools is not None:
        text = text.replace('evals_path = "evals/"\n',
                            f'evals_path = "evals/"\nbuilder_tools = {tools}\n')
    return text


def laptop_project(tools: str):
    return project_from(FIXTURE.read_text().replace(
        'builder_worker = "laptop"\n', f'builder_worker = "laptop"\nbuilder_tools = {tools}\n'))


def mcp_config(builder, ref) -> dict:
    return json.loads((builder.dir(ref) / "run" / "mcp.json").read_text())


def state(builder, ref) -> dict:
    return json.loads((builder.dir(ref) / "state.json").read_text())


# A container binding with operator_browser or desktop is refused at load
# with the reason.

@pytest.mark.parametrize("tool,power", [
    ("operator_browser", "the operator's own signed in browser"),
    ("desktop", "the operator's own screen"),
])
def test_a_container_binding_with_a_laptop_only_tool_is_refused_at_load(tool, power):
    with pytest.raises(ConfigError) as refused:
        project_from(container_text(f'["headless_browser", "{tool}"]'))
    assert refused.value.field == "builder_tools"
    message = refused.value.message
    assert f"{tool} is for a laptop builder only" in message
    assert power in message and "outside the session's dedicated user" in message
    assert "runs in a container" in message or "run in a container" in message
    # The same project with a builder worker, so its builds run on the laptop, loads.
    project = laptop_project(f'["{tool}"]')
    assert project.builder_tools == (tool,)


def test_an_unknown_or_repeated_tool_is_refused_naming_the_key():
    with pytest.raises(ConfigError) as unknown:
        laptop_project('["headless_browser", "teleport"]')
    assert unknown.value.field == "builder_tools[1]" and "teleport" in unknown.value.message
    with pytest.raises(ConfigError) as twice:
        laptop_project('["desktop", "desktop"]')
    assert twice.value.field == "builder_tools" and "twice" in twice.value.message


def test_a_container_refuses_a_laptop_only_tool_at_start_before_making_anything(
        tmp_path, binding):  # noqa: F811
    run_ = Recorder()
    builder = container(tmp_path, run_)
    reached = replace(handoff(project_from(container_text(None))),
                      builder_tools=("headless_browser", "desktop"))
    with pytest.raises(ToolRefused, match="desktop is for a laptop builder only"):
        builder.start(reached, binding, Credential("example-app", minting(GIT_TOKENS)))
    assert run_.calls == []
    assert not (tmp_path / "sessions").exists() or not any((tmp_path / "sessions").iterdir())
    with pytest.raises(ToolRefused, match="operator_browser"):
        grant(["operator_browser"], container=True, output_dir="/workspace/out/browser")


# The default project gives the session the headless browser's MCP entry and
# nothing else.

def test_the_default_project_gives_the_session_the_headless_browser_and_nothing_else(
        tmp_path, binding):  # noqa: F811
    project = project_from(container_text(None))
    assert project.builder_tools == DEFAULT_BUILDER_TOOLS == ("headless_browser",)
    run_ = Recorder()
    run_.reply(["docker", "run"], "container-id\n")
    builder = container(tmp_path, run_)
    ref = builder.start(handoff(project), binding, Credential("example-app",
                                                              minting(GIT_TOKENS)))

    config = mcp_config(builder, ref)
    assert list(config) == ["mcpServers"]
    assert list(config["mcpServers"]) == [PLAYWRIGHT_SERVER]
    server = config["mcpServers"][PLAYWRIGHT_SERVER]
    assert server["command"] == PLAYWRIGHT_COMMAND
    assert {"--headless", "--isolated", "--no-sandbox"} <= set(server["args"])
    assert server["args"][server["args"].index("--output-dir") + 1] == \
        "/workspace/out/browser"
    # The session's HOME is fresh, so the server is told where the image keeps Chromium.
    assert server["env"] == {BROWSERS_VARIABLE: IMAGE_BROWSERS}
    assert f"ENV {BROWSERS_VARIABLE}={IMAGE_BROWSERS}" in DOCKERFILE.read_text()
    # The session reads that file and only that file, with no other flag.
    argv = next(c for c in run_.calls if c[:2] == ["docker", "run"])
    env = env_args(argv)
    assert "SLIPWAY_MCP_CONFIG=/run/slipway/mcp.json" in env
    assert "SLIPWAY_TOOL_FLAGS=" in env
    assert argv[-1] == SESSION_SCRIPT
    assert '--mcp-config "$SLIPWAY_MCP_CONFIG" --strict-mcp-config' in SESSION_SCRIPT
    assert '--strict-mcp-config $SLIPWAY_TOOL_FLAGS' in WORKTREE_SCRIPT
    assert state(builder, ref)["tools_granted"] == ["headless_browser"]
    # The run/ folder is mounted read only, so the session cannot rewrite its grant.
    assert any(m.endswith("target=/run/slipway,readonly") for m in argv)


def test_the_default_handoff_names_the_headless_browser_after_the_fixed_tools():
    rendered = handoff(project_from(container_text(None)))
    assert rendered.tools == TOOLS
    assert rendered.builder_tools == ("headless_browser",)
    line = next(x for x in rendered.text.splitlines() if x.startswith("4. Tools:"))
    assert line.startswith("4. Tools: shell, file read, file edit, git, headless browser")
    assert "operator" not in line
    assert "never assume one this list does not name" in rendered.text


def test_an_empty_list_gives_the_shell_alone(tmp_path, binding):  # noqa: F811
    project = project_from(container_text("[]"))
    assert project.builder_tools == ()
    run_ = Recorder()
    run_.reply(["docker", "run"], "container-id\n")
    builder = container(tmp_path, run_)
    ref = builder.start(handoff(project), binding, Credential("example-app",
                                                              minting(GIT_TOKENS)))
    assert mcp_config(builder, ref) == {"mcpServers": {}}
    assert state(builder, ref)["tools_granted"] == []
    assert "headless browser" not in handoff(project).text


# A laptop binding with desktop writes tools_granted on the build's record
# and in the handoff.

def test_a_laptop_session_with_desktop_and_the_browser_gets_both_and_records_them(
        tmp_path, binding):  # noqa: F811
    project = laptop_project('["desktop", "operator_browser", "headless_browser"]')
    assert project.builder_tools == ("headless_browser", "operator_browser", "desktop")
    run_ = Recorder()
    run_.reply(["sudo"])
    run_.reply([*GIT, "ls-remote", REMOTE], f"{PUSHED}\trefs/heads/slipway/EX-12-hello-page\n")
    builder = laptop(tmp_path, run_)
    builder.desktop_server = DESKTOP
    builder.browsers_path = "/opt/session-browsers"
    ref = builder.start(handoff(project), binding, Credential("example-app",
                                                              minting(GIT_TOKENS)))

    config = mcp_config(builder, ref)["mcpServers"]
    assert sorted(config) == sorted([PLAYWRIGHT_SERVER, DESKTOP_SERVER])
    assert config[DESKTOP_SERVER] == {"type": "stdio", **DESKTOP}
    # On the laptop the browser keeps Chromium's own sandbox, and finds the
    # browsers in the folder the laptop builder names, not under the fresh HOME.
    assert "--no-sandbox" not in config[PLAYWRIGHT_SERVER]["args"]
    assert config[PLAYWRIGHT_SERVER]["env"] == {BROWSERS_VARIABLE: "/opt/session-browsers"}
    _, kwargs = run_.call("sudo")
    assert kwargs["env"]["SLIPWAY_TOOL_FLAGS"] == CHROME_FLAG
    assert kwargs["env"]["SLIPWAY_MCP_CONFIG"] == str(builder.dir(ref) / "run" / "mcp.json")
    assert state(builder, ref)["tools_granted"] == ["headless_browser", "operator_browser",
                                                     "desktop"]


def test_a_laptop_with_no_computer_use_server_refuses_desktop(tmp_path, binding):  # noqa: F811
    builder = laptop(tmp_path, Recorder())
    with pytest.raises(ToolRefused, match="names no computer use server"):
        builder.start(handoff(laptop_project('["desktop"]')), binding,
                      Credential("example-app", minting(GIT_TOKENS)))
    assert not (tmp_path / "sessions").exists() or not any((tmp_path / "sessions").iterdir())


def test_a_laptop_build_with_desktop_records_tools_granted_and_names_it_in_the_handoff(
        tmp_path):
    cards = CardWorld(tmp_path)
    path = cards.home / "projects" / "example.toml"
    path.write_text(path.read_text().replace(
        'builder_worker = "laptop"\n',
        'builder_worker = "laptop"\nbuilder_tools = ["headless_browser", "desktop"]\n'))
    cards.builder.schedule = Schedule.WORKER_PROCESS
    given = []

    def laptop_run(input, label):
        def session(handoff, _binding):
            given.append(handoff)
            return LaptopReturn(cards.result(handoff.branch, "laptop-session"), "", 0)
        return card.laptop_build(card.LaptopPorts(session, cards.clock), input)

    cards.ports.laptop = laptop_run
    build_id = cards.card("EX-4")

    async def scenario():
        task = cards.start(build_id)
        await parked(cards, build_id)
        return task

    run(scenario)
    building = cards.records.evidence(build_id, "building")
    assert building["tools_granted"] == ["headless_browser", "desktop"]
    assert building["worker"] == "laptop-builder"
    # The laptop's session got the same list, and its handoff names it.
    (sent,) = given
    assert sent.builder_tools == ("headless_browser", "desktop")
    tools_line = next(x for x in sent.text.splitlines() if x.startswith("4. Tools:"))
    assert "the operator's own desktop" in tools_line and "computer use" in tools_line
    assert "acts as the operator" in tools_line


def test_a_container_build_records_the_default_grant_on_the_build(tmp_path):
    cards = CardWorld(tmp_path)
    build_id = cards.card("EX-5")

    async def scenario():
        task = cards.start(build_id)
        await parked(cards, build_id)
        return task

    run(scenario)
    assert cards.records.evidence(build_id, "building")["tools_granted"] == [
        "headless_browser"]


# Onboard asks the opt in question with the trade off text, and a "no"
# leaves the default.

def test_the_opt_in_question_states_the_trade_off():
    question = BY_ID["builder_tools"]
    assert question.field == "builder_tools" and question.operator_only
    assert question.default == DEFAULT_BUILDER_TOOLS
    text = question.text + " " + question.why
    for words in ("headless browser", "signed in Chrome", "operator_browser", "desktop",
                  "acts as you", "outside the build's dedicated user", "refused for a "
                  "container", "named on every build"):
        assert words in text, words
    ids = [q.id for q in QUESTIONS]
    assert ids.index("builder_worker") < ids.index("builder_tools")


def answer_all(world, answers):  # noqa: F811
    result = machine.start(world.ports, SLUG, OPERATOR)
    for _ in range(3 * len(QUESTIONS)):
        if result["outcome"] != machine.QUESTION:
            return result
        qid = result["question"]["id"]
        result = machine.answer(world.ports, SLUG, qid, answers[qid], OPERATOR)
        if result["outcome"] == machine.REFUSED:
            return result
    raise AssertionError(f"onboard kept asking: {result}")


@pytest.mark.parametrize("no", ["no", "No", "", False])
def test_a_no_to_the_opt_in_leaves_the_default(world, no):  # noqa: F811
    machine.start(world.ports, SLUG, OPERATOR)
    shown = machine.answer(world.ports, SLUG, "builder_tools", no, OPERATOR)
    assert shown["outcome"] != machine.REFUSED, shown
    assert machine.status(world.ports, SLUG)["answers"]["builder_tools"] == [
        "headless_browser"]
    answer_all(world, {**BITBUCKET, "builder_tools": no})
    written = tomllib.loads(world.project_file.read_text())
    assert written["builder_tools"] == ["headless_browser"]


def test_a_yes_with_the_tools_listed_writes_them(world):  # noqa: F811
    answer_all(world, {**BITBUCKET, "builder_tools": "headless_browser, desktop"})
    written = tomllib.loads(world.project_file.read_text())
    assert written["builder_tools"] == ["headless_browser", "desktop"]


def test_an_opt_in_for_a_container_project_is_refused_with_the_reason(world):  # noqa: F811
    result = answer_all(world, {**BITBUCKET, "builder_worker": "",
                                "builder_tools": "operator_browser"})
    assert result["outcome"] == machine.REFUSED
    assert result["question"]["id"] == "builder_tools"
    assert "operator_browser is for a laptop builder only" in result["reason"]
    assert not world.project_file.exists()


# The image and the explainer.

def test_the_builder_image_installs_the_pinned_browser_server():
    text = DOCKERFILE.read_text()
    assert "ARG PLAYWRIGHT_MCP_VERSION" in text
    assert 'test -n "${PLAYWRIGHT_MCP_VERSION}"' in text
    assert '"@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"' in text
    assert "playwright install --with-deps chromium" in text
    assert f"command -v {PLAYWRIGHT_COMMAND}" in text


def test_the_explainer_exists_and_names_each_file():
    text = EXPLAINER.read_text()
    for name in ("builders/tools.py", "builders/cli.py", "config/project.py",
                 "onboard/questions.py", "builder.Dockerfile", "tools_granted"):
        assert name in text, name
