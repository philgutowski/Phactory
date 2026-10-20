"""The KTD23 dependency rule, checked against src/slipway.

A layer is the first name under slipway: slipway.mcp.server is in layer
mcp, slipway.cli is in layer cli. The walk reads every .py file under
src/slipway with the ast module (it never imports them, so nothing under
test runs), resolves each import statement, relative ones included, to a
module name, and checks each edge (importer, imported) against the rules
below. A forbidden edge is reported as "importer -> imported" with the file
and line, so the failure names exactly what to move.

A rule whose layer does not exist yet is skipped by name, so the rule is in
place before the first card that adds the layer.
"""

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

SURFACES = frozenset({"mcp", "page", "cli"})
SURFACE_MAY_IMPORT = frozenset({"service", "config", "auth", "onboard"})
ABOVE_SERVICE = SURFACES | {"workflow", "app"}


@dataclass(frozen=True)
class Edge:
    importer: str
    imported: str
    path: Path
    line: int

    def __str__(self) -> str:
        return f"{self.importer} -> {self.imported} ({self.path}:{self.line})"


def layer(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] != "slipway" or len(parts) < 2:
        return None
    return parts[1]


def module_name(root: Path, path: Path) -> tuple[str, bool]:
    """Return the dotted name of a file under root and whether it is a package."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def edges_of(root: Path, path: Path) -> list[Edge]:
    importer, is_package = module_name(root, path)
    package = importer.split(".") if is_package else importer.split(".")[:-1]
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                stem = ".".join([*base, node.module] if node.module else base)
            else:
                stem = node.module or ""
            # "from slipway import workflow" imports the workflow layer, so
            # the name joins the stem only when the stem is the root package.
            if stem == "slipway":
                targets = [f"{stem}.{alias.name}" for alias in node.names]
            else:
                targets = [stem]
        else:
            continue
        found += [Edge(importer, t, path, node.lineno) for t in targets if layer(t)]
    return found


def walk(root: Path) -> list[Edge]:
    """Every slipway to slipway import edge under root/slipway."""
    return [e for p in sorted((root / "slipway").rglob("*.py")) for e in edges_of(root, p)]


def layers_present(root: Path) -> set[str]:
    present = set()
    for p in (root / "slipway").rglob("*.py"):
        name = layer(module_name(root, p)[0])
        if name:
            present.add(name)
    return present


@dataclass(frozen=True)
class Rule:
    name: str
    needs: str  # the layer whose existence makes the rule meaningful
    forbids: Callable[[str, str], bool]


def _surface_reach(src: str, dst: str) -> bool:
    a, b = layer(src), layer(dst)
    return a in SURFACES and b != a and b not in SURFACE_MAY_IMPORT


def _service_upward(src: str, dst: str) -> bool:
    return layer(src) == "service" and layer(dst) in ABOVE_SERVICE


def _into_workflow(src: str, dst: str) -> bool:
    return layer(dst) == "workflow" and layer(src) != "workflow"


def _workflow_upward(src: str, dst: str) -> bool:
    return layer(src) == "workflow" and layer(dst) in SURFACES | {"app"}


def _machine_to_surface(src: str, dst: str) -> bool:
    return src == "slipway.onboard.machine" and layer(dst) in SURFACES


RULES = [
    Rule("surfaces import service, config, auth, and onboard only", "service", _surface_reach),
    Rule("service imports no surface, workflow, or app", "service", _service_upward),
    Rule("workflow is imported by nothing outside workflow", "workflow", _into_workflow),
    Rule("workflow imports nothing above it", "workflow", _workflow_upward),
    Rule("onboard/machine.py imports no surface", "onboard", _machine_to_surface),
]


def violations(root: Path, rules=RULES) -> list[str]:
    edges = walk(root)
    return [f"{rule.name}: {e}" for rule in rules for e in edges
            if rule.forbids(e.importer, e.imported)]


def test_walk_sees_the_package():
    assert (SRC / "slipway" / "__init__.py").is_file()
    assert walk(SRC) is not None


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.name)
def test_rule_holds(rule):
    present = layers_present(SRC)
    if rule.needs not in present:
        pytest.skip(f"slipway.{rule.needs} does not exist yet")
    assert violations(SRC, [rule]) == []


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "src"
    for rel, text in {"slipway/__init__.py": "", **files}.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


def test_mcp_importing_workflow_is_named(tmp_path):
    root = _tree(tmp_path, {
        "slipway/mcp/__init__.py": "",
        "slipway/mcp/tools.py": "from slipway.workflow import card\n",
        "slipway/workflow/__init__.py": "",
        "slipway/workflow/card.py": "",
    })
    found = violations(root)
    assert any("slipway.mcp.tools -> slipway.workflow" in v for v in found), found
    assert any(v.startswith("surfaces import") for v in found), found


def test_relative_import_resolves(tmp_path):
    root = _tree(tmp_path, {
        "slipway/mcp/__init__.py": "",
        "slipway/mcp/tools.py": "from ..workflow import card\n",
        "slipway/workflow/__init__.py": "",
    })
    assert any("slipway.mcp.tools -> slipway.workflow" in v for v in violations(root))


def test_allowed_edges_pass(tmp_path):
    root = _tree(tmp_path, {
        "slipway/mcp/tools.py": "from slipway.service import status\nfrom . import audit\n",
        "slipway/service/status.py": "import slipway.records.store\n",
        "slipway/workflow/worker.py": "from slipway.workflow import card\n",
        "slipway/onboard/machine.py": "from slipway import service\n",
    })
    assert violations(root) == []
