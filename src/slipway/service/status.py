"""The composed status: one read that answers "what is going on" (U25, R28).

compose(ports, project) reads each card's latest build in one snapshot of
the records and returns four things a surface shows as they are:

    counts     how many cards are being built, waiting to land, and need
               you, plus how many landed today (UTC)
    cards      one entry per card: its latest build, state, bucket, and the
               pull request's host link; each entry agrees with the atomic
               card query (records.queries.card_detail) for the same key
    needs_you  one entry per card a person must act on, with the line that
               says why, what to do, and the host link
    spoken     one sentence a voice client can read out, such as "3 being
               built, 1 waiting to land, 1 needs you. EX-4 is halted: ..."

A bucket is the page group a state belongs to, from the states table in
the records: being_built, waiting_to_land, needs_you, or done.

across(ports, actor) runs compose over every configured project, for the
operator alone, since it shows every project's cards at once.

Nothing here calls a host or a tracker: the records hold every fact the
status names, so a status read is cheap enough to ask for by voice.
"""

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
    since = day_start(ports.clock())
    project_id = records.project_id(found.slug)  # may insert, so before the read only snapshot
    with records.snapshot():
        rows = records.latest_builds(project_id)
        cards, needs_you = [], []
        counts = dict.fromkeys(BUCKETS, 0) | {"landed_today": 0}
        for row in rows:
            link = host_link(found, row.get("pr_number"))
            cards.append({"key": row["card_key"], "build_id": row["build_id"],
                          "state": row["state"], "bucket": row["bucket"],
                          "pr_number": row.get("pr_number"), "link": link})
            if row["bucket"] in counts:
                counts[row["bucket"]] += 1
            elif row["bucket"] == "done" and row["updated_at"] >= since:
                counts["landed_today"] += 1
            if row["bucket"] == "needs_you":
                needs_you.append({"key": row["card_key"], "build_id": row["build_id"],
                                  "state": row["state"], "line": _why(ports, row),
                                  "what": WHAT_TO_DO.get(row["state"], ""), "link": link})
    return {"project": found.slug, "counts": counts, "cards": cards, "needs_you": needs_you,
            "spoken": spoken(counts, needs_you)}


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


__all__ = ["BUCKETS", "WHAT_TO_DO", "across", "compose", "spoken"]
