"""The operator file, $SLIPWAY_HOME/operator.toml (R3).

One file per operator. It names everything the operator holds: their own
OAuth subject (the stable user id the identity provider puts in a token),
the accounts they pay for, the identity provider, the Langfuse projects,
tracker and host credentials, the gate runner, the builder workers, and the
webhook secrets. It never holds a secret value. Every credential is named
here by the environment variable that holds it, and a project file refers
to these entries by name only.

The file is read on the worker (KTD28); it is absent from the app.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from slipway.config import Reader, home, read_toml, refuse_secrets

FILENAME = "operator.toml"


class AccountKind(StrEnum):
    SUBSCRIPTION = "subscription"  # consumed by a headless CLI session (R14)
    API_KEY = "api_key"  # consumed by the provider's session API


class Host(StrEnum):
    GITHUB = "github"
    BITBUCKET = "bitbucket"


@dataclass(frozen=True)
class Account:
    name: str
    kind: AccountKind
    env: str


@dataclass(frozen=True)
class IdentityProvider:
    issuer: str
    client_id: str
    client_secret_env: str


@dataclass(frozen=True)
class LangfuseProject:
    name: str
    public_key_env: str
    secret_key_env: str


@dataclass(frozen=True)
class TrackerCredential:
    name: str
    site: str
    token_env: str
    email_env: str | None  # Jira's Basic auth needs an email; GitHub does not


@dataclass(frozen=True)
class HostCredential:
    name: str
    host: Host
    kind: str  # for example a GitHub App or a repository access token
    env: str


@dataclass(frozen=True)
class RunnerRepository:
    owner: str
    name: str
    default_branch: str
    version_file: str


@dataclass(frozen=True)
class RunnerRegistration:
    """One repository a runner instance serves, with its token's rotation pair."""

    repository: str
    token_env: str
    previous_token_env: str | None


@dataclass(frozen=True)
class RunnerInstance:
    name: str
    prefix: str
    labels: tuple[str, ...]
    host: str
    repositories: tuple[RunnerRegistration, ...]


@dataclass(frozen=True)
class BuilderWorker:
    name: str
    label: str
    machine: str


@dataclass(frozen=True)
class WebhookSecret:
    """Two secrets are accepted during a rotation window (KTD10)."""

    repository: str
    env: str
    previous_env: str | None


@dataclass(frozen=True)
class Operator:
    path: Path
    operator_subject: str
    identity_provider: IdentityProvider
    accounts: dict[str, Account] = field(default_factory=dict)
    langfuse_projects: dict[str, LangfuseProject] = field(default_factory=dict)
    tracker_credentials: dict[str, TrackerCredential] = field(default_factory=dict)
    host_credentials: dict[str, HostCredential] = field(default_factory=dict)
    runner_repository: RunnerRepository | None = None
    runner_instances: dict[str, RunnerInstance] = field(default_factory=dict)
    runner_read_api: str | None = None
    builder_workers: dict[str, BuilderWorker] = field(default_factory=dict)
    webhook_secrets: dict[str, WebhookSecret] = field(default_factory=dict)


KNOWN = {
    "operator_subject", "identity_provider", "accounts", "langfuse_projects",
    "tracker_credentials", "host_credentials", "runner_repository", "runner_instances",
    "runner_read_api", "builder_workers", "webhook_secrets",
}
# The keys each nested table may hold. A key outside its set is refused by
# its dotted name, so a typo such as tokn_env is named rather than ignored.
IDENTITY_PROVIDER_KEYS = {"issuer", "client_id", "client_secret_env"}
ACCOUNT_KEYS = {"kind", "env"}
LANGFUSE_KEYS = {"public_key_env", "secret_key_env"}
TRACKER_KEYS = {"site", "token_env", "email_env"}
HOST_KEYS = {"host", "kind", "env"}
RUNNER_REPOSITORY_KEYS = {"owner", "name", "default_branch", "version_file"}
RUNNER_INSTANCE_KEYS = {"prefix", "labels", "host", "repositories"}
REGISTRATION_KEYS = {"repository", "token_env", "previous_token_env"}
WORKER_KEYS = {"label", "machine"}
WEBHOOK_KEYS = {"repository", "env", "previous_env"}


def checked(tables, known):
    """Refuse an unknown key in each of tables, a dict or list of Readers; return tables."""
    for table in tables.values() if isinstance(tables, dict) else tables:
        table.refuse_unknown(known)
    return tables


def operator_path(root: Path | None = None) -> Path:
    return (root or home()) / FILENAME


def load_operator(root: Path | None = None) -> Operator:
    """Read and validate the operator file under root (SLIPWAY_HOME by default)."""
    path = operator_path(root)
    return parse_operator(path, read_toml(path))


def parse_operator(path: Path, data: dict) -> Operator:
    refuse_secrets(path, data)
    top = Reader(path, data)
    top.refuse_unknown(KNOWN)

    idp = top.table("identity_provider")
    idp.refuse_unknown(IDENTITY_PROVIDER_KEYS)
    identity_provider = IdentityProvider(
        issuer=idp.text("issuer"),
        client_id=idp.text("client_id"),
        client_secret_env=idp.env("client_secret_env"),
    )

    accounts = {
        name: Account(name, t.choice("kind", AccountKind), t.env("env"))
        for name, t in checked(top.tables("accounts"), ACCOUNT_KEYS).items()
    }
    langfuse = {
        name: LangfuseProject(name, t.env("public_key_env"), t.env("secret_key_env"))
        for name, t in checked(top.tables("langfuse_projects"), LANGFUSE_KEYS).items()
    }
    trackers = {
        name: TrackerCredential(
            name, t.text("site"), t.env("token_env"), t.env("email_env", required=False)
        )
        for name, t in checked(top.tables("tracker_credentials"), TRACKER_KEYS).items()
    }
    hosts = {
        name: HostCredential(name, t.choice("host", Host), t.text("kind"), t.env("env"))
        for name, t in checked(top.tables("host_credentials"), HOST_KEYS).items()
    }

    runner_repository = None
    rr = top.table("runner_repository", required=False)
    if rr is not None:
        rr.refuse_unknown(RUNNER_REPOSITORY_KEYS)
        runner_repository = RunnerRepository(
            rr.text("owner"), rr.text("name"), rr.text("default_branch"), rr.text("version_file")
        )

    instances = {}
    for name, t in checked(top.tables("runner_instances"), RUNNER_INSTANCE_KEYS).items():
        registrations = tuple(
            RunnerRegistration(
                r.text("repository"), r.env("token_env"), r.env("previous_token_env", False)
            )
            for r in checked(t.array_of_tables("repositories"), REGISTRATION_KEYS)
        )
        instances[name] = RunnerInstance(
            name, t.text("prefix"), t.strs("labels", nonempty=True), t.text("host"), registrations
        )

    workers = {
        name: BuilderWorker(name, t.text("label"), t.text("machine"))
        for name, t in checked(top.tables("builder_workers"), WORKER_KEYS).items()
    }

    secrets = {}
    for t in checked(top.array_of_tables("webhook_secrets"), WEBHOOK_KEYS):
        entry = WebhookSecret(t.text("repository"), t.env("env"), t.env("previous_env", False))
        if entry.repository in secrets:
            t.fail("repository", f"{entry.repository!r} is listed twice")
        secrets[entry.repository] = entry

    return Operator(
        path=path,
        operator_subject=top.text("operator_subject"),
        identity_provider=identity_provider,
        accounts=accounts,
        langfuse_projects=langfuse,
        tracker_credentials=trackers,
        host_credentials=hosts,
        runner_repository=runner_repository,
        runner_instances=instances,
        runner_read_api=top.text("runner_read_api", required=False),
        builder_workers=workers,
        webhook_secrets=secrets,
    )
