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

The onboarding reads and the scratch writes (U28):

    read_repository       GET repositories/<workspace>/<slug>
    read_required_checks  GET branch-restrictions?kind=require_passing_builds_to_merge
    read_restriction      GET branch-restrictions?kind=push, and GET user for the
                          identity the token acts as
    base_commits          GET refs/branches/<main>
    branches              GET refs/branches?q=name ~ "<prefix>"
    delete_branch         DELETE refs/branches/<name>
    open_pull_requests    GET pullrequests?q=destination.branch.name = "<base>" ...
    close_pull_request    POST pullrequests/<id>/decline

A branch restriction is Bitbucket's per pattern branch rule. A merge check
is one of kind require_passing_builds_to_merge: the pull request may not
merge until that many builds on its head pass. Bitbucket does not name the
builds, so the required check reads as "1 passing build". A branch
permission is one of kind push: only the users and groups it lists may push
to branches matching its pattern. Bitbucket's patterns are globs with no
way to exclude a prefix, so the restriction read holds the main branch, the
one the gate protects: a push permission must match it, and the identity
the token acts as must not be among its users. Where Bitbucket will not say
who the token acts as, a permission that lists no one still proves it, and
any other answers None. Reading branch restrictions needs the token's
repository admin scope; without it both reads answer None, so onboard asks
the operator instead.

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

import fnmatch
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
    Reading,
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

MERGE_CHECK = "require_passing_builds_to_merge"
PUSH = "push"
GLOB = "glob"
PAGE_SIZE = 100
# Pages one listing may take; a repository with more is a reason, not a guess.
PAGE_LIMIT = 10


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


    # Onboarding reads (U28).
    def _main(self) -> str:
        return (self._repo().get("mainbranch") or {}).get("name") or "main"

    def _pages(self, path: str, params: dict, what: str) -> list | None:
        """Every value of a paged listing, or None when Bitbucket refuses to show it."""
        url, query, found = self._path(path), dict(params, pagelen=PAGE_SIZE), []
        for _ in range(PAGE_LIMIT):
            response = self._client.get(url, query)
            if response.status in (401, 403):
                return None
            body = self._expect(response, 200, what=what) or {}
            found += body.get("values") or []
            url, query = body.get("next"), None
            if not url:
                return found
        raise HostError(f"{what} ran past {PAGE_LIMIT} pages")

    def _restrictions(self, kind: str) -> list | None:
        """The branch restrictions of one kind whose glob matches the main branch."""
        found = self._pages("branch-restrictions", {"kind": kind},
                            f"the {kind} branch restrictions")
        if found is None:
            return None
        main = self._main()
        return [r for r in found if (r.get("branch_match_kind") or GLOB) == GLOB
                and fnmatch.fnmatchcase(main, str(r.get("pattern") or ""))]

    def read_repository(self) -> Reading:
        response = self._client.get(f"/repositories/{self._workspace}/{self._slug}")
        if response.status != 200:
            return Reading(False, f"Bitbucket answered {message_of(response)} (HTTP "
                                  f"{response.status}) for {self._repository}")
        self._repo_cache = response.body
        return Reading(True, f"{self._repository} is reachable, main branch {self._main()}")

    def read_required_checks(self) -> tuple[str, ...]:
        found = self._restrictions(MERGE_CHECK)
        if found is None:
            raise ProbeUndetermined("Bitbucket would not show the branch restrictions; the "
                                    "token needs the repository admin scope to read them")
        counts = [int(r.get("value") or 0) for r in found]
        most = max(counts, default=0)
        if most < 1:
            return ()
        return (f"{most} passing build" + ("s" if most > 1 else ""),)

    def read_restriction(self, namespace: str) -> Reading:
        found = self._restrictions(PUSH)
        main = self._main()
        if found is None:
            return Reading(None, "Bitbucket would not show the branch permissions; the token "
                                 "needs the repository admin scope to read them")
        if not found:
            return Reading(False, f"no branch permission limits who may push to {main}")
        me = self._client.get("/user")
        identity = (me.body or {}).get("uuid") if me.ok and isinstance(me.body, dict) else None
        for restriction in found:
            users = [u.get("uuid") for u in restriction.get("users") or [] if u]
            if identity and identity in users:
                return Reading(False, f"the branch permission on {restriction.get('pattern')} "
                                      f"lets the token's identity push to {main}")
        if identity:
            return Reading(True, f"only listed users may push to {main}, and the token's "
                                 f"identity is not one; it may push under {namespace}")
        if any(not (r.get("users") or r.get("groups")) for r in found):
            return Reading(True, f"no one may push to {main} directly")
        return Reading(None, f"Bitbucket did not say who the token acts as, and the branch "
                             f"permission on {main} lists users or groups")

    # Scratch writes for the board movement check (U28, KTD27).
    def base_commits(self) -> tuple[str, str, str | None]:
        main = self._main()
        response = self._client.get(self._path(f"refs/branches/{main}"))
        target = (self._expect(response, 200, what=f"the head of {main!r}") or {}).get(
            "target") or {}
        parents = target.get("parents") or []
        return main, str(target.get("hash") or ""), (parents[0].get("hash") if parents else None)

    def branches(self, prefix: str) -> tuple[str, ...]:
        found = self._pages("refs/branches", {"q": f'name ~ "{prefix}"'},
                            f"the branches under {prefix!r}")
        if found is None:
            raise HostError(f"Bitbucket refused to list the branches under {prefix!r}")
        return tuple(str(b.get("name")) for b in found
                     if str((b or {}).get("name") or "").startswith(prefix))

    def delete_branch(self, name: str) -> bool:
        response = self._client.delete(self._path(f"refs/branches/{name}"))
        if response.status == 204:
            return True
        if response.status == 404:
            return False
        raise HostError(f"Bitbucket refused to delete the branch {name!r}: "
                        f"{message_of(response)} (HTTP {response.status})")

    def open_pull_requests(self, base: str) -> tuple[PullRequest, ...]:
        query = f'destination.branch.name = "{base}" AND state = "OPEN"'
        found = self._pages("pullrequests", {"q": query}, f"the pull requests into {base!r}")
        if found is None:
            raise HostError(f"Bitbucket refused to list the pull requests into {base!r}")
        return tuple(_pull_request(item) for item in found)

    def close_pull_request(self, pr) -> None:
        number = pr.number if isinstance(pr, PullRequest) else int(str(pr).lstrip("#"))
        response = self._client.post(self._path(f"pullrequests/{number}/decline"), {})
        self._expect(response, 200, what=f"declining pull request #{number}")


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
