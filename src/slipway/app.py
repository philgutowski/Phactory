"""The composition root: the one module that builds the running app from its parts (U29).

The app is one process behind Caddy on port 443 (KTD28). It serves, on one
listener:

    GET  /, /projects/<slug>, /login, /callback   the status page (U20, page/)
    POST /webhooks/github, /webhooks/bitbucket    the webhook endpoint (U9)

U18 adds the MCP routes here later.

Every part comes from somewhere else; this module only connects them:

- The operator file, loaded once at start with load_operator(), the same
  loader the worker uses (deploy/README.md, decided by #39). It gives the
  page its identity provider and the endpoint its webhook secrets.
- The page, page.app.Page, signed in through login.OidcIdentity with the
  value of the operator file's identity_provider.client_secret_env.
- The endpoint, Webhooks below. Each delivery is checked with
  hosts.webhooks.verify, whose secrets_for is built from webhook_secrets;
  recorded twice, in U9's in process DeliveryLog and in U7's host_events
  table; and handed to service.engine.push_host_event for the build it
  names. The app reaches the workflow only through service (KTD23): nothing
  here imports workflow/.

Two variables are Slipway's own and must be set, or the app refuses to
start (StartRefused):

    SLIPWAY_SESSION_KEY   signs the page's cookies; at least 32 bytes.
                          Changing it signs every operator out.
    SLIPWAY_BASE_URL      the https address the page is served at. The
                          login callback is built from it, never from the
                          request's Host header.

Run it with `python -m slipway.app`. Caddy terminates TLS (Transport Layer
Security, the encryption https adds) and forwards plain HTTP to PORT on the
compose network, so the app itself never holds a certificate.
"""

import os
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Protocol
from wsgiref.simple_server import WSGIServer, make_server

from slipway.config import home
from slipway.config.operator import Host, Operator, load_operator
from slipway.config.project import load_project
from slipway.hosts import webhooks
from slipway.page.app import Page
from slipway.page.login import Identity, OidcIdentity
from slipway.records import store
from slipway.service import engine as engine_module
from slipway.service import verbs

SESSION_KEY = "SLIPWAY_SESSION_KEY"
BASE_URL = "SLIPWAY_BASE_URL"
DATABASE_URL = "SLIPWAY_DATABASE_URL"
KEY_BYTES = 32  # the shortest session key the page accepts (login.Signer)
PORT = 8000  # the app's port on the compose network; Caddy forwards 443 here
WEBHOOKS = "/webhooks/"


class StartRefused(Exception):
    """The app cannot start as configured; the message names what to set."""


@dataclass(frozen=True)
class Settings:
    """The two values Slipway fixes for the app, checked."""

    session_key: bytes
    base_url: str


def settings(environ: Mapping[str, str]) -> Settings:
    """Read and check SLIPWAY_SESSION_KEY and SLIPWAY_BASE_URL, or raise StartRefused."""
    key = environ.get(SESSION_KEY, "")
    if not key:
        raise StartRefused(f"{SESSION_KEY} is unset; set it to at least {KEY_BYTES} random bytes")
    if len(key.encode()) < KEY_BYTES:
        raise StartRefused(f"{SESSION_KEY} is shorter than {KEY_BYTES} bytes")
    base = environ.get(BASE_URL, "").strip().rstrip("/")
    if not base:
        raise StartRefused(f"{BASE_URL} is unset; set it to the page's https address")
    if not base.startswith("https://") or len(base) == len("https://"):
        raise StartRefused(f"{BASE_URL} must be an https address, not {base}")
    return Settings(key.encode(), base)


def secrets_for(operator: Operator, environ: Mapping[str, str]) -> webhooks.SecretsFor:
    """U9's secrets_for from the operator file's webhook_secrets (KTD10).

    The entry for the payload's repository, each named variable read from
    the app's environment, current first. No entry means a repository
    Slipway does not manage, and verify refuses it before any compare.
    """
    entries = operator.webhook_secrets

    def resolve(repository: str) -> tuple[str | None, ...] | None:
        entry = entries.get(repository)
        if entry is None:
            return None
        names = (entry.env, entry.previous_env)
        return tuple(environ.get(name) if name else None for name in names)

    return resolve


# The webhook endpoint.

class WebhookRecords(Protocol):
    """What the endpoint writes and reads, over one connection per delivery."""

    def ingest(self, slug: str, normalised: dict) -> bool:
        """Store the event in host_events; False when it was already there."""

    def build_for(self, slug: str, normalised: dict) -> int | None:
        """The build the event is about, or None when no build of the project matches."""

    def close(self) -> None: ...


# The columns an event names a build by, in the order they are tried: the
# pull request first, then the commit a check or a merge group reports.
BUILD_BY_PR = (
    "SELECT b.id FROM builds b JOIN projects p ON p.id = b.project_id "
    "WHERE p.slug = %s AND b.pr_number = %s ORDER BY b.id DESC LIMIT 1")
BUILD_BY_COMMIT = (
    "SELECT b.id FROM builds b JOIN projects p ON p.id = b.project_id "
    "WHERE p.slug = %s AND (b.confirmed_commit = %s OR b.tested_commit = %s) "
    "ORDER BY b.id DESC LIMIT 1")


class PostgresWebhookRecords:
    """WebhookRecords over one autocommit psycopg connection."""

    def __init__(self, conn):
        self.conn = conn

    def ingest(self, slug: str, normalised: dict) -> bool:
        project_id = store.ensure_project(self.conn, slug)
        return store.ingest_host_event(self.conn, "webhook",
                                       {**normalised, "project_id": project_id}).inserted

    def build_for(self, slug: str, normalised: dict) -> int | None:
        number = normalised.get("pr_number")
        if number is not None:
            row = self.conn.execute(BUILD_BY_PR, (slug, number)).fetchone()
            return row[0] if row else None
        for commit in (normalised.get("tested_head"), normalised.get("head_commit"),
                       normalised.get("tested_commit")):
            if commit:
                row = self.conn.execute(BUILD_BY_COMMIT, (slug, commit, commit)).fetchone()
                if row:
                    return row[0]
        return None

    def close(self) -> None:
        self.conn.close()


@dataclass(frozen=True)
class Answer:
    """The endpoint's reply: an HTTP status and one line saying what happened."""

    status: str
    outcome: str
    detail: str = ""

    def body(self) -> bytes:
        return (f"{self.outcome}: {self.detail}\n" if self.detail else f"{self.outcome}\n").encode()


# How each refusal reads to the sender. An unknown repository is dropped,
# not refused: a host pointed at Slipway by mistake gets a 202 and nothing
# more, and learns nothing about which repositories are managed.
REFUSED_STATUS = {
    webhooks.OVER_CAP: "413 Content Too Large",
    webhooks.NO_SIGNATURE: "401 Unauthorized",
    webhooks.BAD_SIGNATURE: "401 Unauthorized",
}


class Webhooks:
    """The webhook endpoint (KTD10): verify, dedupe, record, then wake the build.

    project_for(host, repository) names the project whose file manages the
    repository, or None. records() opens WebhookRecords for one delivery;
    it is called only once a delivery's signature holds.
    """

    def __init__(self, secrets: webhooks.SecretsFor,
                 project_for: Callable[[Host, str], str | None],
                 records: Callable[[], WebhookRecords], engine: engine_module.Engine,
                 deliveries: webhooks.DeliveryLog | None = None):
        self.secrets = secrets
        self.project_for = project_for
        self.records = records
        self.engine = engine
        self.deliveries = deliveries if deliveries is not None else webhooks.DeliveryLog()

    def handle(self, host_name: str, headers: Mapping[str, str], body: bytes) -> Answer:
        try:
            host = Host(host_name)
        except ValueError:
            return Answer("404 Not Found", "refused", f"no webhook endpoint for {host_name}")
        opened: list[WebhookRecords] = []

        def records() -> WebhookRecords:
            if not opened:
                opened.append(self.records())
            return opened[0]

        try:
            return self._handle(host, headers, body, records)
        except Exception as error:
            # The records could not be read or written. The sender may retry;
            # the in process log already holds the id, so a retry is answered
            # as a duplicate, and the run's hourly poll stands in (KTD10).
            return Answer("503 Service Unavailable", "unavailable", type(error).__name__)
        finally:
            for found in opened:
                found.close()

    def _handle(self, host: Host, headers, body: bytes, records) -> Answer:
        slug: list[str | None] = []

        def opened_by_slipway(repository: str, number: int) -> bool:
            # Only reached once the signature holds (webhooks.verify).
            slug.append(self.project_for(host, repository))
            if not slug[-1]:
                return False
            return records().build_for(slug[-1], {"pr_number": number}) is not None

        try:
            result = webhooks.verify(host, headers, body, self.secrets, opened_by_slipway)
        except webhooks.WebhookRefused as refused:
            if refused.reason == webhooks.UNKNOWN_REPOSITORY:
                return Answer("202 Accepted", "dropped", "the repository is not managed here")
            return Answer(REFUSED_STATUS.get(refused.reason, "400 Bad Request"), "refused",
                          refused.reason)
        if isinstance(result, webhooks.Dropped):
            return Answer("202 Accepted", "dropped", result.reason)
        if self.deliveries.seen(result.host, result.delivery_id):
            return Answer("200 OK", "duplicate", result.delivery_id)
        project = slug[-1] if slug else self.project_for(host, result.repository)
        if not project:
            return Answer("202 Accepted", "dropped",
                          f"no project file manages {result.repository}")
        normalised = result.normalised()
        if not records().ingest(project, normalised):
            return Answer("200 OK", "duplicate", result.delivery_id)
        build_id = records().build_for(project, normalised)
        if build_id is None:
            return Answer("202 Accepted", "recorded", "no build of this project matches it")
        # A wake up, never evidence: the workflow re reads the pull request
        # itself, so a push that fails here costs at most the run's hourly poll.
        try:
            engine_module.push_host_event(self.engine, build_id, normalised)
        except Exception as error:
            return Answer("202 Accepted", "recorded",
                          f"the engine did not take it ({type(error).__name__}); "
                          "the run's hourly poll will see it")
        return Answer("202 Accepted", "accepted", f"build {build_id}")


def project_finder(root: Path) -> Callable[[Host, str], str | None]:
    """project_for over the project files under root, read on every call."""

    def find(host: Host, repository: str) -> str | None:
        for slug in verbs.configured_slugs(root):
            try:
                project = load_project(slug, root)
            except Exception:  # an unreadable file manages nothing; the page names it
                continue
            if project.host == host and project.repository == repository:
                return slug
        return None

    return find


# The WSGI application: the page and the endpoint on one listener.

class Connections:
    """Opens database connections for a request and closes them when it ends.

    The page asks for its ports once or twice per request and never closes
    them, so the app keeps each request's connections on its own thread and
    closes them after the response is built.
    """

    def __init__(self, connect: Callable[[], Any]):
        self.connect = connect
        self.local = threading.local()

    def open(self):
        conn = self.connect()
        self.local.__dict__.setdefault("open", []).append(conn)
        return conn

    def close_all(self) -> None:
        for conn in self.local.__dict__.pop("open", []):
            try:
                conn.close()
            except Exception:
                pass


TEXT = [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff")]


class App:
    """The WSGI application: POST /webhooks/<host> to the endpoint, everything else to the page."""

    def __init__(self, page: Page, hooks: Webhooks, connections: Connections | None = None):
        self.page = page
        self.hooks = hooks
        self.connections = connections

    def __call__(self, environ: dict, start_response):
        try:
            path = environ.get("PATH_INFO") or "/"
            if path.startswith(WEBHOOKS):
                return self.webhook(environ, start_response, path.removeprefix(WEBHOOKS))
            return self.page(environ, start_response)
        finally:
            if self.connections:
                self.connections.close_all()

    def webhook(self, environ: dict, start_response, host_name: str):
        if environ.get("REQUEST_METHOD") != "POST":
            start_response("405 Method Not Allowed", [*TEXT, ("Allow", "POST")])
            return [b"refused: a webhook is a POST\n"]
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > webhooks.BODY_CAP_BYTES:
            # Refused before the body is read (KTD10's cap).
            answer = Answer(REFUSED_STATUS[webhooks.OVER_CAP] if length > 0 else
                            "400 Bad Request", "refused", webhooks.OVER_CAP if length > 0
                            else "no readable Content-Length")
        else:
            body = environ["wsgi.input"].read(length) if length else b""
            answer = self.hooks.handle(host_name, request_headers(environ), body)
        start_response(answer.status, TEXT)
        return [answer.body()]


def request_headers(environ: Mapping[str, Any]) -> dict[str, str]:
    """The request's headers by their HTTP names, from the WSGI environ."""
    found = {name[5:].replace("_", "-"): str(value) for name, value in environ.items()
             if name.startswith("HTTP_")}
    if environ.get("CONTENT_TYPE"):
        found["CONTENT-TYPE"] = str(environ["CONTENT_TYPE"])
    return found


def connect_database(environ: Mapping[str, str]) -> Callable[[], Any]:
    dsn = environ.get(DATABASE_URL, "")
    if not dsn:
        raise StartRefused(f"{DATABASE_URL} is unset")

    def connect():
        import psycopg

        return psycopg.connect(dsn, autocommit=True)

    return connect


def oidc(provider, client_secret: str) -> Identity:
    return OidcIdentity(provider, client_secret)


def build(environ: Mapping[str, str], root: Path | None = None,
          connect: Callable[[], Any] | None = None,
          engine: engine_module.Engine | None = None,
          identity: Callable[[Any, str], Identity] = oidc,
          webhook_records: Callable[[Any], WebhookRecords] = PostgresWebhookRecords) -> App:
    """The running app, from the operator file under root and the environment.

    connect opens one database connection, engine is the Hatchet facade,
    identity(provider, client_secret) builds the page's identity provider,
    and webhook_records(conn) wraps a connection for the endpoint; each
    defaults to the real one. Raises StartRefused when the
    configuration cannot serve.
    """
    found = settings(environ)
    root = root or home(environ)
    operator = load_operator(root)
    provider = operator.identity_provider
    client_secret = environ.get(provider.client_secret_env, "")
    if not client_secret:
        raise StartRefused(f"identity_provider.client_secret_env: {provider.client_secret_env} "
                           "is unset")
    connections = Connections(connect or connect_database(environ))
    engine = engine if engine is not None else engine_module.HatchetEngine()
    page = Page(lambda: verbs.real_ports(connections.open(), engine, root),
                identity(provider, client_secret), found.session_key, found.base_url)
    hooks = Webhooks(secrets_for(operator, environ), project_finder(root),
                     lambda: webhook_records(connections.open()), engine)
    return App(page, hooks, connections)


class ThreadingServer(ThreadingMixIn, WSGIServer):
    """The standard library's WSGI server, one thread per request."""

    daemon_threads = True


def main(environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    try:
        app = build(environ)
    except StartRefused as refused:
        print(f"slipway app: {refused}", file=sys.stderr)
        return 2
    with make_server("0.0.0.0", PORT, app, server_class=ThreadingServer) as server:
        print(f"slipway app: serving on port {PORT}", file=sys.stderr)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["App", "Answer", "Connections", "PostgresWebhookRecords", "Settings", "StartRefused",
           "Webhooks", "build", "main", "project_finder", "secrets_for", "settings"]
