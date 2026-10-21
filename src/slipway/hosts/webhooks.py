"""Verify, dedupe, and parse a host webhook into one normalised event (KTD10).

A webhook is an HTTP POST the host sends to Slipway's endpoint when
something happens; its JSON body is the payload. A host event is a wake up,
never evidence: every transition it causes re reads the pull request from
the host first. So this module's job is to let the right wake ups through
and nothing else, in this order:

1. The body cap. A body over BODY_CAP_BYTES is refused before it is read.
2. The repository. The payload names it (GitHub repository.full_name,
   Bitbucket repository.full_name). secrets_for(repository) is the
   resolution: it answers with that repository's secrets, or nothing, and
   an unknown repository is refused there, before any secret is chosen or
   compared, so a stranger's payload never reaches the signature check and
   learns nothing from its timing.
3. The signature. GitHub sends X-Hub-Signature-256, Bitbucket X-Hub-Signature,
   each "sha256=" and the hex HMAC SHA-256 of the raw body under the
   webhook secret. HMAC is a keyed hash: only a sender holding the secret
   can produce it. The compare is constant time, against the current secret
   and the previous one, so a rotation window accepts both (the operator
   file names both, U6). Nothing after this step runs on an unsigned body.
4. The event. The host's event header and the payload's action map onto a
   closed set of kinds (KINDS). An event Slipway does not track is Dropped,
   with its reason, and so is a pull request event for a pull request
   Slipway did not open when the caller passes opened_by_slipway.

The result carries the host's delivery id (GitHub X-GitHub-Delivery,
Bitbucket X-Request-UUID). ingest_host_event (U7) dedupes on (host, delivery
id), and Event.normalised() is the mapping it stores. A host retries a
delivery with the same id, so a replay is one row. Neither host signs a
timestamp, so a replay cannot be refused at the signature; the wake up rule
is what makes that harmless.

Where each field comes from:

    pr.merged         GitHub pull_request closed with merged true:
                      merge_commit_sha, head.sha. Bitbucket pullrequest:fulfilled:
                      merge_commit.hash, source.commit.hash.
    pr.closed         GitHub pull_request closed with merged false; Bitbucket
                      pullrequest:rejected.
    pr.synchronize    GitHub pull_request synchronize: the new head is after
                      (and pull_request.head.sha). Bitbucket pullrequest:updated:
                      source.commit.hash, which is also the head after an edit
                      that moved nothing; the caller compares it with the
                      confirmed commit.
    pr.dequeued       GitHub pull_request dequeued, with its reason.
    queue.requested   GitHub merge_group checks_requested: tested is
                      merge_group.head_sha, and the group's pull request number
                      and head come from head_ref, which GitHub names
                      refs/heads/gh-readonly-queue/<base>/pr-<n>-<head sha>.
                      Bitbucket repo:commit_status_created or updated with an
                      INPROGRESS state on a bitbucket-merge-queue-* ref: tested
                      is commit_status.commit.hash; the payload names no pull
                      request, so the caller resolves it through the adapter.
    check.completed   GitHub check_run completed (name, conclusion, head_sha)
                      and status with a final state (context, state, sha);
                      Bitbucket a commit status in a final state.
"""

import hashlib
import hmac
import json
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from slipway.config.operator import Host

# Bigger than any pull request or merge group payload either host sends; a
# body this size is not a webhook.
BODY_CAP_BYTES = 1_000_000

PR_MERGED = "pr.merged"
PR_CLOSED = "pr.closed"
PR_SYNCHRONIZE = "pr.synchronize"
PR_DEQUEUED = "pr.dequeued"
QUEUE_REQUESTED = "queue.requested"
CHECK_COMPLETED = "check.completed"
KINDS = (PR_MERGED, PR_CLOSED, PR_SYNCHRONIZE, PR_DEQUEUED, QUEUE_REQUESTED, CHECK_COMPLETED)

# Refusal reasons, each a word the endpoint can map to a status code.
OVER_CAP = "body_over_cap"
NOT_JSON = "not_json"
NO_REPOSITORY = "no_repository"
UNKNOWN_REPOSITORY = "unknown_repository"
NO_SIGNATURE = "no_signature"
BAD_SIGNATURE = "bad_signature"
NO_DELIVERY_ID = "no_delivery_id"

GITHUB_SIGNATURE = "X-Hub-Signature-256"
GITHUB_DELIVERY = "X-GitHub-Delivery"
GITHUB_EVENT = "X-GitHub-Event"
BITBUCKET_SIGNATURE = "X-Hub-Signature"
BITBUCKET_DELIVERY = "X-Request-UUID"
BITBUCKET_EVENT = "X-Event-Key"
SIGNATURE_PREFIX = "sha256="
# The hex of a SHA-256 digest, the only shape a signature can take.
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")

# GitHub's temporary queue branch: the base, the pull request number, and
# the pull request head the group was built from.
QUEUE_REF = re.compile(r"^refs/heads/gh-readonly-queue/(?P<base>.+)/pr-(?P<number>\d+)-"
                       r"(?P<head>[0-9a-f]{7,64})$")
BITBUCKET_QUEUE_REF = re.compile(r"^bitbucket-merge-queue-")

GITHUB_FINAL_STATUSES = frozenset({"success", "failure", "error"})
BITBUCKET_FINAL_STATUSES = frozenset({"SUCCESSFUL", "FAILED", "STOPPED"})
BITBUCKET_PENDING = "INPROGRESS"


# How many delivery ids an in process DeliveryLog remembers.
DELIVERY_LOG_SIZE = 10_000


class DeliveryLog:
    """The in process half of dedupe: the delivery ids seen lately, bounded.

    The durable half is host_events (U7), keyed on (host, delivery id). This
    log lets the endpoint answer a host's retry without a database round
    trip; it is a cache, so losing it loses nothing.
    """

    def __init__(self, size: int = DELIVERY_LOG_SIZE):
        self._size = size
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    def seen(self, host: str, delivery_id: str) -> bool:
        """Record the delivery and say whether it was already recorded."""
        key = (str(host), str(delivery_id))
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = None
        while len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return False

    def forget(self, host: str, delivery_id: str) -> None:
        """Drop a recorded delivery, so the host's redelivery is not taken for a duplicate.

        The endpoint records an id before it stores the delivery; when storing
        fails, it forgets the id so the retry is stored instead of answered.
        """
        self._seen.pop((str(host), str(delivery_id)), None)


class WebhookRefused(Exception):
    """The delivery is not one Slipway accepts. reason is one of the words above."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class Event:
    """One accepted delivery, normalised. kind is one of KINDS."""

    kind: str
    host: str
    repository: str
    delivery_id: str
    pr_number: int | None = None
    head_commit: str | None = None
    merge_commit: str | None = None
    tested_commit: str | None = None
    tested_head: str | None = None
    check_name: str | None = None
    check_state: str | None = None
    detail: dict = field(default_factory=dict)

    def normalised(self) -> dict:
        """The mapping ingest_host_event stores: HOST_EVENT_FIELDS plus the rest."""
        return {
            "host": self.host, "delivery_id": self.delivery_id, "repository": self.repository,
            "pr_number": self.pr_number, "kind": self.kind, "merge_commit": self.merge_commit,
            "head_commit": self.head_commit, "tested_commit": self.tested_commit,
            "tested_head": self.tested_head, "check_name": self.check_name,
            "check_state": self.check_state, **self.detail,
        }


@dataclass(frozen=True)
class Dropped:
    """A verified delivery Slipway has nothing to do with."""

    reason: str
    host: str
    repository: str
    delivery_id: str


SecretsFor = Callable[[str], tuple[str | None, ...] | None]
OpenedBySlipway = Callable[[str, int], bool]


def verify(host, headers: Mapping[str, str], body: bytes, secrets_for: SecretsFor,
           opened_by_slipway: OpenedBySlipway | None = None) -> Event | Dropped:
    """Cap, resolve the repository, check the signature, then parse (KTD10).

    secrets_for(repository) returns the current secret and, during a rotation
    window, the previous one; None or an empty tuple means Slipway does not
    manage that repository. opened_by_slipway(repository, number) says whether
    Slipway opened the pull request; without it no pull request is dropped.
    Raises WebhookRefused for anything that fails before parsing.
    """
    host = Host(host)
    if len(body) > BODY_CAP_BYTES:
        raise WebhookRefused(OVER_CAP, f"{len(body)} bytes, the cap is {BODY_CAP_BYTES}")
    found = _Headers(headers)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise WebhookRefused(NOT_JSON, str(error)) from None
    except RecursionError:
        # Brackets nested deeper than the parser's stack, under the cap.
        raise WebhookRefused(NOT_JSON, "the payload nests too deeply") from None
    if not isinstance(payload, dict):
        raise WebhookRefused(NOT_JSON, "the payload is not a JSON object")

    repository = _repository_of(payload)
    if not repository:
        raise WebhookRefused(NO_REPOSITORY, "the payload names no repository")
    secrets = tuple(s for s in (secrets_for(repository) or ()) if s)
    if not secrets:
        raise WebhookRefused(UNKNOWN_REPOSITORY, repository)

    signature_header = GITHUB_SIGNATURE if host == Host.GITHUB else BITBUCKET_SIGNATURE
    check_signature(found.get(signature_header), body, secrets)

    delivery_header = GITHUB_DELIVERY if host == Host.GITHUB else BITBUCKET_DELIVERY
    delivery_id = found.get(delivery_header)
    if not delivery_id:
        raise WebhookRefused(NO_DELIVERY_ID, f"no {delivery_header} header")

    event_header = GITHUB_EVENT if host == Host.GITHUB else BITBUCKET_EVENT
    event_name = found.get(event_header) or ""
    parse_fn = parse_github if host == Host.GITHUB else parse_bitbucket
    result = parse_fn(event_name, payload, repository, delivery_id, host.value)
    if (isinstance(result, Event) and result.kind.startswith("pr.")
            and result.pr_number is not None and opened_by_slipway is not None
            and not opened_by_slipway(repository, result.pr_number)):
        return Dropped(f"pull request #{result.pr_number} was not opened by Slipway",
                       host.value, repository, delivery_id)
    return result


def check_signature(header: str | None, body: bytes, secrets: tuple[str, ...]) -> int:
    """Constant time HMAC check against each secret in turn; returns the index that
    matched (0 current, 1 previous). Raises WebhookRefused otherwise."""
    if not header or not header.startswith(SIGNATURE_PREFIX):
        raise WebhookRefused(NO_SIGNATURE, "no sha256= signature header")
    given = header[len(SIGNATURE_PREFIX):].strip().lower()
    if not HEX_DIGEST.fullmatch(given):
        # Not a hex digest, so it can match nothing; compare_digest would
        # also refuse a non ASCII string with a TypeError instead of a no.
        raise WebhookRefused(BAD_SIGNATURE, "the signature is not a hex digest")
    matched = None
    for index, secret in enumerate(secrets):
        expected = sign(body, secret)
        # Every secret is compared, so the time taken does not say which one matched.
        if hmac.compare_digest(expected, given) and matched is None:
            matched = index
    if matched is None:
        raise WebhookRefused(BAD_SIGNATURE, "the signature matches no secret")
    return matched


def sign(body: bytes, secret: str) -> str:
    """The hex HMAC SHA-256 of body under secret, as both hosts send it."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_header(body: bytes, secret: str) -> str:
    return SIGNATURE_PREFIX + sign(body, secret)


class _Headers:
    """Case insensitive header lookup over whatever mapping the server passed."""

    def __init__(self, headers: Mapping[str, str]):
        self._lower = {str(k).lower(): str(v) for k, v in headers.items()}

    def get(self, name: str) -> str | None:
        return self._lower.get(name.lower())


def _repository_of(payload: dict) -> str | None:
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return None
    full_name = repository.get("full_name")
    return str(full_name) if full_name else None


def _pr_number(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_github(event_name: str, payload: dict, repository: str, delivery_id: str,
                 host: str = Host.GITHUB.value) -> Event | Dropped:
    action = str(payload.get("action") or "")
    pr = payload.get("pull_request") or {}
    head = (pr.get("head") or {}).get("sha")

    def event(kind, **fields):
        return Event(kind, host, repository, delivery_id, **fields)

    def dropped(reason):
        return Dropped(reason, host, repository, delivery_id)

    if event_name == "pull_request":
        number = _pr_number(pr.get("number") or payload.get("number"))
        if action == "closed":
            if pr.get("merged"):
                return event(PR_MERGED, pr_number=number, head_commit=head,
                             merge_commit=pr.get("merge_commit_sha"),
                             detail={"merged_by": (pr.get("merged_by") or {}).get("login")})
            return event(PR_CLOSED, pr_number=number, head_commit=head)
        if action == "synchronize":
            return event(PR_SYNCHRONIZE, pr_number=number,
                         head_commit=payload.get("after") or head,
                         detail={"before": payload.get("before")})
        if action == "dequeued":
            return event(PR_DEQUEUED, pr_number=number, head_commit=head,
                         detail={"reason": payload.get("reason")})
        return dropped(f"pull_request {action or 'without an action'} is not tracked")

    if event_name == "merge_group":
        group = payload.get("merge_group") or {}
        if action != "checks_requested":
            return dropped(f"merge_group {action or 'without an action'} is not tracked")
        match = QUEUE_REF.match(str(group.get("head_ref") or ""))
        number = _pr_number(match.group("number")) if match else None
        return event(QUEUE_REQUESTED, pr_number=number, tested_commit=group.get("head_sha"),
                     tested_head=match.group("head") if match else None,
                     detail={"head_ref": group.get("head_ref"), "base_sha": group.get("base_sha"),
                             "base_ref": group.get("base_ref")})

    if event_name == "check_run":
        run = payload.get("check_run") or {}
        if action != "completed":
            return dropped(f"check_run {action or 'without an action'} is not tracked")
        return event(CHECK_COMPLETED, head_commit=run.get("head_sha"),
                     check_name=run.get("name"), check_state=run.get("conclusion"),
                     pr_number=_first_pr(run.get("pull_requests")))

    if event_name == "status":
        state = str(payload.get("state") or "")
        if state not in GITHUB_FINAL_STATUSES:
            return dropped(f"status {state or 'without a state'} is not final")
        return event(CHECK_COMPLETED, head_commit=payload.get("sha"),
                     check_name=payload.get("context"), check_state=state)

    return dropped(f"{event_name or 'an event without a name'} is not tracked")


def _first_pr(listed) -> int | None:
    for item in listed or []:
        number = _pr_number((item or {}).get("number"))
        if number is not None:
            return number
    return None


def parse_bitbucket(event_key: str, payload: dict, repository: str, delivery_id: str,
                    host: str = Host.BITBUCKET.value) -> Event | Dropped:
    pr = payload.get("pullrequest") or {}
    number = _pr_number(pr.get("id"))
    head = ((pr.get("source") or {}).get("commit") or {}).get("hash")

    def event(kind, **fields):
        return Event(kind, host, repository, delivery_id, **fields)

    def dropped(reason):
        return Dropped(reason, host, repository, delivery_id)

    if event_key == "pullrequest:fulfilled":
        return event(PR_MERGED, pr_number=number, head_commit=head,
                     merge_commit=(pr.get("merge_commit") or {}).get("hash"),
                     detail={"merged_by": _nickname(pr.get("closed_by") or payload.get("actor"))})
    if event_key == "pullrequest:rejected":
        return event(PR_CLOSED, pr_number=number, head_commit=head)
    if event_key == "pullrequest:updated":
        return event(PR_SYNCHRONIZE, pr_number=number, head_commit=head)
    if event_key in ("repo:commit_status_created", "repo:commit_status_updated"):
        status = payload.get("commit_status") or {}
        state = str(status.get("state") or "")
        sha = (status.get("commit") or {}).get("hash")
        name = status.get("key") or status.get("name")
        refname = str(status.get("refname") or "")
        if state == BITBUCKET_PENDING and BITBUCKET_QUEUE_REF.match(refname):
            return event(QUEUE_REQUESTED, tested_commit=sha, check_name=name,
                         detail={"refname": refname})
        if state in BITBUCKET_FINAL_STATUSES:
            return event(CHECK_COMPLETED, head_commit=sha, check_name=name,
                         check_state=state, detail={"refname": refname or None})
        return dropped(f"commit status {state or 'without a state'} on "
                       f"{refname or 'no ref'} is not tracked")
    return dropped(f"{event_key or 'an event without a key'} is not tracked")


def _nickname(user) -> str | None:
    user = user or {}
    return user.get("nickname") or user.get("display_name") or user.get("uuid")
