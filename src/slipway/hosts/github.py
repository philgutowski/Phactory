"""GitHub host adapter (KTD9): REST for the writes and reads, GraphQL for the queue.

The credential is the one the project file names (U6): a GitHub App
installation token minted by the operator's tooling, or a token the
operator holds. Both go in the Authorization header as a bearer token. Under
an App the deterministic check is a check run, GitHub's richer check with a
title and summary; under any other credential it is a commit status, which
needs no App. A ruleset's required status check matches either by name.

What each method calls:

    create_branch      POST git/refs, then GET git/ref/heads/<name> on a 422
    commit_on_remote   GET git/ref/heads/<branch>
    open_pull_request  POST pulls, then GET pulls?head=owner:branch on a 422
    post_check         POST check-runs (App) or POST statuses/<sha> (token)
    enqueue            GraphQL enqueuePullRequest on the queue path, else
                       enablePullRequestAutoMerge with the probed merge method
    pull_request       GET pulls/<n> and GET pulls/<n>/reviews
    probe_landing      GET repos/<owner>/<name> and GET rules/branches/<default>

The probe reads the rules that apply to the default branch, which is the
ruleset view GitHub computes from every active ruleset. A merge_queue rule
names the queue's merge method. Without one, a required_status_checks rule
with the strict (up to date) policy makes the path auto_merge_strict when
the repository allows auto merge, as the spike measured
(docs/spikes/merge-paths.md); anything less is plain_merge. The merge
method off a queue is the one Slipway will pass when it arms auto merge: the
only method the repository allows, or the first of METHOD_PREFERENCE among
those allowed, squash first because it lands one commit per card.

The pull request body line and the title token come from the tracker's
closing reference (U8). open_pull_request on a branch that already has one
returns that pull request, so a retried dispatch step opens nothing twice.
"""

import re

from slipway.hosts.base import (
    AUTO_MERGE_ARMED,
    AUTO_MERGE_STRICT,
    CLOSED,
    ENQUEUED,
    MERGE_QUEUE,
    MERGED,
    OPEN,
    PLAIN_MERGE,
    Branch,
    BranchConflict,
    CheckPost,
    CheckState,
    Client,
    HostError,
    Landing,
    PullRequest,
    body_of,
    landing_from,
    message_of,
    pinned_landing,
    repository_parts,
    titled,
)

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

# The operator file's kind for a credential that may post check runs.
APP_KIND = "github_app"

# A commit status description is capped by GitHub at this many characters.
STATUS_DESCRIPTION_LIMIT = 140

# Off a queue, the method Slipway arms auto merge with when the repository
# allows more than one: squash lands one commit per card.
METHOD_PREFERENCE = ("squash", "merge", "rebase")
ALLOWED_FLAGS = {"squash": "allow_squash_merge", "merge": "allow_merge_commit",
                 "rebase": "allow_rebase_merge"}

CHECK_RUN_STATUS = {CheckState.PENDING: ("in_progress", None),
                    CheckState.SUCCESS: ("completed", "success"),
                    CheckState.FAILURE: ("completed", "failure")}

ENQUEUE_MUTATION = """
mutation($id: ID!, $head: GitObjectID) {
  enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) {
    mergeQueueEntry { id state position }
  }
}
"""

AUTO_MERGE_MUTATION = """
mutation($id: ID!, $head: GitObjectID, $method: PullRequestMergeMethod) {
  enablePullRequestAutoMerge(input: {pullRequestId: $id, expectedHeadOid: $head,
                                     mergeMethod: $method}) {
    pullRequest { autoMergeRequest { enabledAt mergeMethod } }
  }
}
"""

ALREADY_EXISTS = re.compile(r"already exists", re.IGNORECASE)


class GitHubHost:
    def __init__(self, project, credential, opener=None, check_runs=None):
        self._repository = project.repository
        self._owner, self._name = repository_parts(project.repository)
        self._project = project
        self._client = Client(API, credential.token, opener, HEADERS)
        self._check_runs = (credential.kind == APP_KIND) if check_runs is None else check_runs
        self._repo_cache = None

    def __repr__(self) -> str:
        return f"GitHubHost(repository={self._repository!r})"

    # Transport.
    def _path(self, tail: str) -> str:
        return f"/repos/{self._owner}/{self._name}/{tail}"

    def _expect(self, response, *codes, what: str):
        if response.status not in codes:
            raise HostError(f"GitHub refused {what}: {message_of(response)} "
                            f"(HTTP {response.status})")
        return response.body

    def _graphql(self, query: str, variables: dict) -> dict:
        response = self._client.post(GRAPHQL, {"query": query, "variables": variables})
        body = self._expect(response, 200, what="a GraphQL request")
        errors = (body or {}).get("errors") or []
        if errors:
            messages = "; ".join(str(e.get("message") or e) for e in errors)
            raise HostError(f"GitHub GraphQL returned errors: {messages}")
        data = (body or {}).get("data")
        if not isinstance(data, dict):
            raise HostError("GitHub GraphQL returned no data")
        return data

    def _repo(self) -> dict:
        if self._repo_cache is None:
            response = self._client.get(f"/repos/{self._owner}/{self._name}")
            self._repo_cache = self._expect(response, 200, what="the repository read")
        return self._repo_cache

    def _ref(self, name: str) -> str | None:
        """The commit a branch points at, or None when the branch does not exist."""
        response = self._client.get(self._path(f"git/ref/heads/{name}"))
        if response.status == 404:
            return None
        body = self._expect(response, 200, what=f"the read of branch {name!r}")
        return str(((body or {}).get("object") or {}).get("sha") or "") or None

    # Interface.
    def create_branch(self, name: str, sha: str) -> Branch:
        response = self._client.post(self._path("git/refs"),
                                     {"ref": f"refs/heads/{name}", "sha": sha})
        if response.status == 201:
            return Branch(name, sha, created=True)
        if response.status == 422 and ALREADY_EXISTS.search(message_of(response)):
            found = self._ref(name)
            if found == sha:
                return Branch(name, sha, created=False)
            raise BranchConflict(name, sha, found or "an unreadable commit")
        raise HostError(f"GitHub refused the branch {name!r}: {message_of(response)} "
                        f"(HTTP {response.status})")

    def commit_on_remote(self, branch: str, sha: str) -> bool:
        return self._ref(branch) == sha

    def open_pull_request(self, branch: str, title: str, reference, body: str = "",
                          base: str | None = None) -> PullRequest:
        base = base or self._repo().get("default_branch") or "main"
        payload = {"title": titled(title, reference), "head": branch, "base": base,
                   "body": body_of(reference, body)}
        response = self._client.post(self._path("pulls"), payload)
        if response.status == 201:
            return _pull_request(response.body)
        if response.status == 422 and ALREADY_EXISTS.search(_errors_text(response)):
            existing = self._find_open(branch)
            if existing is not None:
                return existing
        raise HostError(f"GitHub refused the pull request for {branch!r}: "
                        f"{_errors_text(response)} (HTTP {response.status})")

    def _find_open(self, branch: str) -> PullRequest | None:
        """The open pull request from branch, the gh pr shape find_pr used."""
        response = self._client.get(self._path("pulls"),
                                    {"head": f"{self._owner}:{branch}", "state": "open"})
        found = self._expect(response, 200, what="the pull request list")
        for item in found or []:
            if ((item.get("head") or {}).get("ref")) == branch:
                return _pull_request(item)
        return None

    def post_check(self, sha: str, name: str, state: str, url: str | None = None,
                   summary: str = "") -> CheckPost:
        state = CheckState(state)
        if self._check_runs:
            status, conclusion = CHECK_RUN_STATUS[state]
            payload = {"name": name, "head_sha": sha, "status": status,
                       "output": {"title": name, "summary": summary or name}}
            if conclusion:
                payload["conclusion"] = conclusion
            if url:
                payload["details_url"] = url
            response = self._client.post(self._path("check-runs"), payload)
            body = self._expect(response, 201, what=f"the check run {name!r}")
            return CheckPost(sha, name, state.value, str(body.get("id")), body.get("html_url"))
        payload = {"state": state.value, "context": name,
                   "description": (summary or name)[:STATUS_DESCRIPTION_LIMIT]}
        if url:
            payload["target_url"] = url
        response = self._client.post(self._path(f"statuses/{sha}"), payload)
        body = self._expect(response, 201, what=f"the status {name!r}")
        return CheckPost(sha, name, state.value, None, body.get("url"))

    def enqueue(self, pr, landing: Landing | None = None) -> str:
        pr = pr if isinstance(pr, PullRequest) else self.pull_request(pr)
        if not pr.node_id:
            raise HostError(f"pull request #{pr.number} has no GraphQL id to enqueue with")
        landing = landing or self.probe_landing(self._project)
        if landing.path == MERGE_QUEUE:
            self._graphql(ENQUEUE_MUTATION, {"id": pr.node_id, "head": pr.head})
            return ENQUEUED
        self._graphql(AUTO_MERGE_MUTATION, {"id": pr.node_id, "head": pr.head,
                                            "method": landing.method.upper()})
        return AUTO_MERGE_ARMED

    def pull_request(self, pr) -> PullRequest:
        number = pr.number if isinstance(pr, PullRequest) else int(str(pr).lstrip("#"))
        response = self._client.get(self._path(f"pulls/{number}"))
        body = self._expect(response, 200, what=f"the read of pull request #{number}")
        reviews = self._client.get(self._path(f"pulls/{number}/reviews"), {"per_page": 100})
        listed = self._expect(reviews, 200, what=f"the reviews of pull request #{number}")
        return _pull_request(body, _approvals(listed or []))

    def probe_landing(self, project) -> Landing:
        pinned = pinned_landing(project)
        if pinned:
            return pinned
        repo = self._repo()
        branch = repo.get("default_branch") or "main"
        response = self._client.get(self._path(f"rules/branches/{branch}"))
        rules = self._expect(response, 200, what=f"the rules for {branch!r}") or []
        by_type = {rule.get("type"): rule.get("parameters") or {} for rule in rules
                   if isinstance(rule, dict)}
        allowed = [m for m in METHOD_PREFERENCE if repo.get(ALLOWED_FLAGS[m])]
        if "merge_queue" in by_type:
            method = str(by_type["merge_queue"].get("merge_method") or "").lower()
            if method not in ALLOWED_FLAGS:
                method = allowed[0] if allowed else "merge"
            return landing_from(project, MERGE_QUEUE, method,
                                f"merge_queue rule on {branch}, method {method}")
        method = allowed[0] if allowed else "merge"
        checks = by_type.get("required_status_checks")
        strict = bool(checks and checks.get("strict_required_status_checks_policy")
                      and checks.get("required_status_checks"))
        if strict and repo.get("allow_auto_merge"):
            names = ", ".join(str(c.get("context")) for c in checks["required_status_checks"])
            return landing_from(project, AUTO_MERGE_STRICT, method,
                                f"required checks {names} with the up to date policy, auto "
                                f"merge allowed, methods allowed {allowed}")
        why = ("no required status check with the up to date policy" if not strict
               else "auto merge is not allowed on the repository")
        return landing_from(project, PLAIN_MERGE, method, f"{why}; methods allowed {allowed}")


def _errors_text(response) -> str:
    body = response.body if isinstance(response.body, dict) else {}
    details = [str(e.get("message") or e) for e in body.get("errors") or []
               if isinstance(e, (dict, str))]
    return "; ".join([message_of(response), *details])


def _pull_request(body: dict, approvals=()) -> PullRequest:
    body = body if isinstance(body, dict) else {}
    if body.get("number") is None:
        raise HostError("GitHub returned a pull request without a number")
    merged = bool(body.get("merged")) or bool(body.get("merged_at"))
    state = MERGED if merged else (OPEN if body.get("state") == "open" else CLOSED)
    return PullRequest(
        number=int(body.get("number")),
        state=state,
        merged=merged,
        head=(body.get("head") or {}).get("sha"),
        merge_commit=body.get("merge_commit_sha") if merged else None,
        approvals=tuple(approvals),
        head_ref=(body.get("head") or {}).get("ref"),
        base_ref=(body.get("base") or {}).get("ref"),
        title=body.get("title") or "",
        url=body.get("html_url"),
        node_id=body.get("node_id"),
        merged_by=(body.get("merged_by") or {}).get("login"),
    )


def _approvals(reviews) -> tuple[str, ...]:
    """The reviewers whose latest decisive review approves. A comment decides nothing."""
    latest: dict[str, str] = {}
    for review in sorted(reviews, key=lambda r: str(r.get("submitted_at") or "")):
        state = str(review.get("state") or "").upper()
        login = (review.get("user") or {}).get("login")
        if login and state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = state
    return tuple(login for login, state in latest.items() if state == "APPROVED")

