"""GitHub Projects tracker adapter, read only (KTD8).

Derived from native-relay/skills/relay/scripts/relay/adapters/github.py.
Kept: make_run, _gh, _issue, candidates, ready, read, status, and the run
injection seam. Dropped: the closeout, filing, and triple coordinator
methods, and the comment search closing_reference (Slipway never writes the
comment it searched for). Added: labels on every read, and closing_reference
renders the "Closes #N" line GitHub's own automation keys on.

Reads go through the gh command line tool, which holds the operator's
authentication, so this module carries no secret. The run callable is
injectable: tests answer from fixtures, and gh may be absent where they run.

The board read changed on purpose. The origin listed the whole project board
with gh project item-list, which costs hundreds of GraphQL points a call (405
on a 313 card board, measured on 2026-10-14). GraphQL is GitHub's query API;
each query is charged points against a budget of 5,000 points an hour per
account, not per token, shared by every project and every process the
account runs. So this adapter never lists a board. It asks each issue which
project items it belongs to (repository.issue(number).projectItems), at one
point per card, and when the budget is spent the read is a tracker_unreadable
skip, never a crash.

A card is terminal when its issue is closed, or when its item on the
project's board reads the project's landed status.

Onboarding's listings (U28) use gh's own commands: gh project list for the
boards the owner holds, gh label list for the repository's labels (a card
is an issue, and an issue carries the repository's labels), gh project
field-list for the Status field's options, and gh project view for whether
the board exists. They are called once per question, not per card, so their
GraphQL cost is a handful of points an onboarding.

GitHubScratch files the board movement check's scratch card as an issue in
the repository, without the lane label so watch never dispatches it, and
closes it as not planned afterwards (KTD27, U28).
"""

import json
import re
import subprocess

from slipway.trackers.base import (
    NETWORK_TIMEOUT_SECONDS,
    Card,
    ClosingReference,
    TrackerStatus,
    skipped,
)

# gh issue list stops at 30 issues unless told otherwise.
READY_ISSUE_LIMIT = 1000
ISSUE_FIELDS = "number,title,body,state,labels"
CLOSED_STATE = "CLOSED"
# The single select field a GitHub Projects board names its columns by. It
# is GitHub's own default field name, not a project value.
STATUS_FIELD = "Status"
# Project memberships per page, and how many pages one card may take. An
# issue on more boards than this is a reason, not a guess.
MEMBERSHIP_PAGE_SIZE = 20
MEMBERSHIP_PAGE_LIMIT = 5

# One issue's state and its project memberships: one GraphQL point. Nothing
# here lists a project's items.
MEMBERSHIP_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $field: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      state
      projectItems(first: PAGE_SIZE, after: $cursor) {
        nodes {
          project { number owner { ... on User { login } ... on Organization { login } } }
          fieldValueByName(name: $field) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".replace("PAGE_SIZE", str(MEMBERSHIP_PAGE_SIZE))

# How many boards, labels, and fields one listing asks gh for.
LISTING_LIMIT = 100
LABEL_LIMIT = 1000
# What gh says when a project number names no board.
NO_BOARD = re.compile(r"could not resolve to a projectv2|not found", re.IGNORECASE)
# The issue number at the end of the URL gh issue create prints.
ISSUE_URL = re.compile(r"/issues/(\d+)\s*$")

# What gh and the GraphQL API say when the hourly budget is spent.
BUDGET_SIGNS = ("rate limit", "rate_limited", "secondary rate")


def make_run(cwd=None, env=None):
    """The default transport: gh, bounded, output captured, no stdin."""

    def run(args, timeout=NETWORK_TIMEOUT_SECONDS):
        return subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, check=False)

    return run


def _budget(text: str) -> bool:
    lowered = text.lower()
    return any(sign in lowered for sign in BUDGET_SIGNS)


def _labels(raw) -> tuple[str, ...]:
    return tuple(str((label or {}).get("name")) for label in raw or [] if label)


class GitHubTracker:
    def __init__(self, project, run=None):
        owner, _, name = project.repository.partition("/")
        if not owner or not name:
            raise ValueError(f"repository {project.repository!r} is not owner/name")
        self._repository = project.repository
        self._owner, self._name = owner, name
        # board is "<owner>/<number>", or "<number>" for a board owned by the
        # repository's owner. Onboarding builds an adapter before the board is
        # chosen, to list the boards; an empty board names none.
        board_owner, _, number = project.board.rpartition("/")
        if project.board and not number.isdigit():
            raise ValueError(f"board {project.board!r} is not <owner>/<number> or <number>")
        self._board_owner = (board_owner or owner).lower()
        self._board_number = int(number) if number.isdigit() else None
        self._lane = project.lane_label
        self._landed = project.landed_status
        self._run = run or make_run()

    # Transport.
    def _gh(self, args):
        """Returns (payload, None) or (None, reason). A failure is a reason, never a crash."""
        try:
            proc = self._run(list(args), timeout=NETWORK_TIMEOUT_SECONDS)
        except subprocess.CalledProcessError as exc:
            proc = subprocess.CompletedProcess(exc.cmd, exc.returncode, exc.stdout, exc.stderr)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"gh could not run: {exc}"
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            if _budget(err):
                return None, f"GitHub GraphQL budget exhausted: {err}"
            return None, f"gh exited {proc.returncode}: {err}"
        try:
            return json.loads(proc.stdout or "null"), None
        except ValueError as exc:
            return None, f"gh returned output that is not JSON: {exc}"

    def _graphql(self, query, variables):
        args = ["gh", "api", "graphql", "-f", "query=" + query]
        for key in sorted(variables):
            value = variables[key]
            if value is None:
                continue
            # -F types a number as a number, -f keeps a string a string.
            flag = "-F" if isinstance(value, int) else "-f"
            args.extend([flag, f"{key}={value}"])
        payload, reason = self._gh(args)
        if payload is None:
            return None, reason
        if not isinstance(payload, dict):
            return None, "GitHub GraphQL returned a non object payload"
        errors = payload.get("errors") or []
        if errors:
            messages = "; ".join(str(e.get("message") or e) for e in errors)
            kinds = " ".join(str(e.get("type") or "") for e in errors)
            if _budget(messages + " " + kinds):
                return None, f"GitHub GraphQL budget exhausted: {messages}"
            return None, f"GitHub GraphQL returned errors: {messages}"
        data = payload.get("data")
        if not isinstance(data, dict):
            return None, "GitHub GraphQL returned no data"
        return data, None

    def _issue(self, key):
        return self._gh(["gh", "issue", "view", str(key), "--repo", self._repository,
                         "--json", ISSUE_FIELDS])

    def _membership(self, key):
        """Returns (state, column, reason) for one issue on the project's board.

        column is None when the issue is not on the board or its item has no
        status set. Pages through the issue's memberships, not the board's
        items, so the cost is one point a page whatever the board's size.
        """
        try:
            number = int(str(key).lstrip("#"))
        except ValueError:
            return None, None, f"{key!r} is not an issue number"
        cursor = None
        for _ in range(MEMBERSHIP_PAGE_LIMIT):
            data, reason = self._graphql(MEMBERSHIP_QUERY, {
                "owner": self._owner, "name": self._name, "number": number,
                "field": STATUS_FIELD, "cursor": cursor,
            })
            if data is None:
                return None, None, reason
            issue = (data.get("repository") or {}).get("issue")
            if not isinstance(issue, dict):
                return None, None, f"GitHub issue #{number} was not found"
            items = issue.get("projectItems")
            if not isinstance(items, dict):
                return None, None, f"GitHub issue #{number} has no readable project memberships"
            for item in items.get("nodes") or []:
                board = (item or {}).get("project") or {}
                login = str((board.get("owner") or {}).get("login") or "").lower()
                if board.get("number") == self._board_number and login == self._board_owner:
                    column = ((item.get("fieldValueByName") or {}).get("name"))
                    return issue.get("state"), column, None
            page = items.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return issue.get("state"), None, None
            cursor = page.get("endCursor")
            if not cursor:
                return None, None, "GitHub project membership pagination lost its cursor"
        return None, None, f"GitHub issue #{number} is on too many projects to read safely"

    def _card(self, issue, status=None) -> Card:
        return Card(
            key=str(issue.get("number") or ""),
            title=issue.get("title") or "",
            body=issue.get("body") or "",
            status=status,
            labels=_labels(issue.get("labels")),
        )

    # Interface.
    def candidates(self):
        """Open issues carrying the project's lane label, with no board status read.

        Returns (cards, reason). Labels live on the issue, so this lists
        issues, never board items.
        """
        payload, reason = self._gh([
            "gh", "issue", "list", "--repo", self._repository, "--state", "open",
            "--label", self._lane, "--limit", str(READY_ISSUE_LIMIT), "--json", ISSUE_FIELDS,
        ])
        if payload is None:
            return [], reason
        lane = self._lane.lower()
        return [self._card(issue) for issue in payload or []
                if lane in (name.lower() for name in _labels(issue.get("labels")))], None

    def ready(self):
        """The lane's open issues, each with its board column, for the ready rule.

        Returns (cards, reason). One membership query per candidate; a
        candidate whose column cannot be read ends the read with its reason,
        because a partial list would read as "nothing else is ready".
        """
        found, reason = self.candidates()
        if reason:
            return [], reason
        cards = []
        for card in found:
            _state, column, reason = self._membership(card.key)
            if reason:
                return [], reason
            cards.append(Card(card.key, card.title, card.body, column, card.labels))
        return cards, None

    def read(self, key) -> Card:
        """One issue with its labels, and its board column as status, as ready() reads it.

        Two reads: gh issue view for the title, body, and labels, then the
        one point membership query for the column, never a board listing.
        status is None when the issue is not on the board or has no column.
        A closed issue reads the issue state, CLOSED, whatever column its
        item sits in: ready() lists only open issues, so a closed card must
        never read as ready. Either read failing is a skip with its reason.
        """
        payload, reason = self._issue(key)
        if payload is None:
            return Card(key=str(key), skipped=reason)
        state, column, reason = self._membership(key)
        if reason:
            return Card(key=str(key), skipped=reason)
        card = self._card(payload, status=state if state == CLOSED_STATE else column)
        return Card(str(key), card.title, card.body, card.status, card.labels)

    def status(self, key) -> TrackerStatus:
        """The board column, terminal when the issue is closed or reads the landed status."""
        state, column, reason = self._membership(key)
        if reason:
            return skipped(f"the card could not be read: {reason}")
        if state == CLOSED_STATE:
            return TrackerStatus(column or state, True)
        terminal = bool(column) and column.lower() == self._landed.lower()
        return TrackerStatus(column, terminal)

    def closing_reference(self, key) -> ClosingReference:
        """GitHub closes the issue on merge when the pull request body says Closes #N."""
        return ClosingReference(body_line=f"Closes #{str(key).lstrip('#')}")

    # Onboarding listings (U28).
    def boards(self):
        """The repository owner's GitHub Projects boards, as owner/number."""
        payload, reason = self._gh(["gh", "project", "list", "--owner", self._owner,
                                    "--limit", str(LISTING_LIMIT), "--format", "json"])
        if payload is None:
            return (), reason
        return tuple(f"{self._owner}/{board.get('number')}"
                     for board in (payload or {}).get("projects") or []
                     if board.get("number") is not None and not board.get("closed")), None

    def labels(self):
        """The repository's labels, which are the labels an issue on the board can carry."""
        payload, reason = self._gh(["gh", "label", "list", "--repo", self._repository,
                                    "--limit", str(LABEL_LIMIT), "--json", "name"])
        if payload is None:
            return (), reason
        return _labels(payload), None

    def statuses(self):
        """The options of the board's Status field, its columns."""
        if self._board_number is None:
            return (), "no board is chosen yet"
        payload, reason = self._gh(["gh", "project", "field-list", str(self._board_number),
                                    "--owner", self._board_owner,
                                    "--limit", str(LISTING_LIMIT), "--format", "json"])
        if payload is None:
            return (), reason
        for found in (payload or {}).get("fields") or []:
            if found.get("name") == STATUS_FIELD:
                return tuple(str(o.get("name")) for o in found.get("options") or []), None
        return (), f"the board has no {STATUS_FIELD} field"

    def read_board(self):
        """Whether the board exists and is open: (True or False, detail), None on a failed read."""
        if self._board_number is None:
            return False, "no board is named"
        name = f"{self._board_owner}/{self._board_number}"
        payload, reason = self._gh(["gh", "project", "view", str(self._board_number),
                                    "--owner", self._board_owner, "--format", "json"])
        if payload is None:
            if NO_BOARD.search(reason or ""):
                return False, f"board {name} was not found: {reason}"
            return None, reason
        if (payload or {}).get("closed"):
            return False, f"board {name} is closed"
        return True, f"board {name} ({(payload or {}).get('title') or 'untitled'}) is open"


class GitHubScratch:
    """Files and closes the board movement check's scratch card, an issue (U28).

    open(title) returns (key, reason), close(key) a reason or None, and
    leftovers(title) the open issues a crashed run left, as (keys, reason).
    """

    def __init__(self, project, run=None):
        self._tracker = GitHubTracker(project, run)
        self._repository = project.repository
        self._run = self._tracker._run

    def _command(self, args):
        """Runs a gh command whose output is text. Returns (stdout, None) or (None, reason)."""
        try:
            proc = self._run(list(args), timeout=NETWORK_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"gh could not run: {exc}"
        if proc.returncode != 0:
            return None, f"gh exited {proc.returncode}: {(proc.stderr or '').strip()}"
        return proc.stdout or "", None

    def open(self, title: str):
        out, reason = self._command([
            "gh", "issue", "create", "--repo", self._repository, "--title", title, "--body",
            "Slipway files this card to watch the board move during onboarding, and closes "
            "it when the check ends."])
        if out is None:
            return None, reason
        found = ISSUE_URL.search(out)
        if not found:
            return None, f"gh issue create printed no issue URL: {out.strip()!r}"
        return found.group(1), None

    def close(self, key) -> str | None:
        _, reason = self._command(["gh", "issue", "close", str(key).lstrip("#"),
                                   "--repo", self._repository, "--reason", "not planned"])
        return reason

    def leftovers(self, title: str):
        payload, reason = self._tracker._gh([
            "gh", "issue", "list", "--repo", self._repository, "--state", "open",
            "--search", f'"{title}" in:title', "--limit", str(LISTING_LIMIT),
            "--json", "number,title"])
        if payload is None:
            return (), reason
        return tuple(str(i.get("number")) for i in payload or []
                     if i.get("title") == title), None
