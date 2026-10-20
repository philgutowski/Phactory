/*
 * 003: abandon edges, and the claim row pinned both ways (card #42).
 *
 * In 001 no edge left claimed, waiting_for_slot, queued, or gate_running for
 * a terminal state, so a card that stopped being ready before dispatch, or a
 * pull request closed while queued or while its gate ran, could not be
 * recorded. Each now has an edge to abandoned, as the plan's state diagram
 * shows.
 *
 * The check on transitions in 001 said a NULL from_state belongs to the
 * claim row (seq 0, to claimed), but not the reverse, so a seq 0 row could
 * carry a from_state. The check below pins both ways: seq 0 is the claim
 * with a NULL from_state, and every later row names one. Postgres named the
 * 001 check transitions_check, the default for an unnamed table check.
 *
 * Block comments only, as in 001, so the no double hyphen rule holds.
 */

INSERT INTO allowed_edges (from_state, to_state) VALUES
    ('claimed', 'abandoned'),
    ('waiting_for_slot', 'abandoned'),
    ('queued', 'abandoned'),
    ('gate_running', 'abandoned');

ALTER TABLE transitions DROP CONSTRAINT transitions_check;

ALTER TABLE transitions ADD CONSTRAINT transitions_claim_row CHECK (
    (seq = 0 AND from_state IS NULL AND to_state = 'claimed')
    OR (seq > 0 AND from_state IS NOT NULL)
);
