"""U11 test scenarios for the builder contract and the handoff."""

import os
import pickle
import re
import subprocess
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from _repo import _GIT_ENV, make_repo
from slipway.builders import base as base_module
from slipway.builders.base import (
    BUILDER_FAILED,
    PRIMITIVES,
    PROTOCOL_MEMBERS,
    RETURNED,
    RETURNED_EMPTY,
    Builder,
    BuilderResult,
    Credential,
    CredentialError,
    Schedule,
    SessionStatus,
    check_base,
)
from slipway.builders.handoff import (
    IN_FLIGHT_STATES,
    TOOLS,
    InFlightBuild,
    in_flight_files,
    render,
)
from slipway.config import ConfigError
from slipway.config.project import parse_project
from slipway.records.queries import IN_FLIGHT_STATES as RECORDS_IN_FLIGHT_STATES
from slipway.trackers.base import Card, ClosingReference

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN = FIXTURES / "handoff" / "EX-12.txt"
EXPLAINER = ROOT / "docs" / "explainers" / "u11-builder-contract.md"

BASE = "0123456789abcdef0123456789abcdef01234567"
BRANCH = "slipway/EX-12-hello-page"
RULES = "Write tests first.\nKeep each function under forty lines."
BODY = (
    "Add a hello page that greets the visitor by name.\n\n```slipway\n"
    '{"good": "The hello page renders the visitor\'s name.", '
    '"paths": ["src/hello.py", "tests/test_hello.py"], '
    '"test": "tests/test_hello.py", "lane": "cloud"}\n```\n'
)
CARD = Card(key="EX-12", title="Add the hello page", body=BODY, status="Ready",
            labels=("cloud",))
IN_FLIGHT = (
    InFlightBuild("EX-9", "building", ("src/nav.py",)),
    InFlightBuild("EX-10", "pr_open", ("src/old.py",), ("src/footer.py", "src/nav.py")),
    InFlightBuild("EX-7", "scored", ("src/done.py",)),
)
CLOSING = ClosingReference(title_token="EX-12", branch_token="EX-12")
# The value the fixture operator file's host credential variable would hold.
SECRET = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture
def project():
    path = FIXTURES / "config" / "project.toml"
    return parse_project(Path("example.toml"), tomllib.loads(path.read_text()))


def rendered(project, **changes):
    args = dict(card=CARD, base=BASE, branch=BRANCH, in_flight=IN_FLIGHT, rules=RULES,
                closing=CLOSING)
    args.update(changes)
    return render(project, **args)


# The rendered handoff.

def test_the_rendered_handoff_matches_the_golden_fixture_byte_for_byte(project):
    assert rendered(project).text == GOLDEN.read_text()


def test_the_explainer_documents_the_golden_fixture_byte_for_byte():
    explainer = EXPLAINER.read_text()
    fenced = re.findall(r"^````text\n(.*?)^````$", explainer, re.MULTILINE | re.DOTALL)
    assert GOLDEN.read_text() in fenced


def test_the_sections_come_in_the_fixed_order(project):
    text = rendered(project).text
    marks = ["1. Base: " + BASE, "2. Branch: " + BRANCH, "3. Rules:", "4. Tools:",
             "5. Files other in flight builds", "6. Done:", "7. Credential:",
             "8. Side reference: refs/slipway/log/EX-12", "The card:"]
    places = [text.index(mark) for mark in marks]
    assert places == sorted(places)
    assert text.index(RULES.splitlines()[0]) < places[3]


def test_no_credential_value_or_variable_name_appears(project, monkeypatch):
    monkeypatch.setenv("EXAMPLE_GITHUB_APP_KEY", SECRET)
    credential = Credential("example-app", lambda: (SECRET, None))
    handoff = rendered(project)
    for shown in (handoff.text, repr(handoff), str(credential), repr(credential)):
        assert SECRET not in shown
        assert "EXAMPLE_GITHUB_APP_KEY" not in shown
    assert "credential named example-app" in handoff.text
    assert handoff.credential_name == "example-app"


def test_the_handoff_fields_carry_what_the_adapter_needs(project):
    handoff = rendered(project)
    assert handoff.base == BASE and handoff.branch == BRANCH
    assert handoff.side_reference == "refs/slipway/log/EX-12"
    assert handoff.tools == TOOLS
    assert handoff.in_flight == ("src/footer.py", "src/nav.py")
    assert "Write tests first." in handoff.rules
    assert "tests/test_usb_device.py" in handoff.rules


def test_the_done_line_reads_the_card_contract(project):
    done = rendered(project).done
    assert done.startswith("The hello page renders the visitor's name.")
    assert "tests/test_hello.py" in done
    assert "make check" in done
    assert "Do not open a pull request" in done


def test_a_card_with_no_block_says_no_rubric_and_no_test(project):
    card = replace(CARD, body="Just do it.")
    done = rendered(project, card=card).done
    assert "names no definition of done" in done
    assert "names no test" in done


def test_no_in_flight_files_and_no_machine_bound_tests(project):
    project = replace(project, machine_bound_tests=())
    handoff = rendered(project, in_flight=(), rules="")
    assert "must not touch:\n   None." in handoff.text
    assert "machine bound" not in handoff.text
    assert "gives no rules" in handoff.rules


def test_a_branch_without_the_trackers_branch_token_is_refused(project):
    with pytest.raises(ValueError, match="EX-12"):
        rendered(project, branch="slipway/hello-page")
    assert rendered(project, branch="slipway/hello-page", closing=ClosingReference(
        body_line="Closes #12")).branch == "slipway/hello-page"


def test_a_missing_base_is_refused(project):
    with pytest.raises(ValueError):
        rendered(project, base="")


def test_the_rendered_handoff_has_no_dash_punctuation(project):
    text = rendered(project).text
    assert not re.search("[" + chr(0x2013) + chr(0x2014) + "]", text)
    assert "-" * 2 not in text


# The side reference procedure (follow up #56).

def procedure(text):
    """The command lines section 8 lists, in order."""
    section = text[text.index("8. Side reference:"):text.index("The card:")]
    return [line.strip() for line in section.splitlines() if line.startswith(" " * 6)]


def run_procedure(repo, text, log):
    (repo.work / ".git" / "actions.jsonl").write_text(log)
    script = "set -e\n" + "\n".join(procedure(text)) + "\n"
    subprocess.run(["sh", "-c", script], cwd=repo.work, env={**os.environ, **_GIT_ENV},
                   check=True, capture_output=True, text=True)
    return repo.git("ls-remote", "origin", "refs/slipway/log/EX-12").split()[0]


def test_section_8_names_one_writer_and_one_procedure(project):
    text = rendered(project).text
    assert "push the log from the repository root with exactly" in text
    assert procedure(text) == [
        "blob=$(git hash-object -w .git/actions.jsonl)",
        "tree=$(printf '100644 blob %s\\tactions.jsonl\\n' \"$blob\" | git mktree)",
        'if git fetch origin refs/slipway/log/EX-12; then parent="-p FETCH_HEAD"; '
        'else parent=""; fi',
        'commit=$(git commit-tree "$tree" $parent -m "slipway log EX-12 actions")',
        'git push origin "$commit:refs/slipway/log/EX-12"',
    ]
    assert "Never push HEAD or the branch to refs/slipway/log/EX-12" in text
    assert "you are this reference's only writer" in text
    assert "Slipway\n   writes the build's closing commit" in text


def test_the_procedure_pushes_one_file_and_no_branch_history(project, tmp_path):
    repo = make_repo(tmp_path)
    repo.git("checkout", "-q", "-b", BRANCH)
    work = repo.commit("hello.py", "print('hello')\n", "hello")
    repo.git("push", "-q", "origin", BRANCH)
    text = rendered(project).text

    first = run_procedure(repo, text, '{"type": "command"}\n')
    assert repo.git("ls-tree", "--name-only", first) == "actions.jsonl"
    assert repo.git("show", f"{first}:actions.jsonl") == '{"type": "command"}'
    assert repo.git("log", "-1", "--format=%s|%P", first) == "slipway log EX-12 actions|"
    # The log is no part of the branch, and the branch is no part of the log.
    assert repo.git("status", "--porcelain") == ""
    assert subprocess.run(["git", "merge-base", "--is-ancestor", work, first],
                          cwd=repo.work, capture_output=True).returncode == 1

    # A second push, such as a retried attempt, is the child of the tip it read.
    second = run_procedure(repo, text, '{"type": "file_change"}\n')
    assert repo.git("log", "-1", "--format=%P", second) == first
    assert repo.git("ls-tree", "--name-only", second) == "actions.jsonl"


# The project file's rules (follow up #56).

def project_with(tmp_path, rules=None, files=None):
    data = tomllib.loads((FIXTURES / "config" / "project.toml").read_text())
    if rules is not None:
        data["rules"] = rules
    folder = tmp_path / "projects"
    folder.mkdir(exist_ok=True)
    for name, content in (files or {}).items():
        (folder / name).write_text(content)
    return parse_project(folder / "example.toml", data)


def test_rules_inline_reach_the_handoff_when_the_caller_passes_none(tmp_path):
    project = project_with(tmp_path, {"text": RULES})
    assert project.rules == RULES
    handoff = render(project, CARD, BASE, BRANCH, IN_FLIGHT, closing=CLOSING)
    assert handoff.text == GOLDEN.read_text()


def test_rules_from_a_file_beside_the_project_file(tmp_path):
    project = project_with(tmp_path, {"file": "example.rules.md"},
                           {"example.rules.md": RULES + "\n"})
    assert project.rules == RULES + "\n"
    assert render(project, CARD, BASE, BRANCH, closing=CLOSING).rules.startswith(RULES)


def test_no_rules_table_means_no_rules(tmp_path):
    project = project_with(tmp_path)
    assert project.rules == ""
    assert "gives no rules" in render(project, CARD, BASE, BRANCH, closing=CLOSING).rules


def test_an_explicit_rules_argument_wins_over_the_project_file(tmp_path):
    project = project_with(tmp_path, {"text": "From the file."})
    assert render(project, CARD, BASE, BRANCH, rules=RULES, closing=CLOSING).rules \
        .startswith(RULES)


@pytest.mark.parametrize(("rules", "files", "field", "message"), [
    ({}, None, "rules", "exactly one of text or file"),
    ({"text": RULES, "file": "x.md"}, None, "rules", "exactly one of text or file"),
    ({"path": "x.md"}, None, "rules.path", "not a known key"),
    ("Write tests first.", None, "rules", "must be dict"),
    ({"text": "  "}, None, "rules.text", "must not be empty"),
    ({"file": "missing.md"}, None, "rules.file", "not found"),
    ({"file": "empty.md"}, {"empty.md": "\n"}, "rules.file", "is empty"),
    ({"file": "../outside.md"}, None, "rules.file", "inside the project file's directory"),
    ({"file": "/etc/hostname"}, None, "rules.file", "relative"),
    ({"file": "binary.md"}, {"binary.md": None}, "rules.file", "not a readable text file"),
])
def test_a_bad_rules_table_names_its_key(tmp_path, rules, files, field, message):
    (tmp_path / "outside.md").write_text(RULES)
    if files and None in files.values():
        (tmp_path / "projects").mkdir()
        (tmp_path / "projects" / "binary.md").write_bytes(b"\xff\xfe\x00")
        files = None
    with pytest.raises(ConfigError) as error:
        project_with(tmp_path, rules, files)
    assert error.value.field == field
    assert message in error.value.message


def test_a_rules_file_that_holds_a_secret_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="looks like a secret") as error:
        project_with(tmp_path, {"file": "example.rules.md"},
                     {"example.rules.md": f"Push with {SECRET}.\n"})
    assert error.value.field == "rules.file"
    assert SECRET not in str(error.value)


# In flight files.

def test_in_flight_files_union_declared_and_replace_with_pushed_for_returned_cards():
    builds = [
        InFlightBuild("1", "dispatched", ("a.py", "b.py")),
        InFlightBuild("2", "building", ("b.py", "c.py")),
        InFlightBuild("3", "returned", ("declared.py",), ("pushed.py",)),
        InFlightBuild("4", "merged", ("old.py",), ("landed.py",)),
        InFlightBuild("5", "gate_red", ("only_declared.py",)),
        InFlightBuild("6", "claimed", ("not_yet.py",)),
        InFlightBuild("7", "scored", ("gone.py",), ("gone.py",)),
        InFlightBuild("8", "building", ()),
    ]
    assert in_flight_files(builds) == (
        "a.py", "b.py", "c.py", "landed.py", "only_declared.py", "pushed.py")


def test_the_card_itself_is_not_its_own_in_flight_file():
    builds = [InFlightBuild("12", "building", ("mine.py",)),
              InFlightBuild("13", "building", ("theirs.py",))]
    assert in_flight_files(builds, exclude_key="12") == ("theirs.py",)


def test_a_declared_path_does_not_survive_a_returned_diff_that_left_it_out():
    builds = [InFlightBuild("3", "pr_open", ("a.py", "b.py"), ("a.py",))]
    assert in_flight_files(builds) == ("a.py",)


def test_the_in_flight_states_are_the_records_states():
    assert IN_FLIGHT_STATES == RECORDS_IN_FLIGHT_STATES


# The builder result and the base check.

def result(commit):
    return BuilderResult(branch=BRANCH, commit=commit, reason="end_turn",
                         provider_ref="session_1", cost=1.25, model="model-large",
                         account="max-sub")


@pytest.fixture
def repo(tmp_path):
    repo = make_repo(tmp_path)
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-q", "-b", "slipway/EX-12-hello-page")
    good = repo.commit("hello.py", "print('hello')\n", "hello")
    repo.git("checkout", "-q", "--orphan", "stray")
    stray = repo.commit("stray.py", "x = 1\n", "stray")
    return repo, base, good, stray


@pytest.fixture
def ancestor(repo):
    """is_ancestor answered by git in the test repository, as gitread would."""
    work = repo[0].work

    def check(base, commit):
        return subprocess.run(["git", "merge-base", "--is-ancestor", base, commit],
                              cwd=work, capture_output=True).returncode == 0
    return check


def test_a_commit_that_descends_from_the_base_is_returned(repo, ancestor):
    _, base, good, _ = repo
    checked = check_base(result(good), base, ancestor)
    assert checked == result(good)
    assert checked.outcome == RETURNED


def test_a_commit_that_does_not_descend_from_the_base_is_returned_empty(repo, ancestor):
    _, base, _, stray = repo
    checked = check_base(result(stray), base, ancestor)
    assert checked.outcome == RETURNED_EMPTY
    assert checked.commit is None
    assert checked.halt_class == BUILDER_FAILED
    assert f"{stray} on {BRANCH} does not descend from base {base}" in checked.reason
    assert (checked.cost, checked.model, checked.account) == (1.25, "model-large", "max-sub")


def test_a_branch_still_at_the_base_is_returned_empty(repo, ancestor):
    _, base, _, _ = repo
    checked = check_base(result(base), base, ancestor)
    assert checked.outcome == RETURNED_EMPTY
    assert "nothing was pushed" in checked.reason


def test_a_result_with_no_commit_is_left_alone():
    empty = replace(result(None), reason="budget_reached")
    assert check_base(empty, BASE, lambda *_: pytest.fail("no commit, no git")) == empty
    assert empty.outcome == RETURNED_EMPTY


def test_a_terminal_status_carries_a_result_and_a_running_one_does_not():
    assert SessionStatus(terminal=True, result=result(None)).result is not None
    assert SessionStatus(terminal=False).result is None
    with pytest.raises(ValueError):
        SessionStatus(terminal=True)
    with pytest.raises(ValueError):
        SessionStatus(terminal=False, result=result(None))


# The primitive table.

def docstring_table():
    rows = re.findall(r"^\| (\w+) +\| (.+?) +\|$", base_module.__doc__, re.MULTILINE)
    return [(name, tuple(re.findall(r"`([^`]+)`", carriers)))
            for name, carriers in rows if name != "Primitive"]


def test_the_primitive_table_names_six_carriers():
    table = docstring_table()
    assert [name for name, _ in table] == [
        "Harness", "Environment", "Tools", "Skills", "Credentials", "Schedule"]
    assert all(carriers for _, carriers in table)
    assert tuple(table) == PRIMITIVES


def test_every_protocol_method_appears_in_exactly_one_row():
    members = sorted(name for name in vars(Builder)
                     if not name.startswith("_") and callable(getattr(Builder, name)))
    members += sorted(Builder.__annotations__)
    assert sorted(members) == sorted(PROTOCOL_MEMBERS)
    every = [carrier for _, carriers in PRIMITIVES for carrier in carriers]
    for member in PROTOCOL_MEMBERS:
        assert every.count(member) == 1, member
    assert len(every) == len(set(every))


# The protocol, as both adapters will implement it.

class FakeAdapter:
    def __init__(self, schedule):
        self.schedule = schedule

    def start(self, handoff, binding, credential):
        return "session_1"

    def poll(self, session_ref):
        return SessionStatus(terminal=False)

    def release(self, session_ref):
        return None

    def delete(self, session_ref):
        return None

    def events(self, session_ref):
        return iter(())


@pytest.mark.parametrize("schedule", list(Schedule))
def test_the_protocol_is_importable_and_satisfied_by_either_schedule(schedule):
    assert isinstance(FakeAdapter(schedule), Builder)
    assert {s.value for s in Schedule} == {"remote_session", "worker_process"}


def test_an_adapter_missing_a_method_is_not_a_builder():
    class Partial:
        schedule = Schedule.REMOTE_SESSION

        def start(self, handoff, binding, credential):
            return "x"

    assert not isinstance(Partial(), Builder)


# The credential.

def minter(*values, expiry=None):
    queue = list(values)
    return lambda: (queue.pop(0), expiry)


def test_refresh_returns_a_new_value_and_never_exposes_the_old_one():
    credential = Credential("example-app", minter("token-one-aaaa", "token-two-bbbb"))
    assert credential.current() == "token-one-aaaa"
    assert credential.refresh() == "token-two-bbbb"
    assert credential.current() == "token-two-bbbb"
    held = [getattr(credential, slot, None) for slot in Credential.__slots__]
    assert "token-one-aaaa" not in held
    assert "token-one-aaaa" not in repr(credential)
    assert not hasattr(credential, "__dict__")


def test_a_rotating_refresh_that_mints_the_same_value_or_nothing_is_refused():
    expires = datetime(2026, 10, 15, 13, 0, tzinfo=UTC)
    with pytest.raises(CredentialError):
        Credential("example-app",
                   minter("same-value-aaaa", "same-value-aaaa", expiry=expires)).refresh()
    with pytest.raises(CredentialError):
        Credential("example-app", minter(""))
    with pytest.raises(CredentialError):
        Credential("example-app", minter("static-value-aaaa", "")).refresh()


def test_a_static_credential_survives_a_refresh_cycle_with_no_error():
    # A static token (no expiry, a Bitbucket repository access token) is
    # never due, and a refresh called anyway keeps the same value.
    credential = Credential("example-static", minter(*["static-value-aaaa"] * 4))
    start = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    for minutes in (0, 50, 100):
        assert not credential.due(start + timedelta(minutes=minutes))
        assert credential.refresh() == "static-value-aaaa"
    assert credential.current() == "static-value-aaaa"
    assert credential.expires_at is None


def test_a_static_credential_takes_a_new_value_when_the_operator_changes_it():
    credential = Credential("example-static", minter("static-one-aaaa", "static-two-bbbb"))
    assert credential.refresh() == "static-two-bbbb"
    held = [getattr(credential, slot, None) for slot in Credential.__slots__]
    assert "static-one-aaaa" not in held


def test_a_rotating_credential_through_a_refresh_cycle_hides_every_old_value():
    first = datetime(2026, 10, 15, 13, 0, tzinfo=UTC)
    mints = iter([("token-one-aaaa", first),
                  ("token-two-bbbb", first + timedelta(hours=1)),
                  ("token-three-cccc", first + timedelta(hours=2))])
    credential = Credential("example-app", lambda: next(mints))
    now = first - timedelta(hours=1)
    refreshed = []
    while now < first + timedelta(hours=1):
        now += timedelta(minutes=5)
        if credential.due(now):
            refreshed.append((now, credential.refresh()))
    assert refreshed == [(first - timedelta(minutes=10), "token-two-bbbb"),
                         (first + timedelta(minutes=50), "token-three-cccc")]
    held = [getattr(credential, slot, None) for slot in Credential.__slots__]
    assert "token-one-aaaa" not in held and "token-two-bbbb" not in held
    assert "token-two-bbbb" not in repr(credential)


def test_a_rotating_credential_that_stops_expiring_must_still_change_value():
    # Static means static on both sides: a rotating token that comes back
    # unchanged with no expiry is still the value about to stop working.
    expires = datetime(2026, 10, 15, 13, 0, tzinfo=UTC)
    mints = iter([("token-one-aaaa", expires), ("token-one-aaaa", None)])
    with pytest.raises(CredentialError):
        Credential("example-app", lambda: next(mints)).refresh()
    # And the other direction: a static value that comes back with an expiry.
    mints = iter([("token-one-aaaa", None), ("token-one-aaaa", expires)])
    with pytest.raises(CredentialError):
        Credential("example-app", lambda: next(mints)).refresh()


def test_a_credential_is_due_within_the_margin_of_its_expiry():
    expires = datetime(2026, 10, 15, 13, 0, tzinfo=UTC)
    credential = Credential("example-app", minter("token-one-aaaa", expiry=expires))
    assert not credential.due(expires - timedelta(minutes=11))
    assert credential.due(expires - timedelta(minutes=10))
    assert not Credential("x", minter("token-one-aaaa")).due(expires)


def test_a_credential_cannot_be_pickled():
    with pytest.raises(TypeError):
        pickle.dumps(Credential("example-app", minter("token-one-aaaa")))
