"""What a builder session may drive beyond the shell (U27, KTD12, KTD24, KTD26).

A project's builder_tools (config/project.py) lists the levels it grants:

    headless_browser   a Playwright browser, a browser with no window that
                       code drives. The session reaches it through the
                       Playwright MCP server (an MCP server is a program that
                       offers an agent tools over the Model Context Protocol),
                       which the builder image installs. It runs inside the
                       container, or as the laptop session's own user, so
                       KTD12's isolation is unchanged.
    operator_browser   the operator's own signed in Chrome, through Claude in
                       Chrome, which the CLI turns on with its --chrome flag.
    desktop            the operator's own screen, through a computer use MCP
                       server the laptop builder names; computer use is an
                       agent taking screenshots and clicking and typing.

The last two act as the operator, outside the session's dedicated user, so
only a laptop builder grants them. A project file that lists either for a
container is refused at load; refuse() below is the same rule again at
start, so a handoff that reached a container some other way is refused
before any session runs.

A Grant is the session's side of the list: the MCP configuration file the
CLI reads with --mcp-config, plus any CLI flag. The session runs with
--strict-mcp-config, so it gets exactly these servers and no other, not
even one the repository's own .mcp.json names. The handoff names the same
tools in its section 4 (handoff.tool_names), so a builder never assumes a
tool it lacks, and the workflow writes the list on the build's record as
tools_granted, so every run that had the power says so.

Nothing here names a project (R1): every value is a tool's own name or
flag, or comes from the adapter.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from slipway.config.project import LAPTOP_ONLY, BuilderTool

# The Playwright MCP server, as the builder image installs it, and the
# name the session's tools carry it under (mcp__playwright__...).
PLAYWRIGHT_SERVER = "playwright"
PLAYWRIGHT_COMMAND = "playwright-mcp"
# The name a laptop's computer use server is given in the configuration.
DESKTOP_SERVER = "desktop"
# The CLI flag that connects the session to Claude in Chrome.
CHROME_FLAG = "--chrome"
# Where Playwright finds its browsers. The session's HOME is a fresh folder
# in its workspace, so Playwright's default cache under a real home is never
# there; the server is always told where the browsers are. The image path is
# where deploy/builder.Dockerfile installs them.
BROWSERS_VARIABLE = "PLAYWRIGHT_BROWSERS_PATH"
IMAGE_BROWSERS = "/opt/ms-playwright"
# The configuration file, in the session's run/ folder.
MCP_CONFIG_FILE = "mcp.json"

# How the handoff names each level, after the fixed tool list.
NAMES = {
    BuilderTool.HEADLESS_BROWSER: (
        f"headless browser (the {PLAYWRIGHT_SERVER} MCP server; it runs as you, inside your "
        "session)"),
    BuilderTool.OPERATOR_BROWSER: (
        "the operator's own signed in Chrome (Claude in Chrome; it acts as the operator)"),
    BuilderTool.DESKTOP: (
        f"the operator's own desktop (the {DESKTOP_SERVER} MCP server, computer use; it acts "
        "as the operator)"),
}


class ToolRefused(ValueError):
    """A level the adapter cannot grant, with the reason."""


def ordered(tools: Iterable[str]) -> tuple[str, ...]:
    """The levels in BuilderTool order, each once. An unknown name is refused."""
    listed = set(tools)
    known = [tool.value for tool in BuilderTool]
    unknown = sorted(listed - set(known))
    if unknown:
        raise ToolRefused(f"{unknown[0]!r} is not a builder tool")
    return tuple(name for name in known if name in listed)


def tool_names(tools: Iterable[str]) -> tuple[str, ...]:
    """What the handoff calls each granted level."""
    return tuple(NAMES[BuilderTool(name)] for name in ordered(tools))


def refuse(tools: Iterable[str], container: bool) -> None:
    """Refuse a laptop only level for a container, with the reason."""
    if not container:
        return
    for tool, power in LAPTOP_ONLY.items():
        if tool.value in ordered(tools):
            raise ToolRefused(f"{tool.value} is for a laptop builder only: it {power}, "
                              "outside the session's dedicated user")


@dataclass(frozen=True)
class Grant:
    """The tools one session is given: its MCP servers and its CLI flags."""

    tools: tuple[str, ...]
    servers: Mapping[str, Mapping] = field(default_factory=dict)
    flags: tuple[str, ...] = ()

    def mcp_config(self) -> str:
        """The file --mcp-config reads."""
        return json.dumps({"mcpServers": self.servers}, indent=2, sort_keys=True) + "\n"


def grant(tools: Iterable[str], container: bool, output_dir: str,
          desktop_server: Mapping | None = None, browsers_path: str | None = None) -> Grant:
    """The Grant for a session.

    container says the session runs in the builder image; output_dir is
    where the browser saves screenshots, as the session sees the path.
    desktop_server is the laptop's computer use server entry (command,
    args, env), which only that machine knows; desktop is refused without it.
    browsers_path is the folder the laptop installed Playwright's browsers
    in; a container always uses the image's. A laptop given none leaves
    the server on Playwright's default cache, which the session's fresh HOME
    does not hold, so the browser will not launch; the rest of the build is
    unaffected, which is why it is not refused.
    """
    tools = ordered(tools)
    refuse(tools, container)
    servers: dict[str, Mapping] = {}
    flags: list[str] = []
    if BuilderTool.HEADLESS_BROWSER in tools:
        args = ["--headless", "--isolated", "--output-dir", output_dir]
        if container:
            # The container is the sandbox: every capability is dropped, so
            # Chromium's own sandbox, which needs them, cannot start there.
            args.append("--no-sandbox")
        browsers = IMAGE_BROWSERS if container else browsers_path
        entry: dict = {"type": "stdio", "command": PLAYWRIGHT_COMMAND, "args": args}
        if browsers:
            entry["env"] = {BROWSERS_VARIABLE: browsers}
        servers[PLAYWRIGHT_SERVER] = entry
    if BuilderTool.OPERATOR_BROWSER in tools:
        flags.append(CHROME_FLAG)
    if BuilderTool.DESKTOP in tools:
        if not desktop_server or not desktop_server.get("command"):
            raise ToolRefused("desktop is granted, but this laptop builder names no computer "
                              "use server")
        servers[DESKTOP_SERVER] = {"type": "stdio", **desktop_server}
    return Grant(tools, servers, tuple(flags))
