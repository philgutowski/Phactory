# Research: manager agents, multi agent coordination, operator surfaces (2026-10-14)

Method: ~19 searches/fetches. Search summaries are secondary; items marked (unverified) were not confirmed on a vendor page.

## Who gave the "10 humans, 10 manager agents, merge manager agent" talk
NOT FOUND. ai.engineer/nyc lists no talk on manager or merge manager agents (full titles "published closer to the event").
Searches on the phrases returned nothing. It may be a side event or a sponsor booth slide. Candidates by theme only:
Blitzy (merge and PR review focus), Factory, Warp, Kepler/GitKraken (backlog to merged PR), CrewAI (side event host). Do not cite any as the speaker.
Best next step: ask the person who saw the slide for the badge or booth, or check the AIE YouTube uploads.

## Sponsor lineup relevant to this area (ai.engineer/nyc)
Gold: Factory, Warp, Daytona, Keycard, LiveKit, RunLayer, Temporal, Sourcegraph. Silver: Pydantic, Sentry, Cloudflare. Supporting: Anthropic, Blitzy, Entire, Jellyfish, Compound AI, Kepler. Speakers include Linear.

## Products
1. Factory (Droids). Agentic dev platform, many models (GPT, Claude, Gemini) per FAQ; Pro $20/mo, Plus $100, Max $200, Teams $60 + $40/seat; CLI/Desktop/SDK listed. No mobile or approval inbox on pricing page; headless API not described there. Solo: yes. Vendor span: models, not other vendors' coding agents. VERDICT: NOT FOR THIS (competes with Phactory builders; could be one builder behind an ADAPTER).
2. Warp Oz / Factories. Cloud agent platform, "fleets of coding agents across your SDLC", one view of all running agents, join sessions to audit, triggers from @warp or events, runs on Warp or own infra. Runs Claude Code, Codex, Warp agent. API docs linked, not described; no mobile or approvals on page; pricing not on page. Solo: yes (terminal downloadable). VERDICT: ADAPTER (a builder backend; not an operator queue).
3. Paperclip. Open source (MIT), self-hosted Node server and React UI, agents in an org chart with goals, heartbeats, per agent budgets, audit log and approvals. Runtime agnostic (Claude Code, Codex, Cursor, bash, HTTP agents). Maturity unclear, star counts conflict, multi-user and cloud still planned. Free. Solo: yes. VERDICT: ADAPTER (closest to a manager layer product; evaluate its approvals and API before building your own).
4. Claude Code agent teams. Experimental, off by default; a lead session assigns work, teammates have own contexts, shared task list and peer messaging. Claude only, one machine. Included with Claude plans. No tracker or merge path. VERDICT: NOT FOR THIS (intra build coordination; useful inside one builder).
5. Claude Code Remote Control (phone). Approve or deny permission prompts from Claude mobile app or claude.ai/code during a session; needs Pro/Max; a phone approval bug was reported and closed June 2026 (third party sources, official page not read). Claude only, per session, not a queue across builds. VERDICT: ADAPTER for the "release from phone" need at session level only.
6. HumanLayer (now a "multiplayer coding agent IDE and cloud"). Parallel sessions on daemons, worktrees, Claude Code/Codex/Copilot, HumanLayer API syncs sessions across devices, mobile client listed. Starter free (3 members, 200 sessions/mo), Pro $100/user/mo. The old SDK `@require_approval` routes approvals via Slack/email (older third party pricing conflicts). No formal approval inbox on page. Solo: yes. VERDICT: ADAPTER (closest "sessions plus phone" product, but it is a workspace for humans, not a verb API for a manager agent).
7. gotoHuman. Approval layer with review templates and a central inbox, webhooks, APIs and SDKs (third party directory text). Pricing not confirmed. Solo: probably yes. VERDICT: ADAPTER (cheapest route to an inbox with approve/edit/retry; verify mobile web and API on its own site).
8. Linear agents. Agent API (Developer Preview): issues are delegated to apps, agent sessions have states (working, waiting for input, errored); free to build, agents not billable seats. Coding agents already use it (Replicas, Warp, Cursor). Tracker is vendor neutral for agents. No approval inbox beyond "waiting for input". Seat price unconfirmed (~$8-10 Standard). Solo: yes. VERDICT: ADAPTER (a tracker side delegation surface; not a merge path or scorer).
9. Kepler (GitKraken, supporting partner). Backlog issue to agents in worktrees to merged PR across repos; agents via Agent Client Protocol: Claude Code, Codex, Cursor, Gemini, Copilot, OpenCode; autonomy modes; free in public preview; desktop app. Closest product to Phactory's dispatch loop. No manager agent API or phone inbox confirmed. VERDICT: ADAPTER or competitor; read it closely before building dispatch.
10. Blitzy (supporting). Enterprise autonomous SDLC; PR Review with checkpoints at planning, generation, merge. Enterprise sales. VERDICT: NOT FOR THIS.
11. Entire (supporting, Dohmke). Open source Checkpoints CLI records agent sessions on a git branch (Claude Code, Gemini CLI at launch); larger git-compatible platform planned; web UI unfinished as of Feb 2026. VERDICT: ADAPTER (provenance for score evidence; not coordination).
12. Jellyfish (supporting). Engineering analytics for AI impact across agents, enterprise. VERDICT: NOT FOR THIS.
13. Sim (gold). Open source (Apache 2.0) visual agent workflow builder; free self host, hosted Pro $25/mo, Max $100/mo; workflow API. VERDICT: NOT FOR THIS (general workflow builder, not coding builds).
14. CrewAI. Framework with hierarchical process: manager agent delegates and reviews. Library, free; business automation leaning. VERDICT: NOT FOR THIS (a library for building a manager, not a manager product).
15. Gas Town (Yegge) and Beads. Open source: coordinator plus workers plus "Refinery" merge queue agent, 20-30 parallel agents; closest published pattern to "merge manager agent" (blog summary). VERDICT: ADAPTER as a pattern reference, risky as dependency.
16. Conductor (Melty), Codeg: parallel Claude/Codex in worktrees with diff review; Codeg has lead agent and approval before landing. Per human desktop tools. VERDICT: NOT FOR THIS.
17. Not covered (budget): OpenAI Agents SDK, LangGraph Platform, Atlassian Rovo, Slack agents, Pydantic AI, LiveKit, Keycard, Compound AI (no company found under that name), Temporal. By category: SDKs and frameworks to build a manager on, not managers over coding agents. LiveKit is the voice front end option; Temporal is the durable workflow option for "waiting for human" states.

## Cross cutting findings
- No source found for a standard "one manager agent per human, cross human shared facts" product. Best match is Gas Town style roles. Cross manager shared facts (what is in flight, files being edited) appear only in research (AgentRoom file claims, CoAgent, ATM papers, arXiv 2026), not product.
- Parallel tools stop at worktree isolation; none confirmed to expose file claims to other humans' agents.

## Recommendation (5 lines)
1. A manager agent layer does not exist as a buyable product for coding agents; closest are Paperclip (open source) and Gas Town (pattern), so build a thin one on Claude Code or an SDK.
2. An operator page with phone approvals partly exists: gotoHuman (inbox), HumanLayer (mobile sessions), Claude Remote Control (per session). None shows "4 building, 2 waiting, 1 needs you" across builders; verify gotoHuman first.
3. Kepler and Warp Oz overlap Phactory's dispatch; test both before writing a dispatcher, and use Linear as the card source.
4. Phactory still owns: the verb API manager agents call, the single trusted merge path, scoring, the cross human in-flight facts, and the aggregated operator summary.
5. Biggest risk is the unidentified talk's vendor; find them before locking scope.

## Sources
https://ai.engineer/nyc | https://factory.com/pricing | https://www.warp.dev/oz | https://github.com/paperclipai/paperclip | https://zenml.io/llmops-database/open-source-agent-orchestration-platform-for-multi-agent-business-automation | https://code.claude.com/docs/en/agent-teams | https://thepromptshelf.dev/blog/claude-code-remote-control-complete-guide-2026 | https://humanlayer.dev | https://enterprisedna.co/directories/open-source/gotohuman | https://linear.app/developers/agents | https://linear.app/docs/agents-in-linear | https://gitkraken.com/kepler | https://www.augmentcode.com/tools/open-source-agent-orchestrators | https://blitzy.com/ | https://rcpmag.com/articles/2026/02/12/ex-github-ceo-thomas-dohmke-unveils-entire.aspx | https://docs.sim.ai/ | https://jellyfish.co/newsroom/ai-coding-agents-are-infiltrating-the-corporate-world-here-are-the-top-tools/ | https://www.ibm.com/think/topics/crew-ai | https://getunblocked.com/blog/ai-agent-orchestration-platforms-compared/
