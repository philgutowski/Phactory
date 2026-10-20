"""Fakes shared by unit tests.

The plan describes this file as lifted from native-relay's tests/_fakes.py.
That repository is not in this checkout, so this is a fresh file. It holds
FakeRun, a stand in for the injectable run callable that adapters use to call
a command line tool (KTD8): a test scripts the replies, the adapter calls it
instead of a real subprocess, and the test reads back what was called. An
unscripted call fails loudly rather than reaching anything real.

It also holds the two tracker transports U8 built, shared so later units
(U9, U25) script a tracker the same way: DispatchRun answers gh by subcommand
and FakeOpener answers urllib by URL fragment, both from the JSON fixtures in
tests/fixtures/tracker, and both refuse anything they have no route for.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

TRACKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tracker"


class UnscriptedCall(AssertionError):
    """The code under test ran a command no test reply covers."""


@dataclass
class Reply:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


@dataclass
class FakeRun:
    """Scripted replies keyed by an argv prefix, matched longest prefix first."""

    replies: dict[tuple[str, ...], Reply] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def reply(self, prefix: Sequence[str], stdout: str = "", stderr: str = "",
              returncode: int = 0) -> None:
        self.replies[tuple(prefix)] = Reply(stdout, stderr, returncode)

    def __call__(self, argv: Sequence[str], check: bool = True,
                 **_ignored) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        for prefix in sorted(self.replies, key=len, reverse=True):
            if tuple(argv[: len(prefix)]) == prefix:
                r = self.replies[prefix]
                if check and r.returncode != 0:
                    raise subprocess.CalledProcessError(
                        r.returncode, argv, r.stdout, r.stderr)
                return subprocess.CompletedProcess(argv, r.returncode, r.stdout, r.stderr)
        raise UnscriptedCall(f"no scripted reply for {argv!r}")


def tracker_fixture(name: str) -> str:
    """The text of one tracker fixture, named without its .json suffix."""
    return (TRACKER_FIXTURES / f"{name}.json").read_text()


class DispatchRun:
    """A run(args, timeout=None) for gh that answers from fixtures by subcommand."""

    def __init__(self, issues=None, listing="github_issue_list",
                 membership="github_issue_membership", failure=None, graphql_failure=None):
        self.issues = dict(issues or {})
        self.listing = listing
        self.membership = membership
        self.failure = failure
        self.graphql_failure = graphql_failure
        self.calls = []

    def __call__(self, args, timeout=None):
        args = list(args)
        self.calls.append(args)
        if self.failure is not None:
            return subprocess.CompletedProcess(args, *self.failure)
        if args[:2] == ["gh", "project"]:
            # Served so a regression shows up as a call, not a crash.
            return subprocess.CompletedProcess(args, 0, tracker_fixture("github_project_items"), "")
        if args[:3] == ["gh", "api", "graphql"]:
            if self.graphql_failure is not None:
                return subprocess.CompletedProcess(args, *self.graphql_failure)
            body = self.membership
            return subprocess.CompletedProcess(
                args, 0, body if body.lstrip().startswith("{") else tracker_fixture(body), "")
        if args[:3] == ["gh", "issue", "list"]:
            return subprocess.CompletedProcess(args, 0, tracker_fixture(self.listing), "")
        if args[:3] == ["gh", "issue", "view"]:
            name = self.issues.get(args[3])
            if name is None:
                return subprocess.CompletedProcess(args, 1, "", f"no issue {args[3]}")
            return subprocess.CompletedProcess(args, 0, tracker_fixture(name), "")
        raise AssertionError(f"no fixture routed for {args!r}")

    def graphql_queries(self):
        return [arg[len("query="):] for call in self.calls if call[:3] == ["gh", "api", "graphql"]
                for arg in call if arg.startswith("query=")]


class _Body:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for urllib's opener. Routes a URL substring to a fixture file."""

    def __init__(self, routes, error=None):
        self.routes = dict(routes)
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        url = request.get_full_url()
        self.requests.append((url, dict(request.header_items()), timeout))
        if self.error:
            raise self.error
        for fragment, name in self.routes.items():
            if fragment in url:
                return _Body(tracker_fixture(name))
        raise AssertionError(f"no fixture routed for {url}")
