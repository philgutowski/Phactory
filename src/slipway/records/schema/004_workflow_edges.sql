/*
 * 004: the edges the card workflow (U14) needs that 001 and 003 lack.
 *
 * Three groups, each named by the rule in the plan that needs it:
 *
 * A binding that does not resolve ends the build as returned_empty before
 * any spend (KTD11, F4), which is before the branch is created, so the build
 * is still claimed or waiting_for_slot when it ends.
 *
 * The workflow re reads the card at every transition Slipway causes, and a
 * card no longer ready moves to abandoned (plan U14, step 6). Those
 * transitions leave dispatched, building, and returned too, so each gets an
 * edge to abandoned. A session started before the abandon still passes
 * through post_build on the way out.
 *
 * On the auto merge paths (auto_merge_strict, plain_merge) no queue event
 * ever arrives, so the host's merged event finds the build in pr_open or
 * awaiting_approval, never in gate_running (KTD7). Each gets an edge to
 * merged.
 *
 * Block comments only, as in 001, so the no double hyphen rule holds.
 */

INSERT INTO allowed_edges (from_state, to_state) VALUES
    ('claimed', 'returned_empty'),
    ('waiting_for_slot', 'returned_empty'),
    ('dispatched', 'abandoned'),
    ('building', 'abandoned'),
    ('returned', 'abandoned'),
    ('pr_open', 'merged'),
    ('awaiting_approval', 'merged');
