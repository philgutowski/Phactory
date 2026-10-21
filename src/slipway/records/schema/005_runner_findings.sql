/*
 * 005: what runner findings need that 001 lacks (U22, KTD20, R38).
 *
 * A finding about the runner itself, such as a runner commit whose build or
 * tests failed, belongs to the deployment, like runner_builds, not to any
 * project, so project_id may be empty for it.
 *
 * runner_commit records the commit a finding is about even when no
 * runner_builds row exists for it (a build that failed never activated), so
 * every finding names its runner commit (R38).
 *
 * gate_run_id ties a finding to the gate run it was observed on, which is
 * how the rollback evaluator asks whether a run carried a runner class
 * finding.
 *
 * related_finding_id points a finding at an earlier one it repeats, such as
 * a second regression on a commit already rolled back.
 *
 * Every column add is nullable, so the file applies to a table with rows.
 */

ALTER TABLE findings ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE findings ADD COLUMN runner_commit text;
ALTER TABLE findings ADD COLUMN gate_run_id bigint REFERENCES gate_runs (id) ON DELETE CASCADE;
ALTER TABLE findings ADD COLUMN related_finding_id bigint REFERENCES findings (id);

CREATE INDEX findings_gate_run ON findings (gate_run_id);
CREATE INDEX findings_runner_commit ON findings (runner_commit, class);
