"""The closed halt set (KTD11): seven classes, each with its kind and evidence."""

import ast
import re
from pathlib import Path

import pytest

import slipway
from slipway import halts
from slipway.halts import Halt, HaltError, Kind, halt
from slipway.trackers import base

SEVEN = {
    "merged_card_not_moved", "commit_mismatch", "tracker_unreadable", "git_unreadable",
    "builder_failed", "builder_expired", "binding_invalid",
}

# One halt per class, with the evidence its needs you line requires. Verify
# produces the first, second, and fourth in test_verify.py; the build outcomes
# and tracker_unreadable are produced by their callers, so they are built here.
EXAMPLES = {
    "merged_card_not_moved": dict(card_key="13", merged="abc", tracker_status="In progress"),
    "commit_mismatch": dict(landing_path="merge_queue", relation="merged_equals_tested",
                            expected="aaa", found="bbb"),
    "tracker_unreadable": dict(card_key="13", reason="GitHub GraphQL budget exhausted"),
    "git_unreadable": dict(reason="git fetch exited 128"),
    "builder_failed": dict(reason="provider error at start"),
    "builder_expired": dict(budget_seconds=7200),
    "binding_invalid": dict(role="builder", reason="no account for the role"),
}


def test_the_set_is_exactly_seven():
    assert set(halts.CLASSES) == SEVEN
    assert set(EXAMPLES) == SEVEN


def test_a_class_outside_the_set_is_refused_at_construction():
    with pytest.raises(HaltError, match="closed"):
        Halt("gate_red", {})
    with pytest.raises(HaltError):
        halt("runner_regressed", reason="x")


@pytest.mark.parametrize("name", sorted(SEVEN))
def test_every_class_is_produced_and_renders_its_line(name):
    found = halt(name, **EXAMPLES[name])
    assert found.name == name
    line = found.line()
    for value in EXAMPLES[name].values():
        assert str(value) in line


@pytest.mark.parametrize("name", sorted(SEVEN))
def test_missing_evidence_is_refused(name):
    evidence = dict(EXAMPLES[name])
    dropped = halts.CLASSES[name].evidence_keys[0]
    del evidence[dropped]
    with pytest.raises(HaltError, match=dropped):
        halt(name, **evidence)


def test_builder_expired_is_a_build_outcome_and_commit_mismatch_a_verify_halt():
    expired = halt("builder_expired", **EXAMPLES["builder_expired"])
    assert expired.kind is Kind.BUILD_OUTCOME and not expired.is_wait
    mismatch = halt("commit_mismatch", **EXAMPLES["commit_mismatch"])
    assert mismatch.kind is Kind.VERIFY_HALT and mismatch.is_wait


def test_kinds_split_as_ktd11_says():
    waits = {n for n, c in halts.CLASSES.items() if c.is_wait}
    assert waits == {"merged_card_not_moved", "commit_mismatch", "tracker_unreadable",
                     "git_unreadable"}


def test_the_tracker_constant_names_a_member():
    assert base.TRACKER_UNREADABLE == halts.TRACKER_UNREADABLE
    assert base.skipped("down").halt_class in halts.CLASSES


# Halt class literals outside halts.py (issue #53). A halt class reaches Halt
# as a plain string written somewhere under src/slipway, and a typo there is
# only refused when a caller builds the Halt. So this scan reads every module
# as a syntax tree (ast, Python's own parse of source into nodes such as
# "assignment" or "call") and collects the string constants that sit where a
# halt class goes. A plain grep for every quoted string would flag dict keys
# and state names, so the scan looks only at these positions:
#
# - the first argument, or name=, of a call to Halt or halt
# - an assignment to a name or attribute ending in halt_class, or ending in a
#   halt suffix such as _INVALID or _UNREADABLE (the self named constants)
# - a halt_class= keyword argument, a halt_class function's return value, and
#   either side of a comparison against halt_class
# - any string shaped like a halt class: snake case ending in a halt suffix
#
# Inside those positions a bare name is followed to the string the same module
# assigns to it, so halt(KIND) is checked through KIND = "...".

SOURCE = Path(slipway.__file__).parent
SUFFIXES = {name.rsplit("_", 1)[-1] for name in SEVEN}  # moved, mismatch, unreadable, ...
_SNAKE = re.compile(r"[a-z]+(?:_[a-z]+)+")


def _ident(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _feeds_a_halt(name: str | None) -> bool:
    if name is None:
        return False
    lowered = name.lower()
    return lowered.endswith("halt_class") or lowered.rsplit("_", 1)[-1] in SUFFIXES


def _is_string(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def halt_literals(source: str, filename: str = "<test>") -> list[tuple[int, str]]:
    """Every (line, string) in source that sits where a halt class goes."""
    tree = ast.parse(source, filename)
    assigned: dict[str, list[ast.Constant]] = {}
    fed: list[ast.AST] = []
    found: list[ast.Constant] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = _ident(target)
                if name and _is_string(node.value):
                    assigned.setdefault(name, []).append(node.value)
                if _feeds_a_halt(name):
                    fed.append(node.value)
        elif isinstance(node, ast.Call):
            if _ident(node.func) in ("Halt", "halt"):
                fed.extend(node.args[:1])
                fed.extend(k.value for k in node.keywords if k.arg == "name")
            fed.extend(k.value for k in node.keywords if k.arg == "halt_class")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "halt_class":
                fed.extend(r.value for r in ast.walk(node)
                           if isinstance(r, ast.Return) and r.value is not None)
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(_feeds_a_halt(_ident(side)) for side in sides):
                fed.extend(sides)
        elif _is_string(node) and _SNAKE.fullmatch(node.value):
            if node.value.rsplit("_", 1)[-1] in SUFFIXES:
                found.append(node)
    for expr in fed:
        for sub in ast.walk(expr):
            if _is_string(sub):
                found.append(sub)
            elif isinstance(sub, ast.Name):
                found.extend(assigned.get(sub.id, []))
    return sorted({(n.lineno, n.value) for n in found})


def _scan_source() -> set[tuple[str, int, str]]:
    return {
        (path.relative_to(SOURCE).as_posix(), line, value)
        for path in sorted(SOURCE.rglob("*.py"))
        for line, value in halt_literals(path.read_text(), str(path))
    }


def test_every_halt_literal_in_the_source_is_a_member_of_the_set():
    found = _scan_source()
    outside = sorted(f"src/slipway/{p}:{line}: {v!r}" for p, line, v in found
                     if v not in halts.CLASSES)
    assert not outside, "halt class literals outside halts.CLASSES:\n" + "\n".join(outside)
    # The scan reaches the literals the review of #49 named, so a scan that
    # silently finds nothing cannot pass.
    where = {(p, v) for p, _, v in found}
    assert ("config/bindings.py", "binding_invalid") in where
    assert ("trackers/base.py", "tracker_unreadable") in where
    assert ("builders/base.py", "builder_failed") in where


# One deliberately misspelled or invented class per position the scan covers.
DRIFT = {
    "halt_class constant": 'HALT_CLASS = "binding_invlid"\n',
    "self named constant": 'BINDING_INVALID = "binding_invlid"\n',
    "halt positional": 'halt("comit_mismatch", reason="x")\n',
    "Halt name keyword": 'Halt(name="git_unredable")\n',
    "halt_class keyword": 'replace(result, halt_class="builder_faild")\n',
    "name followed to its string": 'KIND = "buildr_expired"\nhalt(KIND, reason="x")\n',
    "class attribute": 'class E(Exception):\n    halt_class = "tracker_unreadble"\n',
    "property return": (
        "class S:\n    @property\n    def halt_class(self):\n"
        '        return "tracker_unreadble" if self.skipped else None\n'
    ),
    "comparison": 'if status.halt_class == "trackr_unreadable":\n    pass\n',
    "halt shaped string": 'REASONS = {"why": "gate_failed"}\n',
}


@pytest.mark.parametrize("case", sorted(DRIFT))
def test_the_scan_catches_a_literal_outside_the_set(case):
    outside = {v for _, v in halt_literals(DRIFT[case])} - set(halts.CLASSES)
    assert outside, f"the scan missed the drift in {case!r}"


def test_the_scan_ignores_strings_that_do_not_feed_a_halt():
    source = (
        '"""A docstring may say binding_invalid or gate_failed in prose."""\n'
        'RETURNED_EMPTY = "returned_empty"\n'
        'VERIFY_HALT = "verify_halt"\n'
        'payload = {"card_key": "13", "landing_path": "merge_queue"}\n'
        'halt(BINDING_INVALID, role="builder", reason="no account")\n'
    )
    assert halt_literals(source) == []
