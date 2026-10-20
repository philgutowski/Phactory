"""A project file, $SLIPWAY_HOME/projects/<slug>.toml (R1, R2).

Everything Slipway knows about one project is here, so nothing about any
project lives in code. The slug, the short name in the project's MCP URL,
is the file name; the file does not repeat it.

Validation runs in three passes, and each failure names the dotted key:

1. Shape: every required key present with the right type, no unknown key.
2. Secrets: no string looks like a token or key (redact.TOKEN_PATTERNS) or
   like an environment variable name. A project file names credentials by
   their operator file entry, never by the variable that holds them (R3).
3. Names: check_names(project, operator) refuses a name the operator file
   does not define. It is a separate call because resolve (bindings.py)
   reports a missing name as binding_invalid at dispatch instead.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from slipway.config import (
    LOOKS_LIKE_ENV_NAME,
    ConfigError,
    Reader,
    home,
    read_toml,
    refuse_secrets,
    strings,
)
from slipway.config.operator import Host, Operator

PROJECTS = "projects"
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*")
# Roles every project binds (R13). Other roles, such as a helper, are optional.
REQUIRED_ROLES = ("builder", "judge")


class LandingPath(StrEnum):
    """How the host puts a pull request on the base branch (KTD7, KTD9)."""

    MERGE_QUEUE = "merge_queue"  # GitHub merge queue
    AUTO_MERGE_STRICT = "auto_merge_strict"  # GitHub auto merge, branch kept up to date
    BITBUCKET_QUEUE = "bitbucket_queue"


class MergeMethod(StrEnum):
    """How the host builds the commit that lands."""

    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


class Tracker(StrEnum):
    GITHUB_PROJECTS = "github_projects"
    JIRA = "jira"


class TraceDetail(StrEnum):
    FULL = "full"
    SUMMARY = "summary"


@dataclass(frozen=True)
class RoleBinding:
    """One role bound to an operator account, a model, an effort, and a budget."""

    role: str
    account: str
    model: str
    effort: str
    time_budget_minutes: int


@dataclass(frozen=True)
class Project:
    path: Path
    slug: str
    repository: str
    host: Host
    tracker: Tracker
    tracker_site: str
    board: str
    lane_label: str
    ready_status: str
    in_progress_status: str
    in_review_status: str
    landed_status: str
    gate_command: str
    allowed_builders: tuple[str, ...]
    roles: dict[str, RoleBinding]
    auto_merge: bool
    build_cap: int
    daily_dispatch_cap: int
    trivial_card_target: str
    trace_project: str
    trace_detail: TraceDetail
    tracker_credential: str
    host_credential: str
    allowed_subjects: tuple[str, ...]
    evals_path: str
    landing_path: LandingPath | None = None
    merge_method: MergeMethod | None = None
    runner_labels: tuple[str, ...] = ()
    machine_bound_tests: tuple[str, ...] = ()
    builder_worker: str | None = None


KNOWN = {
    "repository", "host", "tracker", "tracker_site", "board", "lane_label", "ready_status",
    "in_progress_status", "in_review_status", "landed_status", "gate_command",
    "allowed_builders", "roles", "auto_merge", "build_cap", "daily_dispatch_cap",
    "trivial_card_target", "trace_project", "trace_detail", "tracker_credential",
    "host_credential", "allowed_subjects", "evals_path", "landing_path", "merge_method",
    "runner_labels", "machine_bound_tests", "builder_worker",
}
ROLE_KEYS = {"account", "model", "effort", "time_budget_minutes"}


def project_path(slug: str, root: Path | None = None) -> Path:
    if not SLUG.fullmatch(slug):
        raise ConfigError(f"{slug}.toml", "(file)", f"{slug!r} is not a valid slug")
    return (root or home()) / PROJECTS / f"{slug}.toml"


def load_project(slug: str, root: Path | None = None) -> Project:
    """Read and validate one project file. The file is read on every call."""
    path = project_path(slug, root)
    return parse_project(path, read_toml(path))


def refuse_env_names(path: Path, data: dict) -> None:
    for key, value in strings(data):
        if LOOKS_LIKE_ENV_NAME.fullmatch(value.strip()):
            raise ConfigError(
                path, key,
                f"{value!r} looks like an environment variable name; a project file names "
                "credentials by their operator file entry",
            )


def parse_project(path: Path, data: dict) -> Project:
    refuse_secrets(path, data)
    refuse_env_names(path, data)
    top = Reader(path, data)
    top.refuse_unknown(KNOWN)

    roles = {}
    for role, t in top.tables("roles").items():
        t.refuse_unknown(ROLE_KEYS)
        roles[role] = RoleBinding(
            role, t.text("account"), t.text("model"), t.text("effort"),
            t.integer("time_budget_minutes"),
        )
    for role in REQUIRED_ROLES:
        if role not in roles:
            top.fail(f"roles.{role}", "missing")

    return Project(
        path=path,
        slug=path.stem,
        repository=top.text("repository"),
        host=top.choice("host", Host),
        tracker=top.choice("tracker", Tracker),
        tracker_site=top.text("tracker_site"),
        board=top.text("board"),
        lane_label=top.text("lane_label"),
        ready_status=top.text("ready_status"),
        in_progress_status=top.text("in_progress_status"),
        in_review_status=top.text("in_review_status"),
        landed_status=top.text("landed_status"),
        gate_command=top.text("gate_command"),
        allowed_builders=top.strs("allowed_builders", nonempty=True),
        roles=roles,
        auto_merge=top.flag("auto_merge"),
        build_cap=top.integer("build_cap"),
        daily_dispatch_cap=top.integer("daily_dispatch_cap"),
        trivial_card_target=top.text("trivial_card_target"),
        trace_project=top.text("trace_project"),
        trace_detail=top.choice("trace_detail", TraceDetail),
        tracker_credential=top.text("tracker_credential"),
        host_credential=top.text("host_credential"),
        allowed_subjects=top.strs("allowed_subjects", nonempty=True),
        evals_path=top.text("evals_path"),
        landing_path=top.choice("landing_path", LandingPath, required=False),
        merge_method=top.choice("merge_method", MergeMethod, required=False),
        runner_labels=top.strs("runner_labels", required=False),
        machine_bound_tests=top.strs("machine_bound_tests", required=False),
        builder_worker=top.text("builder_worker", required=False),
    )


def check_names(project: Project, operator: Operator) -> None:
    """Refuse any name in the project file that the operator file does not define."""

    def fail(key: str, value: str, where: str):
        raise ConfigError(project.path, key, f"{value!r} is not in the operator file's {where}")

    if project.tracker_credential not in operator.tracker_credentials:
        fail("tracker_credential", project.tracker_credential, "tracker_credentials")
    if project.host_credential not in operator.host_credentials:
        fail("host_credential", project.host_credential, "host_credentials")
    elif (other := operator.host_credentials[project.host_credential].host) != project.host:
        raise ConfigError(
            project.path, "host_credential",
            f"{project.host_credential!r} is a {other} credential, the project's host is "
            f"{project.host}",
        )
    if project.trace_project not in operator.langfuse_projects:
        fail("trace_project", project.trace_project, "langfuse_projects")
    if project.builder_worker and project.builder_worker not in operator.builder_workers:
        fail("builder_worker", project.builder_worker, "builder_workers")
    for role, binding in project.roles.items():
        if binding.account not in operator.accounts:
            fail(f"roles.{role}.account", binding.account, "accounts")
