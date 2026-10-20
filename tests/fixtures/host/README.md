# Host fixtures: documented shapes, not recorded deliveries

Every file here was written by hand from the hosts' published payload and
API documentation, because U2's steps 4 to 6 (the live captures) are still
open. None is a recorded delivery, and no signature header travels with
them: the tests sign each body themselves with an invented secret.

- `github_pull_request_*.json`, `github_merge_group_checks_requested.json`,
  `github_check_run_completed.json`: GitHub webhook payloads, after the
  shapes in GitHub's webhook events reference and the octokit example
  payloads named in `docs/research/2026-10-14-plan/framework-docs-apis.md`
  Part C. The merge group `head_ref` follows GitHub's documented temporary
  branch name, `gh-readonly-queue/<base>/pr-<number>-<head sha>`.
- `bitbucket_pullrequest_*.json`, `bitbucket_commit_status_*.json`: Bitbucket
  Cloud event payloads, after Atlassian's event payloads page. The queue's
  temporary ref name follows the `bitbucket-merge-queue-*` prefix the merge
  queue documentation names.
- `github_api_*.json`, `bitbucket_api_*.json`: REST and GraphQL answers the
  adapters read, after each host's API reference. `github_api_rules_*.json`
  are the rules GitHub computes for a branch from its active rulesets;
  `github_api_rules_no_queue.json` mirrors what `docs/spikes/merge-paths.md`
  recorded for a user owned repository after `ci` became required.

Every name, hash, and id is invented. When U2 records live deliveries, a
recorded file replaces its documented twin under the same name and this
note shrinks.
