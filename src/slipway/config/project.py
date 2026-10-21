"""A project file, $SLIPWAY_HOME/projects/<slug>.toml (R1, R2).

Everything Slipway knows about one project is here, so nothing about any
project lives in code. The slug, the short name in the project's MCP URL,
is the file name; the file does not repeat it.

Validation runs in three passes, and each failure names the dotted key:

1. Shape: every required key present with the right type, no unknown key.
2. Secrets: no string looks like a token or key (redact.TOKEN_PATTERNS),
   and no field that names an operator file entry (NAME_FIELDS and each
   role's account) looks like an environment variable name. A project file
   names credentials by their operator file entry, never by the variable
   that holds them (R3). Any other string, such as a Jira status TO_DO, may
   be written in capitals.
3. Names: check_names(project, operator) refuses a name the operator file
   does not define, a host credential for another host, and a tracker
   credential for another site than tracker_site. It is a separate call
   because resolve (bindings.py) reports the same faults as binding_invalid
   at dispatch instead; onboard calls it before it saves a project file.

Rules (follow up #56). The optional [rules] table is the operator's rules
text for the project, the text the handoff's section 3 carries. It holds
exactly one key: text, the rules written inline, or file, the path of a
rules file relative to the project file's own directory, such as
"example.rules.md" beside "example.toml". The file must stay inside that
directory, so a project file cannot pull in another file on the machine.
It is read with the project file, and its text passes the same secret check
as every string in the project file. Project.rules is the text either way,
and an empty string when the table is absent.

Conventions (U26). Three optional fields let a project keep its own names
without code naming the project:

    branch_template     the card's branch, from {key} (the card key), {slug}
                        (the card title in lower case words joined by
                        hyphens), and {build} (the build id). The default,
                        slipway/{key}-{build}, is the shape every build has
                        used so far. {key} is required, because the board
                        automation and verify find the card by it, and the
                        rendered name must be a legal git branch name.
    pull_request_title  the pull request's title, from {key} and {title}
                        (the card title). The default, {title}, is today's
                        title; the host still puts its tracker token in front
                        when the title does not already carry it.
    checkout            how a laptop session gets its working copy: clone
                        (the default, a fresh clone, as in a container) or
                        worktree (a git worktree, a second working folder of
                        one repository, added from the laptop's shared
                        checkout; builders/cli.py).

branch_namespace gives the literal text in front of the template's first
placeholder, the namespace a machine identity's push rule holds the builder
to (KTD6). Onboard reads it in place of a fixed slipway/ prefix and refuses
an empty one when it pins a machine identity.

landing_path also takes external_lander: something that is neither Slipway
nor the host's queue, such as a project's own watcher session, puts the
branch on the base branch and pushes, and the host then marks the pull
request merged. No host setting reveals it, so the probe takes it only from
this file.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from string import Formatter

from slipway.config import (
    LOOKS_LIKE_ENV_NAME,
    ConfigError,
    Reader,
    home,
    read_toml,
    refuse_secrets,
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
    PLAIN_MERGE = "plain_merge"  # GitHub auto merge, no required up to date check
    # Neither Slipway nor the host's queue lands the branch: a lander of the
    # project's own pushes it and the host marks the pull request merged.
    # Slipway never arms auto merge on this path. Only a project file sets it.
    EXTERNAL_LANDER = "external_lander"


class MergeMethod(StrEnum):
    """How the host builds the commit that lands."""

    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


class Tracker(StrEnum):
    GITHUB_PROJECTS = "github_projects"
    JIRA = "jira"


class Checkout(StrEnum):
    """How a laptop session gets its working copy of the repository."""

    CLONE = "clone"
    WORKTREE = "worktree"


class TraceDetail(StrEnum):
    FULL = "full"
    SUMMARY = "summary"


DEFAULT_BRANCH_TEMPLATE = "slipway/{key}-{build}"
DEFAULT_PULL_REQUEST_TITLE = "{title}"


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
    rules: str = ""
    branch_template: str = DEFAULT_BRANCH_TEMPLATE
    pull_request_title: str = DEFAULT_PULL_REQUEST_TITLE
    checkout: Checkout = Checkout.CLONE

    def branch_for(self, key: str, title: str, build_id: int | str) -> str:
        """The card's branch name under this project's template."""
        return render_branch(self.branch_template, key, title, build_id)

    def title_for(self, key: str, title: str) -> str:
        """The pull request title under this project's template."""
        return self.pull_request_title.format(key=key, title=title)


KNOWN = {
    "repository", "host", "tracker", "tracker_site", "board", "lane_label", "ready_status",
    "in_progress_status", "in_review_status", "landed_status", "gate_command",
    "allowed_builders", "roles", "auto_merge", "build_cap", "daily_dispatch_cap",
    "trivial_card_target", "trace_project", "trace_detail", "tracker_credential",
    "host_credential", "allowed_subjects", "evals_path", "landing_path", "merge_method",
    "runner_labels", "machine_bound_tests", "builder_worker", "rules", "branch_template",
    "pull_request_title", "checkout",
}
ROLE_KEYS = {"account", "model", "effort", "time_budget_minutes"}
RULES_KEYS = ("text", "file")
# Fields whose value is the name of an operator file entry. Only these, plus
# roles.<role>.account, are checked for the shape of an environment variable
# name; a status label or board key in capitals is a legitimate value.
NAME_FIELDS = ("tracker_credential", "host_credential", "trace_project", "builder_worker")


BRANCH_FIELDS = ("key", "slug", "build")
TITLE_FIELDS = ("key", "title")
SLUG_WORDS = re.compile(r"[a-z0-9]+")
SLUG_LIMIT = 40
# What git refuses in a branch name (git check-ref-format): a control or
# space character, ~ ^ : ? * [ \\, two dots, @{, a component starting with a
# dot or ending in .lock, an empty component, a leading hyphen, a trailing
# dot, and the name @ alone.
ILLEGAL_REF = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|(^|/)\.|\.lock(/|$)|//|^/|/$|\.$|^-")
# Values a template is rendered with at load, to prove every card renders a
# legal name. They are shaped like the real ones and hold only legal text.
SAMPLE = {"key": "AB-1", "slug": "a-card", "build": "1", "title": "A card"}


def slugify(title: str) -> str:
    """The card title as lower case words joined by hyphens, at most SLUG_LIMIT long."""
    words = SLUG_WORDS.findall(title.lower())
    slug = ""
    for word in words:
        joined = f"{slug}-{word}" if slug else word
        if len(joined) > SLUG_LIMIT:
            break
        slug = joined
    return slug or (words[0][:SLUG_LIMIT] if words else "card")


def render_branch(template: str, key: str, title: str, build_id: int | str) -> str:
    return template.format(key=key, slug=slugify(title), build=build_id)


def illegal_ref(name: str) -> bool:
    """Whether git would refuse name as a branch name."""
    return name == "@" or bool(ILLEGAL_REF.search(name))


def branch_namespace(template: str) -> str:
    """The literal text in front of the template's first placeholder."""
    return next(iter(Formatter().parse(template)), ("", None))[0]


def _template(top: Reader, name: str, default: str, allowed: tuple[str, ...],
              required: str) -> str:
    """A template field: known placeholders only, the required one present."""
    template = top.text(name, required=False) or default
    try:
        parts = [(field, spec, conv) for _, field, spec, conv in Formatter().parse(template)
                 if field is not None]
    except ValueError as error:
        top.fail(name, f"is not a template: {error}")
    used = {field for field, _, _ in parts}
    bad = [field for field, spec, conv in parts if field not in allowed or spec or conv]
    if bad:
        names = ", ".join("{" + f + "}" for f in allowed)
        top.fail(name, f"may use only {names}, found {{{bad[0]}}}")
    if required not in used:
        top.fail(name, f"must carry {{{required}}}")
    return template


def read_branch_template(top: Reader) -> str:
    template = _template(top, "branch_template", DEFAULT_BRANCH_TEMPLATE, BRANCH_FIELDS, "key")
    # A slug holds only lower case letters, digits, and hyphens, so the one
    # slug that can make a legal sample illegal is one that ends in "lock"
    # after a literal dot; rendering that one too covers every card.
    for values in (SAMPLE, {**SAMPLE, "slug": "lock"}):
        sample = template.format(**values)
        if illegal_ref(sample):
            top.fail("branch_template",
                     f"renders {sample!r}, which is not a legal branch name")
    return template


def project_path(slug: str, root: Path | None = None) -> Path:
    if not SLUG.fullmatch(slug):
        raise ConfigError(f"{slug}.toml", "(file)", f"{slug!r} is not a valid slug")
    return (root or home()) / PROJECTS / f"{slug}.toml"


def load_project(slug: str, root: Path | None = None) -> Project:
    """Read and validate one project file. The file is read on every call."""
    path = project_path(slug, root)
    return parse_project(path, read_toml(path))


def name_fields(data: dict):
    """Yield (dotted key, value) for each field that names an operator file entry.

    This runs before the shape pass, so a value of the wrong type is skipped
    here and reported there.
    """
    for key in NAME_FIELDS:
        if isinstance(data.get(key), str):
            yield key, data[key]
    roles = data.get("roles")
    if isinstance(roles, dict):
        for role, table in roles.items():
            if isinstance(table, dict) and isinstance(table.get("account"), str):
                yield f"roles.{role}.account", table["account"]


def refuse_env_names(path: Path, data: dict) -> None:
    """Refuse an environment variable name where an operator file entry belongs."""
    for key, value in name_fields(data):
        if LOOKS_LIKE_ENV_NAME.fullmatch(value.strip()):
            raise ConfigError(
                path, key,
                f"{value!r} looks like an environment variable name; a project file names "
                "credentials by their operator file entry",
            )


def read_rules(path: Path, top: Reader) -> str:
    """The [rules] table's text, inline or from its file; empty when absent."""
    table = top.table("rules", required=False)
    if table is None:
        return ""
    table.refuse_unknown(RULES_KEYS)
    given = [key for key in RULES_KEYS if key in table.data]
    if len(given) != 1:
        top.fail("rules", "must hold exactly one of text or file")
    if given == ["text"]:
        return table.text("text")
    name = table.text("file")
    if Path(name).is_absolute():
        table.fail("file", "must be relative to the project file's directory")
    folder = path.parent.resolve()
    target = (folder / name).resolve()
    if not target.is_relative_to(folder):
        table.fail("file", "must stay inside the project file's directory")
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        table.fail("file", f"{name!r} not found")
    except (IsADirectoryError, UnicodeDecodeError) as error:
        table.fail("file", f"{name!r} is not a readable text file: {error}")
    if not text.strip():
        table.fail("file", f"{name!r} is empty")
    refuse_secrets(path, {"rules": {"file": text}})
    return text


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
        rules=read_rules(path, top),
        branch_template=read_branch_template(top),
        pull_request_title=_template(top, "pull_request_title", DEFAULT_PULL_REQUEST_TITLE,
                                     TITLE_FIELDS, "title"),
        checkout=top.choice("checkout", Checkout, required=False) or Checkout.CLONE,
    )


def check_names(project: Project, operator: Operator) -> None:
    """Refuse any name in the project file that the operator file does not define."""

    def fail(key: str, value: str, where: str):
        raise ConfigError(project.path, key, f"{value!r} is not in the operator file's {where}")

    if project.tracker_credential not in operator.tracker_credentials:
        fail("tracker_credential", project.tracker_credential, "tracker_credentials")
    elif (site := operator.tracker_credentials[project.tracker_credential].site) != (
        project.tracker_site
    ):
        # The same check resolve_tracker_credential (bindings.py) makes at
        # dispatch, so a file that passes here cannot halt there for it.
        raise ConfigError(
            project.path, "tracker_credential",
            f"{project.tracker_credential!r} is for site {site!r}, the project's site is "
            f"{project.tracker_site!r}",
        )
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
