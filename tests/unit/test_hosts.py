"""U9 test scenarios for the host adapters and the webhook verifier.

Every adapter here is built with an injected FakeApi in place of urllib's
opener. It routes a method and a URL pattern to a status code and a body,
answers from the JSON fixtures in tests/fixtures/host (documented shapes,
not recorded deliveries: see the README there), and records every request
so a test can read back what was sent. The network guard (tests/_nonet.py)
is loaded for every test, and one test below proves it would catch an
adapter built without its fake.

Webhook bodies are signed here, by the tests, with invented secrets.
"""

import http.client
import inspect
import io
import json
import re
import shutil
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from _nonet import NetworkReached
from slipway.config.operator import Host, load_operator
from slipway.config.project import LandingPath, MergeMethod, load_project
from slipway.hosts import base, webhooks
from slipway.hosts.base import (
    AUTO_MERGE_ARMED,
    ENQUEUED,
    INTERFACE,
    NOT_SUPPORTED,
    BranchConflict,
    CheckState,
    Credential,
    HostError,
    ProbeUndetermined,
    PullRequest,
    resolve_host_credential,
)
from slipway.hosts.bitbucket import BitbucketHost
from slipway.hosts.github import GitHubHost
from slipway.hosts.webhooks import Dropped, Event, WebhookRefused, verify
from slipway.trackers.base import ClosingReference
from slipway.verify import AUTO_MERGE_STRICT, BITBUCKET_QUEUE, MERGE_QUEUE, PLAIN_MERGE

TESTS = Path(__file__).resolve().parents[1]
FIXTURES = TESTS / "fixtures" / "host"
CONFIG = TESTS / "fixtures" / "config"
SLUG = "example"

GITHUB_REPOSITORY = "example-org/example-app"
BITBUCKET_REPOSITORY = "example-ws/example-app"
HEAD = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
HEAD2 = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"
MERGE = "c0ffee0123456789abcdef0123456789abcdef01"
BASE = "da38e6c2ec96b89748d4f34ae3cf5b07e5824f7e"
TESTED = "ec26c3e57ca3a959ca5aad62de7213c562f8c821"
BB_HEAD = "7b9c1d2e3f405162738495a6b7c8d9e0f1a2b3c4"
BB_MERGE = "d4c3b2a1f0e9d8c7b6a5948372615049f8e7d6c5"
BB_BASE = "0123456789abcdef0123456789abcdef01234567"
BB_TESTED = "5e4d3c2b1a0f9e8d7c6b5a4938271605f4e3d2c1"
BRANCH = "slipway/u9-host-adapters-webhooks"
TOKEN = "host-token-value-for-tests-only"

SECRET, PREVIOUS = "current-webhook-secret", "previous-webhook-secret"
DELIVERY = "72d3162e-cc78-11e3-81ab-4c9367dc0958"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text()


def fixture_json(name: str):
    return json.loads(fixture(name))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    root = tmp_path / "slipway"
    (root / "projects").mkdir(parents=True)
    shutil.copy(CONFIG / "operator.toml", root / "operator.toml")
    shutil.copy(CONFIG / "project.toml", root / "projects" / f"{SLUG}.toml")
    monkeypatch.setenv("SLIPWAY_HOME", str(root))
    return root


@pytest.fixture
def pinned_project():
    """The fixture project file pins auto_merge_strict and squash."""
    return load_project(SLUG)


@pytest.fixture
def github_project(pinned_project):
    return replace(pinned_project, landing_path=None, merge_method=None)


@pytest.fixture
def bitbucket_project(pinned_project):
    return replace(pinned_project, host=Host.BITBUCKET, repository=BITBUCKET_REPOSITORY,
                   landing_path=None, merge_method=None)


APP = Credential("example-app", Host.GITHUB, "github_app", TOKEN)
PAT = Credential("example-pat", Host.GITHUB, "fine_grained_token", TOKEN)
BB = Credential("example-bb", Host.BITBUCKET, "repository_access_token", TOKEN)


class _Answer:
    def __init__(self, status, body):
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._raw = (body if isinstance(body, str) else json.dumps(body)).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """Stands in for urllib's opener. Routes (method, URL regex) to (status, body)."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def route(self, method, pattern, status=200, body=None, fixture_name=None):
        text = fixture(fixture_name) if fixture_name else body
        self.routes.append((method, re.compile(pattern), status, text))
        return self

    def open(self, request, timeout=None):
        url = request.get_full_url()
        sent = json.loads(request.data.decode()) if request.data else None
        self.requests.append((request.get_method(), url, sent, dict(request.header_items())))
        for method, pattern, status, text in self.routes:
            if method == request.get_method() and pattern.search(url):
                if status >= 400:
                    raise urllib.error.HTTPError(
                        url, status, "refused", {}, io.BytesIO(_Answer(status, text).read()))
                return _Answer(status, text if text is not None else "")
        raise AssertionError(f"no route for {request.get_method()} {url}")

    def sent(self, method, fragment):
        return [r for r in self.requests if r[0] == method and fragment in r[1]]


def github_api():
    api = FakeApi()
    api.route("GET", r"/repos/example-org/example-app$", fixture_name="github_api_repository")
    return api


def bitbucket_api():
    api = FakeApi()
    api.route("GET", r"/repositories/example-ws/example-app$",
              fixture_name="bitbucket_api_repository")
    return api


# The interface.

@pytest.mark.parametrize("adapter", [GitHubHost, BitbucketHost])
def test_adapter_exposes_the_interface_and_nothing_more(adapter):
    public = {name for name, _ in inspect.getmembers(adapter, inspect.isfunction)
              if not name.startswith("_")}
    assert public == set(INTERFACE)
    assert "merge" not in public  # Slipway never merges (R18)


def test_credential_resolves_from_the_operator_file_and_never_prints(pinned_project):
    operator = load_operator()
    credential = resolve_host_credential(pinned_project, operator,
                                         {"EXAMPLE_GITHUB_APP_KEY": TOKEN})
    assert (credential.name, credential.host, credential.kind) == (
        "example-app", Host.GITHUB, "github_app")
    assert credential.token == TOKEN
    assert TOKEN not in repr(credential)
    with pytest.raises(HostError, match="EXAMPLE_GITHUB_APP_KEY is unset"):
        resolve_host_credential(pinned_project, operator, {})
    with pytest.raises(HostError, match="is a github credential"):
        resolve_host_credential(replace(pinned_project, host=Host.BITBUCKET), operator,
                                {"EXAMPLE_GITHUB_APP_KEY": TOKEN})


def test_an_adapter_without_its_fake_is_caught_by_the_network_guard(github_project):
    host = GitHubHost(github_project, APP)
    with pytest.raises(NetworkReached):
        host.commit_on_remote(BRANCH, HEAD)


@pytest.mark.parametrize("error", [
    OSError(f"connection reset while sending Bearer {TOKEN}"),
    http.client.IncompleteRead(f"partial Bearer {TOKEN}".encode()),
    http.client.BadStatusLine(f"HTTP/1.1 Bearer {TOKEN}"),
])
def test_a_transport_failure_is_a_host_error_without_the_token(github_project, error):
    class Broken:
        def open(self, request, timeout=None):
            raise error

    with pytest.raises(HostError) as caught:
        GitHubHost(github_project, APP, opener=Broken()).commit_on_remote(BRANCH, HEAD)
    assert TOKEN not in str(caught.value)
    # An IncompleteRead prints a byte count, not its bytes, so only an error
    # whose text carried the token shows the mark.
    assert ("[redacted]" in str(caught.value)) == (TOKEN in str(error))


# create_branch and commit_on_remote.

def test_github_create_branch_creates_the_reference_at_the_commit(github_project):
    api = github_api().route("POST", r"/git/refs$", 201, fixture_name="github_api_ref")
    branch = GitHubHost(github_project, APP, opener=api).create_branch(BRANCH, BASE)
    assert (branch.name, branch.sha, branch.created) == (BRANCH, BASE, True)
    method, url, sent, headers = api.requests[0]
    assert sent == {"ref": f"refs/heads/{BRANCH}", "sha": BASE}
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-github-api-version"] == "2022-11-28"


def test_github_create_branch_is_idempotent_at_the_same_commit(github_project):
    api = (github_api()
           .route("POST", r"/git/refs$", 422, fixture_name="github_api_ref_exists")
           .route("GET", r"/git/ref/heads/", fixture_name="github_api_ref"))
    branch = GitHubHost(github_project, APP, opener=api).create_branch(BRANCH, BASE)
    assert branch.created is False


def test_github_create_branch_refuses_a_branch_at_another_commit(github_project):
    api = (github_api()
           .route("POST", r"/git/refs$", 422, fixture_name="github_api_ref_exists")
           .route("GET", r"/git/ref/heads/", fixture_name="github_api_ref"))
    with pytest.raises(BranchConflict) as caught:
        GitHubHost(github_project, APP, opener=api).create_branch(BRANCH, HEAD)
    assert (caught.value.wanted, caught.value.found) == (HEAD, BASE)


def test_github_commit_on_remote_compares_the_branch_head(github_project):
    api = github_api().route("GET", r"/git/ref/heads/", fixture_name="github_api_ref")
    host = GitHubHost(github_project, APP, opener=api)
    assert host.commit_on_remote(BRANCH, BASE) is True
    assert host.commit_on_remote(BRANCH, HEAD) is False
    missing = github_api().route("GET", r"/git/ref/heads/", 404, {"message": "Not Found"})
    assert GitHubHost(github_project, APP, opener=missing).commit_on_remote("gone", BASE) is False


def test_bitbucket_create_branch_and_conflict(bitbucket_project):
    api = bitbucket_api().route("POST", r"/refs/branches$", 201,
                                fixture_name="bitbucket_api_branch")
    branch = BitbucketHost(bitbucket_project, BB, opener=api).create_branch(
        "slipway/u9-EX-12-host-adapters", BB_BASE)
    assert branch.created is True
    assert api.requests[0][2] == {"name": "slipway/u9-EX-12-host-adapters",
                                  "target": {"hash": BB_BASE}}
    exists = (bitbucket_api()
              .route("POST", r"/refs/branches$", 400, fixture_name="bitbucket_api_branch_exists")
              .route("GET", r"/refs/branches/", fixture_name="bitbucket_api_branch"))
    host = BitbucketHost(bitbucket_project, BB, opener=exists)
    assert host.create_branch("slipway/u9-EX-12-host-adapters", BB_BASE).created is False
    with pytest.raises(BranchConflict):
        host.create_branch("slipway/u9-EX-12-host-adapters", BB_HEAD)
    assert host.commit_on_remote("slipway/u9-EX-12-host-adapters", BB_BASE) is True


# open_pull_request.

def test_github_open_pull_request_places_the_closing_reference_body_line(github_project):
    api = github_api().route("POST", r"/pulls$", 201, fixture_name="github_api_pull_request")
    reference = ClosingReference(body_line="Closes #12")
    pr = GitHubHost(github_project, APP, opener=api).open_pull_request(
        BRANCH, "U9: Host adapters and webhooks", reference, body=f"Base: {BASE}")
    sent = api.sent("POST", "/pulls")[0][2]
    assert sent["title"] == "U9: Host adapters and webhooks"
    assert sent["body"].startswith("Closes #12\n\n")
    assert sent["body"].endswith(f"Base: {BASE}")
    assert (sent["head"], sent["base"]) == (BRANCH, "main")  # the repository's default branch
    assert (pr.number, pr.state, pr.merged, pr.head) == (104, "open", False, HEAD)
    assert pr.node_id == "PR_kwDOExample104"


def test_github_open_pull_request_returns_the_existing_one_on_a_retry(github_project):
    api = (github_api()
           .route("POST", r"/pulls$", 422, fixture_name="github_api_pull_request_exists")
           .route("GET", r"/pulls\?", body="[" + fixture("github_api_pull_request") + "]"))
    pr = GitHubHost(github_project, APP, opener=api).open_pull_request(
        BRANCH, "U9", ClosingReference(body_line="Closes #12"))
    assert pr.number == 104
    listed = api.sent("GET", "/pulls?")[0][1]
    assert f"head=example-org%3A{BRANCH.replace('/', '%2F')}" in listed


def test_bitbucket_open_pull_request_puts_the_jira_key_in_the_title(bitbucket_project):
    api = bitbucket_api().route("POST", r"/pullrequests$", 201,
                                fixture_name="bitbucket_api_pullrequest")
    reference = ClosingReference(title_token="EX-12", branch_token="EX-12")
    pr = BitbucketHost(bitbucket_project, BB, opener=api).open_pull_request(
        "slipway/u9-EX-12-host-adapters", "Host adapters and webhooks", reference)
    sent = api.sent("POST", "/pullrequests")[0][2]
    assert sent["title"] == "EX-12: Host adapters and webhooks"
    assert sent["source"] == {"branch": {"name": "slipway/u9-EX-12-host-adapters"}}
    assert sent["destination"] == {"branch": {"name": "main"}}
    assert "description" not in sent  # Jira keys on the title, not a body line
    assert (pr.number, pr.state, pr.head, pr.approvals) == (7, "open", BB_HEAD, ("reviewer-one",))


def test_title_token_is_not_doubled():
    reference = ClosingReference(title_token="EX-12")
    assert base.titled("EX-12: already keyed", reference) == "EX-12: already keyed"
    assert base.titled("plain", ClosingReference()) == "plain"


# post_check.

def test_github_post_check_is_a_check_run_under_an_app(github_project):
    api = github_api().route("POST", r"/check-runs$", 201, fixture_name="github_api_check_run")
    posted = GitHubHost(github_project, APP, opener=api).post_check(
        HEAD, "slipway/landing", CheckState.FAILURE, "https://slipway.example.test/c/12",
        summary="commit_mismatch: merged c0ffee is not the tested commit")
    sent = api.sent("POST", "/check-runs")[0][2]
    assert (sent["head_sha"], sent["name"], sent["status"], sent["conclusion"]) == (
        HEAD, "slipway/landing", "completed", "failure")
    assert sent["details_url"] == "https://slipway.example.test/c/12"
    assert sent["output"]["summary"].startswith("commit_mismatch")
    assert (posted.id, posted.state) == ("4242", "failure")


def test_github_post_check_is_a_commit_status_under_a_token(github_project):
    api = github_api().route("POST", rf"/statuses/{HEAD}$", 201, fixture_name="github_api_status")
    posted = GitHubHost(github_project, PAT, opener=api).post_check(
        HEAD, "slipway/landing", "pending", summary="x" * 200)
    sent = api.sent("POST", "/statuses/")[0][2]
    assert (sent["state"], sent["context"]) == ("pending", "slipway/landing")
    assert len(sent["description"]) == 140
    assert posted.id is None


def test_bitbucket_post_check_is_a_build_status_and_needs_a_url(bitbucket_project):
    api = bitbucket_api().route("POST", rf"/commit/{BB_HEAD}/statuses/build$", 201,
                                fixture_name="bitbucket_api_build_status")
    host = BitbucketHost(bitbucket_project, BB, opener=api)
    posted = host.post_check(BB_HEAD, "slipway/landing", "failure",
                             "https://slipway.example.test/c/EX-12", summary="commit_mismatch")
    sent = api.sent("POST", "/statuses/build")[0][2]
    assert (sent["key"], sent["state"], sent["description"]) == (
        "slipway/landing", "FAILED", "commit_mismatch")
    assert posted.state == "failure"
    with pytest.raises(HostError, match="needs a url"):
        host.post_check(BB_HEAD, "slipway/landing", "success")
    assert len(api.requests) == 1


# pull_request.

def test_github_pull_request_reads_state_and_approvals(github_project):
    api = (github_api()
           .route("GET", r"/pulls/104$", fixture_name="github_api_pull_request_merged")
           .route("GET", r"/pulls/104/reviews", fixture_name="github_api_reviews"))
    pr = GitHubHost(github_project, APP, opener=api).pull_request(104)
    assert (pr.state, pr.merged, pr.merge_commit, pr.head) == ("merged", True, MERGE, HEAD)
    # The latest decisive review per reviewer: two changed their mind, three
    # was dismissed, the commenter decided nothing.
    assert pr.approvals == ("reviewer-one", "reviewer-two")
    assert pr.merged_by == "example-operator"
    assert pr.base_ref == "main"


def test_bitbucket_pull_request_reads_state_and_approvals(bitbucket_project):
    api = bitbucket_api().route("GET", r"/pullrequests/7$",
                                fixture_name="bitbucket_api_pullrequest_merged")
    pr = BitbucketHost(bitbucket_project, BB, opener=api).pull_request(PullRequest(
        7, "open", False, BB_HEAD))
    assert (pr.state, pr.merged, pr.merge_commit, pr.head) == ("merged", True, BB_MERGE, BB_HEAD)
    assert pr.approvals == ("reviewer-one",)
    assert pr.merged_by == "example-operator"


def test_pull_request_read_failure_is_a_host_error(github_project):
    api = github_api().route("GET", r"/pulls/104$", 404, {"message": "Not Found"})
    with pytest.raises(HostError, match="Not Found"):
        GitHubHost(github_project, APP, opener=api).pull_request(104)


# enqueue.

def test_bitbucket_enqueue_returns_not_supported_without_a_request(bitbucket_project):
    api = bitbucket_api()
    host = BitbucketHost(bitbucket_project, BB, opener=api)
    assert host.enqueue(PullRequest(7, "open", False, BB_HEAD)) == NOT_SUPPORTED
    assert api.requests == []


def test_github_enqueue_on_a_queue_path_enqueues_at_the_expected_head(github_project):
    api = github_api().route("POST", r"/graphql$", fixture_name="github_api_enqueue")
    landing = base.Landing(MERGE_QUEUE, "rebase", base.LIVE, base.LIVE)
    pr = PullRequest(104, "open", False, HEAD, node_id="PR_kwDOExample104")
    assert GitHubHost(github_project, APP, opener=api).enqueue(pr, landing) == ENQUEUED
    sent = api.sent("POST", "/graphql")[0][2]
    assert "enqueuePullRequest" in sent["query"]
    assert sent["variables"] == {"id": "PR_kwDOExample104", "head": HEAD}


def test_github_enqueue_off_a_queue_arms_auto_merge_with_the_method(github_project):
    api = github_api().route("POST", r"/graphql$", fixture_name="github_api_auto_merge")
    landing = base.Landing(AUTO_MERGE_STRICT, "squash", base.LIVE, base.LIVE)
    pr = PullRequest(104, "open", False, HEAD, node_id="PR_kwDOExample104")
    assert GitHubHost(github_project, APP, opener=api).enqueue(pr, landing) == AUTO_MERGE_ARMED
    sent = api.sent("POST", "/graphql")[0][2]
    assert "enablePullRequestAutoMerge" in sent["query"]
    assert sent["variables"]["method"] == "SQUASH"


def test_github_enqueue_reports_graphql_errors(github_project):
    api = github_api().route("POST", r"/graphql$", body={"errors": [
        {"message": "Pull request is in clean status", "type": "UNPROCESSABLE"}]})
    pr = PullRequest(104, "open", False, HEAD, node_id="PR_kwDOExample104")
    landing = base.Landing(AUTO_MERGE_STRICT, "squash", base.LIVE, base.LIVE)
    with pytest.raises(HostError, match="clean status"):
        GitHubHost(github_project, APP, opener=api).enqueue(pr, landing)


# probe_landing.

def test_probe_returns_auto_merge_strict_with_squash_from_a_ruleset_without_a_queue(
        github_project):
    api = github_api().route("GET", r"/rules/branches/main$",
                             fixture_name="github_api_rules_no_queue")
    landing = GitHubHost(github_project, APP, opener=api).probe_landing(github_project)
    assert (landing.path, landing.method) == (AUTO_MERGE_STRICT, "squash")
    assert (landing.path_source, landing.method_source) == (base.LIVE, base.LIVE)
    assert landing.queue is False
    assert "ci" in landing.detail


def test_probe_returns_the_pinned_values_without_a_request(pinned_project):
    api = github_api()
    landing = GitHubHost(pinned_project, APP, opener=api).probe_landing(pinned_project)
    assert (landing.path, landing.method) == ("auto_merge_strict", "squash")
    assert (landing.path_source, landing.method_source) == (base.PINNED, base.PINNED)
    assert api.requests == []


def test_probe_keeps_a_pinned_method_beside_a_live_path(github_project):
    api = github_api().route("GET", r"/rules/branches/main$",
                             fixture_name="github_api_rules_queue")
    project = replace(github_project, merge_method=MergeMethod.MERGE)
    landing = GitHubHost(project, APP, opener=api).probe_landing(project)
    assert (landing.path, landing.method) == (MERGE_QUEUE, "merge")
    assert (landing.path_source, landing.method_source) == (base.LIVE, base.PINNED)


def test_probe_returns_merge_queue_with_the_queue_method(github_project):
    api = github_api().route("GET", r"/rules/branches/main$",
                             fixture_name="github_api_rules_queue")
    landing = GitHubHost(github_project, APP, opener=api).probe_landing(github_project)
    assert (landing.path, landing.method, landing.queue) == (MERGE_QUEUE, "rebase", True)


def test_probe_returns_plain_merge_without_a_required_up_to_date_check(github_project):
    api = github_api().route("GET", r"/rules/branches/main$",
                             fixture_name="github_api_rules_unprotected")
    landing = GitHubHost(github_project, APP, opener=api).probe_landing(github_project)
    assert landing.path == PLAIN_MERGE


def test_probe_method_is_the_only_allowed_one(github_project):
    repo = dict(fixture_json("github_api_repository"), allow_squash_merge=False,
                allow_merge_commit=False)
    api = (FakeApi().route("GET", r"/repos/example-org/example-app$", body=repo)
           .route("GET", r"/rules/branches/main$", fixture_name="github_api_rules_no_queue"))
    landing = GitHubHost(github_project, APP, opener=api).probe_landing(github_project)
    assert (landing.path, landing.method) == (AUTO_MERGE_STRICT, "rebase")


def test_bitbucket_probe_is_pinned_or_undetermined(bitbucket_project):
    api = bitbucket_api()
    host = BitbucketHost(bitbucket_project, BB, opener=api)
    with pytest.raises(ProbeUndetermined, match="landing_path and merge_method"):
        host.probe_landing(bitbucket_project)
    pinned = replace(bitbucket_project, landing_path=LandingPath.BITBUCKET_QUEUE,
                     merge_method=MergeMethod.SQUASH)
    landing = host.probe_landing(pinned)
    assert (landing.path, landing.method, landing.queue) == (BITBUCKET_QUEUE, "squash", True)
    assert api.requests == []


# Webhooks.

def secrets_for(repository):
    return {GITHUB_REPOSITORY: (SECRET, PREVIOUS),
            BITBUCKET_REPOSITORY: (SECRET, None)}.get(repository)


def github_headers(body, event, secret=SECRET, delivery=DELIVERY, signature=None):
    return {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
            "x-hub-signature-256": signature or webhooks.signature_header(body, secret),
            "Content-Type": "application/json"}


def bitbucket_headers(body, event, secret=SECRET, delivery="bb-request-uuid-1"):
    return {"X-Event-Key": event, "X-Request-UUID": delivery, "X-Hook-UUID": "hook-uuid",
            "X-Hub-Signature": webhooks.signature_header(body, secret)}


def github_delivery(name, event, **kwargs):
    body = fixture(name).encode()
    return github_headers(body, event, **kwargs), body


def test_github_closed_merged_normalises_to_pr_merged_with_the_delivery_id():
    headers, body = github_delivery("github_pull_request_closed", "pull_request")
    event = verify("github", headers, body, secrets_for)
    assert isinstance(event, Event)
    assert (event.kind, event.pr_number, event.merge_commit, event.delivery_id) == (
        "pr.merged", 104, MERGE, DELIVERY)
    assert (event.head_commit, event.repository, event.host) == (HEAD, GITHUB_REPOSITORY, "github")
    stored = event.normalised()
    assert stored["delivery_id"] == DELIVERY and stored["merge_commit"] == MERGE
    assert stored["merged_by"] == "example-operator"


def test_bitbucket_fulfilled_normalises_to_pr_merged_with_merge_commit_hash():
    body = fixture("bitbucket_pullrequest_fulfilled").encode()
    event = verify(Host.BITBUCKET, bitbucket_headers(body, "pullrequest:fulfilled"), body,
                   secrets_for)
    assert (event.kind, event.pr_number, event.merge_commit, event.head_commit) == (
        "pr.merged", 7, BB_MERGE, BB_HEAD)
    assert event.delivery_id == "bb-request-uuid-1"
    assert event.normalised()["merged_by"] == "example-operator"


def test_a_bad_signature_is_refused_before_parsing():
    headers, body = github_delivery("github_pull_request_closed", "pull_request",
                                    secret="some-other-secret")
    with pytest.raises(WebhookRefused) as caught:
        verify("github", headers, body, secrets_for)
    assert caught.value.reason == webhooks.BAD_SIGNATURE
    missing = dict(headers)
    del missing["x-hub-signature-256"]
    with pytest.raises(WebhookRefused) as caught:
        verify("github", missing, body, secrets_for)
    assert caught.value.reason == webhooks.NO_SIGNATURE


def test_hostile_input_is_refused_not_raised():
    # Brackets nested past the parser's stack, under the cap.
    nested = b"[" * 50_000 + b"]" * 50_000
    with pytest.raises(WebhookRefused) as caught:
        verify("github", github_headers(nested, "pull_request"), nested, secrets_for)
    assert caught.value.reason == webhooks.NOT_JSON
    # A signature that is not a hex digest, including a non ASCII one, which
    # hmac.compare_digest would refuse with a TypeError.
    headers, body = github_delivery("github_pull_request_closed", "pull_request",
                                    signature="sha256=caf\u00e9" + "0" * 60)
    with pytest.raises(WebhookRefused) as caught:
        verify("github", headers, body, secrets_for)
    assert caught.value.reason == webhooks.BAD_SIGNATURE
    headers["x-hub-signature-256"] = "sha256=not-hex"
    with pytest.raises(WebhookRefused) as caught:
        verify("github", headers, body, secrets_for)
    assert caught.value.reason == webhooks.BAD_SIGNATURE


def test_the_previous_secret_is_accepted_inside_the_rotation_window():
    headers, body = github_delivery("github_pull_request_closed", "pull_request",
                                    secret=PREVIOUS)
    assert verify("github", headers, body, secrets_for).kind == "pr.merged"
    assert webhooks.check_signature(headers["x-hub-signature-256"], body,
                                    (SECRET, PREVIOUS)) == 1


def test_a_body_over_the_cap_is_refused_before_anything_else():
    body = b'{"repository": {"full_name": "' + GITHUB_REPOSITORY.encode() + b'"}, "pad": "' \
        + b"x" * webhooks.BODY_CAP_BYTES + b'"}'
    calls = []

    def counting(repository):
        calls.append(repository)
        return secrets_for(repository)

    with pytest.raises(WebhookRefused) as caught:
        verify("github", github_headers(body, "pull_request"), body, counting)
    assert caught.value.reason == webhooks.OVER_CAP
    assert calls == []


def test_synchronize_normalises_with_the_new_head():
    headers, body = github_delivery("github_pull_request_synchronize", "pull_request")
    event = verify("github", headers, body, secrets_for)
    assert (event.kind, event.pr_number, event.head_commit) == ("pr.synchronize", 104, HEAD2)
    assert event.normalised()["before"] == HEAD


def test_merge_group_checks_requested_normalises_to_queue_requested():
    headers, body = github_delivery("github_merge_group_checks_requested", "merge_group")
    event = verify("github", headers, body, secrets_for)
    assert (event.kind, event.pr_number) == ("queue.requested", 104)
    assert (event.tested_commit, event.tested_head) == (TESTED, HEAD)
    stored = event.normalised()
    assert (stored["tested_commit"], stored["tested_head"]) == (TESTED, HEAD)
    assert stored["base_ref"] == "refs/heads/main"


def test_an_unmanaged_repository_is_refused_before_a_secret_lookup():
    payload = fixture_json("github_pull_request_closed")
    payload["repository"]["full_name"] = "stranger/repo"
    body = json.dumps(payload).encode()
    asked = []

    def watching(repository):
        asked.append(repository)
        return None

    with pytest.raises(WebhookRefused) as caught:
        verify("github", github_headers(body, "pull_request"), body, watching)
    assert caught.value.reason == webhooks.UNKNOWN_REPOSITORY
    # The lookup by repository is the resolution: it was asked once, answered
    # None, and no secret was chosen or compared, so the check never ran.
    assert asked == ["stranger/repo"]
    with pytest.raises(WebhookRefused) as caught:
        verify("github", github_headers(b"{}", "pull_request"), b"{}", watching)
    assert caught.value.reason == webhooks.NO_REPOSITORY
    assert asked == ["stranger/repo"]


def test_a_pull_request_slipway_did_not_open_is_dropped():
    headers, body = github_delivery("github_pull_request_closed", "pull_request")
    result = verify("github", headers, body, secrets_for,
                    opened_by_slipway=lambda repository, number: number == 7)
    assert isinstance(result, Dropped)
    assert "#104" in result.reason and result.delivery_id == DELIVERY
    kept = verify("github", headers, body, secrets_for,
                  opened_by_slipway=lambda repository, number: number == 104)
    assert isinstance(kept, Event)


def test_dequeued_check_run_and_status_events_normalise():
    headers, body = github_delivery("github_pull_request_dequeued", "pull_request")
    event = verify("github", headers, body, secrets_for)
    assert (event.kind, event.normalised()["reason"]) == ("pr.dequeued", "CI_FAILURE")
    headers, body = github_delivery("github_check_run_completed", "check_run")
    event = verify("github", headers, body, secrets_for)
    assert (event.kind, event.check_name, event.check_state, event.head_commit, event.pr_number
            ) == ("check.completed", "ci", "success", HEAD, 104)
    status = json.dumps({"repository": {"full_name": GITHUB_REPOSITORY}, "sha": HEAD,
                         "context": "ci", "state": "failure"}).encode()
    event = verify("github", github_headers(status, "status"), status, secrets_for)
    assert (event.kind, event.check_name, event.check_state) == ("check.completed", "ci", "failure")


def test_untracked_events_are_dropped_not_refused():
    ping = json.dumps({"zen": "Keep it logically awesome.",
                       "repository": {"full_name": GITHUB_REPOSITORY}}).encode()
    result = verify("github", github_headers(ping, "ping"), ping, secrets_for)
    assert isinstance(result, Dropped) and "ping" in result.reason
    payload = fixture_json("github_pull_request_closed")
    payload["action"] = "labeled"
    body = json.dumps(payload).encode()
    result = verify("github", github_headers(body, "pull_request"), body, secrets_for)
    assert isinstance(result, Dropped) and "labeled" in result.reason


def test_bitbucket_queue_build_status_and_updated_normalise():
    body = fixture("bitbucket_commit_status_queue").encode()
    event = verify("bitbucket", bitbucket_headers(body, "repo:commit_status_created"), body,
                   secrets_for)
    assert (event.kind, event.tested_commit, event.tested_head) == (
        "queue.requested", BB_TESTED, None)
    assert event.normalised()["refname"] == "bitbucket-merge-queue-main-7"
    body = fixture("bitbucket_commit_status_final").encode()
    event = verify("bitbucket", bitbucket_headers(body, "repo:commit_status_updated"), body,
                   secrets_for)
    assert (event.kind, event.check_state, event.head_commit) == (
        "check.completed", "SUCCESSFUL", BB_TESTED)
    body = fixture("bitbucket_pullrequest_updated").encode()
    event = verify("bitbucket", bitbucket_headers(body, "pullrequest:updated"), body,
                   secrets_for)
    assert (event.kind, event.pr_number, event.head_commit) == ("pr.synchronize", 7, BB_HEAD)


def test_an_unreadable_body_and_a_missing_delivery_id_are_refused():
    body = b"not json"
    with pytest.raises(WebhookRefused) as caught:
        verify("github", github_headers(body, "pull_request"), body, secrets_for)
    assert caught.value.reason == webhooks.NOT_JSON
    headers, body = github_delivery("github_pull_request_closed", "pull_request", delivery="")
    with pytest.raises(WebhookRefused) as caught:
        verify("github", headers, body, secrets_for)
    assert caught.value.reason == webhooks.NO_DELIVERY_ID


def test_the_delivery_log_dedupes_within_its_bound():
    log = webhooks.DeliveryLog(size=2)
    assert log.seen("github", "a") is False
    assert log.seen("github", "a") is True
    assert log.seen("github", "b") is False
    assert log.seen("github", "c") is False  # evicts a
    assert log.seen("github", "a") is False
    assert log.seen("bitbucket", "c") is False  # a different host is a different key


def test_normalised_carries_the_store_fields():
    headers, body = github_delivery("github_pull_request_closed", "pull_request")
    stored = verify("github", headers, body, secrets_for).normalised()
    for name in ("host", "delivery_id", "repository", "pr_number", "kind", "merge_commit",
                 "head_commit"):
        assert name in stored
    assert set(webhooks.KINDS) == {"pr.merged", "pr.closed", "pr.synchronize", "pr.dequeued",
                                   "queue.requested", "check.completed"}
