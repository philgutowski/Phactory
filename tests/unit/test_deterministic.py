"""U16 test scenarios: the deterministic score, check_card, and the post_landed step."""

import ast
from dataclasses import dataclass, field
from pathlib import Path

from _repo import make_repo
from slipway.builders.handoff import InFlightBuild
from slipway.cards.contract import parse
from slipway.evals import deterministic, steps
from slipway.evals.deterministic import (
    FAIL,
    NONE_NAMED,
    NOT_DECLARED,
    NOT_READ,
    PARTIAL,
    PASS,
    Gate,
    check_card,
    covers,
    reports,
    score,
)
from slipway.workflow.post_landed import POST_LANDED, SeamContext, load

SRC = Path(deterministic.__file__).resolve().parent


@dataclass(frozen=True)
class FakeProject:
    """The project fields these functions read, with invented values."""

    lane_label: str = "cloud"
    slug: str = "example"


@dataclass(frozen=True)
class FakeCard:
    key: str | None = None
    body: str = ""
    labels: tuple[str, ...] = ("cloud",)
    status: str | None = "Ready"
    skipped: str | None = None


def body(text: str) -> str:
    return f"Prose first.\n\n```slipway\n{text}\n```\n"


CARD = body('{"good": "The page renders.", "paths": ["src/page.py", "tests/test_page.py"], '
            '"test": "tests/test_page.py", "lane": "cloud"}')
BUILD = {"id": 7, "project_id": 3, "card_key": "EX-12", "confirmed_commit": None,
         "tested_commit": None}
PROJECT = FakeProject()

# A gate log in the shape GitHub Actions serves it: a timestamp on every
# line, pytest run with -rA so passing ids are printed.
GREEN_LOG = """\
2026-10-21T17:00:01.0000000Z ============ test session starts ============
2026-10-21T17:00:02.0000000Z tests/test_page.py ..                   [ 50%]
2026-10-21T17:00:02.1000000Z tests/test_other.py ..                  [100%]
2026-10-21T17:00:03.0000000Z ========== short test summary info ==========
2026-10-21T17:00:03.0000000Z PASSED tests/test_page.py::test_renders
2026-10-21T17:00:03.0000000Z PASSED tests/test_page.py::test_title
2026-10-21T17:00:03.0000000Z PASSED tests/test_other.py::test_one
2026-10-21T17:00:03.0000000Z PASSED tests/test_other.py::test_two
2026-10-21T17:00:04.0000000Z ============ 4 passed in 0.10s ============
"""
OTHER_ONLY_LOG = """\
tests/test_other.py ..                  [100%]
PASSED tests/test_other.py::test_one
PASSED tests/test_other.py::test_two
"""


def green(log=GREEN_LOG) -> Gate:
    return Gate("success", log, "host", "c0ffee")


# score.

def test_covers_ae7_a_named_test_absent_from_the_gate_log_fails_that_item_alone():
    result = score(BUILD, ["src/page.py", "tests/test_page.py"], green(OTHER_ONLY_LOG),
                   parse(CARD))
    assert result.gate.status == PASS
    assert result.paths.status == PASS
    assert result.runs.status == FAIL
    assert result.runs.detail["reason"] == "the named test is not in the gate's log"
    assert result.total() == {"verdict": FAIL, "passed": 2, "failed": 1, "of": 3,
                              "absent": []}


def test_a_named_test_in_the_gate_log_that_passed_passes_the_runs_item():
    result = score(BUILD, ["src/page.py"], green(), parse(CARD))
    assert result.runs.status == PASS
    assert result.runs.detail["passed_reports"] == 3  # the progress line and two summaries
    assert result.total()["verdict"] == PASS


def test_a_named_test_that_failed_or_was_skipped_fails_the_runs_item():
    failed = "tests/test_page.py .F  [100%]\nFAILED tests/test_page.py::test_title - boom\n"
    result = score(BUILD, [], Gate("failure", failed), parse(CARD))
    assert result.runs.status == FAIL
    assert result.runs.detail["failed"] == ["tests/test_page.py",
                                            "tests/test_page.py::test_title"]
    assert result.gate.status == FAIL
    for skipped in ("SKIPPED tests/test_page.py::test_renders\n",
                    "SKIPPED [1] tests/test_page.py:12: needs a device\n"):
        assert score(BUILD, [], green(skipped), parse(CARD)).runs.detail["reason"] == (
            "the named test was skipped")


def test_a_named_test_file_missing_from_the_landed_tree_fails_even_without_a_log():
    result = score(BUILD, ["src/page.py"], None, parse(CARD), tree={"src/page.py"})
    assert result.runs.status == FAIL
    assert "not in the landed tree" in result.runs.detail["reason"]
    assert result.gate.status == NOT_READ


def test_a_diff_touching_a_path_outside_paths_fails_the_paths_item_with_the_file_listed():
    diff = ["src/page.py", "src/billing.py", "./docs/notes.md", "tests/test_page.py"]
    result = score(BUILD, diff, green(), parse(CARD))
    assert result.paths.status == FAIL
    assert result.paths.detail["outside"] == ["docs/notes.md", "src/billing.py"]
    assert result.paths.detail["declared"] == ["src/page.py", "tests/test_page.py"]
    assert result.gate.status == PASS and result.runs.status == PASS


def test_a_card_with_no_block_scores_the_gate_and_records_the_two_absences():
    result = score(BUILD, ["anything.py"], Gate("failure", GREEN_LOG), parse("No block."))
    assert result.gate.status == FAIL
    assert result.paths.status == NOT_DECLARED
    assert result.runs.status == NONE_NAMED
    assert result.total() == {"verdict": FAIL, "passed": 0, "failed": 1, "of": 3,
                              "absent": ["paths", "runs"]}
    passing = score(BUILD, ["anything.py"], green(), parse("No block."))
    # An absence never collapses into a fail: a green gate with no block is partial.
    assert passing.total() == {"verdict": PARTIAL, "passed": 1, "failed": 0, "of": 3,
                               "absent": ["paths", "runs"]}


def test_inputs_nobody_read_are_recorded_as_not_read_never_as_fail():
    result = score(BUILD, None, None, parse(CARD))
    assert [item.status for item in result.items] == [NOT_READ] * 3
    assert result.total()["verdict"] == PARTIAL
    unread = score(BUILD, [], green(), None)
    assert (unread.paths.status, unread.runs.status) == (NOT_READ, NOT_READ)
    neutral = score(BUILD, [], Gate("neutral", GREEN_LOG), parse(CARD))
    assert neutral.gate.status == "no conclusion" and neutral.total()["failed"] == 0


def test_the_score_record_matches_the_scores_table():
    record = score(BUILD, ["src/page.py"], green(), parse(CARD)).record()
    # U7's scores table: project_id, build_id, kind, result (jsonb), and the
    # optional langfuse_score_id, unique on (build_id, kind).
    assert set(record) == {"project_id", "build_id", "kind", "result"}
    assert (record["project_id"], record["build_id"], record["kind"]) == (3, 7,
                                                                         "deterministic")
    assert set(record["result"]) == {"gate", "paths", "runs", "total"}
    assert record["result"]["gate"] == {"status": PASS, "conclusion": "success",
                                        "source": "host", "commit": "c0ffee"}


def test_the_log_reader_knows_the_common_test_report_lines():
    log = "\n".join([
        "\x1b[32mtests/a.py::test_x PASSED\x1b[0m   [ 10%]",
        "FAILED tests/a.py::test_y - AssertionError",
        "tests/b.py ..s  [ 50%]",
        "PASS src/app.test.ts",
        "-" * 3 + " FAIL: TestThing (0.01s)",
        "ok  \texample.org/pkg\t0.01s",
        "collected 4 items",
    ])
    assert [(r.id, r.outcome) for r in reports(log)] == [
        ("tests/a.py::test_x", "passed"), ("tests/a.py::test_y", "failed"),
        ("tests/b.py", "passed"), ("src/app.test.ts", "passed"),
        ("TestThing", "failed"), ("example.org/pkg", "passed")]


def test_a_named_test_id_matches_its_cases_and_a_log_printed_from_a_parent_directory():
    card = body('{"test": "tests/test_page.py::test_renders"}')
    log = "PASSED app/tests/test_page.py::test_renders[case one]\n"
    assert score(BUILD, None, green(log), parse(card)).runs.status == PASS
    other = "PASSED tests/test_page.py::test_renders_twice\n"
    assert score(BUILD, None, green(other), parse(card)).runs.status == FAIL


def test_paths_entries_cover_files_directories_and_globs():
    assert covers("src/page.py", "src/page.py")
    assert covers("src/", "src/deep/file.py")
    assert covers("src", "src/deep/file.py")
    assert not covers("src", "srcs/file.py")
    assert covers("tests/test_*.py", "tests/test_page.py")
    assert not covers("post_landed", "src/slipway/evals/steps.py")


# check_card.

def test_check_card_on_a_draft_without_a_lane_label_returns_the_allowed_labels():
    result = check_card(FakeCard(body=CARD, labels=("bug",)), PROJECT,
                        base_tree={"tests/test_page.py"})
    assert not result.would_dispatch
    assert result.allowed_labels == ("cloud",)
    assert result.missing == ("the draft carries no lane label; add one of: cloud",)


def test_check_card_on_a_draft_whose_paths_overlap_an_in_flight_build_names_the_other_card():
    in_flight = [InFlightBuild("EX-9", "building", ("src/page.py", "src/nav.py")),
                 InFlightBuild("EX-10", "pr_open", ("src/page.py",), pushed=("docs/a.md",)),
                 InFlightBuild("EX-11", "landed", ("src/page.py",))]
    result = check_card(FakeCard(body=CARD), PROJECT, base_tree=set(), in_flight=in_flight)
    assert not result.would_dispatch
    assert result.collisions == {"EX-9": ("src/page.py",)}
    assert result.missing == ("paths overlap card EX-9, in flight (building): src/page.py",)


def test_check_card_would_dispatch_a_complete_draft_and_notes_absences_without_blocking():
    assert check_card(FakeCard(body=CARD), PROJECT, base_tree=set()).would_dispatch
    sparse = check_card(FakeCard(body=body('{"lane": "cloud"}')), PROJECT, base_tree=set())
    assert sparse.would_dispatch and sparse.missing == ()
    assert len(sparse.notes) == 3


def test_check_card_lists_each_missing_thing_it_finds():
    draft = FakeCard(key="EX-12", body=body('{"test": "tests/test_gone.py", "lane": "ops"}'),
                     labels=("cloud", "operator"))
    result = check_card(draft, PROJECT, base_tree={"tests/test_page.py"},
                        other_lanes=("operator",))
    assert result.missing == (
        "the draft carries 2 lane labels (cloud, operator); keep one of: cloud",
        "the block names lane ops, but this project's lane is cloud",
        "the named test tests/test_gone.py is not in the base tree and not in the card's "
        "paths",
    )
    broken = check_card(FakeCard(body=body("[1]")), PROJECT, base_tree=set())
    assert broken.missing == ("the slipway block must be a JSON object",)


# The step on the post_landed seam.

@dataclass
class StepRecords:
    evidence: dict
    written: list = field(default_factory=list)

    def entered(self, build_id, state):
        return (None, self.evidence[state]) if state in self.evidence else None

    def record_score(self, build_id, kind, result):
        self.written.append((build_id, kind, result))


@dataclass
class StepGit:
    repo: Path

    def cache(self, project, commits):
        return self.repo


class GateHost:
    def __init__(self, gate):
        self.gate, self.asked = gate, []

    def gate_run(self, commit):
        self.asked.append(commit)
        return self.gate


@dataclass
class StepPorts:
    records: StepRecords
    git: StepGit
    card: FakeCard
    host_adapter: object

    def tracker(self, project):
        card = self.card

        class Tracker:
            def read(self, key):
                return card

        return Tracker()

    def host(self, project):
        return self.host_adapter


def landed_repo(tmp_path):
    """main with a seed, a branch that changes two files and merges main in, squashed."""
    repo = make_repo(tmp_path)
    base = repo.git("rev-parse", "HEAD")
    repo.git("checkout", "-q", "-b", "card")
    repo.commit("src/page.py", "page\n", "page")
    repo.git("checkout", "-q", "main")
    repo.commit("docs/elsewhere.md", "other card\n", "another card lands")
    repo.git("checkout", "-q", "card")
    repo.git("merge", "-q", "--no-edit", "main")
    confirmed = repo.commit("tests/test_page.py", "test\n", "test")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--squash", "card")
    repo.git("commit", "-q", "-m", "EX-12 squashed")
    return repo, base, confirmed, repo.git("rev-parse", "HEAD")


def test_the_step_scores_the_landed_build_from_git_and_the_gate_and_writes_the_row(tmp_path):
    repo, base, confirmed, merged = landed_repo(tmp_path)
    build = {**BUILD, "confirmed_commit": confirmed}
    records = StepRecords({"dispatched": {"base": base}, "landed": {"merged": merged}})
    host = GateHost({"conclusion": "success", "log": GREEN_LOG, "commit": confirmed})
    ports = StepPorts(records, StepGit(repo.work), FakeCard(key="EX-12", body=CARD), host)
    out = steps.score_deterministic(SeamContext(POST_LANDED, PROJECT, build, ports))
    assert out == {"total": {"verdict": PASS, "passed": 3, "failed": 0, "of": 3,
                             "absent": []},
                   "items": {"gate": PASS, "paths": PASS, "runs": PASS}}
    assert host.asked == [confirmed]
    [(build_id, kind, result)] = records.written
    assert (build_id, kind) == (7, "deterministic")
    # The other card's file came in through the merge of main and is not counted.
    assert result["paths"] == {"status": PASS, "files": 2}


def test_the_step_records_not_read_for_a_host_with_no_gate_reader(tmp_path):
    repo, base, confirmed, merged = landed_repo(tmp_path)
    build = {**BUILD, "confirmed_commit": confirmed}
    records = StepRecords({"dispatched": {"base": base}, "landed": {"merged": merged}})
    ports = StepPorts(records, StepGit(repo.work), FakeCard(body=CARD), object())
    out = steps.score_deterministic(SeamContext(POST_LANDED, PROJECT, build, ports))
    assert out["items"] == {"gate": NOT_READ, "paths": PASS, "runs": NOT_READ}
    assert out["total"]["verdict"] == PARTIAL


def test_score_deterministic_is_registered_on_the_post_landed_seam():
    registry = load()
    assert registry.names(POST_LANDED)[0] == "score_deterministic"


# KTD18: no project code.

def _calls_and_imports(path: Path) -> tuple[set[str], set[str], set[str]]:
    tree = ast.parse(path.read_text())
    imports, calls, attributes = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            calls.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
        elif isinstance(node, ast.Attribute):
            attributes.add(node.attr)
    return imports, calls, attributes


def test_no_code_path_in_the_module_invokes_a_subprocess_or_reads_the_gate_command():
    imports, calls, attributes = _calls_and_imports(SRC / "deterministic.py")
    spawners = {"subprocess", "os", "pty", "multiprocessing", "asyncio", "shlex",
                "slipway.gitread", "slipway.verify"}
    assert not {name.split(".")[0] for name in imports} & spawners
    assert not imports & spawners
    assert not calls & {"system", "popen", "Popen", "run", "call", "check_output", "exec",
                        "eval", "spawn", "execv", "execvp", "create_subprocess_exec"}
    assert "gate_command" not in attributes
    # The step reads git through Slipway's own cache, and never the gate command either.
    _, _, step_attributes = _calls_and_imports(SRC / "steps.py")
    assert "gate_command" not in step_attributes
