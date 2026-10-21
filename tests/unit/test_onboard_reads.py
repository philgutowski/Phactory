"""U28: onboarding reads the host and the tracker instead of asking.

The host adapters run over FakeApi, a stand in for urllib's opener that
routes a method and a URL pattern to a fixture under tests/fixtures/host
(documented shapes, see the README there). The tracker adapters run over
FakeRun (gh) and JiraOpener (urllib), answering from tests/fixtures/tracker.
The board movement check runs on FakeHost, FakeBoard, and FakeScratch, which
keep branches, pull requests, and cards in memory and move the card the way
a board's automation would, or never. The network guard (tests/_nonet.py)
is loaded for every test, so no read here reaches a host.
"""

import io
import json
import re
import shutil
import urllib.error
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from _fakes import FakeRun
from _service_fakes import Clock, FakeServiceRecords
from slipway.config.operator import Host, load_operator
from slipway.config.project import LandingPath, MergeMethod, Tracker, load_project
from slipway.hosts.base import Credential, PullRequest
from slipway.hosts.bitbucket import BitbucketHost
from slipway.hosts.github import GitHubHost
from slipway.onboard import checks, machine
from slipway.onboard.checks import Draft, LiveProbe
from slipway.onboard.questions import BY_ID, QUESTIONS
from slipway.trackers.base import SCRATCH_INTERFACE, SCRATCH_TITLE, TrackerStatus
from slipway.trackers.github import GitHubScratch, GitHubTracker
from slipway.trackers.jira import JiraScratch, JiraTracker

TESTS = Path(__file__).resolve().parents[1]
HOST_FIXTURES = TESTS / "fixtures" / "host"
TRACKER_FIXTURES = TESTS / "fixtures" / "tracker"
SLUG = "example"
OPERATOR = "user_operator_example"
TOKEN = "host-token-value-for-tests-only"
HEAD = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
PARENT = "da38e6c2ec96b89748d4f34ae3cf5b07e5824f7e"

APP = Credential("example-app", Host.GITHUB, "github_app", TOKEN)
BB = Credential("example-bitbucket", Host.BITBUCKET, "repository_access_token", TOKEN)


def host_fixture(name: str) -> str:
    return (HOST_FIXTURES / f"{name}.json").read_text()


def tracker_fixture(name: str) -> str:
    return (TRACKER_FIXTURES / f"{name}.json").read_text()


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "slipway"
    (root / "projects").mkdir(parents=True)
    shutil.copy(TESTS / "fixtures" / "onboard" / "operator.toml", root / "operator.toml")
    shutil.copy(TESTS / "fixtures" / "config" / "project.toml",
                root / "projects" / f"{SLUG}.toml")
    monkeypatch.setenv("SLIPWAY_HOME", str(root))
    return root


@pytest.fixture
def project(root):
    """The fixture project: GitHub host, Jira board EX, landing pinned."""
    return load_project(SLUG, root)


@pytest.fixture
def operator(root):
    return load_operator(root)


# Transports.

class _Answer:
    def __init__(self, status, text):
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._raw = (text or "").encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """Routes (method, URL regex) to (status, fixture). First match wins."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def route(self, method, pattern, fixture=None, status=200, body=None):
        text = host_fixture(fixture) if fixture else body
        self.routes.append((method, re.compile(pattern), status, text))
        return self

    def open(self, request, timeout=None):
        url = request.get_full_url()
        sent = json.loads(request.data.decode()) if request.data else None
        self.requests.append((request.get_method(), url, sent))
        for method, pattern, status, text in self.routes:
            if method == request.get_method() and pattern.search(url):
                if status >= 400:
                    raise urllib.error.HTTPError(url, status, "refused", {},
                                                 io.BytesIO((text or "").encode()))
                return _Answer(status, text)
        raise AssertionError(f"no route for {request.get_method()} {url}")

    def sent(self, method, fragment):
        return [r for r in self.requests if r[0] == method and fragment in r[1]]


class JiraOpener(FakeApi):
    """FakeApi with tracker fixtures, for the Jira adapter's urllib reads and writes."""

    def route(self, method, pattern, fixture=None, status=200, body=None):
        text = tracker_fixture(fixture) if fixture else body
        self.routes.append((method, re.compile(pattern), status, text))
        return self


@dataclass(frozen=True)
class JiraCredential:
    site: str = "example.atlassian.net"
    email: str = "operator@example.test"
    token: str = "jira-token-value-for-tests-only"


def github_gate_api(namespace_ruleset="github_api_ruleset_namespace"):
    """A repository whose ruleset requires ci on main and holds the identity to slipway/**."""
    return (FakeApi()
            .route("GET", r"/repos/example-org/example-app$", "github_api_repository")
            .route("GET", r"/rules/branches/main", "github_api_rules_no_queue")
            .route("GET", r"/branches/main/protection/required_status_checks",
                   "github_api_branch_not_protected", status=404)
            .route("GET", r"/rulesets\?", "github_api_rulesets")
            .route("GET", r"/rulesets/25166140\?", "github_api_ruleset_main")
            .route("GET", r"/rulesets/25166141\?", namespace_ruleset))


def bitbucket_gate_api(permissions="bitbucket_api_push_permissions"):
    return (FakeApi()
            .route("GET", r"/repositories/example-ws/example-app$", "bitbucket_api_repository")
            .route("GET", r"branch-restrictions\?kind=require_passing_builds_to_merge",
                   "bitbucket_api_merge_checks")
            .route("GET", r"branch-restrictions\?kind=push", permissions)
            .route("GET", r"/2\.0/user$", "bitbucket_api_user"))


def probe_over(operator, host, tracker=None, **kwargs) -> LiveProbe:
    return LiveProbe(operator, lambda project: tracker, lambda project: host, ("cli",),
                     **kwargs)


# The external gate on captured fixtures (AE6, KTD6).

def test_a_github_ruleset_requiring_ci_that_holds_the_identity_passes_the_gate(
        operator, project):
    host = GitHubHost(project, APP, opener=github_gate_api())
    result = checks.external_gate(probe_over(operator, host), project, {})

    assert result.ok, result
    assert result.attest == {}
    assert result.evidence["required_check"] == "the base branch requires ci"
    assert "slipway-namespace" in result.evidence["restriction"]
    # A ruleset only being evaluated is not read: it enforces nothing.
    assert not host._client._opener.sent("GET", "rulesets/25166142")


def test_a_github_ruleset_that_lets_the_identity_bypass_fails_with_the_fix(operator, project):
    host = GitHubHost(project, APP,
                      opener=github_gate_api("github_api_ruleset_namespace_bypassed"))
    result = checks.external_gate(probe_over(operator, host), project, {})

    assert not result.ok
    assert result.question is None  # the fix is on the host, then recheck
    assert "bypass list of ruleset slipway-namespace" in result.missing
    assert result.next_step == checks.restriction_step(project)
    assert "refs/heads/slipway/**" in result.next_step


def test_a_ruleset_that_leaves_the_default_branch_open_does_not_hold_the_identity(
        operator, project):
    api = github_gate_api()
    loose = json.loads(host_fixture("github_api_ruleset_namespace"))
    loose["conditions"]["ref_name"]["exclude"].append("~DEFAULT_BRANCH")
    api.routes.insert(0, ("GET", re.compile(r"/rulesets/25166141\?"), 200, json.dumps(loose)))
    found = probe_over(operator, GitHubHost(project, APP, opener=api)).restriction(project)
    assert found.ok is False
    assert "no active ruleset closes every branch outside" in found.detail


def bitbucket(project):
    return replace(project, host=Host.BITBUCKET, repository="example-ws/example-app",
                   host_credential="example-bitbucket", landing_path=None,
                   merge_method=None)


def test_bitbucket_merge_checks_and_branch_permissions_pass_the_gate(operator, project):
    project = replace(bitbucket(project), landing_path=LandingPath.BITBUCKET_QUEUE,
                      merge_method=MergeMethod.SQUASH)
    host = BitbucketHost(project, BB, opener=bitbucket_gate_api())
    result = checks.external_gate(probe_over(operator, host), project, {})

    assert result.ok, result
    assert result.attest == {}
    assert result.evidence["required_check"] == "the base branch requires 1 passing build"
    assert "not one" in result.evidence["restriction"]


def test_a_bitbucket_permission_that_lets_the_token_push_fails_with_the_fix(operator, project):
    project = bitbucket(project)
    host = BitbucketHost(project, BB,
                         opener=bitbucket_gate_api("bitbucket_api_push_permissions_token"))
    result = checks.external_gate(probe_over(operator, host), project, {})

    assert not result.ok
    assert "lets the token's identity push to main" in result.missing
    assert result.next_step == checks.restriction_step(project)
    assert "slipway/** branches" in result.next_step


def test_bitbucket_with_no_merge_check_fails_the_gate_with_the_fix(operator, project):
    project = bitbucket(project)
    api = bitbucket_gate_api()
    api.routes.insert(0, ("GET", re.compile(r"kind=require_passing_builds_to_merge"), 200,
                          host_fixture("bitbucket_api_merge_checks_none")))
    result = checks.external_gate(probe_over(operator, BitbucketHost(project, BB, opener=api)),
                                  project, {})
    assert not result.ok
    assert "requires no check before merge" in result.missing
    assert result.next_step == checks.gate_step(project)


# A host that cannot answer on its plan (the attestation stays as the fallback).

def plan_without_rulesets_api():
    return (FakeApi()
            .route("GET", r"/repos/example-org/example-app$", "github_api_repository")
            .route("GET", r"/rules/branches/main", "github_api_not_found", status=404)
            .route("GET", r"/protection/required_status_checks", "github_api_upgrade",
                   status=403)
            .route("GET", r"/rulesets\?", "github_api_not_found", status=404))


def test_a_host_without_rulesets_on_its_plan_answers_none(operator, project):
    probe = probe_over(operator, GitHubHost(project, APP, opener=plan_without_rulesets_api()))
    assert probe.required_check(project).ok is None
    assert probe.restriction(project).ok is None


class ReadyBoard:
    """A tracker whose ready list and board read answer, and nothing more."""

    def ready(self):
        return [], None

    def read_board(self):
        return True, "board example-org/7 is open"


def world_ports(root, probe):
    clock = Clock()
    return machine.Ports(root=root, operator=lambda: load_operator(root), probe=probe,
                         records=FakeServiceRecords(clock), clock=clock)


GITHUB = {
    "host": "github", "repository": "example-org/example-app",
    "host_credential": "example-app", "tracker": "github_projects",
    "tracker_site": "github.com", "tracker_credential": "example-github",
    "board": "example-org/7", "lane_label": "slipway", "ready_status": "",
    "in_progress_status": "", "in_review_status": "", "landed_status": "",
    "other_dispatcher": "", "gate_command": "make check", "allowed_builders": "cli",
    "roles.builder.account": "max-sub", "roles.builder.model": "model-large",
    "roles.builder.effort": "", "roles.builder.time_budget_minutes": "",
    "roles.judge.account": "api-main", "roles.judge.model": "model-medium",
    "roles.judge.effort": "", "roles.judge.time_budget_minutes": "",
    "builder_worker": "laptop", "checkout": "", "builder_tools": "", "auto_merge": "",
    "landing_path": "",
    "merge_method": "", "branch_template": "", "pull_request_title": "", "build_cap": "",
    "daily_dispatch_cap": "", "trace_project": "example-traces", "trace_detail": "",
    "allowed_subjects": OPERATOR, "evals_path": "", "trivial_card_target": "",
    "runner_labels": "", "machine_bound_tests": "", "independence": "yes",
    "verification": "yes", "attest.board_moves": "yes",
}


def onboard(ports, answers) -> dict:
    """Answer every question asked from answers; return the first one answers do not cover."""
    result = machine.start(ports, SLUG, OPERATOR)
    for _ in range(3 * len(QUESTIONS)):
        qid = (result.get("question") or {}).get("id")
        if qid not in answers:
            return result
        result = machine.answer(ports, SLUG, qid, answers[qid], OPERATOR)
        assert result["outcome"] != machine.REFUSED, result
    raise AssertionError(f"onboard kept asking: {result}")


def test_onboarding_asks_the_attestation_when_the_plan_has_no_rulesets(tmp_path, operator):
    home = tmp_path / "home"
    home.mkdir()
    shutil.copy(TESTS / "fixtures" / "onboard" / "operator.toml", home / "operator.toml")
    api = plan_without_rulesets_api()
    probe = LiveProbe(load_operator(home), lambda project: ReadyBoard(),
                      lambda project: GitHubHost(project, APP, opener=api), ("cli",),
                      draft_tracker_for=lambda draft: ReadyBoard(),
                      scratch_for=lambda project: None)
    result = onboard(world_ports(home, probe), GITHUB)

    # board_moves was attested (no scratch class); the gate's first read is next.
    assert result["outcome"] == machine.NEEDS_FIX
    assert result["property"] == checks.EXTERNAL_GATE
    assert result["question"]["id"] == "attest.required_check"
    assert "cannot read" in result["missing"]


# The board, label, and status questions offer live lists.

def listing_run():
    run = FakeRun()
    run.reply(["gh", "project", "list"], tracker_fixture("github_project_list"))
    run.reply(["gh", "label", "list"], tracker_fixture("github_label_list"))
    run.reply(["gh", "project", "field-list"], tracker_fixture("github_project_field_list"))
    return run


def test_the_board_label_and_status_questions_offer_what_github_lists(root, operator):
    run = listing_run()
    probe = LiveProbe(operator, None, None, ("cli",),
                      draft_tracker_for=lambda draft: GitHubTracker(Draft.of(draft), run))
    ports = world_ports(root, probe)
    draft = {"tracker": "github_projects", "repository": "example-org/example-app"}

    # A closed board is not offered.
    assert machine.choices(ports, BY_ID["board"], draft, OPERATOR) == ("example-org/7",)
    draft["board"] = "example-org/7"
    assert machine.choices(ports, BY_ID["lane_label"], draft, OPERATOR) == (
        "slipway", "bug", "cloud")
    assert machine.choices(ports, BY_ID["ready_status"], draft, OPERATOR) == (
        "Ready", "In progress", "In review", "Done")
    assert ["gh", "project", "field-list", "7", "--owner", "example-org"] == \
        run.calls[-1][:6]


def test_the_board_label_and_status_questions_offer_what_jira_lists(root, operator):
    opener = (JiraOpener()
              .route("GET", r"/project/search", "jira_project_search")
              .route("GET", r"/rest/api/3/label", "jira_labels")
              .route("GET", r"/project/EX/statuses", "jira_project_statuses"))
    probe = LiveProbe(operator, None, None, ("cli",), draft_tracker_for=lambda draft:
                      JiraTracker(Draft.of(draft), opener=opener, credential=JiraCredential()))
    ports = world_ports(root, probe)
    draft = {"tracker": "jira", "repository": "example-ws/example-app"}

    assert machine.choices(ports, BY_ID["board"], draft, OPERATOR) == ("EX", "OPS")
    draft["board"] = "EX"
    assert machine.choices(ports, BY_ID["lane_label"], draft, OPERATOR) == (
        "slipway", "bug", "frontend")
    # Each status once, though two issue types share Ready and Done.
    assert machine.choices(ports, BY_ID["in_review_status"], draft, OPERATOR) == (
        "Ready", "In Progress", "In Review", "Done")


def test_a_listing_with_no_credential_offers_nothing_and_takes_a_typed_answer(root, operator):
    probe = LiveProbe(operator, None, None, ("cli",), environ={})
    draft = {"tracker": "jira", "tracker_credential": "example-jira", "board": "EX"}
    assert probe.boards(draft) == ()
    assert probe.statuses(draft) == ()


def test_the_board_read_says_whether_the_board_exists(project):
    run = FakeRun()
    run.reply(["gh", "project", "view", "7"], tracker_fixture("github_project_view"))
    run.reply(["gh", "project", "view", "9"], stderr="Could not resolve to a ProjectV2 with "
              "the number 9.", returncode=1)
    github = replace(project, tracker=Tracker.GITHUB_PROJECTS, board="example-org/7")
    assert GitHubTracker(github, run).read_board()[0] is True
    gone = GitHubTracker(replace(github, board="example-org/9"), run).read_board()
    assert gone[0] is False and "not found" in gone[1]

    opener = (JiraOpener().route("GET", r"/project/EX$", "jira_project")
              .route("GET", r"/project/GONE$", status=404, body='{"errorMessages": []}'))
    assert JiraTracker(project, opener=opener, credential=JiraCredential()).read_board()[0]
    missing = JiraTracker(replace(project, board="GONE"), opener=opener,
                          credential=JiraCredential()).read_board()
    assert missing[0] is False


# The board movement check (KTD27, AE13).

@dataclass
class FakeBoard:
    """The tracker: a card's status, moved by FakeHost's events when automation is on."""

    project: object
    automation: bool = True
    status_of: dict = field(default_factory=dict)

    def ready(self):
        return [], None

    def read_board(self):
        return True, f"board {self.project.board} is open"

    def status(self, key):
        return TrackerStatus(self.status_of.get(key, "Ready"), False)

    def closing_reference(self, key):
        return GitHubTracker.closing_reference(None, key)

    def moved(self, name: str, status: str) -> None:
        if not self.automation:
            return
        for key in self.status_of:
            if f"/{key}-" in name or name.endswith(f"/{key}"):
                self.status_of[key] = status


@dataclass
class FakeScratch:
    board: FakeBoard
    next_key: int = 41
    cards: dict = field(default_factory=dict)  # key to "open" or "closed"

    def open(self, title):
        key = str(self.next_key)
        self.next_key += 1
        self.cards[key] = "open"
        self.board.status_of[key] = "Ready"
        return key, None

    def close(self, key):
        self.cards[key] = "closed"
        return None

    def leftovers(self, title):
        assert title == SCRATCH_TITLE
        return tuple(k for k, state in self.cards.items() if state == "open"), None


@dataclass
class FakeHost:
    board: FakeBoard
    branches_: dict = field(default_factory=dict)
    pulls: dict = field(default_factory=dict)  # number to PullRequest
    events: list = field(default_factory=list)

    def base_commits(self):
        return "main", HEAD, PARENT

    def create_branch(self, name, sha):
        self.events.append(("create", name))
        self.branches_[name] = sha
        self.board.moved(name, self.board.project.in_progress_status)

    def open_pull_request(self, branch, title, reference, body="", base=None):
        number = 100 + len(self.pulls)
        self.events.append(("open", branch, base, reference.body_line))
        self.pulls[number] = PullRequest(number, "open", False, HEAD, head_ref=branch,
                                         base_ref=base, title=title)
        self.board.moved(branch, self.board.project.in_review_status)
        return self.pulls[number]

    def branches(self, prefix):
        return tuple(name for name in self.branches_ if name.startswith(prefix))

    def delete_branch(self, name):
        self.events.append(("delete", name))
        return self.branches_.pop(name, None) is not None

    def open_pull_requests(self, base):
        return tuple(pr for pr in self.pulls.values() if pr.state == "open"
                     and pr.base_ref == base)

    def close_pull_request(self, pr):
        self.events.append(("close", pr.number))
        self.pulls[pr.number] = replace(pr, state="closed")


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


def board_world(operator, project, automation=True):
    board = FakeBoard(project, automation)
    host, scratch, time = FakeHost(board), FakeScratch(board), FakeTime()
    probe = LiveProbe(operator, lambda p: board, lambda p: host, ("cli",),
                      scratch_for=lambda p: scratch, sleep=time.sleep, clock=time.clock)
    return probe, host, scratch, time


def github_board(project):
    return replace(project, tracker=Tracker.GITHUB_PROJECTS, board="example-org/7",
                   tracker_site="github.com", tracker_credential="example-github")


def assert_clean(host, scratch):
    assert host.branches_ == {}
    assert not [pr for pr in host.pulls.values() if pr.state == "open"]
    assert set(scratch.cards.values()) == {"closed"}


def test_a_board_that_moves_the_scratch_card_passes_durable_state(operator, project):
    project = github_board(project)
    probe, host, scratch, time = board_world(operator, project)
    result = checks.durable_state(probe, project, {})

    assert result.ok, result
    assert "In progress after the branch" in result.evidence["moves"]
    assert "In review after pull request #100" in result.evidence["moves"]
    # The base carries no key, so creating it moves nothing; the scratch
    # branch is the project's own template for the scratch card.
    assert host.events[:2] == [("create", "slipway/onboard-scratch-base"),
                               ("create", "slipway/41-scratch")]
    assert host.events[2] == ("open", "slipway/41-scratch", "slipway/onboard-scratch-base",
                              "Closes #41")
    assert time.slept == 0
    assert_clean(host, scratch)


def test_a_board_that_never_moves_fails_with_the_template_and_leaves_nothing(
        operator, project):
    project = github_board(project)
    probe, host, scratch, time = board_world(operator, project, automation=False)
    result = checks.durable_state(probe, project, {})

    assert not result.ok
    assert "did not move the card to in progress or in review" in result.missing
    assert result.next_step == checks.board_step(project)
    assert checks.BOARD_TEMPLATE in result.next_step  # AE13
    # The wait is bounded: one full wait per move, then the run gives up.
    assert time.slept == pytest.approx(2 * checks.MOVE_WAIT_SECONDS)
    assert_clean(host, scratch)


def test_a_jira_board_that_never_moves_names_the_automation_rules(operator, project):
    probe, host, scratch, _ = board_world(operator, project, automation=False)
    result = checks.durable_state(probe, project, {})
    assert not result.ok
    assert "Jira automation rules on EX" in result.next_step
    assert_clean(host, scratch)


def test_a_scratch_branch_left_by_an_earlier_run_is_removed_before_the_next_starts(
        operator, project):
    project = github_board(project)
    probe, host, scratch, _ = board_world(operator, project)
    # A crashed run left its card, both branches, and its pull request.
    scratch.cards["7"] = "open"
    host.branches_.update({"slipway/onboard-scratch-base": PARENT,
                           "slipway/7-scratch": HEAD, "slipway/94-12": HEAD})
    host.pulls[90] = PullRequest(90, "open", False, HEAD, head_ref="slipway/7-scratch",
                                 base_ref="slipway/onboard-scratch-base")

    assert probe.board_moves(project).in_review

    first_create = host.events.index(("create", "slipway/onboard-scratch-base"))
    before = host.events[:first_create]
    assert ("close", 90) in before
    assert ("delete", "slipway/7-scratch") in before
    assert ("delete", "slipway/onboard-scratch-base") in before
    assert scratch.cards["7"] == "closed"
    # A real card's branch in the namespace is never touched.
    assert host.branches_ == {"slipway/94-12": HEAD}


def test_a_board_seen_moving_is_not_checked_twice_in_one_session(operator, project):
    project = github_board(project)
    probe, host, _, _ = board_world(operator, project)
    probe.board_moves(project)
    opened = len(host.pulls)
    probe.board_moves(project)
    assert len(host.pulls) == opened


def test_a_template_without_a_prefix_is_not_watched(operator, project):
    project = replace(github_board(project), branch_template="{key}-{build}")
    probe, host, _, _ = board_world(operator, project)
    assert probe.board_moves(project).in_progress is None
    assert host.events == []


# The adapters' scratch writes, on fixtures.

def test_github_scratch_writes_read_and_send_what_the_api_expects(project):
    api = (FakeApi()
           .route("GET", r"/repos/example-org/example-app$", "github_api_repository")
           .route("GET", r"/commits/main$", "github_api_commit")
           .route("GET", r"/git/matching-refs/heads/slipway/", "github_api_matching_refs")
           .route("DELETE", r"/git/refs/heads/slipway/gone$", "github_api_not_found",
                  status=422)
           .route("DELETE", r"/git/refs/heads/", status=204, body="")
           .route("PATCH", r"/pulls/100$", "github_api_pull_request"))
    host = GitHubHost(project, APP, opener=api)

    assert host.base_commits() == ("main", HEAD, PARENT)
    assert host.branches("slipway/") == ("slipway/onboard-scratch-base", "slipway/EX-41-scratch")
    assert host.delete_branch("slipway/EX-41-scratch") is True
    assert host.delete_branch("slipway/gone") is False
    host.close_pull_request(100)
    assert api.sent("PATCH", "/pulls/100")[0][2] == {"state": "closed"}


def test_bitbucket_scratch_writes_read_and_send_what_the_api_expects(project):
    branch = json.loads(host_fixture("bitbucket_api_branch"))
    branch["target"]["parents"] = [{"hash": PARENT, "type": "commit"}]
    api = (FakeApi()
           .route("GET", r"/repositories/example-ws/example-app$", "bitbucket_api_repository")
           .route("GET", r"/refs/branches/main$", body=json.dumps(branch))
           .route("POST", r"/pullrequests/7/decline$", "bitbucket_api_pullrequest")
           .route("DELETE", r"/refs/branches/", status=204, body=""))
    host = BitbucketHost(bitbucket(project), BB, opener=api)

    name, head, parent = host.base_commits()
    assert (name, parent) == ("main", PARENT) and head == branch["target"]["hash"]
    host.close_pull_request(7)
    assert api.sent("POST", "/pullrequests/7/decline")
    assert host.delete_branch("slipway/onboard-scratch-base") is True


def test_the_github_scratch_card_is_an_issue_without_the_lane_label(project):
    run = FakeRun()
    run.reply(["gh", "issue", "create"],
              "https://github.com/example-org/example-app/issues/41\n")
    run.reply(["gh", "issue", "close"], "✓ Closed issue example-org/example-app#41\n")
    scratch = GitHubScratch(github_board(project), run)

    assert scratch.open(SCRATCH_TITLE) == ("41", None)
    assert "--label" not in run.calls[0]
    assert scratch.close("41") is None
    assert run.calls[1][:4] == ["gh", "issue", "close", "41"]
    assert run.calls[1][-2:] == ["--reason", "not planned"]


def test_the_jira_scratch_card_is_filed_in_a_standard_type_and_closed_to_done(project):
    opener = (JiraOpener()
              .route("GET", r"/createmeta/EX/issuetypes", "jira_issue_types")
              .route("POST", r"/rest/api/3/issue$", "jira_issue_created", status=201)
              .route("GET", r"/issue/EX-41/transitions", "jira_transitions")
              .route("POST", r"/issue/EX-41/transitions", status=204, body=""))
    scratch = JiraScratch(project, opener=opener, credential=JiraCredential())

    assert scratch.open(SCRATCH_TITLE) == ("EX-41", None)
    filed = opener.sent("POST", "/rest/api/3/issue")[0][2]
    assert filed["fields"]["issuetype"] == {"id": "10001"}  # Task, not the sub task
    assert "labels" not in filed["fields"]
    assert scratch.close("EX-41") is None
    assert opener.sent("POST", "/transitions")[0][2] == {"transition": {"id": "31"}}


@pytest.mark.parametrize("cls", [GitHubScratch, JiraScratch])
def test_a_scratch_class_does_only_its_three_things(cls):
    import inspect

    public = {name for name, _ in inspect.getmembers(cls, inspect.isfunction)
              if not name.startswith("_")}
    assert public == set(SCRATCH_INTERFACE)


def test_the_github_sweep_follows_every_page_of_branches(project):
    first = [{"ref": f"refs/heads/slipway/{n}-12", "object": {"sha": HEAD}} for n in range(100)]
    second = [{"ref": "refs/heads/slipway/onboard-scratch-base", "object": {"sha": PARENT}}]
    api = FakeApi().route("GET", r"matching-refs/heads/slipway/\?page=2",
                          body=json.dumps(second))
    api.route("GET", r"matching-refs/heads/slipway/\?per_page", body=json.dumps(first))
    next_page = ("https://api.github.com/repos/example-org/example-app/git/matching-refs/"
                 "heads/slipway/?page=2")
    original = api.open

    def open_with_link(request, timeout=None):
        answer = original(request, timeout)
        if "per_page" in request.get_full_url():
            answer.headers["Link"] = f'<{next_page}>; rel="next", <{next_page}>; rel="last"'
        return answer

    api.open = open_with_link
    found = GitHubHost(project, APP, opener=api).branches("slipway/")
    assert len(found) == 101
    assert found[-1] == "slipway/onboard-scratch-base"
