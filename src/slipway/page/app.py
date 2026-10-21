"""The status page (U20, R28): server rendered HTML over service.status.

The page answers one question for the operator, in the ideation note's
words: "4 being built, 2 waiting to land, 1 needs you, here is what to do".

    GET /                   every project (status.across), totals first
    GET /projects/<slug>    one project (status.compose), the same template
    GET /login              sends the browser to the identity provider
    GET /callback           where the provider sends it back (login.py)

Each project shows five groups: needs you (state, age, why, what to do, and
the link into the host), being built, waiting to land, landed today with
both scores, and open findings. The page is read only: it has no form, takes
no method but GET, and its content security policy forbids form posts, so
every approval is made on the host (R28). It holds no state of its own and
adds no status logic: every fact comes from service.status. It runs no
script; a meta refresh reloads it every minute.

Page is a WSGI application, the standard interface between a Python web
app and the server that runs it, so any WSGI server can serve it.
"""

import html
import secrets
import time
import urllib.parse
from collections.abc import Callable, Iterable
from functools import cache
from http.cookies import CookieError, SimpleCookie
from importlib.resources import files
from string import Template

from slipway.config import ConfigError
from slipway.page.login import Identity, LoginFailed, Signer, pkce_pair
from slipway.service import status
from slipway.service.verbs import Ports

SESSION = "slipway_session"
LOGIN = "slipway_login"
SESSION_AGE = 12 * 3600  # seconds a sign in lasts
LOGIN_AGE = 600  # seconds the browser has to come back from the provider
REFRESH = 60  # seconds between the page's own reloads
# Every response forbids scripts, frames, and form posts, so the page stays
# read only even if a value ever slipped past the escaping below.
HEADERS = [
    ("Content-Type", "text/html; charset=utf-8"),
    ("Cache-Control", "no-store"),
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
]
GROUPS = (("needs_you", "Needs you"), ("being_built", "Being built"),
          ("waiting_to_land", "Waiting to land"))


@cache
def template(name: str) -> Template:
    return Template((files("slipway.page") / "templates" / f"{name}.html").read_text())


def fill(_template: str, **values) -> str:
    """The named template with values already escaped by the caller."""
    return template(_template).substitute(values)


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def link(url: str | None, text: str = "open on the host") -> str:
    """An anchor to the host, or nothing when there is no http(s) link yet."""
    if not url or not url.startswith(("https://", "http://")):
        return ""
    return f'<a href="{esc(url)}" rel="noreferrer">{esc(text)}</a>'


def sentence(counts: dict, needs_you: list) -> str:
    """The operator sentence: the counts, then "here is what to do" when something needs you."""
    said = status.spoken(counts, [])
    return said[:-1] + ", here is what to do." if needs_you else said


def _bucket(name: str, title: str, rows: list[str], empty: str) -> str:
    return fill("bucket", name=name, title=esc(title), count=len(rows),
                rows="\n".join(rows) or fill("empty_row", text=esc(empty)))


def _state(value: str) -> str:
    return esc(value.replace("_", " "))


def render_project(found: dict, heading: str) -> str:
    """One project's groups, from status.compose's result."""
    needs = [fill("needs_you_row", key=esc(e["key"]), state=_state(e["state"]),
                  age=esc(e.get("age")), line=esc(e["line"]), what=esc(e["what"]),
                  link=link(e.get("link"))) for e in found["needs_you"]]
    groups = {"needs_you": needs}
    for name, _title in GROUPS[1:]:
        groups[name] = [fill("card_row", key=esc(c["key"]), state=_state(c["state"]),
                             age=esc(c.get("age")), link=link(c.get("link")))
                        for c in found["cards"] if c["bucket"] == name]
    landed = [fill("landed_row", key=esc(e["key"]), age=esc(e.get("age")),
                   link=link(e.get("link")),
                   deterministic=esc(e["scores"].get("deterministic") or "not scored yet"),
                   judged=esc(e["scores"].get("judged") or "not scored yet"))
              for e in found.get("landed", [])]
    findings = [fill("finding_row", cls=esc(f["class"]), fingerprint=esc(f["fingerprint"]),
                     age=esc(f.get("age")), excerpt=esc(f.get("excerpt")))
                for f in found.get("findings", [])]
    return fill(
        "project", slug=esc(found["project"]), heading=heading,
        sentence=esc(sentence(found["counts"], found["needs_you"])),
        needs_you=_bucket("needs_you", "Needs you", groups["needs_you"], "Nothing needs you."),
        being_built=_bucket("being_built", "Being built", groups["being_built"],
                            "Nothing is being built."),
        waiting_to_land=_bucket("waiting_to_land", "Waiting to land",
                                groups["waiting_to_land"], "Nothing is waiting to land."),
        landed=_bucket("landed", "Landed today", landed, "Nothing landed today."),
        findings=_bucket("findings", "Open findings", findings, "No open findings."))


def render_across(found: dict) -> str:
    """Every project's groups under the totals, from status.across's result."""
    projects = [render_project(p, f'<h2><a href="/projects/{esc(p["project"])}">'
                                  f'{esc(p["project"])}</a></h2>')
                for p in found["projects"]]
    bad_files = ""
    if found["unreadable"]:
        bad_files = fill("unreadable", rows="\n".join(
            fill("unreadable_row", project=esc(u["project"]), error=esc(u["error"]))
            for u in found["unreadable"]))
    every = [e for p in found["projects"] for e in p["needs_you"]]
    return fill("across", sentence=esc(sentence(found["counts"], every)),
                unreadable=bad_files, projects="\n".join(projects))


def document(title: str, body: str) -> bytes:
    return fill("layout", title=esc(title), refresh=REFRESH, body=body).encode()


def message(heading: str, text: str) -> bytes:
    return document(heading, fill("message", heading=esc(heading), text=esc(text)))


def safe_next(value: str | None) -> str:
    """A path on this page to return to after login; anything else becomes /."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


class Page:
    """The WSGI application.

    ports opens the service ports for one request (one database connection,
    the config files). identity is the provider (login.OidcIdentity). key
    signs the cookies. base_url is where the page is served, such as
    https://slipway.example.org, and fixes the redirect address the provider
    is told, so a forged Host header cannot change it.
    """

    def __init__(self, ports: Callable[[], Ports], identity: Identity, key: bytes,
                 base_url: str, clock: Callable[[], float] = time.time):
        self.ports = ports
        self.identity = identity
        self.signer = Signer(key, clock)
        self.base_url = base_url.rstrip("/")
        self.secure = self.base_url.startswith("https://")

    # The WSGI entry.

    def __call__(self, environ: dict, start_response) -> Iterable[bytes]:
        code, headers, body = self.handle(environ)
        start_response(code, [*HEADERS, *headers])
        return [body]

    def handle(self, environ: dict) -> tuple[str, list[tuple[str, str]], bytes]:
        if environ.get("REQUEST_METHOD", "GET") != "GET":
            return "405 Method Not Allowed", [("Allow", "GET")], message(
                "Read only", "This page takes no actions. Every approval is made on the host.")
        path = environ.get("PATH_INFO") or "/"
        query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
        cookies = self._cookies(environ)
        if path == "/login":
            return self.login(safe_next((query.get("next") or ["/"])[0]))
        if path == "/callback":
            return self.callback(query, cookies)
        if path != "/" and not path.startswith("/projects/"):
            return "404 Not Found", [], message("Not found", "There is no such page.")
        session = self.signer.loads(cookies.get(SESSION))
        if session is None:
            target = path + (f"?{environ['QUERY_STRING']}" if environ.get("QUERY_STRING") else "")
            return "302 Found", [("Location", "/login?" + urllib.parse.urlencode(
                {"next": target}))], b""
        ports = self.ports()
        operator = ports.operator().operator_subject
        if not secrets.compare_digest(str(session.get("sub", "")), operator):
            return self.forbidden()
        if path == "/":
            found = status.across(ports, operator)
            if found.get("outcome") == "refused":
                return self.forbidden()
            return "200 OK", [], document("Slipway status", render_across(found))
        return self.project(ports, path.removeprefix("/projects/"))

    def project(self, ports: Ports, slug: str):
        if slug not in ports.slugs():
            return "404 Not Found", [], message("Not found", f"No project is named {slug}.")
        try:
            found = status.compose(ports, slug)
        except ConfigError as error:
            return "500 Internal Server Error", [], message(
                "Project file could not be read", str(error))
        heading = (f'<nav><a href="/">All projects</a></nav><h1>{esc(found["project"])}</h1>')
        return "200 OK", [], document(f"{found['project']} status",
                                      render_project(found, heading))

    def forbidden(self):
        return "403 Forbidden", [], message(
            "Not for this account", "This page is for the operator alone.")

    # Login.

    def _cookies(self, environ: dict) -> dict[str, str]:
        jar = SimpleCookie()
        try:
            jar.load(environ.get("HTTP_COOKIE", ""))
        except CookieError:
            return {}
        return {name: morsel.value for name, morsel in jar.items()}

    def _cookie(self, name: str, value: str, max_age: int) -> tuple[str, str]:
        flags = "; Secure" if self.secure else ""
        return ("Set-Cookie", f"{name}={value}; Max-Age={max_age}; Path=/; HttpOnly; "
                              f"SameSite=Lax{flags}")

    def redirect_uri(self) -> str:
        return f"{self.base_url}/callback"

    def login(self, next_path: str):
        verifier, challenge = pkce_pair()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        try:
            target = self.identity.start(self.redirect_uri(), state, challenge, nonce)
        except Exception as error:  # an unreachable provider is a page, not a traceback
            return "502 Bad Gateway", [], message("Sign in is unavailable", str(error))
        held = self.signer.dumps({"state": state, "nonce": nonce, "verifier": verifier,
                                  "next": next_path}, LOGIN_AGE)
        return "302 Found", [("Location", target), self._cookie(LOGIN, held, LOGIN_AGE)], b""

    def callback(self, query: dict, cookies: dict):
        clear = self._cookie(LOGIN, "", 0)
        held = self.signer.loads(cookies.get(LOGIN))
        state = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        if held is None or not state or not secrets.compare_digest(state, held["state"]):
            return "400 Bad Request", [clear], message(
                "Sign in expired", "Open the page again to sign in.")
        if not code:
            error = (query.get("error") or ["no code"])[0]
            return "403 Forbidden", [clear], message("Sign in refused", error)
        try:
            subject = self.identity.finish(code, held["verifier"], self.redirect_uri(),
                                           held["nonce"])
        except LoginFailed as error:
            return "403 Forbidden", [clear], message("Sign in refused", str(error))
        if not secrets.compare_digest(subject, self.ports().operator().operator_subject):
            refused, _, body = self.forbidden()
            return refused, [clear], body
        session = self._cookie(SESSION, self.signer.dumps({"sub": subject}, SESSION_AGE),
                               SESSION_AGE)
        return "302 Found", [("Location", safe_next(held.get("next"))), session, clear], b""


__all__ = ["Page", "render_across", "render_project", "safe_next", "sentence"]
