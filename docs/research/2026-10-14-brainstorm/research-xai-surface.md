# Research: Grok, grokbot, Cursor, Claude as the manager agent surface (2026-10-14)

Labels: PRIMARY (vendor docs or vendor announcement), THIRD PARTY (blogs, reviews, vendors), UNVERIFIED (no source found).
About 15 lookups. The WebFetch tool returns summaries, not raw pages, so quotes are paraphrase level.

## 1. Grok voice mode and custom remote MCP connectors on mobile

- Connectors are live on Grok iOS and Android (Settings, Connectors), and include "Bring Your Own MCP: connect any custom MCP server". Launched 2026-05-06. PRIMARY [S1]
- A "Voice connector" merging Voice Mode and Connectors went live on iOS, Android, web on 2026-08-10, no setting needed. It cites an @grok post. The article talks only about built in apps (email, calendar). It does not mention custom MCP. THIRD PARTY [S2]
- The connector docs say Grok detects the tools your server exposes and uses them in conversations. The page does not cover voice, mobile, Tasks, Projects, or DCR. PRIMARY [S3]
- Custom MCP in voice mode specifically: no source confirms or denies it. UNVERIFIED.
- The undated "connectors do not work in mobile apps or voice mode" review: I could not find it. The nearest candidate (MyClaw, dated 2026-08-12) makes no such claim; it says only that some capabilities vary by platform. THIRD PARTY [S4]. Verdict: the claim is unsupported, and it is contradicted for mobile by [S1] and for built in connectors in voice by [S2]. Custom connectors in voice remain untested.
- Server must be reachable on the public internet. Custom connectors may need a paid tier (a 2026-07-15 blog says so; the docs say connectors are open to all users, a conflict). [S3] PRIMARY, [S5] THIRD PARTY
- Auth: a GitHub issue dated 2026-09-06 reportedly says Grok only does OAuth with PKCE plus dynamic client registration, and one sign in failed at registration over a callback address. That matches Phactory's design but shows a failure mode. Seen only in a search summary, link not retrieved. THIRD PARTY, UNVERIFIED

## 2. Per project or per customer separation in Grok

- No official source on Projects, workspaces, or per project memory found. UNVERIFIED
- A review (anuma.ai, undated) says Grok has no project based memory separation and memory is inconsistent across web, iOS, Android. THIRD PARTY [S6]
- Another review says the mobile apps lack project support (search summary only). THIRD PARTY, UNVERIFIED
- Connectors are account wide per [S1] and [S3], so one bot seeing one customer cannot come from connector scoping. Isolation would have to live in the Phactory side (one token or URL per customer) or in separate bots.

## 3. Screenshots in the same conversation as voice

- Grok consumer app: no source. UNVERIFIED
- xAI Voice Agent API modalities are "Text, Audio to Text, Audio". No image input. PRIMARY [S7]

## 4. xAI Voice Agent API

- Capabilities include remote MCP tools. PRIMARY [S7]
- Price: grok-voice-3.0 at $0.08 per minute ($4.80 per hour) plus $0.004 per text input item. PRIMARY [S8]. An older third party snippet said $0.05 per minute; treat as stale.
- Max session 120 minutes (a April 2026 third party guide said 30). function_call_output items are not billed. A separate MCP invocation charge was not found. PRIMARY [S7], UNVERIFIED for MCP surcharge
- You would build and host the client app, memory, and per customer separation yourself.

## 5. Grok Tasks and scheduled runs acting on a connector

- Reports say Tasks is folding into Automations, announced 2026-07-16, on iOS, Android, web, with schedules (for example weekdays 8:00). Email triggers are SuperGrok only. THIRD PARTY [S9]
- One source says Automations is the exception to connectors answering only when asked. Whether it can call a custom MCP connector: UNVERIFIED.

## 6. Cursor as an always available phone surface

- Cursor web and mobile agents: start agents from a phone at cursor.com/agents, installable as a PWA. Launched mid 2025. PRIMARY (docs seen via search; fetch redirected) [S10]
- Slack: tag @Cursor to assign tasks to background agents; completion notices go to Slack. PRIMARY/THIRD PARTY [S10], [S11]
- These agents work on code repositories and open PRs. They are not a standing per customer conversational manager. Voice input and custom OAuth MCP in these agents: UNVERIFIED. Good for dispatching build work, weak as the manager surface.

## 7. Claude app comparison on the same five points

- Voice and connectors: "In voice mode, Claude can use the tools you've connected" (Gmail, Calendar, Docs, Slack named), voice available on mobile. MCP not named on that page. PRIMARY [S12]
- Custom remote MCP with OAuth in the Claude app: not fetched this run. UNVERIFIED here.
- Per project separation: Claude Projects exist, but the voice page does not say they apply in voice. UNVERIFIED here.
- Screenshots with voice: the voice page does not say. UNVERIFIED
- Scheduled runs on a connector: not checked. UNVERIFIED
- Net: Claude has a primary source tying connectors to mobile voice. Grok has only a third party source, and only for built in apps.

## Recommendation

1. Best surface today by evidence: the Claude app, because it is the only one with a primary source for connector tools in mobile voice. Grok is plausible but unproven for custom MCP in voice.
2. Cursor is a dispatch surface for coding agents, not the manager. Keep it out of this role.
3. The xAI Voice Agent API ($0.08 per minute, remote MCP, no images) is the fallback if consumer apps fail, at the cost of building your own client and memory.
4. Spike (about 2 hours): register a stub Phactory MCP with OAuth PKCE plus DCR on both Claude and Grok (grok.com/connectors). On a phone, in voice, call a read tool, then attach a screenshot mid conversation, then add a second customer stub and check one bot cannot see the other.
5. Decide on the three results: tool call in voice works, screenshot works, isolation works. Pick the app that passes all three, else the API fallback.

## Sources
- S1 https://x.ai/news/grok-connectors (PRIMARY)
- S2 https://www.basenor.com/blogs/news/grok-voice-connector-5-details-that-matter (THIRD PARTY)
- S3 https://docs.x.ai/grok/connectors (PRIMARY)
- S4 https://myclaw.ai/ar/blog/grok-bot-reviews (THIRD PARTY)
- S5 https://www.context-link.ai/blog/grok-custom-connector (THIRD PARTY)
- S6 https://www.anuma.ai/blog/grok-memory (THIRD PARTY, seen in search only)
- S7 https://docs.x.ai/developers/models/voice-agent-api (PRIMARY)
- S8 https://docs.x.ai/developers/models (PRIMARY)
- S9 https://blockchain.news/news/grok-automations-scheduled-tasks and https://www.testingcatalog.com/xai-is-working-on-automations-feature-for-grok.md (THIRD PARTY)
- S10 https://docs.cursor.com/background-agent/web-and-mobile (PRIMARY, search only)
- S11 https://cursor.com/blog/agent-web (PRIMARY, search only)
- S12 https://support.claude.com/en/articles/11101966-use-voice-mode (PRIMARY)
