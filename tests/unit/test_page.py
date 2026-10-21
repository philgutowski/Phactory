"""U20 test scenarios for the status page, on fixture records.

The records, the tracker, and the config files are _service_fakes' world();
the identity provider is FakeIdentity below, so nothing reaches a network.
Requests go through the WSGI entry the way a server would send them.
"""

import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from _service_fakes import BUCKETS, MANAGER, OPERATOR, SLUG, ready_card, world
from slipway.page import app as page_app
from slipway.page.login import LoginFailed, OidcIdentity, Signer, b64url
from slipway.trackers.base import Card
from test_imports import SRC, walk

KEY = b"k" * 32
BASE = "https://status.example.test"
TEMPLATES = SRC / "slipway" / "page" / "templates"
HALT = {"halt": "commit_mismatch", "line": "commit mismatch: the merge is not the confirmed commit",
        "evidence": {"expected": "a" * 40, "found": "c" * 40}}


class FakeIdentity:
    """The provider: start records what it was sent, finish returns a chosen subject."""

    def __init__(self, subject=OPERATOR):
        self.subject = subject
        self.started: dict = {}
        self.finished: dict = {}

    def start(self, redirect_uri, state, challenge, nonce):
        self.started = {"redirect_uri": redirect_uri, "state": state, "challenge": challenge,
                        "nonce": nonce}
        return f"https://auth.example.test/authorize?state={state}"

    def finish(self, code, verifier, redirect_uri, nonce):
        self.finished = {"code": code, "verifier": verifier, "redirect_uri": redirect_uri,
                         "nonce": nonce}
        if code == "bad":
            raise LoginFailed("the token endpoint refused the code")
        return self.subject


class Response:
    def __init__(self, status, headers, body):
        self.status = int(status.split()[0])
        self.headers = headers
        self.text = b"".join(body).decode()

    def header(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def cookie(self, name):
        for value in self.header("Set-Cookie"):
            if value.startswith(f"{name}="):
                return value.split(";")[0].split("=", 1)[1]
        return None


def make(w, identity=None):
    return page_app.Page(lambda: w.ports, identity or FakeIdentity(), KEY, BASE,
                         clock=lambda: w.clock().timestamp())


def get(page, path, cookies=None, method="GET"):
    path, _, query = path.partition("?")
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
               "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())}
    seen = {}

    def start_response(status, headers):
        seen["status"], seen["headers"] = status, headers

    body = page(environ, start_response)
    return Response(seen["status"], seen["headers"], body)


def session(w, subject=OPERATOR):
    return {page_app.SESSION: Signer(KEY, lambda: w.clock().timestamp()).dumps(
        {"sub": subject}, 3600)}


def section(text, name):
    """The rows of one group on a page, as the HTML between its list tags."""
    return re.findall(rf'<ul class="rows bucket-{name}">(.*?)</ul>', text, re.S)


def keys(fragment):
    return re.findall(r'data-key="([^"]+)"', fragment)


# The card's test scenarios.

def test_a_halted_card_renders_under_needs_you_with_its_line_and_host_link(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-1", "building")
    w.records.seed("EX-5", "halted", pr_number=5, evidence=HALT)

    found = get(make(w), f"/projects/{SLUG}", session(w))

    assert found.status == 200
    [needs] = section(found.text, "needs_you")
    assert keys(needs) == ["EX-5"]
    assert HALT["line"] in needs
    assert 'href="https://github.com/example-org/example-app/pull/5"' in needs
    assert "What to do: check the evidence" in needs
    assert keys(section(found.text, "being_built")[0]) == ["EX-1"]
    assert "1 being built, 1 needs you, here is what to do." in found.text


def test_an_abandoned_card_renders_once_and_goes_once_it_leaves_ready(tmp_path):
    w = world(tmp_path, [ready_card("EX-7")])
    w.records.seed("EX-7", "abandoned", pr_number=7,
                   evidence={"reason": "pull request #7 closed unmerged"})
    page = make(w)

    shown = get(page, f"/projects/{SLUG}", session(w))
    assert shown.text.count('data-key="EX-7"') == 1
    assert keys(section(shown.text, "needs_you")[0]) == ["EX-7"]
    assert "pull request #7 closed unmerged" in shown.text

    w.tracker.cards["EX-7"] = ready_card("EX-7", status="Backlog")
    gone = get(page, f"/projects/{SLUG}", session(w))
    assert 'data-key="EX-7"' not in gone.text
    assert "Nothing needs you." in gone.text
    across = get(page, "/", session(w))
    assert 'data-key="EX-7"' not in across.text


def test_the_across_view_counts_every_project_and_puts_each_state_in_one_bucket(tmp_path):
    w = world(tmp_path, lanes={"other": "laptop"})
    states = sorted(BUCKETS)
    for n, state in enumerate(states):
        project = SLUG if n % 2 == 0 else "other"
        w.records.seed(f"S-{state}", state, project=project, pr_number=n + 1,
                       evidence=HALT if state == "halted" else {"reason": state})

    found = get(make(w), "/", session(w))

    assert found.status == 200
    groups = {name: [] for name in ("needs_you", "being_built", "waiting_to_land", "landed")}
    for name in groups:
        for fragment in section(found.text, name):
            groups[name] += keys(fragment)
    for state in states:
        holding = [name for name, found_keys in groups.items() if f"S-{state}" in found_keys]
        expected = "landed" if BUCKETS[state] == "done" else BUCKETS[state]
        assert holding == [expected], state
        assert groups[expected].count(f"S-{state}") == 1, state
    total = {b: sum(1 for s in states if BUCKETS[s] == b)
             for b in ("being_built", "waiting_to_land", "needs_you", "done")}
    headline = re.search(r'<h1>All projects</h1>\s*<p class="sentence">([^<]*)</p>',
                         found.text).group(1)
    assert headline == (f"{total['being_built']} being built, {total['waiting_to_land']} "
                        f"waiting to land, {total['needs_you']} need you, {total['done']} "
                        "landed today, here is what to do.")
    assert '<a href="/projects/example">example</a>' in found.text
    assert '<a href="/projects/other">other</a>' in found.text


def test_a_manager_agents_session_receives_403_on_both_views(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-5", "halted", pr_number=5, evidence=HALT)
    page = make(w)

    for path in ("/", f"/projects/{SLUG}"):
        found = get(page, path, session(w, MANAGER))
        assert found.status == 403, path
        assert "EX-5" not in found.text


def test_no_template_posts_an_approval_and_page_imports_no_mcp_or_workflow(tmp_path):
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text().lower()
        assert "<form" not in text and "method=" not in text, path.name
        assert "approv" not in text.replace("every approval is made on the host", ""), path.name
        assert "<script" not in text, path.name
    edges = [e for e in walk(SRC) if e.importer.startswith("slipway.page")]
    assert edges, "the walk saw the page package"
    assert not [str(e) for e in edges
                if e.imported.split(".")[1] in {"mcp", "workflow", "records", "hosts"}]

    w = world(tmp_path)
    found = get(make(w), "/", session(w), method="POST")
    assert found.status == 405 and found.header("Allow") == ["GET"]
    assert "form-action 'none'" in found.header("Content-Security-Policy")[0]


# Login.

def test_a_visitor_with_no_session_is_sent_to_login_and_back(tmp_path):
    w = world(tmp_path)
    identity = FakeIdentity()
    page = make(w, identity)

    first = get(page, f"/projects/{SLUG}")
    assert first.status == 302
    assert first.header("Location") == ["/login?next=%2Fprojects%2Fexample"]

    login = get(page, "/login?next=%2Fprojects%2Fexample")
    assert login.status == 302
    assert login.header("Location")[0].startswith("https://auth.example.test/authorize")
    assert identity.started["redirect_uri"] == f"{BASE}/callback"
    held = login.cookie(page_app.LOGIN)
    assert "HttpOnly" in login.header("Set-Cookie")[0]
    assert "Secure" in login.header("Set-Cookie")[0]

    back = get(page, f"/callback?code=abc&state={identity.started['state']}",
               {page_app.LOGIN: held})
    assert back.status == 302 and back.header("Location") == ["/projects/example"]
    verifier = identity.finished["verifier"]
    assert b64url(hashlib.sha256(verifier.encode()).digest()) == identity.started["challenge"]
    assert identity.finished["nonce"] == identity.started["nonce"]
    cookie = back.cookie(page_app.SESSION)
    assert cookie

    assert get(page, f"/projects/{SLUG}", {page_app.SESSION: cookie}).status == 200


def test_a_manager_agent_signing_in_gets_no_session(tmp_path):
    w = world(tmp_path)
    identity = FakeIdentity(subject=MANAGER)
    page = make(w, identity)
    held = get(page, "/login").cookie(page_app.LOGIN)

    back = get(page, f"/callback?code=abc&state={identity.started['state']}",
               {page_app.LOGIN: held})

    assert back.status == 403
    assert back.cookie(page_app.SESSION) is None


@pytest.mark.parametrize("query", ["code=abc&state=wrong", "code=bad&state={state}",
                                   "code=abc&state=%C3%A9",
                                   "error=access_denied&state={state}"])
def test_a_callback_that_cannot_be_trusted_sets_no_session(tmp_path, query):
    w = world(tmp_path)
    identity = FakeIdentity()
    page = make(w, identity)
    held = get(page, "/login").cookie(page_app.LOGIN)

    back = get(page, "/callback?" + query.format(state=identity.started["state"]),
               {page_app.LOGIN: held})

    assert back.status in (400, 403)
    assert back.cookie(page_app.SESSION) is None


def test_a_session_cookie_with_a_non_ascii_signature_is_sent_to_login(tmp_path):
    w = world(tmp_path)
    body = session(w)[page_app.SESSION].split(".")[0]
    assert get(make(w), "/", {page_app.SESSION: f"{body}.\u00e9"}).status == 302


def test_a_forged_or_expired_session_is_sent_to_login(tmp_path):
    w = world(tmp_path)
    page = make(w)
    good = session(w)[page_app.SESSION]
    body, mac = good.split(".")
    forged = b64url(json.dumps({"sub": OPERATOR, "exp": 2**40}).encode()) + "." + mac

    assert get(page, "/", {page_app.SESSION: forged}).status == 302
    w.later(timedelta(hours=2))
    assert get(page, "/", {page_app.SESSION: good}).status == 302


@pytest.mark.parametrize("value", ["//evil.example", "https://evil.example", "\\\\evil", "",
                                   "/x\r\nX-Injected: y", "/x\ty", "/x y"])
def test_login_returns_only_to_a_path_on_the_page(value):
    assert page_app.safe_next(value) == "/"


def test_a_return_path_with_a_line_break_never_reaches_the_location_header(tmp_path):
    w = world(tmp_path)
    identity = FakeIdentity()
    page = make(w, identity)
    held = get(page, "/login?next=%2Fx%0D%0AX-Injected:y").cookie(page_app.LOGIN)

    back = get(page, f"/callback?code=abc&state={identity.started['state']}",
               {page_app.LOGIN: held})

    assert back.header("Location") == ["/"]


def test_an_unknown_project_is_not_found(tmp_path):
    w = world(tmp_path)
    assert get(make(w), "/projects/..%2Foperator", session(w)).status == 404
    assert get(make(w), "/projects/nobody", session(w)).status == 404


# What the rows carry.

def test_landed_today_shows_both_scores_and_findings_show_their_excerpt(tmp_path):
    w = world(tmp_path)
    landed = w.records.seed("EX-2", "scored", pr_number=2)
    w.records.seed("EX-3", "scored", at=w.clock() - timedelta(days=2))
    w.records.score(landed, "deterministic",
                    {"total": {"verdict": "pass", "passed": 3, "failed": 0, "of": 3}})
    w.records.finding("runner_step", "fp-9", "step 4: <exit 1>")

    found = get(make(w), f"/projects/{SLUG}", session(w))

    [rows] = section(found.text, "landed")
    assert keys(rows) == ["EX-2"]
    assert "Deterministic: pass, 3 of 3. Judged: not scored yet." in rows
    [findings] = section(found.text, "findings")
    assert "fp-9" in findings and "step 4: &lt;exit 1&gt;" in findings


def test_a_score_that_is_not_an_object_still_renders(tmp_path):
    w = world(tmp_path)
    w.records.score(w.records.seed("EX-2", "scored"), "judged", [0.9])

    found = get(make(w), f"/projects/{SLUG}", session(w))

    assert "Judged: written." in section(found.text, "landed")[0]


def test_a_card_in_its_state_for_hours_shows_its_age(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-1", "gate_red", pr_number=1, at=w.clock() - timedelta(hours=3),
                   evidence={"reason": "the gate failed"})

    found = get(make(w), f"/projects/{SLUG}", session(w))

    assert '<span class="age">3h</span>' in section(found.text, "needs_you")[0]


def test_a_hostile_value_is_escaped(tmp_path):
    w = world(tmp_path)
    w.records.seed("EX-1", "returned_empty",
                   evidence={"line": '<script>alert(1)</script><form method="post">'})

    found = get(make(w), f"/projects/{SLUG}", session(w))

    assert "<script>" not in found.text and "<form" not in found.text
    assert "&lt;script&gt;" in found.text


# The provider's ID token.

def _id_token(**claims):
    base = {"iss": "https://auth.example.test", "aud": "client_example", "sub": OPERATOR,
            "exp": 2_000_000_000, "nonce": "n1"}
    payload = b64url(json.dumps({**base, **claims}).encode())
    return f"{b64url(b'{}')}.{payload}.sig"


def _oidc(tmp_path, token, calls):
    from slipway.config.operator import load_operator

    w = world(tmp_path)
    provider = load_operator(w.root).identity_provider

    def fetch(url, form=None):
        calls.append((url, form))
        if url.endswith("/.well-known/openid-configuration"):
            return {"authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token"}
        return {"id_token": token}

    return OidcIdentity(provider, "secret-value", fetch, clock=lambda: 1_900_000_000)


def test_the_provider_flow_sends_pkce_and_reads_the_subject(tmp_path):
    calls = []
    oidc = _oidc(tmp_path, _id_token(), calls)

    url = oidc.start(f"{BASE}/callback", "s1", "challenge", "n1")
    assert "code_challenge=challenge" in url and "code_challenge_method=S256" in url
    assert oidc.finish("abc", "verifier", f"{BASE}/callback", "n1") == OPERATOR
    token_call = calls[-1][1]
    assert token_call["code_verifier"] == "verifier"
    assert token_call["grant_type"] == "authorization_code"


@pytest.mark.parametrize("claims", [{"iss": "https://other.example"}, {"aud": "another"},
                                    {"nonce": "n2"}, {"exp": 1}, {"sub": ""}])
def test_an_id_token_with_a_wrong_claim_is_refused(tmp_path, claims):
    oidc = _oidc(tmp_path, _id_token(**claims), [])
    with pytest.raises(LoginFailed):
        oidc.finish("abc", "verifier", f"{BASE}/callback", "n1")


@pytest.mark.parametrize("payload", [[1, 2], {"iss": 7}, {"nonce": "\u00e9"}])
def test_a_malformed_id_token_is_refused_not_raised(tmp_path, payload):
    token = f"{b64url(b'{}')}.{b64url(json.dumps(payload).encode())}.sig"
    with pytest.raises(LoginFailed):
        _oidc(tmp_path, token, []).finish("abc", "verifier", f"{BASE}/callback", "n1")


def test_the_page_package_names_no_project():
    """R1: no customer, repository, or subject from the fixtures is written into page/."""
    for path in Path(SRC / "slipway" / "page").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".html"}:
            text = path.read_text()
            for value in ("example-org", OPERATOR, MANAGER, "Ready"):
                assert value not in text, (path.name, value)


def test_an_unreadable_tracker_keeps_the_abandoned_card(tmp_path):
    w = world(tmp_path)
    w.tracker.cards["EX-8"] = Card("EX-8", skipped="the tracker timed out")
    w.records.seed("EX-8", "abandoned", evidence={"reason": "card no longer ready"})

    found = get(make(w), f"/projects/{SLUG}", session(w))

    assert keys(section(found.text, "needs_you")[0]) == ["EX-8"]
