"""The deterministic score of a landed card, and check_card for a draft (U16).

A score is a record of how a landed card measures against what it declared.
The deterministic one is computed from facts alone, with no model and no
judgement, and it has three items (R24):

    gate   the gate's own result for the landed change, as the host or the
           runner reports it: pass or fail.
    paths  every file the diff touches sits inside the card's declared paths:
           pass, or fail with the offending files listed.
    runs   the test the card names appears in the gate's log and passed:
           pass, or fail with the reason (not in the landed tree, not in the
           log, failed, skipped).

Each item can also be absent, and an absence is never a fail (KTD5): a card
with no paths records "not declared", a card with no test records "none
named", and an input nobody could read (no gate result, no log, no diff)
records "not read". The total counts what passed and what failed and names
what was absent, and its verdict is fail only when an item failed.

Slipway executes no project code (KTD18). This module reads text and lists
it is handed; it starts no process, runs no gate command, and reads no
repository. The runs item learns whether the named test ran by finding it in
the gate's log, which the gate itself wrote. So a project's gate must print
test ids for this item to pass: pytest with -rA or -v does, pytest -q alone
prints only dots, and then the runs item fails with "not in the gate's log".

check_card is the read only check of a draft card before anyone files it: the
lane label, the slipway block (through the same parser the ready rule uses,
cards.contract.parse), the named test, and whether the declared paths
collide with files another in flight build holds. It answers would dispatch,
or the list of what is missing.
"""

import re
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from slipway.builders.handoff import IN_FLIGHT_STATES, PUSHED_STATES, InFlightBuild
from slipway.cards.contract import Block, parse

KIND = "deterministic"
ITEMS = ("gate", "paths", "runs")

PASS = "pass"
FAIL = "fail"
# The absences, each recorded as itself so none of them reads as a fail.
NOT_DECLARED = "not declared"
NONE_NAMED = "none named"
NOT_READ = "not read"
NO_CONCLUSION = "no conclusion"

# The total's verdict when nothing failed but something was absent.
PARTIAL = "partial"

# Gate conclusions across GitHub check runs, GitHub statuses, Bitbucket
# pipelines, and the runner's own record, folded to pass or fail. Anything
# else (neutral, skipped, still running, missing) is no conclusion.
PASSING = frozenset({"success", "successful", "passed", "pass"})
FAILING = frozenset({"failure", "failed", "fail", "error", "timed_out", "cancelled",
                     "canceled", "stopped", "action_required", "startup_failure"})


@dataclass(frozen=True)
class Gate:
    """The gate's result for one commit, read from the host or the runner.

    conclusion is the gate's own word for how it ended, log the text it
    printed (None when the log was not read), source where it was read
    from ("host" or "runner"), and commit the commit it ran on.
    """

    conclusion: str | None
    log: str | None = None
    source: str = "host"
    commit: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Item:
    """One item of the score: its status and the facts behind it."""

    name: str
    status: str
    detail: Mapping = field(default_factory=dict)

    @property
    def absent(self) -> bool:
        return self.status not in (PASS, FAIL)

    def to_json(self) -> dict:
        return {"status": self.status, **self.detail}


@dataclass(frozen=True)
class Score:
    """The three items and their total."""

    build_id: int
    project_id: int
    gate: Item
    paths: Item
    runs: Item

    @property
    def items(self) -> tuple[Item, ...]:
        return (self.gate, self.paths, self.runs)

    def total(self) -> dict:
        passed = [item.name for item in self.items if item.status == PASS]
        fails = [item.name for item in self.items if item.status == FAIL]
        absent = [item.name for item in self.items if item.absent]
        verdict = FAIL if fails else PASS if not absent else PARTIAL
        return {"verdict": verdict, "passed": len(passed), "failed": len(fails),
                "of": len(self.items), "absent": absent}

    def to_json(self) -> dict:
        """The scores row's result column (U7: scores.result is JSON)."""
        return {**{item.name: item.to_json() for item in self.items}, "total": self.total()}

    def record(self) -> dict:
        """The scores row this score writes, one per build and kind (U7)."""
        return {"project_id": self.project_id, "build_id": self.build_id, "kind": KIND,
                "result": self.to_json()}


# Paths.

def _clean(entry: str) -> str:
    return entry.strip().removeprefix("./")


def covers(entry: str, path: str) -> bool:
    """Whether a declared paths entry covers one file path.

    An entry is a file (covers itself), a directory ending in a slash or
    named by its prefix (covers everything under it), or a glob with *, ?,
    or [ (covers what it matches).
    """
    entry, path = _clean(entry), _clean(path)
    if not entry:
        return False
    if any(mark in entry for mark in "*?["):
        return fnmatchcase(path, entry)
    if entry.endswith("/"):
        return path.startswith(entry)
    return path == entry or path.startswith(entry + "/")


def overlap(a: str, b: str) -> bool:
    """Whether two paths entries can name the same file."""
    return covers(a, b) or covers(b, a)


def _paths_item(block: Block | None, diff: Iterable[str] | None) -> Item:
    if block is None:
        return Item("paths", NOT_READ, {"reason": "the card was not read"})
    if not block.paths:
        detail = {"block_error": block.error} if block.error else {}
        return Item("paths", NOT_DECLARED, detail)
    if diff is None:
        return Item("paths", NOT_READ, {"reason": "the landed diff was not read"})
    files = sorted({_clean(path) for path in diff if _clean(path)})
    outside = [path for path in files if not any(covers(e, path) for e in block.paths)]
    if outside:
        return Item("paths", FAIL, {"outside": outside, "declared": list(block.paths)})
    return Item("paths", PASS, {"files": len(files)})


# The gate's log.

# A GitHub Actions log line opens with an ISO timestamp, and colour output
# leaves ANSI escapes; both are taken off before a line is read.
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
_WORDS = {"PASSED": PASSED, "XFAIL": PASSED, "XPASS": PASSED, "FAILED": FAILED,
          "ERROR": FAILED, "SKIPPED": SKIPPED, "PASS": PASSED, "FAIL": FAILED,
          "SKIP": SKIPPED, "ok": PASSED}
_MARKS = {".": PASSED, "x": PASSED, "X": PASSED, "F": FAILED, "E": FAILED, "s": SKIPPED}

# The test report lines of the runners a gate commonly calls. Each gives the
# id it reports (a file, or a file and a test name, whose parameters in
# brackets may hold spaces) and the word or the progress marks that say how
# it ended.
_REPORTS = (
    # pytest -v: "tests/test_a.py::test_one PASSED   [ 50%]"
    re.compile(r"^(?P<id>[^\s\[]+::[^\s\[]+(?:\[[^\]]*\])?) "
               r"(?P<word>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"),
    # pytest -rA, and the failure summary every pytest run prints:
    # "FAILED tests/test_a.py::test_one - AssertionError"
    re.compile(r"^(?P<word>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) "
               r"(?P<id>[^\s\[]+(?:\[[^\]]*\])?)(?: - .*)?$"),
    # pytest -rA prints a skip by file and line: "SKIPPED [1] tests/test_a.py:12: why"
    re.compile(r"^(?P<word>SKIPPED) \[\d+\] (?P<id>[^\s:]+):\d+"),
    # pytest's default progress: "tests/test_a.py ..F.   [ 40%]"
    re.compile(r"^(?P<id>\S+\.py) (?P<marks>[.FEsxX]+)(?:\s+\[\s*\d+%\])?$"),
    # Jest and Vitest: "PASS src/a.test.ts", "FAIL src/b.test.ts"
    re.compile(r"^\s*(?P<word>PASS|FAIL) (?P<id>\S+)"),
    # go test -v: a line opening with three hyphens, then "PASS: TestOne", per
    # test, and "ok  example.org/pkg  0.01s" per package.
    re.compile(r"^\s*-{3} (?P<word>PASS|FAIL|SKIP): (?P<id>\S+)"),
    re.compile(r"^(?P<word>ok|FAIL)\s+(?P<id>\S+)\s"),
)


@dataclass(frozen=True)
class Report:
    """One test result the gate printed."""

    id: str
    outcome: str


def reports(log: str) -> list[Report]:
    """Every test result line in a gate log, in order."""
    found = []
    for raw in log.splitlines():
        line = _STAMP.sub("", _ANSI.sub("", raw)).rstrip()
        for pattern in _REPORTS:
            match = pattern.match(line)
            if not match:
                continue
            groups = match.groupdict()
            test_id = groups["id"].removeprefix("./")
            if groups.get("marks"):
                outcomes = {_MARKS[mark] for mark in groups["marks"]}
                outcome = (FAILED if FAILED in outcomes else
                           PASSED if PASSED in outcomes else SKIPPED)
            else:
                outcome = _WORDS[groups["word"]]
            found.append(Report(test_id, outcome))
            break
    return found


def _names(test: str, report_id: str) -> bool:
    """Whether a report line is about the named test.

    A named file matches every report under it, and a named test id matches
    itself, its parametrised cases, and the tests under a named class. A log
    that prints paths from a parent directory still matches by suffix.
    """
    test = _clean(test)
    if "::" not in test:
        report_id = report_id.split("::", 1)[0]
    starts = [report_id] + [report_id[i + 1:] for i, c in enumerate(report_id) if c == "/"]
    return any(start == test or start.startswith((test + "[", test + "::"))
               for start in starts)


def _runs_item(block: Block | None, gate: Gate | None, tree: Container[str] | None
               ) -> Item:
    if block is None:
        return Item("runs", NOT_READ, {"reason": "the card was not read"})
    if not block.test:
        detail = {"block_error": block.error} if block.error else {}
        return Item("runs", NONE_NAMED, detail)
    test = block.test
    file = _clean(test.split("::", 1)[0])
    if tree is not None and file not in tree:
        return Item("runs", FAIL, {"test": test,
                                   "reason": "the named test file is not in the landed tree"})
    if gate is None or gate.log is None:
        return Item("runs", NOT_READ, {"test": test, "reason": "the gate's log was not read"})
    matched = [report for report in reports(gate.log) if _names(test, report.id)]
    if not matched:
        return Item("runs", FAIL, {"test": test, "reason": "the named test is not in the "
                                   "gate's log", "source": gate.source})
    failed = sorted({report.id for report in matched if report.outcome == FAILED})
    if failed:
        return Item("runs", FAIL, {"test": test, "reason": "the named test failed",
                                   "failed": failed, "source": gate.source})
    if all(report.outcome == SKIPPED for report in matched):
        return Item("runs", FAIL, {"test": test, "reason": "the named test was skipped",
                                   "source": gate.source})
    # A count of report lines, not of tests: a file's progress line and its
    # tests' summary lines each count once.
    reported = sum(report.outcome == PASSED for report in matched)
    return Item("runs", PASS, {"test": test, "passed_reports": reported,
                               "source": gate.source})


def _gate_item(gate: Gate | None) -> Item:
    if gate is None:
        return Item("gate", NOT_READ, {"reason": "no gate result was read"})
    detail = {"conclusion": gate.conclusion, "source": gate.source}
    detail.update({k: v for k, v in (("commit", gate.commit), ("url", gate.url)) if v})
    word = (gate.conclusion or "").strip().lower()
    if word in PASSING:
        return Item("gate", PASS, detail)
    if word in FAILING:
        return Item("gate", FAIL, detail)
    return Item("gate", NO_CONCLUSION, detail)


def score(build: Mapping, diff: Iterable[str] | None, gate: Gate | None,
          block: Block | None, tree: Container[str] | None = None) -> Score:
    """Score one landed build.

    build is the build's record, whose id and project_id key the scores row.
    diff is the files the landed change touches (None when it was not read),
    gate the gate's result and log (None when neither was read), block the
    card's parsed slipway block (None when the card could not be read), and
    tree the files of the landed commit when it was read, so a named test
    that never landed fails even without a log.
    """
    return Score(int(build["id"]), int(build["project_id"]), _gate_item(gate),
                 _paths_item(block, diff), _runs_item(block, gate, tree))


# check_card.

@dataclass(frozen=True)
class Check:
    """check_card's answer. would_dispatch is True only when missing is empty.

    notes names the absences, which do not block (KTD5). collisions maps
    each in flight card the draft's paths overlap to the files they share.
    """

    would_dispatch: bool
    missing: tuple[str, ...]
    notes: tuple[str, ...]
    allowed_labels: tuple[str, ...]
    collisions: Mapping[str, tuple[str, ...]]
    block: Block


def _held(build: InFlightBuild) -> tuple[str, ...]:
    """The files an in flight build holds: its pushed diff once read, else its paths."""
    if build.state in PUSHED_STATES and build.pushed is not None:
        return tuple(build.pushed)
    return tuple(build.declared)


def check_card(draft, project, base_tree: Container[str],
               in_flight: Iterable[InFlightBuild] = (), other_lanes: Iterable[str] = ()
               ) -> Check:
    """Check a draft card the way dispatch would read it, changing nothing.

    draft carries labels and body (and key, once filed). base_tree holds the
    files of the project's base commit. in_flight is the project's other
    builds, as the records give them. other_lanes names lane labels that are
    not this project's, as the ready rule takes them.
    """
    missing: list[str] = []
    notes: list[str] = []
    lane = project.lane_label
    allowed = (lane,)
    lanes = {lane.lower(), *(name.lower() for name in other_lanes)}
    carried = sorted({label for label in draft.labels if label.lower() in lanes})
    if not carried:
        missing.append(f"the draft carries no lane label; add one of: {', '.join(allowed)}")
    elif len(carried) > 1:
        missing.append(f"the draft carries {len(carried)} lane labels ({', '.join(carried)}); "
                       f"keep one of: {', '.join(allowed)}")
    elif carried[0].lower() != lane.lower():
        missing.append(f"the draft is in lane {carried[0]}, which this project does not "
                       f"dispatch; the allowed labels are: {', '.join(allowed)}")

    block = parse(draft.body)
    collisions: dict[str, tuple[str, ...]] = {}
    if not block.ok:
        missing.append(block.error)
    else:
        if block.lane and block.lane.lower() != lane.lower():
            missing.append(f"the block names lane {block.lane}, but this project's lane is "
                           f"{lane}")
        if "good" in block.absent:
            notes.append("good is absent: the judged score will record no rubric")
        if block.test:
            file = _clean(block.test.split("::", 1)[0])
            if file not in base_tree and not any(covers(e, file) for e in block.paths):
                missing.append(f"the named test {file} is not in the base tree and not in "
                               "the card's paths")
        else:
            notes.append("test is absent: the runs item will record none named")
        if block.paths:
            key = getattr(draft, "key", None)
            for build in in_flight:
                if build.state not in IN_FLIGHT_STATES or build.card_key == key:
                    continue
                shared = tuple(sorted(f for f in _held(build)
                                      if any(overlap(p, f) for p in block.paths)))
                if shared:
                    collisions[build.card_key] = shared
                    missing.append(f"paths overlap card {build.card_key}, in flight "
                                   f"({build.state}): {', '.join(shared)}")
        else:
            notes.append("paths are absent: the paths item will record not declared, and "
                         "no in flight files were checked")
    return Check(not missing, tuple(missing), tuple(notes), allowed, collisions, block)
