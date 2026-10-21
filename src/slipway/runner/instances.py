"""The ports the runner cron and the rollback evaluator share (U22, KTD20).

A runner instance is one supervisor on one host (the VPS or the laptop)
running one runner process per repository it serves, each registering a
just in time runner (one that registers, runs a single job, and exits)
under the instance's name prefix. The operator file names each instance,
its prefix, its labels, its host, and the runner repository it tracks.

A port is the narrow interface a piece of logic reaches the world through,
so a test hands it a fake and the deployment hands it the real thing. This
module holds four, gathered in RunnerPorts:

    records   the runner_builds rows and runner findings, over Postgres
    scripts   the deploy/runner scripts on each host: build, test, swap,
              rollback. Slipway never builds or runs the runner's code
              itself (KTD18); the scripts do that, on the runner's host, in
              a sandbox container
    api       the runner's read API, one per deployment, which says which
              runner processes are busy
    head      the runner repository's default branch head

The records are the truth about which build an instance runs. A swap is
first a script, which repoints the path the supervisor launches next, and
then a compare and set on the instance's active row: an update that only
applies when the row is still the one the caller read, so two writers never
both win.
"""

import json
import shlex
import subprocess
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from psycopg import errors
from psycopg.rows import dict_row

from slipway import redact
from slipway.config.operator import Operator, RunnerRepository
from slipway.records import queries

# Finding classes this unit writes. U23 writes the runner class findings
# rollback.RUNNER_CLASSES names.
BUILD_RED = "runner_build_red"
REGRESSION = "runner_regression"

# A finding's excerpt keeps the end of a script's output, where the error is.
EXCERPT_LINES = 40
SCRIPT_TIMEOUT_SECONDS = 3600
API_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class RunnerBuild:
    """One runner_builds row: which runner commit an instance ran, and when."""

    id: int
    instance: str
    runner_commit: str
    state: str
    activated_at: datetime
    deactivated_at: datetime | None = None


@dataclass(frozen=True)
class Finding:
    """A finding before it is written. Build one with finding() so it is scrubbed."""

    finding_class: str
    fingerprint: str
    runner_commit: str
    excerpt: str | None = None
    project_id: int | None = None
    gate_run_id: int | None = None
    runner_build_id: int | None = None
    related_finding_id: int | None = None


def finding(finding_class: str, fingerprint: str, runner_commit: str,
            excerpt: str | None = None, **links: Any) -> Finding:
    """A Finding whose excerpt is bounded and scrubbed by redact (KTD24)."""
    if excerpt is not None:
        excerpt = redact.scrub("\n".join(excerpt.splitlines()[-EXCERPT_LINES:]))
    return Finding(finding_class, fingerprint, runner_commit, excerpt, **links)


@dataclass(frozen=True)
class ScriptResult:
    ok: bool
    output: str = ""


class Records(Protocol):
    def active_build(self, instance: str) -> RunnerBuild | None: ...
    def runner_build(self, build_id: int) -> RunnerBuild | None: ...
    def previous_build(self, build: RunnerBuild) -> RunnerBuild | None: ...
    def rejected(self, runner_commit: str) -> bool: ...
    def has_finding(self, fingerprint: str) -> bool: ...
    def activate(self, instance: str, runner_commit: str, expected_id: int | None,
                 at: datetime) -> int | None: ...
    def roll_back(self, build_id: int, runner_commit: str, at: datetime) -> int | None: ...
    def latest_gate_run(self, project_id: int, instance: str,
                        runner_commit: str) -> Mapping[str, Any] | None: ...
    def finding_classes(self, gate_run_id: int) -> set[str]: ...
    def rollback_finding(self, runner_build_id: int) -> int | None: ...
    def add_finding(self, item: Finding) -> int: ...


class Scripts(Protocol):
    def build(self, host: str, repository: RunnerRepository, commit: str) -> ScriptResult: ...
    def test(self, host: str, commit: str) -> ScriptResult: ...
    def swap(self, host: str, instance: str, commit: str) -> ScriptResult: ...
    def rollback(self, host: str, instance: str, commit: str) -> ScriptResult: ...


class Api(Protocol):
    def runners(self) -> list[Mapping[str, Any]]: ...


@dataclass
class RunnerPorts:
    records: Records
    scripts: Scripts
    api: Api
    head: Callable[[RunnerRepository], str]
    operator: Callable[[], Operator]
    now: Callable[[], datetime]


def busy_under(prefix: str, runners: Sequence[Mapping[str, Any]]) -> list[str]:
    """Names of the busy runner processes under prefix.

    A runner counts as busy when its record says busy is true or its status
    is busy; an online idle runner and an offline one between jobs do not.
    """
    return sorted(str(r.get("name")) for r in runners
                  if str(r.get("name", "")).startswith(prefix)
                  and (r.get("busy") is True or r.get("status") == "busy"))


def parse_runners(payload: Any) -> list[Mapping[str, Any]]:
    """The runner list from /api/runners, as a bare list or under a runners key."""
    if isinstance(payload, Mapping):
        payload = payload.get("runners")
    if not isinstance(payload, list):
        raise ValueError("the runner read API did not return a list of runners")
    return [r for r in payload if isinstance(r, Mapping)]


def parse_ls_remote(output: str, ref: str) -> str:
    """The commit git ls-remote printed for ref."""
    for line in output.splitlines():
        commit, _, name = line.partition("\t")
        if name.strip() == ref:
            return commit.strip()
    raise LookupError(f"the runner repository has no {ref}")


# The real ports.

@dataclass
class ReadApi:
    """The runner's read API over HTTP. It listens on the compose network only (KTD28)."""

    base_url: str
    timeout: int = API_TIMEOUT_SECONDS

    def runners(self) -> list[Mapping[str, Any]]:
        url = self.base_url.rstrip("/") + "/api/runners"
        with urllib.request.urlopen(url, timeout=self.timeout) as response:  # noqa: S310
            return parse_runners(json.load(response))


def git_head(repository: RunnerRepository, run=subprocess.run) -> str:
    """The default branch head of the runner repository, through git ls-remote.

    The runner is a GitHub Actions runner, so its repository is on GitHub;
    the operator file gives the owner, name, and branch.
    """
    ref = f"refs/heads/{repository.default_branch}"
    url = f"https://github.com/{repository.owner}/{repository.name}.git"
    done = run(["git", "ls-remote", url, ref], capture_output=True, text=True,
               timeout=60, check=True)
    return parse_ls_remote(done.stdout, ref)


@dataclass
class ShellScripts:
    """The deploy/runner scripts, run on the host each instance lives on.

    directory is where the scripts are on every host. remotes maps a host
    name from the operator file to the command prefix that reaches it, such
    as ["ssh", "runner@laptop"]; a host with no entry is this machine.
    """

    directory: str
    remotes: Mapping[str, Sequence[str]] = field(default_factory=dict)
    run: Callable[..., Any] = subprocess.run
    timeout: int = SCRIPT_TIMEOUT_SECONDS

    def _call(self, host: str, script: str, *args: str) -> ScriptResult:
        path = f"{self.directory.rstrip('/')}/{script}.sh"
        remote = list(self.remotes.get(host, ()))
        # A remote shell joins its arguments into one line, so each is quoted.
        argv = [*remote, *(shlex.quote(a) for a in (path, *args))] if remote else [path, *args]
        try:
            done = self.run(argv, capture_output=True, text=True, timeout=self.timeout,
                            check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            return ScriptResult(False, f"{script}.sh did not finish: {error}")
        return ScriptResult(done.returncode == 0, (done.stdout or "") + (done.stderr or ""))

    def build(self, host: str, repository: RunnerRepository, commit: str) -> ScriptResult:
        url = f"https://github.com/{repository.owner}/{repository.name}.git"
        return self._call(host, "build", url, commit, repository.version_file)

    def test(self, host: str, commit: str) -> ScriptResult:
        return self._call(host, "test", commit)

    def swap(self, host: str, instance: str, commit: str) -> ScriptResult:
        return self._call(host, "swap", instance, commit)

    def rollback(self, host: str, instance: str, commit: str) -> ScriptResult:
        return self._call(host, "rollback", instance, commit)


_BUILD_COLUMNS = "id, instance, runner_commit, state, activated_at, deactivated_at"


class PostgresRecords:
    """Records over one autocommit psycopg connection to the slipway database."""

    def __init__(self, conn):
        self.conn = conn

    def _build(self, query: str, params) -> RunnerBuild | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(query, params).fetchone()
        return RunnerBuild(**row) if row else None

    def active_build(self, instance: str) -> RunnerBuild | None:
        return self._build(f"SELECT {_BUILD_COLUMNS} FROM runner_builds "
                           "WHERE instance = %s AND state = 'active'", (instance,))

    def runner_build(self, build_id: int) -> RunnerBuild | None:
        return self._build(f"SELECT {_BUILD_COLUMNS} FROM runner_builds WHERE id = %s",
                           (build_id,))

    def previous_build(self, build: RunnerBuild) -> RunnerBuild | None:
        # The build this one replaced: its interval ends where this one's
        # begins. A rejected build is never a target to go back to.
        return self._build(f"SELECT {_BUILD_COLUMNS} FROM runner_builds "
                           "WHERE instance = %s AND state = 'previous' AND deactivated_at = %s "
                           "AND id <> %s ORDER BY id DESC LIMIT 1",
                           (build.instance, build.activated_at, build.id))

    def rejected(self, runner_commit: str) -> bool:
        return self.conn.execute(
            "SELECT EXISTS (SELECT 1 FROM runner_builds WHERE runner_commit = %s "
            "AND state = 'rejected')", (runner_commit,)).fetchone()[0]

    def has_finding(self, fingerprint: str) -> bool:
        return self.conn.execute("SELECT EXISTS (SELECT 1 FROM findings WHERE fingerprint = %s)",
                                 (fingerprint,)).fetchone()[0]

    def activate(self, instance: str, runner_commit: str, expected_id: int | None,
                 at: datetime) -> int | None:
        try:
            with self.conn.transaction():
                if expected_id is None:
                    if self.active_build(instance) is not None:
                        return None
                else:
                    closed = self.conn.execute(
                        "UPDATE runner_builds SET state = 'previous', deactivated_at = %s "
                        "WHERE id = %s AND instance = %s AND state = 'active'",
                        (at, expected_id, instance))
                    if closed.rowcount != 1:
                        return None
                return self._insert_active(instance, runner_commit, at)
        except errors.UniqueViolation:
            # Another writer activated a first build on this instance meanwhile.
            return None

    def roll_back(self, build_id: int, runner_commit: str, at: datetime) -> int | None:
        with self.conn.transaction():
            row = self.conn.execute(
                "UPDATE runner_builds SET state = 'rejected', deactivated_at = %s "
                "WHERE id = %s AND state = 'active' RETURNING instance",
                (at, build_id)).fetchone()
            if row is None:
                return None
            return self._insert_active(row[0], runner_commit, at)

    def _insert_active(self, instance: str, runner_commit: str, at: datetime) -> int:
        return self.conn.execute(
            "INSERT INTO runner_builds (instance, runner_commit, state, activated_at) "
            "VALUES (%s, %s, 'active', %s) RETURNING id",
            (instance, runner_commit, at)).fetchone()[0]

    def latest_gate_run(self, project_id: int, instance: str,
                        runner_commit: str) -> Mapping[str, Any] | None:
        return queries.latest_gate_run(self.conn, project_id, instance, runner_commit)

    def finding_classes(self, gate_run_id: int) -> set[str]:
        rows = self.conn.execute("SELECT DISTINCT class FROM findings WHERE gate_run_id = %s",
                                 (gate_run_id,)).fetchall()
        return {row[0] for row in rows}

    def rollback_finding(self, runner_build_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM findings WHERE runner_build_id = %s AND class = %s "
            "AND related_finding_id IS NULL ORDER BY id LIMIT 1",
            (runner_build_id, REGRESSION)).fetchone()
        return row[0] if row else None

    def add_finding(self, item: Finding) -> int:
        return self.conn.execute(
            "INSERT INTO findings (project_id, runner_build_id, class, fingerprint, excerpt, "
            "runner_commit, gate_run_id, related_finding_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (item.project_id, item.runner_build_id, item.finding_class, item.fingerprint,
             item.excerpt, item.runner_commit, item.gate_run_id,
             item.related_finding_id)).fetchone()[0]


__all__ = ["BUILD_RED", "REGRESSION", "Api", "Finding", "PostgresRecords", "ReadApi",
           "Records", "RunnerBuild", "RunnerPorts", "ScriptResult", "Scripts", "ShellScripts",
           "busy_under", "finding", "git_head", "parse_ls_remote", "parse_runners"]
