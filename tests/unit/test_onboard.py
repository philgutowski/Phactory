"""U19: onboard's state machine and its terminal client, on fake adapters.

FakeProbe stands in for the host and the tracker: it lists boards, labels,
statuses, and builders, and answers each property read from its fields, so
a test sets a field to make a check fail. The records are the service fakes
(tests/_service_fakes.py), which keep audit rows and builds in memory. No
test reaches a network, and the guard below fails any test that would start
a process, so "nothing ran on the control plane" is checked on every one.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from _service_fakes import Clock, FakeServiceRecords
from slipway.config.operator import load_operator
from slipway.config.project import load_project
from slipway.hosts.base import LIVE, Landing, ProbeUndetermined, pinned_landing
from slipway.onboard import checks, machine, terminal
from slipway.onboard.checks import Finding, Moves
from slipway.onboard.questions import BY_ID, QUESTIONS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "onboard"
OPERATOR = "user_operator_example"
MANAGER = "agent_manager_example"
STRANGER = "agent_stranger"
SLUG = "example"
TOKEN = "ghp_" + "a1B2c3D4e5" * 4

# The Bitbucket repository with a Jira board, answer by answer. An empty
# string takes the question's default, or leaves an optional key out.
BITBUCKET = {
    "host": "bitbucket", "repository": "example-workspace/example-app",
    "host_credential": "example-bitbucket", "tracker": "jira",
    "tracker_site": "example.atlassian.net", "tracker_credential": "example-jira",
    "board": "EX", "lane_label": "slipway", "ready_status": "", "in_progress_status": "",
    "in_review_status": "", "landed_status": "", "other_dispatcher": "",
    "gate_command": "make check", "allowed_builders": "cli",
    "roles.builder.account": "max-sub", "roles.builder.model": "model-large",
    "roles.builder.effort": "", "roles.builder.time_budget_minutes": "",
    "roles.judge.account": "api-main", "roles.judge.model": "model-medium",
    "roles.judge.effort": "", "roles.judge.time_budget_minutes": "",
    "builder_worker": "laptop", "checkout": "", "auto_merge": "",
    "landing_path": "bitbucket_queue", "merge_method": "squash", "branch_template": "",
    "pull_request_title": "", "build_cap": "", "daily_dispatch_cap": "",
    "trace_project": "example-traces", "trace_detail": "",
    "allowed_subjects": f"{OPERATOR}, {MANAGER}", "evals_path": "",
    "trivial_card_target": "", "runner_labels": "", "machine_bound_tests": "",
    "independence": "yes", "verification": "yes",
}
GITHUB = {**BITBUCKET, "host": "github", "repository": "example-org/example-app",
          "host_credential": "example-app", "tracker": "github_projects",
          "tracker_site": "github.com", "tracker_credential": "example-github",
          "board": "example-org/7", "landing_path": "", "merge_method": ""}


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    """Onboard never runs a command on the control plane (KTD18)."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"a process was started: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("os.system", refuse)


@dataclass
class FakeProbe:
    boards_: tuple = ("EX", "OPS", "example-org/7")
    labels_: tuple = ("slipway", "bug")
    statuses_: tuple = ("Ready", "In Progress", "In Review", "Done")
    builders_: tuple = ("cli", "managed_agents")
    repository_ok: bool | None = True
    tracker_ok: bool | None = True
    moves: Moves = Moves(True, True, "moved on branch and pull request")
    required: Finding = Finding(True, "check gate required")
    restricted: Finding = Finding(True, "identity limited to refs/heads/slipway/**")
    output: str = ""
    reads: list = field(default_factory=list)

    def builders(self):
        return self.builders_

    def boards(self, draft):
        return self.boards_

    def labels(self, draft):
        return self.labels_

    def statuses(self, draft):
        return self.statuses_

    def repository(self, project):
        return Finding(self.repository_ok, "repository read")

    def board(self, project):
        if project.board in self.boards_:
            return Finding(True, f"board {project.board} has a Status field")
        return Finding(False, f"no board {project.board}")

    def tracker(self, project):
        return Finding(self.tracker_ok, "the ready list was read")

    def board_moves(self, project):
        self.reads.append("board_moves")
        return self.moves

    def required_check(self, project):
        return self.required

    def restriction(self, project):
        return self.restricted

    def landing(self, project):
        pinned = pinned_landing(project)
        if pinned:
            return pinned
        if project.host == "bitbucket":
            raise ProbeUndetermined("pin landing_path and merge_method")
        return Landing("auto_merge_strict", "squash", LIVE, LIVE, "ruleset read")

    def gate_output(self, project, pr_number):
        return self.output


@dataclass
class World:
    root: Path
    probe: FakeProbe
    records: FakeServiceRecords
    ports: machine.Ports

    @property
    def project_file(self) -> Path:
        return self.root / "projects" / f"{SLUG}.toml"


@pytest.fixture
def world(tmp_path) -> World:
    root = tmp_path / "home"
    root.mkdir()
    shutil.copy(FIXTURES / "operator.toml", root / "operator.toml")
    clock = Clock()
    records = FakeServiceRecords(clock)
    probe = FakeProbe()
    ports = machine.Ports(root=root, operator=lambda: load_operator(root), probe=probe,
                          records=records, clock=clock)
    return World(root, probe, records, ports)


def onboard(world: World, answers: dict, actor: str = OPERATOR) -> dict:
    """Answer every question the machine asks from answers; return the first other result."""
    result = machine.start(world.ports, SLUG, actor)
    for _ in range(3 * len(QUESTIONS)):  # a bound, so a question asked forever fails
        if result["outcome"] != machine.QUESTION:
            return result
        qid = result["question"]["id"]
        result = machine.answer(world.ports, SLUG, qid, answers[qid], actor)
        assert result["outcome"] != machine.REFUSED, result
    raise AssertionError(f"onboard kept asking: {result}")


def scripted(lines):
    feed = iter(lines)

    def read(prompt):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    return read


# The terminal on a Bitbucket repository with a Jira board.

def test_terminal_writes_the_hand_written_project_file(world):
    out = []
    lines = [BITBUCKET[q.id] for q in QUESTIONS]
    code = terminal.run(world.ports, SLUG, OPERATOR, scripted(lines), out.append)

    assert code == 1  # it stops at the trivial card, which the operator files
    written = world.project_file.read_text()
    expected = (FIXTURES / "bitbucket-jira.toml").read_text().replace("ACTOR", OPERATOR)
    assert written == expected
    assert "Every check passed. File this card:" in out
    assert any("Slipway onboarding: add docs/slipway-onboarding.md" in line for line in out)
    project = load_project(SLUG, world.root)
    assert project.host == "bitbucket" and project.tracker == "jira"


def test_terminal_takes_a_choice_by_number(world):
    lines = ["2", "example-workspace/example-app", "1"]  # bitbucket, then example-bitbucket
    terminal.run(world.ports, SLUG, OPERATOR, scripted(lines), [].append)
    answers = machine.status(world.ports, SLUG)["answers"]
    assert answers["host"] == "bitbucket"
    assert answers["host_credential"] == "example-bitbucket"


def test_a_bare_board_number_is_the_board_not_a_choice(world):
    lines = [GITHUB[q.id] for q in QUESTIONS[:6]] + ["2"]
    terminal.run(world.ports, SLUG, OPERATOR, scripted(lines), [].append)
    assert machine.status(world.ports, SLUG)["answers"]["board"] == "2"


def test_a_yes_no_answer_of_the_wrong_type_is_refused(world):
    machine.start(world.ports, SLUG, OPERATOR)
    result = machine.answer(world.ports, SLUG, "other_dispatcher", ["yes"], OPERATOR)
    assert result["outcome"] == machine.REFUSED


def test_the_project_file_appears_only_once_whole(world):
    machine.start(world.ports, SLUG, OPERATOR)
    for qid in ("host", "repository", "host_credential"):
        machine.answer(world.ports, SLUG, qid, BITBUCKET[qid], OPERATOR)
    assert not world.project_file.exists()
    state = json.loads((world.root / "onboard" / f"{SLUG}.json").read_text())
    assert state["answers"]["repository"] == "example-workspace/example-app"
    assert state["by"]["repository"]["actor"] == OPERATOR
    assert state["verified"] is False


# AE6: the external gate.

def test_no_required_check_stops_at_the_gate_with_the_fix(world):
    world.probe.required = Finding(False, "no branch restriction requires a build")
    result = onboard(world, BITBUCKET)

    assert result["outcome"] == machine.NEEDS_FIX
    assert result["property"] == checks.EXTERNAL_GATE
    assert "an external gate that refuses broken changes" in result["missing"]
    assert "require a passing build before merge" in result["next_step"]
    assert result["question"]["id"] == "recheck"


def test_an_unrestricted_identity_stops_at_the_gate_with_the_fix(world):
    world.probe.restricted = Finding(False, "the token can write to main")
    result = onboard(world, GITHUB)

    assert result["outcome"] == machine.NEEDS_FIX
    assert result["property"] == checks.EXTERNAL_GATE
    assert "not restricted" in result["missing"]
    assert "refs/heads/slipway/**" in result["next_step"]

    world.probe.restricted = Finding(True, "restricted now")
    again = machine.answer(world.ports, SLUG, "recheck", "yes", OPERATOR)
    assert again["outcome"] == machine.FILE_CARD


def test_the_restriction_follows_the_branch_template(world):
    world.probe.restricted = Finding(False, "the token can write to main")
    result = onboard(world, {**GITHUB, "branch_template": "agent/{key}-{slug}"})
    assert "refs/heads/agent/**" in result["next_step"]


def test_a_template_with_no_prefix_has_no_namespace_to_hold(world):
    result = onboard(world, {**GITHUB, "branch_template": "{key}-{build}"})
    assert result["outcome"] == machine.NEEDS_FIX
    assert result["question"]["id"] == "branch_template"

    fixed = machine.answer(world.ports, SLUG, "branch_template", "slipway/{key}", OPERATOR)
    assert fixed["outcome"] == machine.FILE_CARD


def test_the_u26_fields_reach_the_project_file(world):
    onboard(world, {**BITBUCKET, "checkout": "worktree", "landing_path": "external_lander",
                    "branch_template": "slipway/{key}-{slug}",
                    "pull_request_title": "{key}: {title}"})
    project = load_project(SLUG, world.root)
    assert project.checkout == "worktree"
    assert project.landing_path == "external_lander"
    assert project.branch_for("EX-3", "Add a page", 9) == "slipway/EX-3-add-a-page"
    assert project.title_for("EX-3", "Add a page") == "EX-3: Add a page"


def test_a_bad_branch_template_is_refused_with_the_reason(world):
    machine.start(world.ports, SLUG, OPERATOR)
    result = machine.answer(world.ports, SLUG, "branch_template", "slipway/{build}", OPERATOR)
    assert result["outcome"] == machine.REFUSED
    assert "must carry {key}" in result["reason"]
    assert "branch_template" not in machine.status(world.ports, SLUG)["answers"]


def test_an_answer_the_whole_file_refuses_is_not_saved(world):
    onboard(world, BITBUCKET)
    before = world.project_file.read_bytes()
    result = machine.answer(world.ports, SLUG, "pull_request_title", "{key}", OPERATOR)
    assert result["outcome"] == machine.REFUSED
    assert "must carry {title}" in result["reason"]
    assert world.project_file.read_bytes() == before
    assert machine.status(world.ports, SLUG)["outcome"] == machine.FILE_CARD


def test_an_unreadable_restriction_is_attested_with_identity(world):
    world.probe.restricted = Finding(None, "no API for branch permissions")
    result = onboard(world, BITBUCKET)
    assert result["question"]["id"] == "attest.restriction"

    done = machine.answer(world.ports, SLUG, "attest.restriction", "yes", OPERATOR)
    assert done["outcome"] == machine.FILE_CARD
    state = json.loads((world.root / "onboard" / f"{SLUG}.json").read_text())
    assert state["attested"]["restriction"]["actor"] == OPERATOR


def test_bitbucket_with_no_pins_is_asked_for_them(world):
    result = onboard(world, {**BITBUCKET, "landing_path": "", "merge_method": ""})
    assert result["outcome"] == machine.NEEDS_FIX
    assert result["question"]["id"] == "landing_path"


# KTD24: a secret looking answer.

def test_a_token_answer_is_refused_and_only_its_hash_is_audited(world):
    machine.start(world.ports, SLUG, OPERATOR)
    result = machine.answer(world.ports, SLUG, "gate_command", f"make check {TOKEN}", OPERATOR)

    assert result["outcome"] == machine.REFUSED
    assert "looks like a secret" in result["reason"]
    row = world.records.audit_rows[-1]
    assert row["tool"] == "onboard_answer"
    assert row["arguments"]["value"]["length"] == len(f"make check {TOKEN}")
    assert len(row["arguments"]["value"]["sha256"]) == 64
    everything = json.dumps(world.records.audit_rows) + "".join(
        p.read_text() for p in world.root.rglob("*") if p.is_file())
    assert TOKEN not in everything


# KTD15: operator only questions.

def test_a_manager_cannot_answer_the_accounts_question(world):
    onboard(world, BITBUCKET)
    before = world.project_file.read_bytes()

    result = machine.answer(world.ports, SLUG, "roles.builder.account", "api-main", MANAGER)

    assert result["outcome"] == machine.REFUSED
    assert result["reason"] == machine.OPERATOR_SETS_THIS
    assert world.project_file.read_bytes() == before
    assert world.records.audit_rows[-1]["subject"] == MANAGER


def test_a_manager_is_shown_no_account_or_langfuse_names(world):
    onboard(world, BITBUCKET)
    for qid in ("roles.builder.account", "roles.judge.account", "trace_project"):
        assert machine.choices(world.ports, BY_ID[qid], {}, MANAGER) == ()
        assert machine.choices(world.ports, BY_ID[qid], {}, OPERATOR)

    seen = json.dumps(machine.status(world.ports, SLUG, MANAGER))
    for name in ("max-sub", "api-main", "example-traces"):
        assert name not in seen


def test_a_manager_collects_the_rest_and_hands_back_the_operator_questions(world):
    machine.start(world.ports, SLUG, OPERATOR)
    machine.answer(world.ports, SLUG, "allowed_subjects", [OPERATOR, MANAGER], OPERATOR)

    result = machine.status(world.ports, SLUG, MANAGER)
    assert result["question"]["id"] == "lane_label"  # the first one a manager may answer
    assert not result["question"]["operator_only"]


def test_a_subject_off_the_allowed_list_cannot_start(world):
    result = machine.start(world.ports, SLUG, STRANGER)
    assert result["outcome"] == machine.REFUSED
    assert world.records.audit_rows[-1]["subject"] == STRANGER


# AE13: the board moves.

def test_a_projects_board_without_the_workflow_fails_with_the_template(world):
    world.probe.moves = Moves(False, False, "the card stayed in Ready")
    result = onboard(world, GITHUB)

    assert result["outcome"] == machine.NEEDS_FIX
    assert result["property"] == checks.DURABLE_STATE
    assert checks.BOARD_TEMPLATE in result["next_step"]


def test_a_projects_board_with_the_workflow_passes(world):
    result = onboard(world, GITHUB)
    assert result["outcome"] == machine.FILE_CARD
    assert world.probe.reads == ["board_moves"]


def test_another_dispatcher_stops_the_finish(world):
    result = onboard(world, {**BITBUCKET, "other_dispatcher": "yes"})
    assert result["outcome"] == machine.NEEDS_FIX
    assert result["property"] == checks.SINGLE_DISPATCHER
    assert result["question"]["id"] == "other_dispatcher"

    done = machine.answer(world.ports, SLUG, "other_dispatcher", "no", OPERATOR)
    assert done["outcome"] == machine.FILE_CARD


# R32 and R33: resume and re entry.

def test_a_dropped_session_resumes_from_status(world):
    machine.start(world.ports, SLUG, OPERATOR)
    for q in QUESTIONS[:5]:
        machine.answer(world.ports, SLUG, q.id, BITBUCKET[q.id], OPERATOR)

    resumed = machine.status(world.ports, SLUG)
    assert resumed["question"]["id"] == QUESTIONS[5].id
    assert resumed["answers"]["repository"] == "example-workspace/example-app"
    assert machine.start(world.ports, SLUG, OPERATOR)["question"]["id"] == QUESTIONS[5].id


def test_reentry_on_a_deleted_board_asks_only_the_board(world):
    onboard(world, BITBUCKET)
    world.probe.boards_ = ("OPS",)

    asked = []
    result = machine.start(world.ports, SLUG, OPERATOR)
    assert result["pending"] == ["board"]
    while result["outcome"] == machine.QUESTION:
        asked.append(result["question"]["id"])
        result = machine.answer(world.ports, SLUG, result["question"]["id"], "OPS", OPERATOR)

    assert asked == ["board"]
    assert result["outcome"] == machine.FILE_CARD
    assert load_project(SLUG, world.root).board == "OPS"


def test_reentry_on_a_removed_account_asks_only_that_account(world):
    onboard(world, BITBUCKET)
    text = (world.root / "operator.toml").read_text()
    (world.root / "operator.toml").write_text(
        text.replace("[accounts.api-main]", "[accounts.api-new]"))

    result = machine.start(world.ports, SLUG, OPERATOR)
    assert result["pending"] == ["roles.judge.account"]
    assert "no longer in the operator file" in result["question"]["note"]


# R31: the trivial card.

def test_the_trivial_card_scoring_verifies_the_project(world):
    card = onboard(world, BITBUCKET)
    assert card["card"]["label"] == "slipway"
    assert '"paths": [\n    "docs/slipway-onboarding.md"' in card["card"]["body"]

    waiting = machine.answer(world.ports, SLUG, "trivial_card", "EX-1", OPERATOR)
    assert waiting["outcome"] == machine.WAITING

    world.records.seed("EX-1", "scored")
    done = machine.status(world.ports, SLUG)
    assert done["outcome"] == machine.VERIFIED
    assert done["verified"] is True


def test_a_wrong_gate_command_returns_to_the_gate_question_with_the_host_output(world):
    onboard(world, {**BITBUCKET, "gate_command": "npm test"})
    machine.answer(world.ports, SLUG, "trivial_card", "EX-1", OPERATOR)
    world.records.seed("EX-1", "gate_red", pr_number=7,
                       evidence={"reason": "pull request #7 left the queue"})
    world.probe.output = "npm ERR! Missing script: \"test\""

    result = machine.status(world.ports, SLUG)

    assert result["outcome"] == machine.QUESTION
    assert result["question"]["id"] == "gate_command"
    assert 'npm ERR! Missing script: "test"' in result["question"]["note"]
    assert result["verified"] is False

    restarted = machine.start(world.ports, SLUG, OPERATOR)  # the terminal run again
    assert restarted["question"]["id"] == "gate_command"
    assert 'Missing script: "test"' in restarted["question"]["note"]

    fixed = machine.answer(world.ports, SLUG, "gate_command", "make check", OPERATOR)
    assert fixed["outcome"] == machine.FILE_CARD
    assert load_project(SLUG, world.root).gate_command == "make check"


def test_the_live_probe_asks_for_attestations_it_cannot_read(world):
    class Tracker:
        def ready(self):
            return [], None

    probe = checks.LiveProbe(load_operator(world.root), lambda project: Tracker(),
                             lambda project: world.probe, ("cli",))
    ports = machine.Ports(world.root, world.ports.operator, probe, world.records,
                          world.ports.clock)
    result = onboard(World(world.root, probe, world.records, ports), BITBUCKET)
    assert result["question"]["id"] == "attest.board_moves"


def test_an_unset_tracker_credential_is_a_next_step_not_a_crash(world):
    from slipway.config.bindings import BindingInvalid

    def tracker_for(project):
        raise BindingInvalid("tracker_credentials.example-jira.token_env",
                             "environment variable EXAMPLE_JIRA_TOKEN is unset")

    probe = checks.LiveProbe(load_operator(world.root), tracker_for,
                             lambda project: world.probe, ("cli",))
    ports = machine.Ports(world.root, world.ports.operator, probe, world.records,
                          world.ports.clock)
    result = onboard(World(world.root, probe, world.records, ports), BITBUCKET)
    assert result["outcome"] == machine.NEEDS_FIX
    assert result["property"] == checks.DURABLE_STATE
    assert "EXAMPLE_JIRA_TOKEN is unset" in result["missing"]


def test_a_subject_off_the_list_causes_no_probe_read_on_reentry(world):
    onboard(world, BITBUCKET)
    world.probe.reads.clear()
    world.probe.repository_ok = None

    def counted(project):
        world.probe.reads.append("repository")
        return Finding(True)

    world.probe.repository = counted
    assert machine.start(world.ports, SLUG, STRANGER)["outcome"] == machine.REFUSED
    assert world.probe.reads == []


def test_a_fault_no_question_fixes_is_named_not_pinned_on_a_question(world):
    onboard(world, BITBUCKET)
    with world.project_file.open("a") as handle:
        handle.write('\n[rules]\nfile = "missing.rules.md"\n')

    result = machine.start(world.ports, SLUG, OPERATOR)
    assert result["outcome"] == machine.REFUSED
    assert "rules.file" in result["reason"]


def test_a_site_no_credential_serves_is_refused_up_front(world):
    machine.start(world.ports, SLUG, OPERATOR)
    result = machine.answer(world.ports, SLUG, "tracker_site", "other.atlassian.net", OPERATOR)
    assert result["outcome"] == machine.REFUSED
    assert "no tracker credential" in result["reason"]
