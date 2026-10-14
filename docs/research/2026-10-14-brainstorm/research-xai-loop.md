# Research: does the xAI / SpaceX / Cursor family make Phactory redundant?
Date 2026-10-14. ~15 searches/fetches. P = PRIMARY (vendor docs), T = THIRD PARTY, U = UNVERIFIED.
Context: SpaceX merged xAI (Feb 2026, now "SpaceXAI") and closed the Cursor (Anysphere) purchase 2026-08-14 [T: 9, 10]. So one vendor family now owns Cursor agents, Grok Build, Grok Bot, and Origin (code host). Everything below is single-family; none is cross-vendor.

Stage key: card, dispatch, build, return, land, eval, trace, page, manager

## 1. Cursor Cloud Agents (API v1)
- dispatch/build: POST /v1/agents takes prompt + repos[].url + startingRef; pushes to a new cursor/... branch; repos may be GitHub, GitLab, Bitbucket Cloud, Azure DevOps [P: 1]. Can attach to an existing PR (prUrl).
- card: triggers via @cursor in Slack, Linear, GitHub, Bitbucket PR comment; API; web/iOS [P: 2]. Jira not mentioned (U).
- return: run result field (final reply), SSE stream per run, artifacts, per-run token usage [P: 1]. Transcript only via built-in "Cursor Cloud MCP" diagnostics [P: 2]; no transcript endpoint in the API page. v1 webhooks "coming soon" [P: 1].
- parallel: one active run per agent (409 agent_busy); many agents allowed; no account-wide cap documented (U) [P: 1].
- pricing: model API rates, spend limit, paid plan required [P: 2].
- NOT covered: fixed handoff contract (rules, sibling-edit files, done line), merge queue entry, git/tracker landing verification, eval/rubric scoring, Langfuse export (U), cross-builder page, needs-you queue.
- Verdict: ADAPTER (Cursor is a second builder behind Phactory's handoff; Bitbucket Cloud push makes it useful).

## 2. Cursor Automations
- card: cron, webhook POST, GitHub/GitLab/Bitbucket Cloud events, Slack, Linear (created, status changed), Sentry, PagerDuty; runs cloud agents; service-account billing [P: 3]. Jira: not mentioned. Concurrency: not mentioned.
- Covers card + dispatch inside Cursor only. Verdict: REPLACES A STAGE (card intake/dispatch for Cursor-only work); not for Phactory's tracker-agnostic handoff.

## 3. Cursor Bugbot
- PR review on GitHub, GitLab, Bitbucket (incl. Data Center), Azure DevOps, Origin; usage billing; Autofix launches a Cloud Agent, Bitbucket supports commit-to-existing-branch only [P: 4].
- Covers a review step after return. It is a reviewer, not a deterministic eval or cross-builder score. Verdict: ADAPTER-ish / optional review signal Phactory can read. Not a stage replacement.

## 4. Grok Build (CLI)
- build: terminal coding agent, plan mode, subagents; headless `grok -p` with JSON/stream-JSON, ACP, `--permission-mode dontAsk`, strict sandbox, XAI_API_KEY for CI [P: 5, 6]. Reported worktree subagents, up to 8 (T: 11).
- Scheduled prompts: /loop, 50 tasks, 7-day expiry, only while a session is alive (P: 5 search snippet; hosted runtime not documented).
- git host / tracker / PR / merge queue / eval / trace export: not on overview page [P: 7] (U). Enterprise page names "telemetry/OpenTelemetry, hosted/cloud agents" as features but gives no detail [P: 8] (U, worth a follow-up fetch).
- Verdict: ADAPTER. A headless CLI Phactory can run in its own sandbox, like any builder. This matches the existing board-lanes use of Grok agents.

## 5. Grok Bot (SpaceXAI, runs on Cursor plans)
- Persistent cloud computer per user, multiple Bots in parallel, work continues with laptop closed; routines (50 per Bot, 20 run records kept), triggers from Slack/GitHub events; no API documented [P: 9b, 10b]. Coding agents, Linear, Jira, Bitbucket: not covered.
- Covers personal-assistant "manager agent" territory loosely. Verdict: NOT FOR THIS (no API, so not drivable by Phactory; not a loop).

## 6. xAI API: Remote MCP tool
- Responses API tool type "mcp": Grok calls your MCP server over streaming HTTP/SSE; allowed_tools, headers; require_approval unsupported [P: 11b]. This is a tool-calling feature of a model, not an agent runtime.
- Search found no xAI managed agent runtime, hosted task API, or "Grok Code" agent beyond Grok Build [searches 3, 5]. Verdict: NOT FOR THIS, except that a Phactory manager agent could use Grok models with MCP (surface, not a stage).

## 7. Origin (Cursor/SpaceX code host, beta, launched 2026-08-17)
- Mirrors GitHub repos, pushes still go to GitHub; PRs, checks, merge function, agent-assisted conflict and CI-fix claimed [T: 12]. Merge queue: not found (U). Cursor has published no Origin data-retention or export terms [T: 12].
- Verdict: NOT FOR THIS now. Watch: it could become a native merge queue host the family controls.

## 8. Anything else from SpaceX
- No SpaceX-branded agent product found beyond the above (U).

## Coverage matrix (P = partial, Y = yes, - = none)
| Product | card | dispatch | build | return | land | eval | trace | page | manager |
| Cursor Cloud API | - | Y | Y | P | P (branch/PR only) | - | P (stream, usage) | P (cursor.com/agents) | - |
| Cursor Automations | Y | Y | Y | P | - | - | - | P | - |
| Bugbot | - | - | - | - | - | P (review) | - | - | - |
| Grok Build | - | P | Y | P | - | - | U | - | - |
| Grok Bot | P | P | U | - | - | - | - | P | P |
| xAI remote MCP | - | - | - | - | - | - | - | - | - |
None verifies landing from git+tracker, scores with eval+rubric, runs more than one vendor, or records per-step actors. Every product trusts its own agent's finish.

## Verdict
INVALIDATES PHACTORY: no. No product does card in to verified merge out, scored, across vendors. The family is single-vendor by design and its incentive is lock-in.
REPLACES A STAGE: Cursor Automations replaces card intake plus dispatch for Cursor-only, GitHub/Linear/Bitbucket work. Bugbot overlaps an optional review signal.
ADAPTER: Cursor Cloud Agents (Bitbucket Cloud ready), Grok Build headless.
NOT FOR THIS: Grok Bot, xAI remote MCP, Origin (today).

## Recommendation (5 lines)
1. Proceed; the cross-vendor verify, eval and compare layer is the gap nobody in this family fills.
2. Build the Cursor Cloud adapter second: POST /v1/agents with repos[].url and startingRef, poll the run, read the pushed cursor/ branch from git, not the reply.
3. Treat Grok Build `grok -p` as a third adapter inside Phactory's own sandbox; fetch the enterprise telemetry/hosted-agents detail before committing.
4. Do not build intake that duplicates Automations for Cursor-only users; keep Phactory's card reader tracker-native and the fixed handoff as the contract.
5. Re-check in 60 days: Cursor v1 webhooks, a transcript endpoint, Origin merge queue, any Grok hosted runtime. Any of them could narrow the adapter work, not the verify/eval value.

## Sources
1 [P] https://cursor.com/docs/cloud-agent/api/endpoints
2 [P] https://cursor.com/docs/cloud-agent
3 [P] https://cursor.com/docs/cloud-agent/automations
4 [P] https://cursor.com/docs/bugbot
5 [P, via search snippets] https://docs.x.ai/build/features/background-tasks , https://docs.x.ai/build/modes-and-commands
6 [T] https://mer.vin/2026/05/grok-build-cli-xai-terminal-coding-agent-with-plan-mode-subagents-and-headless-ci/
7 [P] https://docs.x.ai/build/overview
8 [P] https://docs.x.ai/build/enterprise
9 [T] https://www.techzine.eu/news/devops/142197/spacex-acquires-cursor-for-60-billion/
9b [P] https://docs.x.ai/grok-bot
10 [T] https://www.crowdfundinsider.com/?p=297180
10b [P] https://docs.x.ai/grok-bot/skills-routines-and-automations
11 [T] https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026
11b [P] https://docs.x.ai/developers/tools/remote-mcp
12 [T] https://thenewstack.io/cursor-origin-github-alternative/
