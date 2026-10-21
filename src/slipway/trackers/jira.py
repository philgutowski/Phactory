"""Jira tracker adapter, read only (KTD8).

Derived from native-relay/skills/relay/scripts/relay/adapters/jira.py.
Kept: _adf_text, _request, _issue, the four reads (candidates, ready, read,
status), and the opener injection seam. Dropped: the closeout, filing, and
triple coordinator methods, every write, and the comment search
closing_reference. Added: labels on every read, and closing_reference
renders the issue key Jira's own development integration keys on.

Reads go straight to the Jira REST API with urllib. The site, the account
email, and the API token come from resolve_tracker_credential (U6), which
reads the operator file's tracker credential entry and the environment
variables it names. The project file only names that entry, so no site,
email, or token name from a project file ever reaches this module (R3). The
instance holds the token and the email twice: once inside the Basic
authorization header it sends, and once as the pair scrub removes from a
failure reason before that reason leaves the adapter. Neither leaves this
object in any other form, and the repr shows only the site and the board.

What ready means on a Jira board is the project's: its board key, its ready
status, and its lane label, all from the project file. A card is terminal
when its status is one of the done statuses, the project's landed status by
default.
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from slipway.config.bindings import BindingInvalid, resolve_tracker_credential
from slipway.redact import scrub
from slipway.trackers.base import (
    NETWORK_TIMEOUT_SECONDS,
    Card,
    ClosingReference,
    TrackerStatus,
    skipped,
)

ISSUE_FIELDS = "summary,description,status,labels"
ISSUE_PATH = "/rest/api/3/issue/%s"
# Enhanced search, the current Jira Cloud path for a JQL query.
SEARCH_PATH = "/rest/api/3/search/jql"
SEARCH_PAGE_SIZE = 100
SEARCH_PAGE_LIMIT = 20
PROJECT_SEARCH_PATH = "/rest/api/3/project/search"
PROJECT_PATH = "/rest/api/3/project/%s"
STATUSES_PATH = "/rest/api/3/project/%s/statuses"
LABEL_PATH = "/rest/api/3/label"
ISSUE_TYPES_PATH = "/rest/api/3/issue/createmeta/%s/issuetypes"
CREATE_PATH = "/rest/api/3/issue"
TRANSITIONS_PATH = "/rest/api/3/issue/%s/transitions"
LISTING_PAGE_SIZE = 100
# Jira's status category for every done status, whatever a site names them.
DONE_CATEGORY = "done"


def _adf_text(node):
    """Flatten Atlassian Document Format, the nested JSON Jira stores rich text in, to plain
    text, so a description reads as the body a GitHub issue would carry."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(item) for item in node)
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "text":
            return str(node.get("text", ""))
        if kind == "hardBreak":
            return "\n"
        inner = _adf_text(node.get("content"))
        if kind in ("paragraph", "heading", "listItem", "blockquote", "codeBlock", "rule"):
            return inner + "\n"
        return inner
    return ""


def _quote(value: str) -> str:
    """A JQL string literal."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class JiraTracker:
    def __init__(self, project, opener=None, credential=None, root=None, environ=None,
                 done_statuses=None):
        """credential defaults to resolve_tracker_credential(project.slug), which raises
        BindingInvalid (halt class binding_invalid) when it cannot resolve."""
        if credential is None:
            credential = resolve_tracker_credential(project.slug, root, environ)
        if not credential.email:
            raise BindingInvalid(
                "tracker_credential", "a Jira credential needs an email_env in the operator file")
        self._site = credential.site.rstrip("/")
        self._board = project.board
        self._lane = project.lane_label
        self._ready = project.ready_status
        done = done_statuses if done_statuses is not None else (project.landed_status,)
        self._done = tuple(str(name).lower() for name in done)
        self._secrets = (credential.token, credential.email)
        pair = f"{credential.email}:{credential.token}".encode()
        self._authorization = "Basic " + base64.b64encode(pair).decode("ascii")
        self._opener = opener or urllib.request.build_opener()

    def __repr__(self) -> str:
        return f"JiraTracker(site={self._site!r}, board={self._board!r})"

    # Transport.
    def _request(self, method, path, params=None, body=None):
        """Returns (payload, None) or (None, reason). Never raises for a transport failure."""
        url = f"https://{self._site}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": self._authorization, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as body:
                raw = body.read().decode("utf-8")
                return (json.loads(raw) if raw.strip() else {}), None
        except urllib.error.HTTPError as exc:
            # An HTTPError is also an open response; close it.
            exc.close()
            return None, f"jira returned {exc.code} for {path}"
        except (OSError, ValueError) as exc:
            return None, scrub(f"jira read failed: {exc}", self._secrets)

    def _get(self, path, params=None):
        return self._request("GET", path, params)

    def _issue(self, key):
        return self._get(ISSUE_PATH % urllib.parse.quote(str(key), safe=""),
                         {"fields": ISSUE_FIELDS})

    def _card(self, issue, key=None) -> Card:
        fields = issue.get("fields") or {}
        return Card(
            key=str(issue.get("key") or key or ""),
            title=fields.get("summary") or "",
            body=_adf_text(fields.get("description")).strip(),
            status=(fields.get("status") or {}).get("name"),
            labels=tuple(str(name) for name in fields.get("labels") or []),
        )

    def _search(self, jql):
        """Every page of a JQL search, to a bound. Returns (cards, reason). A done
        status is filtered again on what comes back, in case Jira matched loosely."""
        params = {"jql": jql, "fields": ISSUE_FIELDS, "maxResults": SEARCH_PAGE_SIZE}
        found = []
        for _ in range(SEARCH_PAGE_LIMIT):
            payload, reason = self._get(SEARCH_PATH, params)
            if payload is None:
                return [], reason
            for issue in payload.get("issues") or []:
                card = self._card(issue)
                if card.status and card.status.lower() in self._done:
                    continue
                found.append(card)
            token = payload.get("nextPageToken")
            if payload.get("isLast", True) or not token:
                break
            params = dict(params, nextPageToken=token)
        return found, None

    # Interface.
    def candidates(self):
        """The board's cards that are not done, oldest first. Returns (cards, reason)."""
        jql = f"project = {_quote(self._board)}"
        if self._done:
            done = ", ".join(_quote(name) for name in self._done)
            jql += f" AND status not in ({done})"
        return self._search(jql + " ORDER BY created ASC")

    def ready(self):
        """Cards in the ready status carrying the lane label, from the project's JQL."""
        return self._search(self._ready_jql())

    def _ready_jql(self) -> str:
        """The JQL ready runs, built from the project file's board, status, and label."""
        return (f"project = {_quote(self._board)} AND status = {_quote(self._ready)} "
                f"AND labels = {_quote(self._lane)} ORDER BY created ASC")

    def read(self, key) -> Card:
        payload, reason = self._issue(key)
        if payload is None:
            return Card(key=str(key), skipped=reason)
        return self._card(payload, key)

    def status(self, key) -> TrackerStatus:
        payload, reason = self._issue(key)
        if payload is None:
            return skipped(reason)
        name = ((payload.get("fields") or {}).get("status") or {}).get("name")
        return TrackerStatus(name, bool(name) and name.lower() in self._done)

    def closing_reference(self, key) -> ClosingReference:
        """Jira links a pull request to a card by its upper case key in the title or branch."""
        token = str(key).upper()
        return ClosingReference(title_token=token, branch_token=token)

    # Onboarding listings (U28).
    def _paged(self, path, key):
        """Every page of a startAt paged listing, to a bound. Returns (values, reason)."""
        found, start = [], 0
        for _ in range(SEARCH_PAGE_LIMIT):
            payload, reason = self._get(path, {"startAt": start,
                                               "maxResults": LISTING_PAGE_SIZE})
            if payload is None:
                return [], reason
            values = payload.get("values") or []
            found += [v.get(key) if isinstance(v, dict) else v for v in values]
            if payload.get("isLast", True) or not values:
                return [str(v) for v in found if v], None
            start += len(values)
        return [], f"{path} ran past {SEARCH_PAGE_LIMIT} pages"

    def boards(self):
        """The keys of the Jira projects this credential can see."""
        found, reason = self._paged(PROJECT_SEARCH_PATH, "key")
        return tuple(found), reason

    def labels(self):
        """The site's labels; Jira keeps one list for every project."""
        found, reason = self._paged(LABEL_PATH, None)
        return tuple(found), reason

    def statuses(self):
        """Every status the board's issue types can reach, once each, in Jira's order."""
        if not self._board:
            return (), "no board is chosen yet"
        payload, reason = self._get(STATUSES_PATH % urllib.parse.quote(self._board, safe=""))
        if payload is None:
            return (), reason
        names = [str(status.get("name")) for kind in payload or []
                 for status in (kind or {}).get("statuses") or [] if status.get("name")]
        return tuple(dict.fromkeys(names)), None

    def read_board(self):
        """Whether the board's project exists: (True or False, detail), None on a failed read."""
        if not self._board:
            return False, "no board is named"
        payload, reason = self._get(PROJECT_PATH % urllib.parse.quote(self._board, safe=""))
        if payload is None:
            if reason and reason.startswith("jira returned 404"):
                return False, f"board {self._board} was not found on {self._site}"
            return None, reason
        return True, f"board {self._board} ({payload.get('name') or 'unnamed'}) is readable"


class JiraScratch:
    """Files and closes the board movement check's scratch card (U28).

    open(title) returns (key, reason), close(key) a reason or None, and
    leftovers(title) the open cards a crashed run left, as (keys, reason).
    """

    def __init__(self, project, **kwargs):
        self._tracker = JiraTracker(project, **kwargs)
        self._board = project.board
        self._done = self._tracker._done

    def open(self, title: str):
        board = urllib.parse.quote(self._board, safe="")
        payload, reason = self._tracker._get(ISSUE_TYPES_PATH % board)
        if payload is None:
            return None, reason
        kinds = payload.get("issueTypes") or payload.get("values") or []
        standard = [k for k in kinds if not k.get("subtask") and k.get("id")]
        if not standard:
            return None, f"board {self._board} has no issue type a card can be filed in"
        created, reason = self._tracker._request("POST", CREATE_PATH, body={"fields": {
            "project": {"key": self._board}, "summary": title,
            "issuetype": {"id": str(standard[0]["id"])}}})
        if created is None:
            return None, reason
        key = created.get("key")
        return (str(key), None) if key else (None, "jira filed a card but gave no key")

    def close(self, key) -> str | None:
        path = TRANSITIONS_PATH % urllib.parse.quote(str(key), safe="")
        payload, reason = self._tracker._get(path)
        if payload is None:
            return reason
        moves = payload.get("transitions") or []

        def lands(move, named: bool) -> bool:
            target = move.get("to") or {}
            if named:
                return str(target.get("name") or "").lower() in self._done
            return (target.get("statusCategory") or {}).get("key") == DONE_CATEGORY

        chosen = next((m for named in (True, False) for m in moves if lands(m, named)), None)
        if chosen is None:
            return f"{key} has no transition to a done status"
        _, reason = self._tracker._request("POST", path,
                                           body={"transition": {"id": str(chosen.get("id"))}})
        return reason

    def leftovers(self, title: str):
        jql = (f"project = {_quote(self._board)} AND summary ~ {_quote(_quote(title))} "
               "AND statusCategory != Done ORDER BY created ASC")
        found, reason = self._tracker._search(jql)
        return tuple(card.key for card in found if card.title == title), reason
