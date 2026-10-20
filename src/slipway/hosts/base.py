"""What every host adapter returns, and the interface they share (KTD9).

A host is where a project's repository lives: GitHub or Bitbucket Cloud. An
adapter is the whole of what Slipway knows about one host. Unlike a tracker
adapter (U8), a host adapter writes: it creates the card's branch, opens the
pull request, posts checks, and enqueues. It never merges (R18). The host's
own merge path lands the pull request, and verify (U10) reads git afterwards.

The interface every adapter implements, in the order a card uses it:

    create_branch(name, sha)                     -> Branch
    commit_on_remote(branch, sha)                -> bool
    open_pull_request(branch, title, reference)  -> PullRequest
    post_check(sha, name, state, url, summary)   -> CheckPost
    enqueue(pr, landing=None)                    -> str  (ENQUEUED, AUTO_MERGE_ARMED,
                                                          or NOT_SUPPORTED)
    pull_request(pr)                             -> PullRequest
    probe_landing(project)                       -> Landing

create_branch creates the reference at the commit and is idempotent when the
branch already sits there; a branch at another commit is a BranchConflict,
because a card's branch is created once at dispatch (KTD27) and a second
creation at a different commit means two dispatches crossed.

commit_on_remote answers whether the branch's head on the host is exactly
the commit the builder says it pushed. Slipway records that commit as the
confirmed commit only when this is true (KTD6).

open_pull_request takes the tracker's ClosingReference (U8): the title token
goes into the title, the body line into the body. The branch token is
already in the branch name, which dispatch chose. Opening twice on one
branch returns the pull request that already exists.

post_check posts Slipway's deterministic check (R18) on any commit, the pull
request head or the queue's tested commit (KTD7). state is one of
CHECK_STATES. A failing check carries its reason in summary (R42).

enqueue puts the pull request on the host's landing path once the host will
take it: a merge queue entry on GitHub's queue path, auto merge armed on
GitHub's auto merge paths, and NOT_SUPPORTED on Bitbucket, where the queue
has no API and the repository's own "automatically queue once builds pass"
setting or a click does the work.

pull_request reads the live state. Every event driven transition re reads
the pull request this way before it records anything (KTD10).

probe_landing finds the landing path and merge method the host will use.
The project file may pin either or both (U6); whatever it does not pin is
read live from the ruleset, queue, or repository settings. The answer is
never a table in code (KTD9).

A transport failure is a HostError with a reason that never carries the
credential. Every adapter takes an injectable opener for urllib, so tests
answer from fixtures and never reach a network.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from slipway.config.operator import Host, HostCredential, Operator
from slipway.config.project import LandingPath, MergeMethod, Project
from slipway.redact import scrub
from slipway.trackers.base import ClosingReference
from slipway.verify import AUTO_MERGE_STRICT, BITBUCKET_QUEUE, MERGE_QUEUE, PLAIN_MERGE

# Every HTTP request is bounded by this many seconds.
NETWORK_TIMEOUT_SECONDS = 30

# Pull request states after normalisation. A host's own words (MERGED,
# DECLINED, closed with merged true) map onto these three.
OPEN, CLOSED, MERGED = "open", "closed", "merged"
PULL_REQUEST_STATES = (OPEN, CLOSED, MERGED)

# What enqueue returns.
ENQUEUED = "enqueued"
AUTO_MERGE_ARMED = "auto_merge_armed"
NOT_SUPPORTED = "not_supported"

# The landing paths a probe can answer, the three the project file may pin
# plus plain_merge, which verify (U10) names for a repository with auto merge
# on but no required up to date check (docs/spikes/merge-paths.md).
LANDING_PATHS = (MERGE_QUEUE, AUTO_MERGE_STRICT, BITBUCKET_QUEUE, PLAIN_MERGE)
MERGE_METHODS = tuple(method.value for method in MergeMethod)

# Where a probe's answer came from.
PINNED = "project file"
LIVE = "live"


class CheckState(StrEnum):
    """The three states Slipway posts. Each host spells them its own way."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


CHECK_STATES = tuple(state.value for state in CheckState)


class HostError(Exception):
    """A host request failed. The message is a reason, scrubbed of the credential."""


class BranchConflict(HostError):
    """The branch already exists at another commit."""

    def __init__(self, name: str, wanted: str, found: str):
        self.name, self.wanted, self.found = name, wanted, found
        super().__init__(f"branch {name!r} already exists at {found}, not {wanted}")


class ProbeUndetermined(HostError):
    """The host exposes no setting the probe can read; the project file must pin it."""


@dataclass(frozen=True)
class Branch:
    name: str
    sha: str
    created: bool  # False when the branch already sat at sha


@dataclass(frozen=True)
class PullRequest:
    """One pull request as the host reports it.

    number is the host's own id. state is one of PULL_REQUEST_STATES. head is
    the branch head the host shows now, merge_commit the commit that landed
    (None until merged), approvals the reviewers who approved. node_id is
    GitHub's GraphQL id, which enqueue needs; Bitbucket leaves it None.
    """

    number: int
    state: str
    merged: bool
    head: str | None
    merge_commit: str | None = None
    approvals: tuple[str, ...] = ()
    head_ref: str | None = None
    base_ref: str | None = None
    title: str = ""
    url: str | None = None
    node_id: str | None = None
    merged_by: str | None = None


@dataclass(frozen=True)
class CheckPost:
    """What the host recorded for one posted check."""

    sha: str
    name: str
    state: str
    id: str | None = None  # the check run id on GitHub, None for a commit status
    url: str | None = None


@dataclass(frozen=True)
class Landing:
    """The landing path and merge method the host will use for this project.

    path is one of LANDING_PATHS, method one of MERGE_METHODS. path_source and
    method_source say whether each came from the project file (PINNED) or the
    live probe (LIVE). detail is the probe's own words for the record.
    """

    path: str
    method: str
    path_source: str
    method_source: str
    detail: str = ""

    @property
    def queue(self) -> bool:
        return self.path in (MERGE_QUEUE, BITBUCKET_QUEUE)


@dataclass(frozen=True)
class Credential:
    """The host credential resolved from the operator file and the environment.

    kind is the operator file's word for it (for example github_app or
    repository_access_token); token the value. The token never prints.
    """

    name: str
    host: Host
    kind: str
    token: str

    def __repr__(self) -> str:
        return f"Credential(name={self.name!r}, host={self.host!r}, kind={self.kind!r}, token=...)"


def resolve_host_credential(project: Project, operator: Operator,
                            environ: Mapping[str, str]) -> Credential:
    """The project's host credential, read from the environment the operator file names.

    A project file names the credential by its operator file entry (R3); the
    entry names the environment variable; the variable holds the token.
    Raises HostError naming the entry, never the value, when a step fails.
    """
    entry: HostCredential | None = operator.host_credentials.get(project.host_credential)
    if entry is None:
        raise HostError(
            f"host_credential {project.host_credential!r} is not in the operator file")
    if entry.host != project.host:
        raise HostError(
            f"host_credential {entry.name!r} is a {entry.host} credential, the project's "
            f"host is {project.host}")
    token = environ.get(entry.env, "")
    if not token:
        raise HostError(f"host_credentials.{entry.name}.env: environment variable "
                        f"{entry.env} is unset")
    return Credential(entry.name, entry.host, entry.kind, token)


def pinned_landing(project: Project) -> Landing | None:
    """The landing when the project file pins both path and method, else None."""
    if project.landing_path and project.merge_method:
        return Landing(project.landing_path.value, project.merge_method.value, PINNED, PINNED,
                       "both pinned by the project file")
    return None


def landing_from(project: Project, path: str, method: str, detail: str) -> Landing:
    """A probed landing, with any value the project file pins put in its place."""
    pinned_path: LandingPath | None = project.landing_path
    pinned_method: MergeMethod | None = project.merge_method
    return Landing(
        path=pinned_path.value if pinned_path else path,
        method=pinned_method.value if pinned_method else method,
        path_source=PINNED if pinned_path else LIVE,
        method_source=PINNED if pinned_method else LIVE,
        detail=detail,
    )


def titled(title: str, reference: ClosingReference) -> str:
    """The pull request title with the tracker's title token in front, once."""
    token = reference.title_token
    if not token or token.lower() in title.lower():
        return title
    return f"{token}: {title}"


def body_of(reference: ClosingReference, text: str = "") -> str:
    """The pull request body: the tracker's body line, then any text."""
    parts = [part for part in (reference.body_line, text) if part]
    return "\n\n".join(parts)


def repository_parts(repository: str) -> tuple[str, str]:
    """owner and name from "owner/name"; a Bitbucket workspace and slug look the same."""
    owner, _, name = repository.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"repository {repository!r} is not owner/name")
    return owner, name


@dataclass(frozen=True)
class Response:
    """One HTTP answer: the status code and the decoded JSON body (None when empty)."""

    status: int
    body: object
    headers: Mapping[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Client:
    """JSON over HTTPS to one host API, with the credential as a bearer token.

    request returns a Response for any HTTP status, so an adapter reads a 404
    or a 422 as an answer, not an exception. A failure to reach the host at
    all is a HostError whose text is scrubbed of the token (KTD24).
    """

    def __init__(self, base_url: str, token: str, opener=None, headers=None):
        self._base = base_url.rstrip("/")
        self._token = token
        self._headers = dict(headers or {})
        self._opener = opener or urllib.request.build_opener()

    def url(self, path: str, params: Mapping | None = None) -> str:
        url = path if path.startswith("https://") else self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def request(self, method: str, path: str, body=None, params=None) -> Response:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                   **self._headers}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url(path, params), data=data, method=method,
                                         headers=headers)
        try:
            with self._opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as answer:
                return Response(answer.status, _decode(answer.read()), dict(answer.headers))
        except urllib.error.HTTPError as error:
            raw = error.read()
            error.close()
            return Response(error.code, _decode(raw), dict(error.headers or {}))
        except (OSError, ValueError) as error:
            raise HostError(scrub(f"{method} {path} failed: {error}", (self._token,))) from None

    def get(self, path, params=None) -> Response:
        return self.request("GET", path, params=params)

    def post(self, path, body) -> Response:
        return self.request("POST", path, body)

    def put(self, path, body) -> Response:
        return self.request("PUT", path, body)


def _decode(raw: bytes):
    text = raw.decode("utf-8") if raw else ""
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def message_of(response: Response) -> str:
    """The host's own error text, or the status code when it gave none."""
    body = response.body
    if isinstance(body, dict):
        for key in ("message", "error"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message")
            if value:
                return str(value)
    return f"HTTP {response.status}"


# The public surface of every adapter. The tests assert no other public
# method exists, so a merge method can never creep in (R18).
INTERFACE = ("create_branch", "commit_on_remote", "open_pull_request", "post_check",
             "enqueue", "pull_request", "probe_landing")
