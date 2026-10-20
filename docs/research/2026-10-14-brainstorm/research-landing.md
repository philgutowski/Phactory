# Research: merge queues, landing, tracker automation (2026-10-14)

Method: 11 searches/fetches. Pricing figures from third parties are marked (3p) and need checking on vendor pages. Source numbers in [].

## Conference sponsors (coordinator list, ai.engineer/nyc) [1]
No merge queue vendor sponsors. Gate and review sponsors: CloudBees, Sonar (talk on agentic CI verification and remediation loops), Greptile, Qodo, Socket, LaunchDarkly, Keycard.
- These are gate or review stages that feed a required check on the PR or merge_group; none lands code. Verdict for all: ADAPTER, plug in as a required check, not as the lander.
- Greptile: own docs describe Bitbucket Cloud via Forge app (a 3p comparison says no Bitbucket, docs win) [14]. Qodo, CloudBees, Sonar, Socket, Keycard: product claims not verified here (one search returned nothing). Check each before depending on it.
- Keycard (agent identity) could matter for "one pusher per repo" credentials; unverified.

## Per product

GitHub merge queue + rulesets [2][3]
- Serialised, gate on merge result: YES. Checks run on a temporary merge_group branch with the PR applied to latest base plus PRs ahead in queue [3]. Workflows must trigger on merge_group or the queue stalls [2].
- Exact tested commit: partly unverified. Docs say the group branch sha differs from the PR sha and do not state that the tested sha is what lands [3]. Test this before trusting it.
- Auto merge on green, human option per repo: YES via ruleset (require approvals, merge queue) per repo [2]. Which plans get queue: docs page did not say, check.
- Bitbucket: no, GitHub only.
- Tracker move: not by itself; Projects built-in workflow sets PR item to Done on merge, linked issues need the "Item closed" workflow (3p) [8].
- Add-to-queue API: not documented on that page [3]. Price: included with GitHub plan (plan limits unconfirmed).
- Verdict: USE IT (GitHub repos).

Bitbucket Cloud merge queues + merge checks + Pipelines [4]
- Serialised, gate on merge result: YES. Creates a temporary restricted branch with one prepared merge commit, runs the pipeline under a `merge-queues` selector, auto merges on pass, removes on fail [4]. Order kept even with parallel validation.
- Auto merge vs human: merge checks (approvals) must pass before "Add to merge queue" [4]; queue then merges automatically. Per repo setting. Whether the queue merges the exact validated commit is implied (prepared commit) but not stated.
- Bitbucket only (it is the native one). Standard or Premium workspace with Pipelines.
- Tracker: Bitbucket merge dialog can transition Jira issues; Jira automation has a Pull request merged trigger [5][6].
- Price: no seat fee; uses Pipelines minutes [4].
- Verdict: USE IT (Bitbucket repos).

Mergify [7]
- Merge queue with speculative checks, GitHub focused. Bitbucket: sources conflict, official pricing page does not list it, assume NO.
- Price: free up to 5 users on private repos, Max $21/seat/month [7]. At one person scale likely free.
- Adds over GitHub native: rules engine, conditions, auto merge. Verdict: NOT FOR THIS (duplicates native queue; GitHub only).

Graphite merge queue [9]
- Stack aware queue, GitHub only, no Bitbucket evidence. Price disagrees across 3p: $18 to $40 per user per month; advanced queue features reported Enterprise only. Stacking is not Slipway's model (one PR per card).
- Verdict: NOT FOR THIS.

Aviator MergeQueue [10]
- Speculative parallel batches, GitHub only per sources, no Bitbucket evidence. Price (3p): free under 15 devs, about $12 per active dev.
- Verdict: NOT FOR THIS (no Bitbucket; GitHub native queue covers one person scale).

Trunk merge [11]
- Works with any CI but requires GitHub hosting [11]. Price not found. Verdict: NOT FOR THIS.

Jira Automation + Rovo [5][6][12]
- Automation rule: trigger "Pull request merged" (works with Bitbucket, GitHub, GitLab), action transition issue, no custom code [5][6]. Needs issue key in PR title, branch or commit or the rule does not fire (3p) [5]. No built in PR approved trigger; webhook workaround [5].
- Not a landing tool. Rovo agents: credit metered, 10 credits per agent request, Standard has pooled 25 credits per user per month, Standard Rovo tier was in beta (3p) [12]. Rules are deterministic, so skip Rovo for the move step.
- Price: Jira Free to 10 users, Standard about $7.91 per user (3p) [12]; extra automation steps about $0.50 per 1000 (3p).
- Verdict: USE IT for the card move (rule), NOT FOR THIS for Rovo.

GitHub Projects automation [8]
- Built in workflow sets item Done on PR merge or issue close, no code. Free. Verdict: USE IT if a repo's tracker is GitHub Projects.

Linear [13]
- GitHub integration moves linked issues on PR open and merge per team setting, closing keywords work [13]. No agent evidence checked beyond that. GitHub only for the PR link. Verdict: USE IT where Linear is the tracker.

## Gaps not covered
GitHub queue plan limits and add-to-queue API (GraphQL enqueuePullRequest is not verified here). Whether either native queue merges the exact tested sha. Bitbucket queue API/automation to enqueue.

## Recommendation (5 lines)
1. The "serialise, test the merge result, merge the verified commit, auto on green" core is a native queue on each host: GitHub merge queue and Bitbucket Cloud merge queues. Do not build a queue; configure one per repo.
2. Card moved on merge is a stock Jira rule (Pull request merged) or GitHub Projects/Linear workflow, no code. Enforce issue key in PR title at push time.
3. Slipway still builds: verify landed from both sides (git shows exact sha on main AND tracker status), since rules fail silently on a missing key.
4. Still to build: the cross-builder lease and one-pusher rule before PRs reach the host queue (native queues serialise per branch, not per builder), plus the gate-on-production-like machine (queue calls your CI) and the needs-you queue (human approval waits, red gates, unfired rules).
5. Check first: whether the native queues merge the exact tested sha and have an enqueue API, since Slipway's "never re-merge" rule depends on it. Skip Mergify, Graphite, Aviator, Trunk: GitHub only, and no sponsor overlap.

## Sources
[1] https://ai.engineer/nyc
[2] https://humanwhocodes.com/blog/2026/04/improving-developer-velocity-github-merge-queue
[3] https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue
[4] https://www.atlassian.com/blog/bitbucket/unlocking-efficiency-with-merge-queues-in-bitbucket-cloud-now-ga ; https://support.atlassian.com/bitbucket-cloud/docs/manage-pull-requests-with-merge-queues/
[5] https://support.atlassian.com/jira/kb/transition-jira-issue-when-bitbucket-pr-is-approved/
[6] https://www.atlassian.com/software/jira/automation-template-library/bitbucket-github-gitlab
[7] https://mergify.com/pricing
[8] https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations
[9] https://graphite.dev/docs/get-started-merge-queue ; https://mergify.com/compare/graphite
[10] https://docs.aviator.co/mergequeue/concepts/queue-modes ; https://aviator.co/pricing
[11] https://docs.trunk.io/merge-queue/merge-queue
[12] https://www.atlassian.com/licensing/usage-based-pricing ; https://www.eesel.ai/blog/atlassian-intelligence-and-rovo-pricing-explained
[14] https://www.greptile.com/docs/integrations/code-providers
[13] https://linear.app/features/progress ; https://linear.app/docs/gitlab
