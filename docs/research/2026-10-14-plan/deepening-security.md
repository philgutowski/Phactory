# Deepening review: security (2026-10-14)

Reviewer: security pass over the first version plan, dispatched by the planning session after the initial write. Scope: KTD6, KTD9, KTD10, KTD12, KTD13, KTD14, KTD20, System-Wide Impact, Risks, units U9, U13, U18, U19, U23. Disposition of every finding: accepted and integrated on 2026-10-14 (KTD6, KTD10, KTD12, KTD13, KTD18, KTD20, KTD24, KTD25, AE12, U2, U3, U5, U13, U16, U19, U22, U23). Finding 4's review requirement narrows Phillip's auto merge decision on the KTD25 paths only; recorded as a conflict call-out on the Key Decision.

1. The deterministic score runs the customer's test command on the control plane host. Severity: Critical.
Path: U16 step 3 said the named test "runs" check executes the project's test command in the verify clone under PHACTORY_HOME. That clone contains the diff a builder just wrote plus the customer's repository, so a prompt injected builder, a malicious dependency, or a customer conftest runs arbitrary code inside the Phactory worker, which holds the operator file, the Postgres DSN, the Hatchet connection, and on compose the Docker socket. Over MCP it is worse: onboard accepts the gate and test command as free text from a manager agent, so an agent can set the command to anything and U16 runs it on the VPS at the next landing.
Change: the "runs" item reads the gate's own log for the named test; any execution of project code happens only inside a builder container; onboard stores gate and test commands and never executes them.

2. Neither host can scope a write credential to refs/heads/phactory/*. Severity: High.
Evidence: a GitHub fine grained PAT and a deploy key grant Contents write for the whole repository; there is no ref level permission. A fine grained PAT can only be scoped to repositories the holder owns or belongs to, so a customer's repository needs a GitHub App installation token or a classic PAT. On Bitbucket Cloud, scopes are per repository, not per branch, and app passwords are on Atlassian's retirement schedule; repository access tokens are the current form. A prompt injected builder with the credential pushes to main, force pushes another in flight branch after Phactory opened its pull request, or exfiltrates the repository.
Change: repository wide machine identity, branch namespace enforced by a host ruleset or branch permissions that exclude the identity, a pull_request synchronize webhook posting a failing check when the head moves after confirmation, verify comparing the merged commit to the confirmed commit.

3. The CLI builder container holds a one year subscription token and a shared per account config volume while running customer code. Severity: High.
Change: no shared volume; empty configuration directory per container; token by environment only; transcript copied out before removal; non root, no Docker socket, read only root, isolated network, credential through a helper and never in the remote URL.

4. A pull request chooses its runner, and on Phactory's own repository it merges itself. Severity: High.
Path: on a pull_request event the workflow file in the pull request is what runs, so a PR can set runs-on to an instance's labels; the runner's v0 supports public repositories only, which is the unsafe case for self hosted runners; the laptop instance as a launchd job under Phillip's user exposes his home. Phactory's own repository auto merges with a required check and no review, so a steered cloud session lands code that compose deploys to the host holding every customer credential.
Change: repository scoped registration, fork approval, dedicated user on the laptop serving only listed repositories, VPS instance in a container without the Docker socket on the isolated network, one review through CODEOWNERS on the gate, deploy, and credential paths.

5. With an external authorization server, audience is checked but subject is not. Severity: High.
Change: sign up closed; the verifier requires the OAuth subject to be in the project file's allowed subjects; issuer pinned; a token with no audience rejected; U3 tests those cases; the registered client name is never an identity decision.

6. Transcripts, traces, and audit rows carry secrets and customer code into places the plan treated as safe. Severity: High.
Change: one redaction module applied before transcript push, trace export, audit write, and evidence record; refused answers stored as reason, hash, length; validation before save; evidence json structured; the Integrel permission question for transcript persistence at Anthropic and diffs at Langfuse cloud recorded under Outstanding Questions with a per project trace detail switch.

7. A host event alone moves card state, with no delivery dedupe, one shared secret, and no rotation rule. Severity: Medium.
Change: an event is a wake up, never evidence; every event driven transition re reads the pull request; dedupe on the host's delivery id in a host_events table; one secret per repository with a rotation pair; constant time compare; body cap.

8. The filing agent's token cannot be scoped the way the plan assumed, and its input is attacker chosen text. Severity: Medium.
Change: a GitHub App the runner's owner installs with Issues scope only; two templated tools with no free text arguments; dedupe over open and thirty days of closed issues; a cap per runner commit per day; classification prefers structured fields over log regex.

Checked and found sound: approval and merge stay in the host with no tool argument for either; project from the token; not found over forbidden; dispatch idempotent with a per day cap; raw body signature verification before parsing; the judge never seeing the transcript; Managed Agents with no web tools and limited networking; one Langfuse project per Phactory project; the handoff carrying credential names only; project file validation refusing secret shaped values.
