"""Fakes shared by unit tests.

The plan describes this file as lifted from native-relay's tests/_fakes.py.
That repository is not in this checkout, so this is a fresh file. It holds
FakeRun, a stand in for the injectable run callable that adapters use to call
a command line tool (KTD8): a test scripts the replies, the adapter calls it
instead of a real subprocess, and the test reads back what was called. An
unscripted call fails loudly rather than reaching anything real.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field


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
