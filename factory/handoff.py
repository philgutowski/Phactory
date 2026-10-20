"""The ready rule and the handoff for Slipway's own cloud cards.

A card is one GitHub issue on this repository, titled "U<n>: ..." for a plan
unit or "U<n> follow up: ..." for a follow up, carrying the lane label cloud
or operator and the fenced slipway block the card template shows. This
module reads the repository's issues, pull requests, and branches as the
GitHub REST API returns them (plain JSON, fetched by the caller, so nothing
here touches the network) and answers two questions:

    ready    which cloud cards can be dispatched now, and why the others cannot
    render   the handoff text a cloud session starts from, in the plan's shape

The ready rule, from the plan's Build Process section:

    1. The card is open and carries the cloud label.
    2. Its slipway block parses.
    3. Every unit on its Dependencies line is closed. An operator spike whose
       findings are recorded but whose issue stays open is named by the caller
       with assume_done, never guessed here.
    4. It is not already dispatched: no branch exists under its name and no
       open pull request closes it.
    5. None of its declared paths overlaps a path a dispatched card declares.
       Two cards on one file would race for the same lines; the second waits.

The handoff carries, in this order: base commit, branch, rules, files other
dispatched cards declare, and the done line. A cloud session opens its own
pull request, unlike a Slipway built card where Slipway opens it, because
until U14 lands there is no Slipway to open one.

Run as a command:

    python -m factory.handoff ready  --issues issues.json --pulls pulls.json \
        --branches branches.json [--assume-done U2]
    python -m factory.handoff render --card 12 --base <sha> --issues ... \
        --pulls ... --branches ... [--assume-done U2] [--branch <name>]

where issues.json is GET /repos/{owner}/{repo}/issues?state=all (every page),
pulls.json is GET .../pulls?state=open, and branches.json is GET .../branches.
"""

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from slipway.cards.contract import Block, parse

BRANCH_PREFIX = "slipway/"
CLOUD = "cloud"
OPERATOR = "operator"
LANES = (CLOUD, OPERATOR)

UNIT_TITLE = re.compile(r"^\s*U(?P<n>\d+)\b")
DEPENDENCIES = re.compile(r"\*\*Dependencies:\*\*\s*(?P<text>.*)")
DEPENDENCY_UNIT = re.compile(r"\bU(\d+)\b")
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)
SLUG_STOP = frozenset({"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
                       "its", "is", "are", "so", "as", "at", "by", "from", "that", "this"})
SLUG_WORDS = 4

RULES = (
    "Read CLAUDE.md in the repository root first and follow it: the environment setup, "
    "the gate, the first law, the import rule, and the no dashes rule. "
    "Define jargon on first use in one plain sentence, and write the explainer under "
    "docs/explainers/ (one file per unit; a follow up extends its unit's file)."
)

DONE = (
    "Tests, ruff, and the import test green under CI=true; the explainer written; "
    "a pull request opened from the branch with `Closes #{number}` in the body, the base "
    "commit recorded in the body, and auto merge enabled; then the pull request watched "
    "until it merges, with every blocking finding of the review check fixed and pushed."
)


@dataclass(frozen=True)
class Card:
    """One issue read as a card."""

    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    body: str
    unit: int | None
    dependencies: tuple[int, ...]
    block: Block

    @property
    def lane(self) -> str | None:
        lanes = [label for label in self.labels if label in LANES]
        return lanes[0] if len(lanes) == 1 else None

    @property
    def is_unit(self) -> bool:
        """True for the plan unit itself (U9: ...), false for a follow up."""
        return self.unit is not None and re.match(rf"^\s*U{self.unit}\s*:", self.title) is not None


@dataclass(frozen=True)
class Verdict:
    """ready's answer for one cloud card. reasons is empty when the card is ready."""

    card: Card
    branch: str
    reasons: tuple[str, ...] = ()
    in_flight: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return not self.reasons


def unit_number(title: str) -> int | None:
    match = UNIT_TITLE.match(title)
    return int(match.group("n")) if match else None


def dependencies(body: str) -> tuple[int, ...]:
    match = DEPENDENCIES.search(body or "")
    if not match:
        return ()
    return tuple(int(n) for n in DEPENDENCY_UNIT.findall(match.group("text")))


def slug(title: str) -> str:
    """A short branch slug from the words after the title's colon."""
    text = title.split(":", 1)[1] if ":" in title else title
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in SLUG_STOP]
    return "-".join(words[:SLUG_WORDS]) or "card"


def branch_name(card: Card) -> str:
    token = f"u{card.unit}" if card.unit is not None else f"issue-{card.number}"
    return f"{BRANCH_PREFIX}{token}-{slug(card.title)}"


def read_card(issue: dict) -> Card:
    body = issue.get("body") or ""
    return Card(
        number=int(issue["number"]),
        title=issue.get("title") or "",
        state=(issue.get("state") or "").lower(),
        labels=tuple(label["name"] if isinstance(label, dict) else str(label)
                     for label in issue.get("labels") or ()),
        body=body,
        unit=unit_number(issue.get("title") or ""),
        dependencies=dependencies(body),
        block=parse(body),
    )


def cards_of(issues: list[dict]) -> list[Card]:
    """Every issue as a card, pull requests (which the issues API also lists) left out."""
    return [read_card(issue) for issue in issues if "pull_request" not in issue]


def closes(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in CLOSES.findall(text or ""))


def overlaps(a: str, b: str) -> bool:
    """True when two declared paths can name the same file.

    A path ending in a slash is a directory and covers everything under it; a
    path with a wildcard is matched with fnmatch; otherwise the two must be
    equal. Each side is tried as the pattern.
    """
    if a == b:
        return True
    for x, y in ((a, b), (b, a)):
        if x.endswith("/") and y.startswith(x):
            return True
        if any(c in x for c in "*?[") and fnmatch.fnmatch(y, x):
            return True
    return False


def dispatched(cards: list[Card], pulls: list[dict], branches: list[dict]) -> dict[int, str]:
    """Card number to the branch that marks it dispatched (a branch or an open pull request)."""
    names = {b["name"] for b in branches}
    found: dict[int, str] = {}
    for pull in pulls:
        head = (pull.get("head") or {}).get("ref") or ""
        if not head.startswith(BRANCH_PREFIX):
            continue
        for number in closes(pull.get("body") or ""):
            found.setdefault(number, head)
    for card in cards:
        if card.state == "open" and card.number not in found and branch_name(card) in names:
            found[card.number] = branch_name(card)
    return found


def in_flight_files(cards: list[Card], active: dict[int, str], exclude: int | None = None
                    ) -> tuple[str, ...]:
    files: set[str] = set()
    for card in cards:
        if card.number in active and card.number != exclude:
            files.update(card.block.paths)
    return tuple(sorted(files))


def ready(issues: list[dict], pulls: list[dict], branches: list[dict],
          assume_done: tuple[int, ...] = ()) -> list[Verdict]:
    """A verdict for every open cloud card, ready ones first, then by number."""
    cards = cards_of(issues)
    closed_units = {c.unit for c in cards if c.is_unit and c.state == "closed"}
    closed_units.update(assume_done)
    active = dispatched(cards, pulls, branches)
    verdicts = []
    for card in cards:
        if card.state != "open" or card.lane != CLOUD:
            continue
        reasons = []
        if card.block.error:
            reasons.append(f"slipway block: {card.block.error}")
        missing = [f"U{n}" for n in card.dependencies if n not in closed_units]
        if missing:
            reasons.append("waits for " + ", ".join(missing))
        if card.number in active:
            reasons.append(f"dispatched on {active[card.number]}")
        flight = in_flight_files(cards, active, exclude=card.number)
        clashes = sorted({f for p in card.block.paths for f in flight if overlaps(p, f)})
        if clashes and card.number not in active:
            reasons.append("declares files in flight: " + ", ".join(clashes))
        verdicts.append(Verdict(card, branch_name(card), tuple(reasons), flight))
    verdicts.sort(key=lambda v: (not v.ready, v.card.number))
    return verdicts


def render(card: Card, base: str, branch: str, in_flight: tuple[str, ...],
           repository: str, rules: str = RULES) -> str:
    """The handoff text in the plan's shape: base, branch, rules, files, done, then the card."""
    if not base or not branch:
        raise ValueError("a handoff needs a base commit and a branch")
    files = "\n".join(f"   * {f}" for f in in_flight) or "   None."
    lines = [
        f"Slipway handoff for card #{card.number}: {card.title}",
        f"Repository: {repository}",
        "",
        f"1. Base: {base}",
        "   Start from this commit on main. The branch below already exists at it:",
        f"   git fetch origin {branch} && git checkout -B {branch} origin/{branch}",
        "",
        f"2. Branch: {branch}",
        "   Push every commit to this branch and to no other. Plain commit messages,",
        "   no co author trailers, no dashes as punctuation.",
        "",
        "3. Rules:",
        f"   {rules}",
        "",
        "4. Files other dispatched cards declare, which this card must not touch:",
        files,
        "",
        f"5. Done: {DONE.format(number=card.number)}",
        "",
        "How to finish:",
        "   * Run the gate from CLAUDE.md (ruff check ., CI=true python -m pytest -q) and",
        "     fix everything before the first push.",
        f"   * Open the pull request with the GitHub tools, base main, head {branch}.",
        f"     Title it like the card. The body starts with `Closes #{card.number}`, then",
        f"     `Base: {base}`, then what changed and where it differs from the card.",
        "   * Nobody approves the pull request by hand. Auto merge lands it once the",
        "     `ci` and `review` checks are green, so the review check is the review.",
        "   * Enable auto merge (squash). Subscribe to the pull request's activity and",
        "     stay until it merges: the `ci` check runs the gate and the `review` check",
        "     is a model review. Fix every blocking finding and push; answer advisory",
        "     ones in one line on the pull request. Never skip or weaken a test to get",
        "     green, never force push, never push an empty commit.",
        "   * When something in the card cannot be done as written, do everything else,",
        "     and say exactly what you left out and why in the pull request body.",
        "",
        "The card:",
        "",
        card.body.strip(),
        "",
    ]
    return "\n".join(lines)


def _load(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    # gh api --paginate concatenates pages as adjacent arrays; --slurp nests them.
    if data and isinstance(data[0], list):
        data = [item for page in data for item in page]
    return data


def _units(values: list[str]) -> tuple[int, ...]:
    return tuple(int(v.strip().upper().lstrip("U")) for v in values if v.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factory.handoff", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ready", "render"):
        p = sub.add_parser(name)
        p.add_argument("--issues", required=True)
        p.add_argument("--pulls", required=True)
        p.add_argument("--branches", required=True)
        p.add_argument("--assume-done", action="append", default=[],
                       help="a unit whose findings are recorded though its issue is open")
        if name == "render":
            p.add_argument("--card", type=int, required=True, help="the issue number")
            p.add_argument("--base", required=True, help="the commit on main to start from")
            p.add_argument("--repository", required=True, help="owner/name")
            p.add_argument("--branch", help="override the computed branch name")
    args = parser.parse_args(argv)
    issues, pulls, branches = _load(args.issues), _load(args.pulls), _load(args.branches)
    verdicts = ready(issues, pulls, branches, _units(args.assume_done))

    if args.command == "ready":
        out = [{"number": v.card.number, "title": v.card.title, "branch": v.branch,
                "ready": v.ready, "reasons": list(v.reasons), "paths": list(v.card.block.paths),
                "in_flight": list(v.in_flight)} for v in verdicts]
        print(json.dumps(out, indent=2))
        return 0

    verdict = next((v for v in verdicts if v.card.number == args.card), None)
    if verdict is None:
        print(f"#{args.card} is not an open cloud card", file=sys.stderr)
        return 2
    if not verdict.ready:
        print(f"#{args.card} is not ready: " + "; ".join(verdict.reasons), file=sys.stderr)
        return 1
    print(render(verdict.card, args.base, args.branch or verdict.branch, verdict.in_flight,
                 args.repository))
    return 0


if __name__ == "__main__":
    sys.exit(main())
