"""The terminal client of onboard's state machine (U19, R29, R33).

    python -m slipway.onboard.terminal <slug> [--actor <subject>]

The terminal is a client of the same three calls the MCP tools will wrap
(U18): it calls machine.start, shows the question, reads one line, calls
machine.answer, and repeats. It holds no rule of its own, so the terminal
and the MCP server ask the same questions in the same order (AE8).

What the operator types:

- a choice's number or the choice itself where only the choices are
  taken; where free text is taken too, the choice or the answer as text,
  since a number may be an answer of its own (a GitHub Projects board)
- for a list, the entries separated by commas (numbers work here too)
- an empty line for the default, or to leave an optional key out

End of input (Ctrl D) stops the session. Run the command again to pick up
where it stopped: every accepted answer is already saved.

run takes the ports, an input callable, and an output callable, so the tests
drive it with scripted answers. main builds the real ports: the operator
file under SLIPWAY_HOME, the live probe (checks.LiveProbe), and the records
in Postgres when SLIPWAY_DSN names a database. Without one, audit rows go to
a JSON lines file beside the session's state, and the trivial card's build
cannot be followed until a database is named.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from slipway.onboard import machine
from slipway.onboard.questions import LIST

DSN_VARIABLE = "SLIPWAY_DSN"
# Builder adapters Slipway ships (KTD26); the live probe offers them as choices.
BUILDERS = ("cli", "managed_agents")


def _numbered(question: dict) -> bool:
    """Whether a typed number names a choice: only where free text is not taken.

    Where it is, a number is a legitimate answer of its own (a GitHub
    Projects board can be a bare number), so the choice is typed as text.
    """
    return bool(question["choices"]) and not question["free_text"]


def _pick(question: dict, line: str):
    """The answer a typed line means: numbers name choices, a list splits on commas."""
    offered = question["choices"]

    def one(text: str) -> str:
        text = text.strip()
        if _numbered(question) and text.isdigit() and 1 <= int(text) <= len(offered):
            return offered[int(text) - 1]
        return text

    if question["kind"] == LIST:
        return [one(part) for part in line.split(",") if part.strip()]
    return one(line)


def show_question(question: dict, write: Callable[[str], None]) -> None:
    write("")
    if question.get("note"):
        write(f"  Note: {question['note']}")
    write(question["text"])
    write(f"  Why: {question['why']}")
    if _numbered(question):
        for number, choice in enumerate(question["choices"], 1):
            write(f"  {number}. {choice}")
    elif question["choices"]:
        write(f"  Choices: {', '.join(question['choices'])}, or type your own.")
    if question["default"] is not None:
        write(f"  Default: {question['default']}")
    elif not question["required"]:
        write("  Leave empty for none.")


def show(result: dict, write: Callable[[str], None]) -> None:
    outcome = result["outcome"]
    if outcome == machine.REFUSED:
        write(f"Not taken: {result['reason']}")
    elif outcome == machine.NEEDS_FIX:
        write("")
        write(f"Check failed: {result['property'].replace('_', ' ')}")
        write(f"  Missing: {result['missing']}")
        write(f"  Next step: {result['next_step']}")
        if result.get("reason"):
            write(result["reason"])
    elif outcome == machine.FILE_CARD:
        card = result["card"]
        write("")
        if result.get("reason"):
            write(result["reason"])
        write("Every check passed. File this card:")
        write(f"  Title: {card['title']}")
        write(f"  Label: {card['label']}")
        write(f"  Status: {card['status']}")
        write("  Body:")
        for line in card["body"].splitlines():
            write(f"    {line}")
        write(card["instruction"])
    elif outcome == machine.WAITING:
        write(f"Waiting: {result['reason']}. Run onboard again to check.")
    elif outcome == machine.VERIFIED:
        write(f"Verified: the trivial card {result['card_key']} scored. The project is "
              "ready for cards.")


def run(ports: machine.Ports, slug: str, actor: str, read: Callable[[str], str] = input,
        write: Callable[[str], None] = print) -> int:
    """Ask until the session needs something outside the terminal; 0 once verified."""
    result = machine.start(ports, slug, actor)
    while True:
        show(result, write)
        question = result.get("question")
        if question is None:
            return 0 if result["outcome"] == machine.VERIFIED else 1
        show_question(question, write)
        try:
            line = read("> ")
        except EOFError:
            write("Stopped. Run onboard again to pick up where you left off.")
            return 1
        result = machine.answer(ports, slug, question["id"], _pick(question, line), actor)


class FileRecords:
    """Audit rows in a JSON lines file, for a terminal run with no database named.

    It knows no builds, so the trivial card waits until SLIPWAY_DSN is set.
    """

    follows_builds = False

    def __init__(self, path: Path):
        self.path = path

    def audit(self, slug, subject, tool, arguments, result, client=None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"project_slug": slug, "subject": subject, "client_name": client, "tool": tool,
               "arguments": arguments, "result": result}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def project_id(self, slug: str) -> int:
        return 0

    def latest(self, project_id: int, key: str):
        return None

    def entered(self, build_id: int, state: str):
        return None


def _host_for(operator):
    def host_for(project):
        from slipway.config.operator import Host
        from slipway.hosts.base import resolve_host_credential

        credential = resolve_host_credential(project, operator, os.environ)
        if project.host is Host.BITBUCKET:
            from slipway.hosts.bitbucket import BitbucketHost

            return BitbucketHost(project, credential)
        from slipway.hosts.github import GitHubHost

        return GitHubHost(project, credential)

    return host_for


def main(argv: list[str] | None = None) -> int:
    from slipway.config import home
    from slipway.config.operator import load_operator
    from slipway.onboard.checks import LiveProbe
    from slipway.service.verbs import PostgresRecords, tracker_for

    parser = argparse.ArgumentParser(prog="slipway onboard",
                                     description="Onboard a project, one question at a time.")
    parser.add_argument("slug", help="the project's short name, as in its MCP URL")
    parser.add_argument("--actor", help="your subject; defaults to the operator's")
    args = parser.parse_args(argv)
    root = home()
    operator = load_operator(root)
    dsn = os.environ.get(DSN_VARIABLE, "").strip()
    if dsn:
        import psycopg

        records = PostgresRecords(psycopg.connect(dsn, autocommit=True))
    else:
        records = FileRecords(root / machine.ONBOARD / f"{args.slug}.audit.jsonl")
    probe = LiveProbe(operator, tracker_for, _host_for(operator), BUILDERS)
    ports = machine.real_ports(records, probe, root)
    return run(ports, args.slug, args.actor or operator.operator_subject)


if __name__ == "__main__":
    sys.exit(main())
