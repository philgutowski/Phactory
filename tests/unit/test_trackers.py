"""U8 test scenarios for the GitHub Projects and Jira tracker adapters.

Every adapter here is built with an injected transport: DispatchRun stands in
for gh, FakeOpener for urllib's opener. Both answer from the JSON fixtures in
tests/fixtures/tracker and refuse anything unrouted. The six fixtures named
github_* and jira_* without a further suffix are native-relay's, unchanged;
the others add what Phactory reads and the origin never did (labels, and the
per issue project membership answer). The network guard (tests/_nonet.py) is
loaded for every test, and two tests below prove it would catch an adapter
built without its fake.
"""

import base64
import inspect
import json
import re
import shutil
import subprocess
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from _nonet import NetworkReached
from phactory.config.bindings import BindingInvalid, resolve_tracker_credential
from phactory.config.project import Tracker, load_project
from phactory.trackers import github as github_module
from phactory.trackers import jira as jira_module
from phactory.trackers.base import INTERFACE, TRACKER_UNREADABLE, ClosingReference
from phactory.trackers.github import GitHubTracker
from phactory.trackers.jira import JiraTracker
from test_imports import SRC, edges_of, layer

TESTS = Path(__file__).resolve().parents[1]
FIXTURES = TESTS / "fixtures" / "tracker"
CONFIG = TESTS / "fixtures" / "config"
SLUG = "example"

# The values the operator file's example-jira entry names. They live only in
# this environment, never in the project file.
JIRA_ENV = {"EXAMPLE_JIRA_EMAIL": "builder@example.test",
            "EXAMPLE_JIRA_TOKEN": "jira-example-token-value"}


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    root = tmp_path / "phactory"
    (root / "projects").mkdir(parents=True)
    shutil.copy(CONFIG / "operator.toml", root / "operator.toml")
    shutil.copy(CONFIG / "project.toml", root / "projects" / f"{SLUG}.toml")
    monkeypatch.setenv("PHACTORY_HOME", str(root))
    return root


@pytest.fixture
def jira_project():
    return load_project(SLUG)


@pytest.fixture
def github_project(jira_project):
    return replace(jira_project, tracker=Tracker.GITHUB_PROJECTS, repository="o/r",
                   board="o/7", tracker_site="github.com", tracker_credential="example-github")


# Transports.

class DispatchRun:
    """A run(args, timeout=None) for gh that answers from fixtures by subcommand."""

    def __init__(self, issues=None, listing="github_issue_list",
                 membership="github_issue_membership", failure=None, graphql_failure=None):
        self.issues = dict(issues or {})
        self.listing = listing
        self.membership = membership
        self.failure = failure
        self.graphql_failure = graphql_failure
        self.calls = []

    def __call__(self, args, timeout=None):
        args = list(args)
        self.calls.append(args)
        if self.failure is not None:
            return subprocess.CompletedProcess(args, *self.failure)
        if args[:2] == ["gh", "project"]:
            # Served so a regression shows up as a call, not a crash.
            return subprocess.CompletedProcess(args, 0, fixture("github_project_items"), "")
        if args[:3] == ["gh", "api", "graphql"]:
            if self.graphql_failure is not None:
                return subprocess.CompletedProcess(args, *self.graphql_failure)
            body = self.membership
            return subprocess.CompletedProcess(
                args, 0, body if body.lstrip().startswith("{") else fixture(body), "")
        if args[:3] == ["gh", "issue", "list"]:
            return subprocess.CompletedProcess(args, 0, fixture(self.listing), "")
        if args[:3] == ["gh", "issue", "view"]:
            name = self.issues.get(args[3])
            if name is None:
                return subprocess.CompletedProcess(args, 1, "", f"no issue {args[3]}")
            return subprocess.CompletedProcess(args, 0, fixture(name), "")
        raise AssertionError(f"no fixture routed for {args!r}")

    def graphql_queries(self):
        return [arg[len("query="):] for call in self.calls if call[:3] == ["gh", "api", "graphql"]
                for arg in call if arg.startswith("query=")]


class _Body:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for urllib's opener. Routes a URL substring to a fixture file."""

    def __init__(self, routes, error=None):
        self.routes = dict(routes)
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        url = request.get_full_url()
        self.requests.append((url, dict(request.header_items()), timeout))
        if self.error:
            raise self.error
        for fragment, name in self.routes.items():
            if fragment in url:
                return _Body(fixture(name))
        raise AssertionError(f"no fixture routed for {url}")


def jira(project, opener):
    credential = resolve_tracker_credential(SLUG, environ=JIRA_ENV)
    return JiraTracker(project, opener=opener, credential=credential)


def membership(column, state="OPEN", number=7, owner="o", next_page=False):
    return json.dumps({"data": {"repository": {"issue": {"state": state, "projectItems": {
        "nodes": [{"project": {"number": number, "owner": {"login": owner}},
                   "fieldValueByName": {"name": column} if column else None}],
        "pageInfo": {"hasNextPage": next_page, "endCursor": "c2" if next_page else None},
    }}}}})


# GitHub read.

def test_github_read_returns_labels_from_the_fixture(github_project):
    run = DispatchRun(issues={"12": "github_issue_labelled"})
    card = GitHubTracker(github_project, run=run).read("12")
    assert card.labels == ("cloud", "docs")
    assert card.title == "Add the brief renderer"
    assert card.status == "OPEN"
    assert "```phactory" in card.body
    assert run.calls == [["gh", "issue", "view", "12", "--repo", "o/r",
                          "--json", "number,title,body,state,labels"]]


def test_github_read_of_an_origin_fixture_has_no_labels(github_project):
    run = DispatchRun(issues={"13": "github_issue_open"})
    card = GitHubTracker(github_project, run=run).read("13")
    assert (card.key, card.title, card.labels, card.skipped) == (
        "13", "Wire the run loop", (), None)


def test_github_read_failure_is_a_skip_not_a_crash(github_project):
    card = GitHubTracker(github_project, run=DispatchRun()).read("99")
    assert card.skipped == "gh exited 1: no issue 99"


# GitHub ready.

def test_github_ready_filters_by_label_through_the_injected_run(github_project):
    run = DispatchRun()
    cards, reason = GitHubTracker(github_project, run=run).ready()
    assert reason is None
    assert [card.key for card in cards] == ["12", "13"]
    assert all("cloud" in card.labels for card in cards)
    assert cards[0].status == "Ready"
    listing = run.calls[0]
    assert listing[:3] == ["gh", "issue", "list"]
    assert listing[listing.index("--label") + 1] == "cloud"
    assert listing[listing.index("--state") + 1] == "open"
    # One membership query per candidate, never a board listing.
    assert len(run.graphql_queries()) == 2


def test_github_never_lists_a_project_board(github_project):
    run = DispatchRun(issues={"12": "github_issue_labelled"})
    tracker = GitHubTracker(github_project, run=run)
    tracker.ready()
    tracker.candidates()
    tracker.read("12")
    tracker.status("12")
    assert run.calls
    for call in run.calls:
        assert call[:2] != ["gh", "project"], call
        assert "item-list" not in call
    queries = run.graphql_queries()
    assert queries
    for query in queries:
        # The only connection named items is the issue's own projectItems.
        assert "projectV2" not in query
        assert re.search(r"(?<!project)[Ii]tems\s*\(", query) is None
        assert "projectItems(" in query
    # And the module itself names no board listing outside its docstring.
    source = inspect.getsource(github_module).replace(github_module.__doc__, "")
    assert "item-list" not in source
    assert "projectV2(" not in source


def test_github_membership_is_one_query_per_card(github_project):
    run = DispatchRun()
    GitHubTracker(github_project, run=run).status("12")
    assert len(run.calls) == 1
    call = run.calls[0]
    assert call[call.index("-F") + 1] == "number=12"
    assert "owner=o" in call and "name=r" in call and "field=Status" in call


def test_github_ready_listing_failure_is_a_reason(github_project):
    run = DispatchRun(failure=(1, "", "HTTP 502"))
    cards, reason = GitHubTracker(github_project, run=run).ready()
    assert cards == []
    assert reason == "gh exited 1: HTTP 502"


# GitHub budget exhausted.

def test_github_budget_exhausted_on_exit_is_a_tracker_unreadable_skip(github_project):
    run = DispatchRun(graphql_failure=(1, "", "gh: API rate limit exceeded for user ID 1."))
    status = GitHubTracker(github_project, run=run).status("12")
    assert status.skipped.startswith("the card could not be read: GitHub GraphQL budget exhausted")
    assert status.halt_class == TRACKER_UNREADABLE
    assert status.terminal is False


def test_github_budget_exhausted_in_errors_is_a_tracker_unreadable_skip(github_project):
    run = DispatchRun(membership="github_graphql_rate_limited")
    status = GitHubTracker(github_project, run=run).status("12")
    assert "budget exhausted" in status.skipped
    assert status.halt_class == TRACKER_UNREADABLE


def test_github_budget_exhausted_during_ready_is_a_reason(github_project):
    run = DispatchRun(membership="github_graphql_rate_limited")
    cards, reason = GitHubTracker(github_project, run=run).ready()
    assert cards == []
    assert "budget exhausted" in reason


def test_github_transport_that_raises_is_a_reason(github_project):
    def run(args, timeout=None):
        raise subprocess.TimeoutExpired(args, timeout)

    status = GitHubTracker(github_project, run=run).status("12")
    assert "gh could not run" in status.skipped


def test_github_check_style_run_that_raises_on_failure_keeps_stderr(github_project):
    def run(args, timeout=None):
        raise subprocess.CalledProcessError(1, args, "", "API rate limit exceeded")

    status = GitHubTracker(github_project, run=run).status("12")
    assert "budget exhausted" in status.skipped


# GitHub status.

def test_github_status_is_terminal_when_the_column_equals_the_landed_status(github_project):
    status = GitHubTracker(github_project, run=DispatchRun(membership=membership("Done"))).status(
        "12")
    assert (status.status, status.terminal, status.skipped) == ("Done", True, None)


def test_github_status_is_not_terminal_in_another_column(github_project):
    # The fixture's issue sits in Done on board 3 and Ready on board 7, the project's.
    status = GitHubTracker(github_project, run=DispatchRun()).status("12")
    assert (status.status, status.terminal) == ("Ready", False)


def test_github_status_matches_the_board_owner_as_well_as_the_number(github_project):
    run = DispatchRun(membership=membership("Done", owner="someone-else"))
    status = GitHubTracker(github_project, run=run).status("12")
    assert (status.status, status.terminal, status.skipped) == (None, False, None)


def test_github_status_of_a_closed_issue_is_terminal(github_project):
    run = DispatchRun(membership=membership("In review", state="CLOSED"))
    status = GitHubTracker(github_project, run=run).status("12")
    assert status.terminal


def test_github_board_number_alone_means_the_repository_owner(github_project):
    project = replace(github_project, board="7")
    status = GitHubTracker(project, run=DispatchRun(membership=membership("Done"))).status("12")
    assert status.terminal


def test_github_rejects_a_board_that_is_not_a_number(github_project):
    with pytest.raises(ValueError, match="board"):
        GitHubTracker(replace(github_project, board="o/Main board"), run=DispatchRun())


# Jira read.

def test_jira_read_returns_labels_from_fields_labels(jira_project):
    opener = FakeOpener({"/issue/ABC-85": "jira_issue_labelled"})
    card = jira(jira_project, opener).read("ABC-85")
    assert card.labels == ("cloud", "frontend")
    assert (card.key, card.title, card.body, card.status) == (
        "ABC-85", "Add the status page", "Render the page.", "Ready")
    assert "fields=summary%2Cdescription%2Cstatus%2Clabels" in opener.requests[0][0]


def test_jira_read_flattens_the_description(jira_project):
    card = jira(jira_project, FakeOpener({"/issue/ABC-83": "jira_issue_done"})).read("ABC-83")
    assert card.body == (
        "Render the task brief from a template.\nSecond paragraph of the description.")
    assert card.labels == ()


# Jira ready.

def test_jira_ready_runs_the_projects_jql_with_the_operator_credential(jira_project):
    opener = FakeOpener({"/search/jql": "jira_search_ready"})
    cards, reason = jira(jira_project, opener).ready()
    assert reason is None
    # The Done card in the answer is filtered again.
    assert [card.key for card in cards] == ["ABC-85"]
    assert cards[0].labels == ("cloud",)

    url, headers, timeout = opener.requests[0]
    assert url.startswith("https://example.atlassian.net/rest/api/3/search/jql?")
    assert 'project+%3D+%22EX%22+AND+status+%3D+%22Ready%22+AND+labels+%3D+%22cloud%22' in url
    assert timeout == 30

    expected = base64.b64encode(b"builder@example.test:jira-example-token-value").decode()
    assert headers["Authorization"] == "Basic " + expected
    # Built from the operator file's entry and its environment, not the project file.
    project_text = (Path(jira_project.path)).read_text()
    for value in JIRA_ENV.values():
        assert value not in project_text
    assert "EXAMPLE_JIRA" not in project_text


def test_jira_resolves_its_credential_itself_from_the_project_slug(jira_project, monkeypatch):
    for name, value in JIRA_ENV.items():
        monkeypatch.setenv(name, value)
    opener = FakeOpener({"/search/jql": "jira_search_ready"})
    JiraTracker(jira_project, opener=opener).ready()
    expected = base64.b64encode(b"builder@example.test:jira-example-token-value").decode()
    assert opener.requests[0][1]["Authorization"] == "Basic " + expected


def test_jira_with_its_credential_unset_is_binding_invalid(jira_project):
    with pytest.raises(BindingInvalid) as caught:
        JiraTracker(jira_project, opener=FakeOpener({}), environ={})
    assert caught.value.halt_class == "binding_invalid"


def test_jira_never_prints_its_token(jira_project):
    tracker = jira(jira_project, FakeOpener({}))
    assert "jira-example-token-value" not in repr(tracker)


def test_jira_candidates_filter_done(jira_project):
    opener = FakeOpener({"/search/jql": "jira_search"})
    cards, reason = jira(jira_project, opener).candidates()
    assert reason is None
    assert [card.key for card in cards] == ["ABC-84"]
    assert "status+not+in+%28%22done%22%29" in opener.requests[0][0]


# Jira status.

def test_jira_status_is_terminal_when_in_done_statuses(jira_project):
    tracker = jira(jira_project, FakeOpener({"/issue/ABC-83": "jira_issue_done",
                                             "/issue/ABC-84": "jira_issue_open"}))
    done, open_ = tracker.status("ABC-83"), tracker.status("ABC-84")
    assert (done.status, done.terminal) == ("Done", True)
    assert (open_.status, open_.terminal) == ("Backlog", False)


def test_jira_done_statuses_can_be_widened(jira_project):
    credential = resolve_tracker_credential(SLUG, environ=JIRA_ENV)
    tracker = JiraTracker(jira_project, opener=FakeOpener({"/issue/": "jira_issue_open"}),
                          credential=credential, done_statuses=("Done", "Backlog"))
    assert tracker.status("ABC-84").terminal


def test_jira_unreadable_status_is_a_tracker_unreadable_skip(jira_project):
    error = urllib.error.HTTPError("https://example.atlassian.net", 429, "Too many", {}, None)
    status = jira(jira_project, FakeOpener({}, error=error)).status("ABC-83")
    assert status.skipped == "jira returned 429 for /rest/api/3/issue/ABC-83"
    assert status.halt_class == TRACKER_UNREADABLE


# Closing references.

def test_github_closing_reference_carries_the_body_line_and_no_title_token(github_project):
    ref = GitHubTracker(github_project, run=DispatchRun()).closing_reference("12")
    assert ref == ClosingReference(title_token=None, body_line="Closes #12", branch_token=None)


def test_jira_closing_reference_is_the_key_in_title_and_branch(jira_project):
    ref = jira(jira_project, FakeOpener({})).closing_reference("abc-83")
    assert ref == ClosingReference(title_token="ABC-83", body_line=None, branch_token="ABC-83")


# The network guard and the shape of the derived files.

def test_a_github_adapter_without_its_fake_meets_the_network_guard(github_project):
    with pytest.raises(NetworkReached):
        GitHubTracker(github_project).status("12")


def test_a_jira_adapter_without_its_fake_meets_the_network_guard(jira_project):
    credential = resolve_tracker_credential(SLUG, environ=JIRA_ENV)
    with pytest.raises(NetworkReached):
        JiraTracker(jira_project, credential=credential).status("ABC-83")


@pytest.mark.parametrize("module, origin", [
    (github_module, "native-relay/skills/relay/scripts/relay/adapters/github.py"),
    (jira_module, "native-relay/skills/relay/scripts/relay/adapters/jira.py"),
])
def test_the_origin_path_is_in_the_docstring(module, origin):
    assert origin in module.__doc__


@pytest.mark.parametrize("cls", [GitHubTracker, JiraTracker])
def test_no_closeout_filing_or_comment_search_method_remains(cls):
    public = sorted(name for name, _ in inspect.getmembers(cls, inspect.isfunction)
                    if not name.startswith("_"))
    assert public == sorted(INTERFACE)
    every = [name for name, _ in inspect.getmembers(cls, inspect.isfunction)]
    for word in ("closeout", "filing", "comment", "triple", "write"):
        assert not [name for name in every if word in name], word


def test_cards_and_trackers_import_only_config_redact_and_each_other():
    root = SRC
    allowed = {"cards": {"cards"}, "trackers": {"trackers", "config", "redact"}}
    for package, may in allowed.items():
        for path in sorted((root / "phactory" / package).rglob("*.py")):
            for edge in edges_of(root, path):
                assert layer(edge.imported) in may, str(edge)


def test_github_membership_paging_is_bounded(github_project):
    run = DispatchRun(membership=membership("Done", number=3, next_page=True))
    status = GitHubTracker(github_project, run=run).status("12")
    assert "too many projects" in status.skipped
    assert len(run.calls) == github_module.MEMBERSHIP_PAGE_LIMIT
    assert run.calls[1][run.calls[1].index("cursor=c2")] == "cursor=c2"
