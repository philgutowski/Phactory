# Hosted coding agent builders as Slipway adapters (2026-10-14)

Method: about 17 searches/fetches. Many vendor docs were not reachable, so several cells are UNVERIFIED. Third party pricing is marked (3P) and conflicts often.

## Conference check
ai.engineer/nyc sponsor list [1]: builders there: Factory (Gold, Droids), Sourcegraph (Gold, Amp), Warp (Gold, Oz), Google DeepMind (Gold, Jules), Blitzy and Anthropic (supporting). Per coordinator, also listed: Entire, Sonar, Qodo, Greptile, Zed. Devin, OpenHands, Codegen absent. Cursor only in past attendees. Also sponsors: Sourcegraph, Warp, Zed, Qodo, Greptile (review/IDE tools, not builders). Anthropic is a supporting partner.

## Per product (API start / returns branch or PR / transcript by API / Bitbucket / parallel / low-volume cost)

1. Claude Managed Agents (Anthropic API, beta) [2][3]
 - API: yes, create agent, environment, session, send user.message events. Repo+branch mounting not confirmed in what I read (UNVERIFIED); you can also give it bash and git yourself.
 - Branch/PR: no native PR output; agent pushes via git/MCP if you give it a token.
 - Transcript: yes. Event history persisted server side and fetchable in full; events include agent.tool_use, agent.tool_result, span.model_request_end with token counts, session.usage with cost [3].
 - Bitbucket: host agnostic (it is a bash sandbox), no native integration.
 - Parallel: create limit 300 req/min, read 1,200/min per org, plus usage tier limits [3]. No fixed session cap stated.
 - Cost: API token rates (pay per use); exact runtime fee not verified.
 - Not eligible for Zero Data Retention [2]. Branding bars calling it "Claude Code" [3].
2. Claude Code on the web (cloud sessions)
 - API: only by firing a saved "routine" with a per-routine bearer token, returns session ID and URL; experimental; no arbitrary-session endpoint found [4]. Needs a claude.ai plan.
 - Others unverified. Verdict below uses Managed Agents or the Agent SDK instead.
3. OpenAI Codex cloud [5][6]
 - API to start cloud tasks: not documented. CLI can start a task and list status [5]. Codex SDK and App Server exist but cloud use unconfirmed.
 - PR: yes in UI. Logs by API: not documented. GitHub only, GitLab beta, no Bitbucket [5][6 (3P)].
 - Cost: plan limits shared local/cloud, or API key token rates; credit billing since April 2026 (3P) [6].
4. GitHub Copilot coding agent
 - Trigger: assign issue to Copilot or @copilot in a PR; GA March 2026 [7 (3P)]. Public REST for it not confirmed; one source says Copilot REST is admin/metrics only [7].
 - Output: PR (native). Transcript API: unverified. Bitbucket: no (GitHub only by design).
 - Cost: premium requests, about $0.04 overage (3P, conflicting) [7].
5. Google Jules [8]
 - API: yes. Create session with sourceContext (repo, starting branch), automationMode AUTO_CREATE_PR; poll GetSession/ListSessions [8].
 - Output: PR with AUTO_CREATE_PR. Transcript: activities listing unverified. Bitbucket: no found (GitHub).
 - Parallel (3P): Free 3 concurrent, Pro $19.99 15 concurrent, Ultra $124.99 60 concurrent [8].
6. Cognition Devin [9][10]
 - API: yes, POST /v3/organizations/{org}/sessions with prompt [10]. Repo/branch fields, PR fields, message retrieval, concurrency not in the page I could read (UNVERIFIED; v3 reference pages and llms.txt hold them).
 - Bitbucket: not confirmed. Cost (3P, conflicting): Pro $20, Max $200, Teams $80 minimum, ACU billed [9]. Old Team-only API claim vs newer includes API: unresolved.
7. Factory Droids [11]
 - API: droid exec headless CLI with FACTORY_API_KEY, autonomy flags --auto low/high; shown running in GitHub Actions [11]. This runs on YOUR runner, not a hosted session API.
 - Branch/PR: you script git. Transcript: not confirmed. Bitbucket: not found. Cost (3P): Pro $20, Plus $100, Max $200 per seat, opt-in extra usage credits [11]. Only conference sponsor in the set.
8. OpenHands Cloud [12][13]
 - API: yes. App-conversations start with initial_message and selected_repository (owner/repo); branch param not documented [12]. V0 endpoint deprecated, use V1.
 - PR output field and event/trajectory retrieval: not documented on that page (UNVERIFIED). Open source, so self host gives full event log.
 - Bitbucket: listed as supported on cloud [13] (only product in set with it confirmed).
 - Parallel: older conversations auto paused past a cap, raise by contacting vendor [12].
 - Cost: pay as you go, LLM at 2x markup, or $20/month Pro with BYO key [13].
9. Cursor cloud agents [14 (3P)]
 - API: yes, REST (api.cursor.com), launch with repo + ref, branchName, autoCreatePr; status includes git.branches and PR URLs; conversation history endpoint exists [14]. Official docs not fetched, so all 3P.
 - GitHub repos and PR URLs only found. Cost: not found. Cursor is not a sponsor.
10. Codegen [15]: standalone service reported closed Jan 9 2026, now enterprise inside ClickUp (3P). NOT YET.
11. Amp: pricing is prepaid credits at provider rates, spun out Dec 2025 (3P) [16]. SDK/API terms and Bitbucket not found. UNVERIFIED.

12. Warp Oz (Gold sponsor) [17]: cloud agent platform with CLI, REST API and SDKs, runs on Warp cloud or a self hosted worker, scheduled or manual start; agents authenticate with an agent API key. Repo/branch input, PR output, transcript API, Bitbucket, parallel cap: UNVERIFIED. Cost (3P): credits; Build $18, Business $45, Max $180 per month. NEXT ADAPTER candidate once docs confirm branch and PR fields.
13. Blitzy (supporting partner) [18]: enterprise autonomous platform, delivers PRs, repos from GitHub, GitLab, Azure DevOps (no Bitbucket found). No public API found; contracts reportedly start at $500K per year (3P). NOT YET, wrong scale for a solo operator.
14. Entire (supporting partner) [19]: NOT a builder. Open source (MIT) CLI that stores each agent session (transcript, prompts, tool calls, token usage) on an entire/checkpoints/v1 git branch, linked by a commit trailer. Supports Claude Code, Gemini CLI, Cursor, Copilot CLI, Factory. Relevant to Slipway as a trace source that travels with the branch, host agnostic. Evaluate as a tracing helper, not an adapter.
15. Sonar (platinum) and Qodo, Greptile (gold): review and verification tools, not builders; candidates for the CI gate or scoring step, outside this brief. Zed: editor, no hosted builder found.

## Verdicts
- Claude Managed Agents (+ Agent SDK): FIRST ADAPTER. Cleanest session API, full event log with per tool call detail and cost, host agnostic so Bitbucket works if Slipway pushes the branch itself.
- OpenHands Cloud: NEXT ADAPTER. Documented start API, Bitbucket confirmed, open source fallback. Gaps: branch param, PR and trace retrieval.
- Jules: NEXT ADAPTER. Documented API with branch and auto PR, 15 concurrent at $20 (3P). GitHub only, thin trace.
- Cursor cloud agents: NEXT ADAPTER if its official docs confirm the 3P account.
- Warp Oz: NEXT ADAPTER (pending doc check). Blitzy: NOT YET. Entire: not a builder, trace helper.
- Devin: NOT YET (docs unverified for branch/PR/transcript/Bitbucket, costly).
- Factory: NOT YET as hosted (it is a CLI you host; fine later as a local adapter). 
- Codex cloud, Copilot coding agent: NOT YET (no confirmed start API, GitHub only).
- Claude Code web: NOT YET (routine fire only, experimental). Codegen: NOT YET. Amp: NOT YET.

## Recommendation (5 lines)
1. Drive Claude Managed Agents first; the fixed handoff maps to an agent system prompt plus a session, and the event history gives Slipway its trace and cost for free.
2. Slipway should own git (clone, branch, push, PR), so Bitbucket and GitHub both work and no builder needs repo integrations.
3. Add OpenHands Cloud second (Bitbucket native, open source), then Jules or Cursor for GitHub only repos.
4. None of these does the whole loop: card in from a tracker, CI gate, verify landing from git and tracker, per task score. The best partial are Copilot (issue to PR) and Devin/Cursor (PR out), all stop at the PR.
5. So the control plane is not redundant for a solo operator running multi vendor parallel builds; it is redundant only if he commits to GitHub plus Copilot alone and accepts no scoring.

## Sources
[1] https://ai.engineer/nyc
[2] https://platform.claude.com/docs/en/managed-agents/overview
[3] https://platform.claude.com/docs/en/managed-agents/reference
[4] https://platform.claude.com/docs/en/api/claude-code/routines-fire
[5] https://learn.chatgpt.com/docs/cloud
[6] https://developers.openai.com/codex/pricing , https://aicodereview.cc/tool/openai-codex , https://www.verdent.ai/guides/codex-pricing-2026
[7] https://www.morphllm.com/copilot-coding-agent , https://www.morphllm.com/copilot-api
[8] https://jules.google/docs/api/reference/ , https://pasqualepillitteri.it/en/news/2163/google-jules-async-ai-coding-agent
[9] https://pensero.ai/blog/devin-pricing , https://omidsaffari.com/blog/devin-pricing
[10] https://docs.devin.ai/api-reference/overview
[11] https://docs.factory.ai/droid-exec/overview.md , https://usagepricing.com/blueprint/factory
[12] https://docs.openhands.dev/openhands/usage/cloud/cloud-api
[13] https://docs.all-hands.dev/openhands/usage/cloud/pro-subscription , https://hub.openhands.dev/pricing
[14] https://docs.port.io/workflows/build-workflows/action-nodes/integration-actions/cursor-cloud-agents/ , https://techdevnotes.com/releases/cursor-docs/20260626-202020Z-6c7064724f5e/content/pages/cloud-agent/api/endpoints.txt
[15] https://toolradar.com/tools/codegen-ai
[16] https://stackpick.net/pricing/amp-sourcegraph/
[17] https://docs.warp.dev/platform/agents/ , https://itsfoss.com/news/warp-launches-oz/ , https://www.everydev.ai/tools/oz
[18] https://sacra.com/c/blitzy , https://sdtimes.com/software-development/blitzy-launches-sandbox-offering-free-autonomous-software-development-for-enterprise-codebases
[19] https://docs.entire.io/cli/checkpoints , https://entire.io/home.md
