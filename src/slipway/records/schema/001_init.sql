/*
 * 001: the first version of Slipway's records (KTD16).
 *
 * Comments in this file use block comments only, so the repository's no
 * double hyphen rule holds in SQL too. migrate.py applies this file in one
 * transaction with a short lock_timeout and records it in schema_migrations.
 *
 * Every table that belongs to a project carries project_id with a foreign
 * key to projects that cascades, so deleting a project later removes its
 * records with no format change. audit is the exception: it is keyed by the
 * project's slug as text so it outlives that deletion.
 */

/*
 * The states a build can be in, and which page bucket each one shows in.
 * ownership_terminal marks the set KTD3 names: a build in one of these no
 * longer owns its card, so a new build for the same card may be claimed.
 */
CREATE TABLE states (
    name text PRIMARY KEY,
    bucket text NOT NULL CHECK (bucket IN ('being_built', 'waiting_to_land', 'needs_you', 'done')),
    ownership_terminal boolean NOT NULL DEFAULT false
);

INSERT INTO states (name, bucket, ownership_terminal) VALUES
    ('claimed', 'being_built', false),
    ('waiting_for_slot', 'being_built', false),
    ('dispatched', 'being_built', false),
    ('building', 'being_built', false),
    ('returned', 'being_built', false),
    ('pr_open', 'waiting_to_land', false),
    ('queued', 'waiting_to_land', false),
    ('gate_running', 'waiting_to_land', false),
    ('merged', 'waiting_to_land', false),
    ('verifying', 'waiting_to_land', false),
    ('awaiting_approval', 'needs_you', false),
    ('gate_red', 'needs_you', false),
    ('halted', 'needs_you', false),
    ('returned_empty', 'needs_you', true),
    ('abandoned', 'needs_you', true),
    ('landed', 'done', false),
    ('scored', 'done', true);

/*
 * The card state machine as data. A transition row must name one of these
 * pairs (the composite foreign key on transitions), and a build's state can
 * only move along one of them (the trigger on builds).
 */
CREATE TABLE allowed_edges (
    from_state text NOT NULL REFERENCES states (name),
    to_state text NOT NULL REFERENCES states (name),
    PRIMARY KEY (from_state, to_state)
);

INSERT INTO allowed_edges (from_state, to_state) VALUES
    ('claimed', 'waiting_for_slot'),
    ('claimed', 'dispatched'),
    ('waiting_for_slot', 'dispatched'),
    ('dispatched', 'building'),
    ('dispatched', 'returned_empty'),
    ('building', 'returned'),
    ('building', 'returned_empty'),
    ('returned', 'pr_open'),
    ('pr_open', 'awaiting_approval'),
    ('pr_open', 'abandoned'),
    ('awaiting_approval', 'queued'),
    ('awaiting_approval', 'abandoned'),
    ('queued', 'gate_running'),
    ('gate_running', 'merged'),
    ('gate_running', 'gate_red'),
    ('gate_red', 'queued'),
    ('gate_red', 'abandoned'),
    ('merged', 'verifying'),
    ('verifying', 'landed'),
    ('verifying', 'halted'),
    ('halted', 'verifying'),
    ('landed', 'scored');

/*
 * events_floor is the highest transitions.id a pruning step has removed for
 * this project. Nothing prunes in this version, so it stays 0; a cursor below
 * it makes events_since report truncated.
 */
CREATE TABLE projects (
    id serial PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    events_floor bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

/* paths is the card's declared file list (KTD5); NULL means not declared. */
CREATE TABLE cards (
    id bigserial PRIMARY KEY,
    project_id integer NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    card_key text NOT NULL,
    title text,
    paths jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, card_key)
);

/*
 * One row per attempt to build a card. The columns from run_id down to
 * runner_commit are claim columns: a side effect step writes its column in
 * the same transaction as the transition that follows it, and skips the call
 * when the column is already set. last_seq is the seq of the build's latest
 * transition, advanced under the row lock the update takes.
 */
CREATE TABLE builds (
    id bigserial PRIMARY KEY,
    project_id integer NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    card_key text NOT NULL,
    state text NOT NULL REFERENCES states (name),
    claimed_by text NOT NULL,
    claim_reason text,
    branch text,
    run_id text,
    session_ref text,
    pr_number integer,
    check_run_id text,
    trace_id text,
    confirmed_commit text,
    tested_commit text,
    runner_commit text,
    last_seq integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, card_key) REFERENCES cards (project_id, card_key) ON DELETE CASCADE
);

/* KTD3: at most one build per card outside the ownership terminal set. */
CREATE UNIQUE INDEX builds_one_owner ON builds (project_id, card_key)
    WHERE state NOT IN ('scored', 'abandoned', 'returned_empty');

/*
 * The second guard on the state machine: even an UPDATE that skips
 * record_transition cannot move a build along an edge that is not allowed.
 */
CREATE FUNCTION builds_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT EXISTS (
        SELECT 1 FROM allowed_edges WHERE from_state = OLD.state AND to_state = NEW.state
    ) THEN
        RAISE EXCEPTION 'build % cannot move from % to %', OLD.id, OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER builds_state_guard BEFORE UPDATE OF state ON builds
    FOR EACH ROW EXECUTE FUNCTION builds_state_guard();

/* One row per builder session, so cost and usage (R15) come from Postgres alone. */
CREATE TABLE attempts (
    id bigserial PRIMARY KEY,
    project_id integer NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    build_id bigint NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
    account text,
    model text,
    effort text,
    cost_usd numeric(12, 6),
    input_tokens bigint,
    output_tokens bigint,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);

/*
 * The history of every build. seq counts per build: 0 is the claim, which is
 * the only row allowed a NULL from_state (a NULL skips the composite foreign
 * key, so the check below pins that row to the claim). Every other row must
 * name an allowed edge.
 */
CREATE TABLE transitions (
    id bigserial PRIMARY KEY,
    project_id integer NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    build_id bigint NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
    seq integer NOT NULL,
    from_state text,
    to_state text NOT NULL REFERENCES states (name),
    actor text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (build_id, seq),
    FOREIGN KEY (from_state, to_state) REFERENCES allowed_edges (from_state, to_state),
    CHECK (from_state IS NOT NULL OR (seq = 0 AND to_state = 'claimed'))
);

CREATE INDEX transitions_project_id ON transitions (project_id, id);

/*
 * Host events from either the webhook endpoint or the poll path (KTD10).
 * normalised is the canonical JSON text of the event, so the two sources
 * write byte equal rows. delivery_id is NULL for a polled event. The second
 * key dedupes an event that carries a merge commit across both sources;
 * events without one compare as distinct under it.
 */
CREATE TABLE host_events (
    id bigserial PRIMARY KEY,
    project_id integer NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    source text NOT NULL CHECK (source IN ('webhook', 'poll')),
    host text NOT NULL,
    delivery_id text,
    repository text NOT NULL,
    pr_number integer,
    kind text NOT NULL,
    merge_commit text,
    head_commit text,
    normalised text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (host, delivery_id),
    UNIQUE (project_id, pr_number, kind, merge_commit)
);

/* The rubric result lives here; the Langfuse score id is only a viewer link. */
CREATE TABLE scores (
    id bigserial PRIMARY KEY,
    project_id integer NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    build_id bigint NOT NULL REFERENCES builds (id) ON DELETE CASCADE,
    kind text NOT NULL,
    result jsonb NOT NULL,
    langfuse_score_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (build_id, kind)
);

/* Runner builds belong to the deployment, not to a project. */
CREATE TABLE runner_builds (
    id bigserial PRIMARY KEY,
    instance text NOT NULL,
    runner_commit text NOT NULL,
    state text NOT NULL CHECK (state IN ('active', 'previous', 'rejected')),
    activated_at timestamptz NOT NULL,
    deactivated_at timestamptz,
    CHECK (deactivated_at IS NULL OR deactivated_at >= activated_at)
);

CREATE UNIQUE INDEX runner_builds_one_active ON runner_builds (instance) WHERE state = 'active';

/* Findings are written already scrubbed by redact (KTD24). */
CREATE TABLE findings (
    id bigserial PRIMARY KEY,
    project_id integer NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    build_id bigint REFERENCES builds (id) ON DELETE CASCADE,
    runner_build_id bigint REFERENCES runner_builds (id),
    class text NOT NULL,
    fingerprint text NOT NULL,
    excerpt text,
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz
);

/*
 * A gate run is attributed to the runner build that was active on its
 * instance when the job started (KTD20), stored in runner_build_id.
 */
CREATE TABLE gate_runs (
    id bigserial PRIMARY KEY,
    project_id integer NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    host text NOT NULL,
    host_run_id text NOT NULL,
    instance text NOT NULL,
    runner_build_id bigint REFERENCES runner_builds (id),
    head_commit text,
    conclusion text,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    UNIQUE (host, host_run_id)
);

/* No foreign key on purpose: an audit row survives its project's deletion. */
CREATE TABLE audit (
    id bigserial PRIMARY KEY,
    project_slug text NOT NULL,
    subject text NOT NULL,
    client_name text,
    tool text NOT NULL,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb,
    at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_project_slug ON audit (project_slug, id);
