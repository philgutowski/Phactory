"""U29 test scenarios: the composition root, the worker's entry point, and compose.

The app is built by app.build from the fixture operator and project files
under a temporary SLIPWAY_HOME, with a fake identity provider, a fake
engine, and in memory webhook records, so nothing reaches a network. The
last test runs the endpoint's SQL against the local Postgres.
"""

import io
import json
import os
import re
import shutil
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from test_page import FakeIdentity

from _service_fakes import CONFIG, ROOT, SLUG, FakeEngine
from slipway import app as app_module
from slipway.builders.cli import ContainerBuilder
from slipway.config.bindings import resolve_binding
from slipway.config.operator import load_operator
from slipway.config.project import load_project
from slipway.hosts import webhooks
from slipway.records import migrate, store
from slipway.workflow import worker
from test_imports import SRC, edges_of

BASE = "https://slipway.example.test"
KEY = "k" * 32
REPOSITORY = "example-org/example-app"
SECRET = "webhook-secret-example"
HOST_FIXTURES = ROOT / "tests" / "fixtures" / "host"
COMPOSE = ROOT / "deploy" / "compose.yaml"


def home(tmp_path: Path, builder_worker: bool = True) -> Path:
    root = tmp_path / "home"
    (root / "projects").mkdir(parents=True)
    shutil.copy(CONFIG / "operator.toml", root / "operator.toml")
    text = (CONFIG / "project.toml").read_text()
    if not builder_worker:
        text = text.replace('builder_worker = "laptop"\n', "")
    (root / "projects" / f"{SLUG}.toml").write_text(text)
    return root


def environ(**changes) -> dict:
    found = {"SLIPWAY_SESSION_KEY": KEY, "SLIPWAY_BASE_URL": BASE,
             "SLIPWAY_IDP_CLIENT_SECRET": "client-secret-example",
             "EXAMPLE_APP_WEBHOOK_SECRET": SECRET}
    found.update(changes)
    return {k: v for k, v in found.items() if v is not None}


class FakeConn:
    closed = False

    def close(self):
        self.closed = True


class FakeHookRecords:
    """WebhookRecords in memory: host_events rows, and builds by pull request."""

    def __init__(self, builds: dict[int, int]):
        self.builds = builds  # pull request number to build id
        self.rows: list[tuple[str, dict]] = []
        self.closed = 0

    def ingest(self, slug, normalised):
        if any(row["delivery_id"] == normalised["delivery_id"] for _, row in self.rows):
            return False
        self.rows.append((slug, normalised))
        return True

    def build_for(self, slug, normalised):
        return self.builds.get(normalised.get("pr_number"))

    def close(self):
        self.closed += 1


class Built:
    def __init__(self, tmp_path, builds=None, env=None):
        self.identity = FakeIdentity()
        self.made_with: dict = {}
        self.engine = FakeEngine()
        self.records = FakeHookRecords(builds or {})
        self.conns: list[FakeConn] = []

        def identity(provider, secret):
            self.made_with = {"issuer": provider.issuer, "secret": secret}
            return self.identity

        def connect():
            self.conns.append(FakeConn())
            return self.conns[-1]

        self.app = app_module.build(env or environ(), home(tmp_path), connect=connect,
                                    engine=self.engine, identity=identity,
                                    webhook_records=lambda conn: self.records)


def call(app, path, method="GET", body=b"", headers=None):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    seen = {}

    def start_response(status, headers):
        seen["status"], seen["headers"] = status, dict(headers)

    out = b"".join(app(environ, start_response))
    return int(seen["status"].split()[0]), seen["headers"], out.decode()


def delivery(repository=REPOSITORY, secret=SECRET, delivery_id="delivery-1"):
    payload = json.loads((HOST_FIXTURES / "github_pull_request_synchronize.json").read_text())
    payload["repository"]["full_name"] = repository
    body = json.dumps(payload).encode()
    return body, {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": delivery_id,
                  "X-Hub-Signature-256": webhooks.signature_header(body, secret),
                  "Content-Type": "application/json"}


# The card's test scenarios.

def test_the_app_serves_the_login_redirect_with_the_callback_at_the_base_url(tmp_path):
    built = Built(tmp_path)

    status, headers, _ = call(built.app, "/login")

    assert status == 302
    assert headers["Location"].startswith("https://auth.example.test/authorize")
    assert built.identity.started["redirect_uri"] == BASE + "/callback"
    # The page's provider comes from the operator file and its secret's variable.
    assert built.made_with == {"issuer": "https://auth.example.test",
                               "secret": "client-secret-example"}


def test_a_page_request_without_a_session_goes_to_login_and_closes_nothing_it_did_not_open(
        tmp_path):
    built = Built(tmp_path)

    status, headers, _ = call(built.app, "/")

    assert status == 302 and headers["Location"].startswith("/login")


@pytest.mark.parametrize("changes, said", [
    ({"SLIPWAY_SESSION_KEY": None}, "SLIPWAY_SESSION_KEY is unset"),
    ({"SLIPWAY_SESSION_KEY": "k" * 31}, "shorter than 32 bytes"),
    ({"SLIPWAY_BASE_URL": "http://slipway.example.test"}, "must be an https address"),
    ({"SLIPWAY_BASE_URL": None}, "SLIPWAY_BASE_URL is unset"),
    ({"SLIPWAY_IDP_CLIENT_SECRET": None}, "SLIPWAY_IDP_CLIENT_SECRET is unset"),
], ids=["key unset", "key short", "http base", "base unset", "client secret unset"])
def test_the_app_refuses_to_start(tmp_path, changes, said):
    with pytest.raises(app_module.StartRefused, match=said):
        Built(tmp_path, env=environ(**changes))


def test_main_reports_a_refusal_and_exits_without_serving(tmp_path, capsys):
    env = environ(SLIPWAY_SESSION_KEY=None, SLIPWAY_HOME=str(home(tmp_path)))

    assert app_module.main(env) == 2
    assert "SLIPWAY_SESSION_KEY is unset" in capsys.readouterr().err


def test_a_signed_delivery_for_a_managed_repository_is_accepted_and_wakes_its_build(tmp_path):
    built = Built(tmp_path, builds={104: 7})
    body, headers = delivery()

    status, _, text = call(built.app, "/webhooks/github", "POST", body, headers)

    assert status == 202 and text.startswith("accepted"), text
    [(slug, row)] = built.records.rows
    assert slug == SLUG and row["kind"] == webhooks.PR_SYNCHRONIZE
    [(kind, build_id, payload)] = built.engine.pushes
    assert (kind, build_id) == (webhooks.PR_SYNCHRONIZE, 7)
    assert payload["delivery_id"] == "delivery-1"
    assert all(conn.closed for conn in built.conns) and built.conns


def test_a_delivery_for_an_unmanaged_repository_is_dropped(tmp_path):
    built = Built(tmp_path, builds={104: 7})
    body, headers = delivery(repository="someone-else/their-app")

    status, _, text = call(built.app, "/webhooks/github", "POST", body, headers)

    assert status == 202 and text.startswith("dropped"), text
    assert built.records.rows == [] and built.engine.pushes == []
    assert built.conns == []  # no database work before a signature holds


def test_a_badly_signed_delivery_is_refused(tmp_path):
    built = Built(tmp_path, builds={104: 7})
    body, headers = delivery(secret="not-the-secret")

    status, _, text = call(built.app, "/webhooks/github", "POST", body, headers)

    assert status == 401 and "bad_signature" in text
    assert built.records.rows == [] and built.engine.pushes == []


def test_a_retried_delivery_is_recorded_and_pushed_once(tmp_path):
    built = Built(tmp_path, builds={104: 7})
    body, headers = delivery()

    first = call(built.app, "/webhooks/github", "POST", body, headers)
    again = call(built.app, "/webhooks/github", "POST", body, headers)

    assert first[0] == 202 and again[0] == 200 and again[2].startswith("duplicate")
    assert len(built.records.rows) == 1 and len(built.engine.pushes) == 1


def test_a_pull_request_slipway_did_not_open_is_dropped(tmp_path):
    built = Built(tmp_path, builds={})
    body, headers = delivery()

    status, _, text = call(built.app, "/webhooks/github", "POST", body, headers)

    assert status == 202 and "not opened by Slipway" in text
    assert built.engine.pushes == []


def test_the_endpoint_takes_only_a_post_and_refuses_a_body_over_the_cap(tmp_path):
    built = Built(tmp_path)

    assert call(built.app, "/webhooks/github")[0] == 405
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/webhooks/github",
               "CONTENT_LENGTH": str(webhooks.BODY_CAP_BYTES + 1), "wsgi.input": io.BytesIO()}
    seen = {}
    app_module.App.__call__(built.app, environ, lambda s, h: seen.update(status=s))
    assert seen["status"].startswith("413")
    assert call(built.app, "/webhooks/gitlab", "POST", b"{}")[0] == 404


def service(name: str) -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"][name]


def worker_variables() -> set[str]:
    """Every variable the fixture operator file gives the worker alone (deploy/README.md)."""
    op = load_operator(CONFIG)
    names = {a.env for a in op.accounts.values()}
    names |= {h.env for h in op.host_credentials.values()}
    for t in op.tracker_credentials.values():
        names |= {t.token_env, t.email_env} - {None}
    for lf in op.langfuse_projects.values():
        names |= {lf.public_key_env, lf.secret_key_env}
    return names


APP_VARIABLES = {"SLIPWAY_HOME", "SLIPWAY_DATABASE_URL", "HATCHET_CLIENT_TOKEN",
                 "HATCHET_CLIENT_HOST_PORT", "HATCHET_CLIENT_TLS_STRATEGY", "SLIPWAY_BASE_URL",
                 "SLIPWAY_SESSION_KEY"}


def test_compose_gives_the_app_none_of_the_workers_variables():
    app, work = service("app"), service("worker")

    assert set(app["environment"]) == APP_VARIABLES
    assert not set(app["environment"]) & worker_variables()
    # Its secrets file is its own: not the worker's, not the runner's.
    assert app["env_file"] == ["${SLIPWAY_APP_ENV_FILE:?the root owned, mode 600 environment "
                               "file of the app}"]
    text = json.dumps(app)
    for other in ("SLIPWAY_WORKER_ENV_FILE", "RUNNER_ENV_FILE", "SLIPWAY_BUILDER_",
                  "docker.sock"):
        assert other not in text, other
    assert "SLIPWAY_APP_ENV_FILE" not in json.dumps(work)
    # The mounts are the README's: projects/ writable, operator.toml alone read only.
    assert [re.sub(r"\$\{[^}]*\}", "HOME", v).split(":", 1)[1] for v in app["volumes"]] == [
        "/srv/slipway/home/projects", "/srv/slipway/home/operator.toml:ro"]


def test_compose_publishes_only_443_and_the_grpc_port():
    published = []
    for name, found in yaml.safe_load(COMPOSE.read_text())["services"].items():
        for port in found.get("ports", []):
            if not str(port).startswith("127.0.0.1:"):  # loopback is not the internet
                published.append((name, str(port)))

    assert published == [("caddy", "443:443"), ("caddy", "7077:7077")]
    assert "SERVER_GRPC_INSECURE" in service("hatchet-lite")["environment"]
    assert "HATCHET_GRPC_INSECURE" not in COMPOSE.read_text()


def test_app_imports_no_workflow_module():
    path = SRC / "slipway" / "app.py"
    edges = edges_of(SRC, path)

    assert edges, "the walk should see app.py's imports"
    assert [str(e) for e in edges if e.imported.startswith("slipway.workflow")] == []


# The worker's entry point.

def test_the_worker_refuses_to_start_naming_each_unset_variable(capsys):
    assert worker.main([], {"SLIPWAY_DATABASE_URL": "postgresql://x"}) == 2
    err = capsys.readouterr().err
    for name in ("SLIPWAY_BUILDER_IMAGE", "SLIPWAY_BUILDER_NETWORK", "SLIPWAY_BUILDER_USER",
                 "SLIPWAY_SESSIONS"):
        assert name in err
    assert "SLIPWAY_DATABASE_URL" not in err


def test_the_worker_builds_a_container_per_card_or_hands_the_laptop_its_build(tmp_path,
                                                                             monkeypatch):
    env = {"SLIPWAY_BUILDER_IMAGE": "builder:test", "SLIPWAY_BUILDER_NETWORK": "builders",
           "SLIPWAY_BUILDER_USER": "1000:1000", "SLIPWAY_SESSIONS": str(tmp_path / "sessions")}
    make = worker.container_builders(env)

    laptop_home = home(tmp_path / "laptop")
    binding = resolve_binding(SLUG, "builder", laptop_home)
    assert isinstance(make(load_project(SLUG, laptop_home), binding), worker.LaptopScheduled)

    vps_home = home(tmp_path / "vps", builder_worker=False)
    found = make(load_project(SLUG, vps_home), binding)
    assert isinstance(found, ContainerBuilder)
    assert (found.image, found.network, found.user) == ("builder:test", "builders", "1000:1000")
    assert found.git.remote == f"https://github.com/{REPOSITORY}.git"
    assert found.git.username == "x-access-token"


def test_the_worker_mints_the_projects_host_credential_without_expiry(tmp_path, monkeypatch):
    root = home(tmp_path)
    monkeypatch.setenv("SLIPWAY_HOME", str(root))

    found = worker.host_credentials({"EXAMPLE_GITHUB_APP_KEY": "host-token"})(
        load_project(SLUG, root))

    assert found.current() == "host-token" and found.expires_at is None
    assert "host-token" not in repr(found)


def test_the_git_credential_helper_answers_for_a_managed_repository_only(tmp_path,
                                                                       monkeypatch):
    root = home(tmp_path)
    monkeypatch.setenv("SLIPWAY_HOME", str(root))
    env = {"EXAMPLE_GITHUB_APP_KEY": "host-token"}
    asked = "protocol=https\nhost=github.com\npath=example-org/example-app.git\n\n"
    out = io.StringIO()

    assert worker.credential_helper(["credential", "get"], io.StringIO(asked), out, env) == 0
    assert out.getvalue() == "username=x-access-token\npassword=host-token\n"
    assert worker.credential_answer({"protocol": "https", "host": "github.com",
                                     "path": "someone-else/their-app.git"}, env, root) == {}
    assert worker.credential_answer({"protocol": "http", "host": "github.com",
                                     "path": "example-org/example-app.git"}, env, root) == {}
    config = worker.git_helper_config("/usr/bin/python3")
    assert config["GIT_CONFIG_VALUE_0"] == "!/usr/bin/python3 -m slipway.workflow.worker credential"


# The endpoint's SQL over the real records.

@pytest.fixture
def conn():
    dsn = os.environ.get("SLIPWAY_TEST_DSN", "")
    if not dsn:
        pytest.skip("SLIPWAY_TEST_DSN is not set")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(dsn, autocommit=True, client_encoding="utf8")
    admin.execute(f"CREATE SCHEMA {schema}")
    c = psycopg.connect(dsn, autocommit=True, client_encoding="utf8",
                        options=f"-c search_path={schema}")
    try:
        migrate.apply(c)
        yield c
    finally:
        c.close()
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


@pytest.mark.integration
def test_postgres_webhook_records_ingest_once_and_find_the_build(conn):
    project_id = store.ensure_project(conn, SLUG)
    build_id = store.claim(conn, project_id, "EX-1", "test").build_id
    conn.execute("UPDATE builds SET pr_number = 104, confirmed_commit = %s WHERE id = %s",
                 ("c" * 40, build_id))
    records = app_module.PostgresWebhookRecords(conn)
    body, headers = delivery()
    event = webhooks.verify("github", headers, body, lambda repository: (SECRET,))
    normalised = event.normalised()

    assert records.ingest(SLUG, normalised) is True
    assert records.ingest(SLUG, normalised) is False
    assert records.build_for(SLUG, normalised) == build_id
    assert records.build_for(SLUG, {"pr_number": 999}) is None
    assert records.build_for(SLUG, {"pr_number": None, "head_commit": "c" * 40}) == build_id
    assert records.build_for("another", {"pr_number": 104}) is None


def test_an_engine_that_refuses_the_push_leaves_the_event_recorded(tmp_path):
    built = Built(tmp_path, builds={104: 7})

    def refuse(*args):
        raise ConnectionError("engine unreachable")

    built.engine.push = refuse
    body, headers = delivery()

    status, _, text = call(built.app, "/webhooks/github", "POST", body, headers)

    assert status == 202 and text.startswith("recorded") and "ConnectionError" in text
    assert len(built.records.rows) == 1


def test_a_delivery_whose_storing_fails_is_stored_on_redelivery(tmp_path):
    built = Built(tmp_path, builds={104: 7})
    ingest = built.records.ingest

    def down(slug, normalised):
        raise ConnectionError("database unreachable")

    built.records.ingest = down
    body, headers = delivery()

    first = call(built.app, "/webhooks/github", "POST", body, headers)
    built.records.ingest = ingest
    again = call(built.app, "/webhooks/github", "POST", body, headers)

    assert first[0] == 503 and "ConnectionError" in first[2]
    assert again[0] == 202 and again[2].startswith("accepted"), again[2]
    assert len(built.records.rows) == 1 and len(built.engine.pushes) == 1


def test_a_managed_repository_without_a_project_file_is_dropped_naming_it(tmp_path):
    built = Built(tmp_path, builds={104: 7})
    (tmp_path / "home" / "projects" / f"{SLUG}.toml").unlink()
    body, headers = delivery()

    status, _, text = call(built.app, "/webhooks/github", "POST", body, headers)

    assert status == 202 and f"no project file manages {REPOSITORY}" in text, text
    assert built.records.rows == [] and built.engine.pushes == []
