"""The review check: a model reads the pull request against its card and the brief.

.github/workflows/review.yml runs this on every pull request. It gathers the
diff, the card the pull request closes, and a few facts a script can settle
better than a model can (which files sit outside the card's paths, which
belong to another dispatched card, where a dash was used as punctuation),
hands all of it with the brief to Claude Code in print mode, and turns the
structured verdict into a pull request review and an exit status:

    0   verdict pass, the check is green
    1   verdict block, or the review could not run

A review that could not run (no key, a provider error, no verdict) is red,
never silently green: a check that passes without having reviewed anything
would be no check at all.

Everything the script needs comes from the environment the workflow sets:

    REPO, PR_NUMBER, BASE_SHA, HEAD_SHA   the pull request
    CLAUDE_CODE_OAUTH_TOKEN               the reviewer's subscription token, from a
                                          repository secret, or ANTHROPIC_API_KEY for
                                          API billing; one of the two must be set
    GH_TOKEN                              the job's token, to read the card and post
    REVIEW_MODEL, REVIEW_EFFORT           the model and its effort (default the brief's)
    REVIEW_BUDGET_USD                     a cap on one review's spend
    REVIEW_POST                           "0" prints the review instead of posting it

The pure parts (facts from a diff, the verdict from the CLI's JSON, the review
body) take and return values, so tests/unit/test_factory.py drives them with
no git, no gh, and no model.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from factory.handoff import closes, overlaps
from slipway.cards.contract import parse

HERE = Path(__file__).resolve().parent
BRIEF = HERE / "review-brief.md"
DEFAULT_MODEL = "claude-fable-5-1"
DEFAULT_EFFORT = "high"
DEFAULT_BUDGET_USD = "8"
DIFF_INLINE_LIMIT = 400_000  # bytes; a larger diff is left in a file for the model to read
BRANCH_PREFIX = "slipway/"
# One of these must be set: a subscription token from `claude setup-token`, or an API key.
CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

# Read only. Bash is limited to the three git verbs a reviewer needs.
ALLOWED_TOOLS = "Read,Glob,Grep,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "block"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocking", "advisory"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["severity", "file", "line", "title", "detail"],
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
}

# Written as code points so this file itself holds no dash character.
DASHES = {chr(0x2014): "em dash", chr(0x2013): "en dash"}
DOUBLE_HYPHEN = re.compile(r"(?<![-\w])--(?![-\w])|\w--\w")


@dataclass(frozen=True)
class Finding:
    severity: str
    file: str
    line: int
    title: str
    detail: str


@dataclass(frozen=True)
class Verdict:
    verdict: str
    summary: str
    findings: tuple[Finding, ...]
    cost_usd: float | None = None

    @property
    def blocked(self) -> bool:
        return self.verdict == "block" or any(f.severity == "blocking" for f in self.findings)


@dataclass(frozen=True)
class Facts:
    """What the script settled before the model read anything."""

    touched: tuple[str, ...]
    outside_paths: tuple[str, ...]
    in_flight_clashes: tuple[str, ...]
    dashes: tuple[str, ...]
    double_hyphens: tuple[str, ...]
    commit_messages: tuple[str, ...]
    closes: tuple[int, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)


def touched_files(name_only: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in name_only.splitlines() if line.strip())


def outside_paths(touched: tuple[str, ...], paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f for f in touched if not any(overlaps(f, p) for p in paths))


def clashes(touched: tuple[str, ...], others: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f for f in touched if any(overlaps(f, p) for p in others))


def dash_findings(diff: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Added lines holding a dash character, and added lines holding a bare double hyphen.

    The first list is a rule break on its face. The second is for the model to
    judge: a double hyphen opening a flag is code, one between two words is
    punctuation.
    """
    dashes, doubles = [], []
    file = "?"
    for line in diff.splitlines():
        if line.startswith("+++ "):
            file = line[4:].removeprefix("b/")
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        text = line[1:]
        for char, name in DASHES.items():
            if char in text:
                dashes.append(f"{file}: {name}: {text.strip()[:120]}")
        if DOUBLE_HYPHEN.search(text):
            doubles.append(f"{file}: {text.strip()[:120]}")
    return tuple(dashes), tuple(doubles)


def facts_from(diff: str, name_only: str, card_paths: tuple[str, ...],
               other_paths: tuple[str, ...], commit_messages: tuple[str, ...],
               pr_body: str) -> Facts:
    touched = touched_files(name_only)
    dashes, doubles = dash_findings(diff)
    for i, message in enumerate(commit_messages, 1):
        for char, name in DASHES.items():
            if char in message:
                dashes += (f"commit {i}: {name}: {message.strip().splitlines()[0][:120]}",)
    return Facts(
        touched=touched,
        outside_paths=outside_paths(touched, card_paths),
        in_flight_clashes=clashes(touched, other_paths),
        dashes=dashes,
        double_hyphens=doubles,
        commit_messages=commit_messages,
        closes=closes(pr_body),
    )


def _list(items, empty="None."):
    return "\n".join(f"- {item}" for item in items) if items else empty


def prompt_for(brief: str, card_title: str, card_body: str, facts: Facts, diff: str,
               diff_path: str | None = None) -> str:
    """The whole prompt: the brief, the card, the facts, then the diff."""
    block = parse(card_body)
    parts = [
        brief.strip(),
        "",
        "## The card",
        "",
        f"Title: {card_title or 'no card: the pull request closes no issue'}",
        "",
        card_body.strip() or "(no card body)",
        "",
        "## Facts the script settled",
        "",
        f"Definition of done (good): {block.good or 'not declared'}",
        f"Named test: {block.test or 'not declared'}",
        f"Declared paths: {', '.join(block.paths) or 'none declared'}",
        "",
        "Files the pull request touches:",
        _list(facts.touched),
        "",
        "Touched files outside the card's declared paths (fine when explained):",
        _list(facts.outside_paths),
        "",
        "Touched files that another dispatched card declares (blocking):",
        _list(facts.in_flight_clashes),
        "",
        "Added lines or commit messages with an em dash or en dash (blocking):",
        _list(facts.dashes),
        "",
        "Added lines with a bare double hyphen (judge each: a flag is code, "
        "punctuation is a rule break):",
        _list(facts.double_hyphens),
        "",
        "Issues the pull request body closes: "
        + (", ".join(f"#{n}" for n in facts.closes) or "none"),
        "",
        "Commit messages:",
        _list(m.strip().splitlines()[0] for m in facts.commit_messages),
        "",
    ]
    parts += [f"- note: {n}" for n in facts.notes]
    parts += ["", "## The diff", ""]
    if diff_path:
        parts.append(f"The diff is {len(diff)} bytes, too long to inline. Read it from "
                     f"{diff_path} in parts, and the files themselves from the tree.")
    else:
        parts += ["```diff", diff.rstrip("\n"), "```"]
    return "\n".join(parts) + "\n"


def verdict_from(cli_json: str) -> Verdict:
    """The verdict out of `claude -p --output-format json` output, or a block that says why not."""
    try:
        data = json.loads(cli_json)
    except ValueError as error:
        return Verdict("block", f"the reviewer returned no JSON: {error}", ())
    cost = data.get("total_cost_usd")
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        why = str(data.get("result") or data.get("subtype") or "unknown error")[:500]
        return Verdict("block", f"the review could not run: {why}", (), cost)
    out = data.get("structured_output")
    if not isinstance(out, dict) or out.get("verdict") not in ("pass", "block"):
        return Verdict("block", "the reviewer returned no verdict in the schema's shape", (),
                       cost)
    findings = []
    for item in out.get("findings") or ():
        try:
            findings.append(Finding(
                severity="blocking" if item.get("severity") == "blocking" else "advisory",
                file=str(item.get("file") or "?"),
                line=int(item.get("line") or 0),
                title=str(item.get("title") or "").strip() or "(untitled)",
                detail=str(item.get("detail") or "").strip(),
            ))
        except (TypeError, ValueError):
            continue
    return Verdict(out["verdict"], str(out.get("summary") or "").strip(), tuple(findings), cost)


def review_body(verdict: Verdict, model: str, run_url: str | None = None) -> str:
    """The pull request review's markdown."""
    mark = "Blocked" if verdict.blocked else "Passed"
    lines = [f"## Review check: {mark}", "", verdict.summary or "(no summary)", ""]
    blocking = [f for f in verdict.findings if f.severity == "blocking"]
    advisory = [f for f in verdict.findings if f.severity != "blocking"]
    for title, items in (("Blocking", blocking), ("Advisory", advisory)):
        if not items:
            continue
        lines += [f"### {title}", ""]
        for f in items:
            where = f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"
            lines.append(f"- {where} {f.title}" + (f". {f.detail}" if f.detail else ""))
        lines.append("")
    if blocking:
        lines += ["Fix each blocking finding and push; the check runs again on the new head.",
                  ""]
    tail = f"Reviewed by `{model}`"
    if verdict.cost_usd is not None:
        tail += f" for ${verdict.cost_usd:.2f}"
    if run_url:
        tail += f", [log]({run_url})"
    lines += [f"_{tail}. The brief is `factory/review-brief.md`._"]
    return "\n".join(lines) + "\n"


# Everything below touches git, gh, or the model, and is not under unit test.

def _run(args, **kw) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, **kw).stdout


def _gh(path: str, method: str = "GET", body: dict | None = None):
    args = ["gh", "api", "-X", method, path]
    if body is not None:
        args += ["--input", "-"]
    out = _run(args, input=json.dumps(body) if body is not None else None)
    return json.loads(out) if out.strip() else None


def _other_dispatched_paths(repo: str, pr_number: int) -> tuple[str, ...]:
    """Paths declared by the cards other open slipway/ pull requests close."""
    paths: set[str] = set()
    for pull in _gh(f"repos/{repo}/pulls?state=open&per_page=100") or ():
        if pull["number"] == pr_number:
            continue
        if not (pull.get("head") or {}).get("ref", "").startswith(BRANCH_PREFIX):
            continue
        for number in closes(pull.get("body") or ""):
            issue = _gh(f"repos/{repo}/issues/{number}")
            paths.update(parse(issue.get("body") or "").paths)
    return tuple(sorted(paths))


def run_review(env: dict) -> int:
    repo, number = env["REPO"], int(env["PR_NUMBER"])
    base, head = env["BASE_SHA"], env["HEAD_SHA"]
    model = env.get("REVIEW_MODEL") or DEFAULT_MODEL
    effort = env.get("REVIEW_EFFORT") or DEFAULT_EFFORT
    budget = env.get("REVIEW_BUDGET_USD") or DEFAULT_BUDGET_USD
    post = env.get("REVIEW_POST", "1") != "0"
    run_url = env.get("RUN_URL")
    work = Path(env.get("RUNNER_TEMP") or env.get("TMPDIR") or "/tmp") / "slipway-review"
    work.mkdir(parents=True, exist_ok=True)

    if not any(env.get(name, "").strip() for name in CREDENTIALS):
        verdict = Verdict("block", "the review could not run: neither the "
                          "CLAUDE_CODE_OAUTH_TOKEN secret nor ANTHROPIC_API_KEY is set on "
                          "this repository", ())
        return _finish(verdict, model, repo, number, head, post, run_url, env)

    pull = _gh(f"repos/{repo}/pulls/{number}")
    pr_body = pull.get("body") or ""
    card_title, card_body = "", ""
    closed = closes(pr_body)
    if closed:
        issue = _gh(f"repos/{repo}/issues/{closed[0]}")
        card_title, card_body = issue.get("title") or "", issue.get("body") or ""

    diff = _run(["git", "diff", f"{base}...{head}"])
    name_only = _run(["git", "diff", "--name-only", f"{base}...{head}"])
    messages = tuple(m for m in _run(
        ["git", "log", "--format=%B%x00", f"{base}..{head}"]).split("\0") if m.strip())
    facts = facts_from(diff, name_only, parse(card_body).paths,
                       _other_dispatched_paths(repo, number), messages, pr_body)

    diff_path = None
    if len(diff.encode()) > DIFF_INLINE_LIMIT:
        diff_path = str(work / "diff.patch")
        Path(diff_path).write_text(diff)
    prompt = prompt_for(BRIEF.read_text(), card_title, card_body, facts, diff, diff_path)
    (work / "prompt.md").write_text(prompt)

    result = subprocess.run(
        ["claude", "-p", "--model", model, "--effort", effort,
         "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
         "--allowedTools", ALLOWED_TOOLS, "--no-session-persistence",
         "--max-budget-usd", budget],
        input=prompt, capture_output=True, text=True,
    )
    if result.returncode != 0 and not result.stdout.strip():
        verdict = Verdict("block", "the review could not run: claude exited "
                          f"{result.returncode}: {result.stderr.strip()[:500]}", ())
    else:
        verdict = verdict_from(result.stdout)
    return _finish(verdict, model, repo, number, head, post, run_url, env)


def _finish(verdict: Verdict, model: str, repo: str, number: int, head: str, post: bool,
            run_url: str | None, env: dict) -> int:
    body = review_body(verdict, model, run_url)
    print(body)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(body)
    if post:
        try:
            _gh(f"repos/{repo}/pulls/{number}/reviews", "POST",
                {"event": "COMMENT", "body": body, "commit_id": head})
        except subprocess.CalledProcessError as error:
            print(f"::warning::could not post the review: {error.stderr.strip()[:300]}",
                  file=sys.stderr)
    if verdict.blocked:
        print("::error::the review check found a blocking finding, or could not run")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_review(dict(os.environ)))
