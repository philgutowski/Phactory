# xAI / SpaceXAI suite research, 2026-10-14
Method: WebFetch/WebSearch summaries. x.ai/bot, x.ai/grok, x.ai/news returned HTTP 403 to fetch; their content was seen only through search snippets. Fetch summaries come from a small model, so quotes are paraphrase-grade.

## 1. Grok Bot
- PRIMARY (docs.x.ai/grok-bot/overview): a named AI teammate with a job and context that builds over time. Each runs on a persistent cloud computer (browser, filesystem, terminal). You message by text, dictation or voice chat. Uses connectors where available, computer use otherwise.
- PRIMARY (x.ai/news/introducing-grok-bot, via search; third-party coverage dates launch): beta launched 2026-08-11. Bots can learn a task by watching, then save it as a routine.
- PRIMARY (x.ai/news/grok-bot-more-plans, 2026-08-26): included with SuperGrok, SuperGrok Plus, SuperGrok Heavy, Cursor Pro, Pro+, Ultra, Teams Standard and Premium. Enterprise is a waitlist. Bots have their own usage allowance, separate from Grok and Cursor plan usage.
- PRIMARY (docs): runs on macOS, Windows, Linux, iOS, Android. Tag @bot on X needs a connected X account, not in all regions.
- Tools: connectors plus computer use. Overview page does not mention MCP. Whether custom MCP works with Bots: UNVERIFIED. (A skills/routines docs page exists: docs.x.ai/grok-bot/skills-routines-and-automations, not fetched.)
- Several Bots: PRIMARY yes, run in parallel, message each other, group chats. BUT PRIMARY: "All of your Bots use the same cloud computer, sharing its files, browser sessions, and app logins." Conversation and learned context are separate per Bot. One Bot runs one computer-use task at a time. Max Bot count: UNVERIFIED (not stated).
- One per customer: not a documented pattern. The computer belongs to the account, so per-customer Bots would share logins and files. Strict isolation is described between users, not between Bots. Inference only.
- Price: PRIMARY no dollar price for Bot itself. It is bundled in plans (see 3). Usage "resets weekly".
- Routines (PRIMARY, docs): up to 50 routines per Bot, last 20 run records kept per routine.
- THIRD PARTY (ecosystemastartup, thebridgechronicle, thetechoutlook): original beta limited to SuperGrok Heavy, Cursor Ultra, Teams Premium. Superseded by the 08-26 post.
- Note: "Grok bot" is also used loosely for the @grok reply account on X. Different thing.

## 2. Naming
- PRIMARY (x.ai/news/x-subscription, 2026-10-12): footer reads "© 2026 SpaceXAI LLC". So SpaceXAI is an official legal/entity name. Page title and social handle use "SpaceXSI" (unexplained, may be a typo or second brand).
- PRIMARY (search snippet of x.ai/news): page title "SpaceXAI — Creators of Grok, the AI Chatbot".
- PRIMARY (docs.x.ai/docs/overview): Code API description says "Available on the SpaceXSI API" (spelling again). Domains stay x.ai, api.x.ai, docs.x.ai, console.x.ai; env var stays XAI_API_KEY.
- Product line names (PRIMARY, docs overview): Grok (consumer), Grok Build (grok-4.7, coding model and CLI), Code API, Responses API, Voice API, Imagine API (images, video), Grok Bot, Grok connectors. Cursor is sold alongside inside the same plans.
- THIRD PARTY (ClickUp timeline): xAI became the "SpaceXAI division" in May 2026. SpaceX acquired xAI announced 2026-02-02 (Yahoo/Reuters, third party).
- Recommendation: use "SpaceXAI" for company, "Grok" for models and products, "xAI API / api.x.ai" for the API surface. No single official "agent product line" umbrella name found; closest is Grok Bot (agents) and Grok Build / Code API (dev).

## 3. Consumer plans (October 2026)
- PRIMARY (x.ai/news/x-subscription, 2026-10-12), new X Subscription: Free $0, Plus $8/mo, Premium $30/mo (bundles SuperGrok + Cursor Pro + X Premium+, was $90 combined), Super $100/mo, Ultra $200/mo. Usage multiples vs Plus: Premium 4x, Super 15x, Ultra 50x.
- PRIMARY (docs.x.ai/x-subscription/pricing via search): existing SuperGrok, Cursor and X Premium plans continue. Legacy SuperGrok Lite/Plus/Heavy dollar prices not confirmed from primary. THIRD PARTY blogs: Lite $10, SuperGrok $30, Heavy $300. UNVERIFIED officially.
- Custom MCP connectors: PRIMARY (docs.x.ai/grok/connectors) "available to all Grok users", no tier gating stated. x.ai/news/grok-connectors (2026-05-06) lists web, iOS, Android, no tiers. Business/Enterprise need admin provisioning first.
- Automations: PRIMARY (x.ai/news/grok-automations, 2026-07-16) scheduled automations for all users; email triggers need SuperGrok. Connectors can be @-mentioned inside an automation.
- Voice: PRIMARY page does not tie voice to tiers. Tier gating UNVERIFIED. THIRD PARTY blogs say higher voice limits on paid.
- Grok Bot per tier: bundled in SuperGrok family and Cursor paid plans (08-26 post). Whether the new Plus/Premium/Super/Ultra tiers include Bot: page does not say.

## 4. Separation of memory and connectors
- Per Bot (PRIMARY, docs): each Bot keeps its own conversations, memory, preferences. A "named Bot" keeps memory, files, browser sessions across sessions. But computer, files, browser sessions, app logins are shared by all Bots on the account. Per-Bot connector scoping: UNVERIFIED.
- Consumer Grok memory: PRIMARY x.ai/grok lists memory across chats. No Projects or Workspaces feature found in any fetched primary page; per-project memory/connector separation: UNVERIFIED, likely absent. Date checked 2026-10-14.
- Org level: PRIMARY connectors docs: Business/Enterprise admin provisions connectors for members. No per-project scope. Grok Build CLI memory is an on/off setting (PRIMARY, changelog, via search).
- API side not checked for per-key/team separation (console.x.ai teams). UNVERIFIED.
- Implication for Slipway: for per-customer isolation, use separate accounts or API keys, not multiple Bots in one account.

## Sources
- https://docs.x.ai/grok-bot/overview
- https://x.ai/news/grok-bot-more-plans
- https://x.ai/news/introducing-grok-bot (search snippet only)
- https://x.ai/news/x-subscription
- https://docs.x.ai/docs/overview
- https://docs.x.ai/grok/connectors
- https://x.ai/news/grok-connectors
- https://x.ai/news/grok-automations
- https://docs.x.ai/x-subscription/pricing (search snippet only)
- https://docs.x.ai/grok-bot/skills-routines-and-automations (search snippet only)
- https://www.thetechoutlook.com/new-release/software-apps/xai-introduces-grok-bot-available-in-early-beta-on-desktop-and-ios/
- https://ecosistemastartup.com/grok-bot-de-x-ai-agentes-de-ia-con-computadora-propia-para-tu-startup-en-2026/
- https://finance.yahoo.com/news/elon-musk-announces-spacex-acquisition-095834261.html
- https://krater.ai/blog/grok-pricing (third party)
