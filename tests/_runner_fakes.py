"""Fakes for the runner cron and the rollback evaluator (U22).

FakeRecords keeps runner_builds, gate_runs, and findings in lists with the
same rules the Postgres records enforce: one active row per instance, a
compare and set that applies only to the row the caller read, and a gate run
attributed to the build whose activation interval contains its start.
FakeScripts records each script call and fails the ones a test names.
FakeApi answers /api/runners from a list a test sets. RunnerWorld gathers
them with a clock a test moves by hand.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from slipway.config.operator import (
    IdentityProvider,
    Operator,
    RunnerInstance,
    RunnerRegistration,
    RunnerRepository,
)
from slipway.runner.instances import Finding, RunnerBuild, RunnerPorts, ScriptResult

T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
A, B, C = "a" * 40, "b" * 40, "c" * 40
REPOSITORY = RunnerRepository("example-owner", "example-runner", "main", "VERSION")


def instance(name: str, host: str) -> RunnerInstance:
    return RunnerInstance(name, f"{name}-runner", ("self-hosted", name), host,
                          (RunnerRegistration("example-org/example-app", "EXAMPLE_TOKEN", None),))


def operator(*instances: RunnerInstance) -> Operator:
    return Operator(
        path=Path("operator.toml"), operator_subject="user_operator_example",
        identity_provider=IdentityProvider("https://auth.example.test", "client", "IDP_SECRET"),
        runner_repository=REPOSITORY,
        runner_instances={i.name: i for i in instances},
        runner_read_api="http://runner-api.internal:8080",
    )


@dataclass
class FakeRecords:
    builds: list[RunnerBuild] = field(default_factory=list)
    gate_runs: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)

    def active_build(self, instance):
        return next((b for b in self.builds if b.instance == instance and b.state == "active"),
                    None)

    def runner_build(self, build_id):
        return next((b for b in self.builds if b.id == build_id), None)

    def previous_build(self, build):
        found = [b for b in self.builds if b.instance == build.instance and b.state == "previous"
                 and b.deactivated_at == build.activated_at and b.id != build.id]
        return found[-1] if found else None

    def rejected(self, runner_commit):
        return any(b.runner_commit == runner_commit and b.state == "rejected"
                   for b in self.builds)

    def has_finding(self, fingerprint):
        return any(f["fingerprint"] == fingerprint for f in self.findings)

    def _replace(self, build_id, **changes):
        self.builds = [replace(b, **changes) if b.id == build_id else b for b in self.builds]

    def _insert(self, instance, runner_commit, at):
        assert self.active_build(instance) is None, "one active row per instance"
        new = RunnerBuild(len(self.builds) + 1, instance, runner_commit, "active", at)
        self.builds.append(new)
        return new.id

    def activate(self, instance, runner_commit, expected_id, at):
        active = self.active_build(instance)
        if (active.id if active else None) != expected_id:
            return None
        if active:
            self._replace(active.id, state="previous", deactivated_at=at)
        return self._insert(instance, runner_commit, at)

    def roll_back(self, build_id, runner_commit, at):
        build = self.runner_build(build_id)
        if build is None or build.state != "active":
            return None
        self._replace(build_id, state="rejected", deactivated_at=at)
        return self._insert(build.instance, runner_commit, at)

    def latest_gate_run(self, project_id, instance, runner_commit):
        runs = [g for g in self.gate_runs if g["project_id"] == project_id
                and g["instance"] == instance and g["runner_build_id"] is not None
                and self.runner_build(g["runner_build_id"]).runner_commit == runner_commit]
        return max(runs, key=lambda g: (g["started_at"], g["id"]), default=None)

    def finding_classes(self, gate_run_id):
        return {f["class"] for f in self.findings if f["gate_run_id"] == gate_run_id}

    def rollback_finding(self, runner_build_id):
        return next((f["id"] for f in self.findings if f["runner_build_id"] == runner_build_id
                     and f["class"] == "runner_regression"
                     and f["related_finding_id"] is None), None)

    def add_finding(self, item: Finding):
        row = {"id": len(self.findings) + 1, "class": item.finding_class,
               "fingerprint": item.fingerprint, "excerpt": item.excerpt,
               "runner_commit": item.runner_commit, "project_id": item.project_id,
               "gate_run_id": item.gate_run_id, "runner_build_id": item.runner_build_id,
               "related_finding_id": item.related_finding_id}
        self.findings.append(row)
        return row["id"]

    # Test helpers, standing in for observation (U23).

    def gate_run(self, project_id, instance, started_at, *classes):
        """A gate run attributed by its start time, with the findings observation wrote."""
        build = next((b for b in self.builds if b.instance == instance
                      and b.activated_at <= started_at
                      and (b.deactivated_at is None or b.deactivated_at > started_at)), None)
        run = {"id": len(self.gate_runs) + 1, "project_id": project_id, "instance": instance,
               "runner_build_id": build.id if build else None, "started_at": started_at}
        self.gate_runs.append(run)
        for name in classes:
            self.add_finding(Finding(name, f"{name}:{run['id']}",
                                     build.runner_commit if build else "",
                                     project_id=project_id, gate_run_id=run["id"],
                                     runner_build_id=run["runner_build_id"]))
        return run


@dataclass
class FakeScripts:
    calls: list[tuple] = field(default_factory=list)
    failing: set[tuple] = field(default_factory=set)  # (script, host) pairs that fail
    output: str = "error: zig build test failed"

    def _call(self, script, host, *args):
        self.calls.append((script, host, *args))
        if (script, host) in self.failing:
            return ScriptResult(False, self.output)
        return ScriptResult(True, "")

    def build(self, host, repository, commit):
        return self._call("build", host, commit)

    def test(self, host, commit):
        return self._call("test", host, commit)

    def swap(self, host, instance, commit):
        return self._call("swap", host, instance, commit)

    def rollback(self, host, instance, commit):
        return self._call("rollback", host, instance, commit)

    def named(self, script):
        return [c for c in self.calls if c[0] == script]


@dataclass
class FakeApi:
    listed: list[dict] = field(default_factory=list)
    down: bool = False

    def runners(self):
        if self.down:
            raise OSError("connection refused")
        return list(self.listed)


@dataclass
class RunnerWorld:
    operator: Operator
    head: str = A
    now: datetime = T0
    records: FakeRecords = field(default_factory=FakeRecords)
    scripts: FakeScripts = field(default_factory=FakeScripts)
    api: FakeApi = field(default_factory=FakeApi)

    @property
    def ports(self) -> RunnerPorts:
        return RunnerPorts(records=self.records, scripts=self.scripts, api=self.api,
                           head=lambda repository: self.head, operator=lambda: self.operator,
                           now=lambda: self.now)

    def later(self, delta: timedelta = timedelta(hours=1)) -> datetime:
        self.now += delta
        return self.now


def world(*instances: RunnerInstance) -> RunnerWorld:
    return RunnerWorld(operator(*(instances or (instance("vps", "vps"),))))
