"""The composed status: one read that answers "what is going on" (U25, R28).

compose(ports, project) reads each card's latest build in one snapshot of
the records and returns what a surface shows as it is:

    counts     how many cards are being built, waiting to land, and need
               you, plus how many landed today (UTC)
    cards      one entry per card: its latest build, state, bucket, age, and
               the pull request's host link; each entry agrees with the
               atomic card query (records.queries.card_detail) for the same key
    needs_you  one entry per card a person must act on, with the line that
               says why, what to do, its age, and the host link
    landed     one entry per card that landed today, with both scores
    findings   the project's open findings, newest last
    spoken     one sentence a voice client can read out, such as "3 being
               built, 1 waiting to land, 1 needs you. EX-4 is halted: ..."

An age is how long a card has been in its state, such as "5m" or "2d".
A finding is a fault Slipway saw in a gate run that is not the card's own,
such as a runner step that failed (KTD20).

A bucket is the page group a state belongs to, from the states table in
the records: being_built, waiting_to_land, needs_you, or done.

across(ports, actor) runs compose over every configured project, for the
operator alone, since it shows every project's cards at once.

Nothing here calls a host, and the tracker is read for one fact alone: an
abandoned card is shown only while the card is still in the ready status,
so it is dropped once a person moves it off (the plan's states table). That
read happens only when a card's latest build is abandoned, so a status read
stays cheap enough to ask for by voice.
"""

from datetime import datetime

from slipway.service.verbs import Ports, day_start, halt_of, host_link

BUCKETS = ("being_built", "waiting_to_land", "needs_you")
# What a person does about each needs you state. halted carries its own
# line from the halt class (halts.py); the others say it here.
WHAT_TO_DO = {
    "awaiting_approval": "approve the pull request on the host",
    "gate_red": "fix the gate on the host and queue the pull request again",
    "halted": "check the evidence; land re verifies, and an override lands a commit mismatch",
    "returned_empty": "dispatch the card again with a reason",
    "abandoned": "dispatch the card again with a reason if it is still wanted",
}
# How many needs you entries the spoken sentence reads out before it counts the rest.
SPOKEN_NEEDS_YOU = 3
LABELS = {"being_built": "being built", "waiting_to_land": "waiting to land",
          "needs_you": "need you"}
# The two scores a landed build carries: the deterministic one (U16) and the
# judged one, a model's reading of the diff against the card (U17).
SCORE_KINDS = ("deterministic", "judged")


def age(now: datetime, then: datetime | None) -> str:
    """How long ago then was, in the largest whole unit: "now", "5m", "3h", "2d"."""
    if then is None:
        return ""
    seconds = int((now - then).total_seconds())
    if seconds < 60:
        return "now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return "now"


def score_line(result: dict | None) -> str | None:
    """One score as a short line, such as "pass, 3 of 3", or None when it is not written yet."""
    if result is None:
        return None
    if not isinstance(result, dict):  # a step that wrote a bare value still shows as written
        return "written"
    total = result.get("total") if isinstance(result.get("total"), dict) else result
    verdict = total.get("verdict")
    if "passed" in total and "of" in total:
        counted = f"{total['passed']} of {total['of']}"
        return f"{verdict}, {counted}" if verdict else counted
    if "score" in total:
        return f"{verdict}, {total['score']}" if verdict else str(total["score"])
    return str(verdict) if verdict else "written"


def _still_ready(ports: Ports, project, key: str) -> bool:
    """Whether an abandoned card is still in the ready status; True when the tracker cannot say."""
    try:
        card = ports.tracker(project).read(key)
    except Exception:  # an unreadable tracker keeps the entry: hiding it would hide work
        return True
    if getattr(card, "skipped", None):
        return True
    status = (card.status or "").strip().lower()
    return status == (project.ready_status or "").strip().lower()


def _why(ports: Ports, row: dict) -> str:
    """The line that says why a needs you card is there."""
    state = row["state"]
    if state == "halted":
        found = halt_of(ports, row["build_id"]) or {}
        return found.get("line") or "halted with no recorded line"
    evidence = ports.records.entered(row["build_id"], state) or {}
    if evidence.get("line"):
        return evidence["line"]
    if evidence.get("reason"):
        return str(evidence["reason"])
    if state == "awaiting_approval":
        return f"pull request #{row.get('pr_number')} waits for approval"
    return state.replace("_", " ")


def compose(ports: Ports, project: str) -> dict:
    """The project's status from one snapshot of the records."""
    found = ports.project(project)
    records = ports.records
    now = ports.clock()
    since = day_start(now)
    project_id = records.project_id(found.slug)  # may insert, so before the read only snapshot
    with records.snapshot():
        rows = records.latest_builds(project_id)
        landed_rows = [row for row in rows
                       if row["bucket"] == "done" and row["updated_at"] >= since]
        landed_ids = {row["build_id"] for row in landed_rows}
        scores = records.scores(sorted(landed_ids))
        findings = [{"id": row["id"], "class": row["class"], "fingerprint": row["fingerprint"],
                     "excerpt": row.get("excerpt") or "", "build_id": row.get("build_id"),
                     "age": age(now, row.get("created_at"))}
                    for row in records.open_findings(project_id)]
        lines = {row["build_id"]: _why(ports, row) for row in rows
                 if row["bucket"] == "needs_you"}
    dropped = {row["card_key"] for row in rows if row["state"] == "abandoned"
               and not _still_ready(ports, found, row["card_key"])}
    cards, needs_you, landed = [], [], []
    counts = dict.fromkeys(BUCKETS, 0) | {"landed_today": 0}
    for row in rows:
        if row["card_key"] in dropped:
            continue
        link = host_link(found, row.get("pr_number"))
        when = age(now, row.get("updated_at"))
        cards.append({"key": row["card_key"], "build_id": row["build_id"],
                      "state": row["state"], "bucket": row["bucket"],
                      "pr_number": row.get("pr_number"), "link": link, "age": when})
        if row["bucket"] in counts:
            counts[row["bucket"]] += 1
        elif row["build_id"] in landed_ids:
            counts["landed_today"] += 1
            kinds = scores.get(row["build_id"], {})
            landed.append({"key": row["card_key"], "build_id": row["build_id"],
                           "state": row["state"], "link": link, "age": when,
                           "scores": {kind: score_line(kinds.get(kind))
                                      for kind in SCORE_KINDS}})
        if row["bucket"] == "needs_you":
            needs_you.append({"key": row["card_key"], "build_id": row["build_id"],
                              "state": row["state"], "line": lines[row["build_id"]],
                              "what": WHAT_TO_DO.get(row["state"], ""), "link": link,
                              "age": when})
    return {"project": found.slug, "counts": counts, "cards": cards, "needs_you": needs_you,
            "landed": landed, "findings": findings, "spoken": spoken(counts, needs_you)}


def spoken(counts: dict, needs_you: list[dict]) -> str:
    """The status as one sentence, every non zero bucket named, then each needs you card."""
    parts = []
    for bucket in BUCKETS:
        n = counts.get(bucket, 0)
        if n:
            label = LABELS[bucket]
            if bucket == "needs_you" and n == 1:
                label = "needs you"
            parts.append(f"{n} {label}")
    if counts.get("landed_today"):
        parts.append(f"{counts['landed_today']} landed today")
    sentence = (", ".join(parts) if parts else "Nothing is in flight") + "."
    for entry in needs_you[:SPOKEN_NEEDS_YOU]:
        state = entry["state"].replace("_", " ")
        sentence += f" {entry['key']} is {state}: {entry['line'].rstrip('.')}."
    rest = len(needs_you) - SPOKEN_NEEDS_YOU
    if rest > 0:
        sentence += f" {rest} more need you."
    return sentence


def across(ports: Ports, actor: str) -> dict:
    """compose over every configured project, for the operator's subject only."""
    if actor != ports.operator().operator_subject:
        return {"outcome": "refused", "reason": "only the operator sees every project"}
    projects, unreadable = [], []
    for slug in ports.slugs():
        try:
            projects.append(compose(ports, slug))
        except ValueError as error:  # a project file that fails validation (ConfigError)
            unreadable.append({"project": slug, "error": str(error)})
    totals = dict.fromkeys(BUCKETS, 0) | {"landed_today": 0}
    for status in projects:
        for name, n in status["counts"].items():
            totals[name] += n
    needs_you = [{**entry, "key": f"{status['project']} {entry['key']}"}
                 for status in projects for entry in status["needs_you"]]
    sentence = spoken(totals, needs_you)
    if unreadable:
        sentence += f" {len(unreadable)} project files could not be read."
    return {"counts": totals, "projects": projects, "unreadable": unreadable,
            "spoken": sentence}


__all__ = ["BUCKETS", "SCORE_KINDS", "WHAT_TO_DO", "across", "age", "compose", "score_line",
           "spoken"]
