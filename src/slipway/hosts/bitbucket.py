"""Bitbucket Cloud host adapter (KTD9): REST 2.0 under a repository access token.

A repository access token is a credential Bitbucket issues for one
repository with named scopes; it goes in the Authorization header as a
bearer token. The project file names the operator file entry that holds it
(U6).

What each method calls:

    create_branch      POST refs/branches, then GET refs/branches/<name> on a 400
    commit_on_remote   GET refs/branches/<branch>
    open_pull_request  POST pullrequests, then GET pullrequests?q=... on a 400
    post_check         POST commit/<sha>/statuses/build
    enqueue            nothing: returns NOT_SUPPORTED without a request
    pull_request       GET pullrequests/<id>
    probe_landing      nothing live: the pinned values, else ProbeUndetermined

Bitbucket Cloud's merge queue has no REST endpoint to add an entry, list the
queue, or read the queue's strategy (BCLOUD-24176 is still gathering
interest, docs/research/2026-10-14-plan/framework-docs-apis.md Part C2), so
enqueue returns NOT_SUPPORTED and the repository's own "automatically queue
once builds pass" setting, or a click the needs you entry names (KTD7), puts
the pull request on the queue. For the same reason the probe has nothing to
read: a Bitbucket project pins landing_path and merge_method in its project
file, and a project that pins neither gets ProbeUndetermined naming both
keys rather than a guess.

Jira keys a pull request to its card by the upper case issue key in the
title or the source branch name (Part C4), so open_pull_request puts the
closing reference's title token in front of the title. A build status on
Bitbucket needs a url, so post_check refuses a check without one before any
request.
"""

import re

from slipway.hosts.base import (
    CLOSED,
    MERGED,
    NOT_SUPPORTED,
    OPEN,
    Branch,
    BranchConflict,
    CheckPost,
    CheckState,
    Client,
    HostError,
    Landing,
    ProbeUndetermined,
    PullRequest,
    body_of,
    message_of,
    pinned_landing,
    repository_parts,
    titled,
)

API = "https://api.bitbucket.org/2.0"

BUILD_STATE = {CheckState.PENDING: "INPROGRESS", CheckState.SUCCESS: "SUCCESSFUL",
               CheckState.FAILURE: "FAILED"}
# Bitbucket's pull request states, onto the three every adapter reports.
STATES = {"OPEN": OPEN, "MERGED": MERGED, "DECLINED": CLOSED, "SUPERSEDED": CLOSED}

ALREADY_EXISTS = re.compile(r"already (exists|has|open)", re.IGNORECASE)


class BitbucketHost:
    def __init__(self, project, credential, opener=None):
        self._repository = project.repository
        self._workspace, self._slug = repository_parts(project.repository)
        self._project = project
        self._client = Client(API, credential.token, opener)
        self._repo_cache = None

    def __repr__(self) -> str:
        return f"BitbucketHost(repository={self._repository!r})"

    # Transport.
    def _path(self, tail: str) -> str:
        return f"/repositories/{self._workspace}/{self._slug}/{tail}"

    def _expect(self, response, *codes, what: str):
        if response.status not in codes:
            raise HostError(f"Bitbucket refused {what}: {message_of(response)} "
                            f"(HTTP {response.status})")
        return response.body

    def _repo(self) -> dict:
        if self._repo_cache is None:
            response = self._client.get(f"/repositories/{self._workspace}/{self._slug}")
            self._repo_cache = self._expect(response, 200, what="the repository read")
        return self._repo_cache

    def _branch(self, name: str) -> str | None:
        response = self._client.get(self._path(f"refs/branches/{name}"))
        if response.status == 404:
            return None
        body = self._expect(response, 200, what=f"the read of branch {name!r}")
        return str(((body or {}).get("target") or {}).get("hash") or "") or None

    # Interface.
    def create_branch(self, name: str, sha: str) -> Branch:
        response = self._client.post(self._path("refs/branches"),
                                     {"name": name, "target": {"hash": sha}})
        if response.status == 201:
            return Branch(name, sha, created=True)
        if response.status in (400, 409) and ALREADY_EXISTS.search(message_of(response)):
            found = self._branch(name)
            if found == sha:
                return Branch(name, sha, created=False)
            raise BranchConflict(name, sha, found or "an unreadable commit")
        raise HostError(f"Bitbucket refused the branch {name!r}: {message_of(response)} "
                        f"(HTTP {response.status})")

    def commit_on_remote(self, branch: str, sha: str) -> bool:
        return self._branch(branch) == sha

    def open_pull_request(self, branch: str, title: str, reference, body: str = "",
                          base: str | None = None) -> PullRequest:
        base = base or ((self._repo().get("mainbranch") or {}).get("name")) or "main"
        payload = {
            "title": titled(title, reference),
            "source": {"branch": {"name": branch}},
            "destination": {"branch": {"name": base}},
            "close_source_branch": True,
        }
        description = body_of(reference, body)
        if description:
            payload["description"] = description
        response = self._client.post(self._path("pullrequests"), payload)
        if response.status == 201:
            return _pull_request(response.body)
        if response.status in (400, 409) and ALREADY_EXISTS.search(message_of(response)):
            existing = self._find_open(branch)
            if existing is not None:
                return existing
        raise HostError(f"Bitbucket refused the pull request for {branch!r}: "
                        f"{message_of(response)} (HTTP {response.status})")

    def _find_open(self, branch: str) -> PullRequest | None:
        query = f'source.branch.name = "{branch}" AND state = "OPEN"'
        response = self._client.get(self._path("pullrequests"), {"q": query})
        found = self._expect(response, 200, what="the pull request list")
        for item in (found or {}).get("values") or []:
            source = ((item.get("source") or {}).get("branch") or {}).get("name")
            if source == branch:
                return _pull_request(item)
        return None

    def post_check(self, sha: str, name: str, state: str, url: str | None = None,
                   summary: str = "") -> CheckPost:
        state = CheckState(state)
        if not url:
            raise HostError(f"a Bitbucket build status needs a url; {name!r} has none")
        payload = {"key": name, "name": name, "state": BUILD_STATE[state], "url": url,
                   "description": summary or name}
        response = self._client.post(self._path(f"commit/{sha}/statuses/build"), payload)
        body = self._expect(response, 200, 201, what=f"the build status {name!r}")
        return CheckPost(sha, name, state.value, None, (body or {}).get("url") or url)

    def enqueue(self, pr, landing: Landing | None = None) -> str:
        return NOT_SUPPORTED

    def pull_request(self, pr) -> PullRequest:
        number = pr.number if isinstance(pr, PullRequest) else int(str(pr).lstrip("#"))
        response = self._client.get(self._path(f"pullrequests/{number}"))
        body = self._expect(response, 200, what=f"the read of pull request #{number}")
        return _pull_request(body)

    def probe_landing(self, project) -> Landing:
        pinned = pinned_landing(project)
        if pinned:
            return pinned
        missing = [key for key, value in (("landing_path", project.landing_path),
                                          ("merge_method", project.merge_method)) if not value]
        raise ProbeUndetermined(
            "Bitbucket Cloud exposes no API for its merge queue or its strategy; pin "
            + " and ".join(missing) + " in the project file")


def _pull_request(body: dict) -> PullRequest:
    body = body if isinstance(body, dict) else {}
    if body.get("id") is None:
        raise HostError("Bitbucket returned a pull request without an id")
    raw_state = str(body.get("state") or "").upper()
    state = STATES.get(raw_state, CLOSED)
    merged = state == MERGED
    approvals = tuple(
        _login(p.get("user")) for p in body.get("participants") or []
        if p.get("approved") and _login(p.get("user")))
    return PullRequest(
        number=int(body.get("id")),
        state=state,
        merged=merged,
        head=((body.get("source") or {}).get("commit") or {}).get("hash"),
        merge_commit=(body.get("merge_commit") or {}).get("hash") if merged else None,
        approvals=approvals,
        head_ref=((body.get("source") or {}).get("branch") or {}).get("name"),
        base_ref=((body.get("destination") or {}).get("branch") or {}).get("name"),
        title=body.get("title") or "",
        url=((body.get("links") or {}).get("html") or {}).get("href"),
        node_id=None,
        merged_by=_login(body.get("closed_by")) if merged else None,
    )


def _login(user) -> str | None:
    user = user or {}
    return user.get("nickname") or user.get("display_name") or user.get("uuid")
