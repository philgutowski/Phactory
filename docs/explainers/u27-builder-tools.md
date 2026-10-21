# U27 explainer: builder tools beyond the shell

Until this card a builder session had a shell, file tools, and git, and nothing that could look at a page. This card adds a per project switch, `builder_tools`, with three levels: a headless browser that every project gets by default, and two opt ins that hand a laptop build the operator's own signed in browser or screen. The handoff names exactly what a build was given, and the build's record says so too.

## Words used below

- **Headless browser.** A real browser with no window on screen, driven by code. Playwright is the library that drives it here, with Chromium as the browser.
- **MCP server.** A program that offers an agent tools over the Model Context Protocol. The Playwright MCP server turns browser actions (open a page, click, take a screenshot) into tools the session can call.
- **MCP configuration.** The JSON file that tells the Claude Code CLI which MCP servers to start for a session, read with `--mcp-config`. `--strict-mcp-config` makes the CLI use that file alone and ignore any other, such as a `.mcp.json` in the repository.
- **Claude in Chrome.** The browser extension that lets a Claude session drive the operator's own Chrome, with the operator's sign ins. The CLI connects to it with its `--chrome` flag.
- **Computer use.** An agent taking screenshots of a screen and clicking and typing on it, through an MCP server that runs on that machine.
- **Grant.** The tools one session is given: its MCP servers and its CLI flags.
- **Container binding, laptop binding.** Where a project's builds run. A project with no `builder_worker` builds in a container on the VPS, the rented server Slipway runs on; one that names a builder worker builds on that laptop (U13, U29).

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/config/project.py` | Adds `BuilderTool` (the three levels) and the `builder_tools` field. An absent key means `["headless_browser"]`, an empty list means the shell alone, an unknown or repeated name is refused, and `operator_browser` or `desktop` in a project with no `builder_worker` is refused at load with the reason. |
| `src/slipway/builders/tools.py` | Turns the list into a `Grant`: the Playwright server entry for `headless_browser`, the `--chrome` flag for `operator_browser`, and the laptop's own computer use server for `desktop`. `refuse` is the container rule again, and `tool_names` is how the handoff names each level. |
| `src/slipway/builders/cli.py` | Writes the grant to the session's `run/mcp.json`, starts `claude` with `--mcp-config` and `--strict-mcp-config` plus any flag, and records the levels in the session's `state.json` as `tools_granted`. A container refuses a laptop only level before it makes anything; a laptop with no computer use server refuses `desktop` the same way. |
| `src/slipway/builders/handoff.py` | Section 4 names the fixed tools, then each granted level, then says the session has no other tool. `Handoff.builder_tools` carries the levels to the adapter, including through the laptop's task input. |
| `src/slipway/workflow/card.py` | Writes `tools_granted` on the build's `building` transition, the build's record, for a container build and a laptop build alike. |
| `src/slipway/onboard/questions.py` | The opt in question, with the trade off in its text, placed after the builder worker question. |
| `src/slipway/onboard/machine.py` | One line: for a list question with a default, the answer "no" means the default, so the opt in is declined in a word. |
| `deploy/builder.Dockerfile` | Installs the Playwright MCP server at a pinned version (`PLAYWRIGHT_MCP_VERSION`) and the Chromium its own Playwright expects, under `/opt/ms-playwright`. |
| `tests/unit/test_builder_tools.py` | The card's four scenarios and the edges around them. |

## How the pieces connect

```
project file           builder_tools = ["headless_browser", "desktop"]
   |  load: refused if a laptop only level meets a container binding
   v
handoff.render         Handoff.builder_tools, and section 4 names each level
   |
   +-> workflow/card.py    building transition: tools_granted = [...]
   |
   v
cli adapter start      tools.grant(...) -> run/mcp.json and flags
   |                   state.json: tools_granted = [...]
   v
claude -p --mcp-config run/mcp.json --strict-mcp-config [--chrome]
```

The handoff text is the first message of the session's transcript, and the transcript goes on the card's side reference with the trace, so the trace carries the grant too.

## The three levels

**headless_browser**, the default. The server runs inside the container, or as the laptop session's own user, with `--headless` (no window), `--isolated` (the browser profile lives in memory and is gone when the session ends), and `--output-dir` set to the session's `out/browser` folder. Its own environment names `PLAYWRIGHT_BROWSERS_PATH`, the folder Chromium is installed in: the image's `/opt/ms-playwright` in a container, and the laptop builder's `browsers_path` on the laptop. The session's HOME is a fresh folder in its workspace, so Playwright's default cache under a real home is never there. A laptop builder given no `browsers_path` still starts the build, since everything but the browser works, but the browser will not launch, so the operator sets it before any screenshot card. In the container it also gets `--no-sandbox`: every Linux capability is dropped there, and Chromium's own sandbox needs some of them, so the container is the sandbox instead. KTD12's isolation is unchanged, because nothing leaves the session's user. This is what a card uses to take a screenshot of a page it changed, such as the first project's visual rule. A screenshot the session names itself is saved relative to the session's working folder, the repository, so the session must not commit it.

**operator_browser** and **desktop**, laptop only. Each acts as the operator, outside KTD12's dedicated session user: anything the operator is signed in to, the session can reach. So each is off unless the project file lists it, onboard asks about it as an explicit opt in with that trade off in the question, a container binding is refused at load, and every build that had the power says so in `tools_granted` and in its handoff.

`desktop` needs the laptop's own computer use server, which only that machine knows: `LaptopBuilder(desktop_server={"command": ..., "args": [...]})`. Without one, a build that grants `desktop` is refused at start.

## The record

`tools_granted` is on the build's `building` transition (its evidence, the JSON every transition carries) and in the session's `state.json`. The card also asked for it on the attempt row. That row is written by `PostgresRecords.start_attempt` in `src/slipway/workflow/worker.py`, which U29 holds while this card is built, so the column and its write are a follow up once U29 lands; the transition carries the same list meanwhile.

## For the operator

The live part of this card is yours:

1. Build the builder image with `--build-arg PLAYWRIGHT_MCP_VERSION=<a pinned version>` beside the existing arguments.
2. On the laptop, install the server for the session user: `npm install -g @playwright/mcp@<the same version>`, and make sure `playwright-mcp` is on the session's PATH. The package has named its binary `mcp-server-playwright`, `mcp`, and `playwright-mcp` in different releases (0.0.83 installs `playwright-mcp`), so for another version link `playwright-mcp` to the package's `cli.js`, as the image does. Then, from that package's folder, run `PLAYWRIGHT_BROWSERS_PATH=<a folder the session user can read> npx playwright install chromium`, and start the laptop worker's `LaptopBuilder` with `browsers_path` set to that folder.
3. Run one laptop card that takes a headless screenshot, and record it in the pull request.
4. For each opt in, add it to one project's `builder_tools` (through onboard or by hand), run one card, and record the run. `desktop` also needs the laptop worker started with its computer use server.
