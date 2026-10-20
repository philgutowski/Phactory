/*
 * 002: a stable identity for polled host events (card #41).
 *
 * A polled event carries no delivery id, and a polled pr.synchronize or
 * pr.closed carries no merge commit. Postgres treats NULLs as distinct in a
 * unique key, so neither key in 001 matched a second poll of the same state,
 * and every poll cycle inserted a new row and woke the workflow again.
 *
 * ingest_host_event (store.py) now mints poll_identity for every event whose
 * source is poll, from the event's own facts (see poll_identity there), and
 * leaves it NULL for a webhook, whose delivery id already dedupes it. Two
 * identical polls of one pull request head therefore share a poll_identity,
 * and the key below makes the second one a no op.
 *
 * Block comments only, as in 001, so the no double hyphen rule holds.
 */

ALTER TABLE host_events ADD COLUMN poll_identity text;

ALTER TABLE host_events
    ADD CONSTRAINT host_events_poll_identity_key UNIQUE (project_id, poll_identity);
