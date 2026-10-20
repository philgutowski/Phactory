# Slipway research: durable workflow engines and agent sandboxes (2026-10-14)

Conference sponsor page (ai.engineer/nyc) lists in scope: Temporal (Gold), Daytona (Gold), Modal (Gold), Cloudflare (Silver), Union (Silver, unassessed), Runpod/Browserbase (not relevant). Inngest, Trigger.dev, Restate, Hatchet, DBOS, E2B, Runloop, Blaxel are not listed sponsors. [1]
Method: 15 fetches of vendor pricing/doc pages. "Not verified" means the page did not state it. Hourly figures marked (calc) are my arithmetic from the per-second or per-hour rates.

## Workflow engines
Key terms: a "signal" or "event wait" is a durable pause that survives restarts until a message (the human approval) arrives. "Checkpoint" means state is saved so a crashed step resumes, not restarts.

- Temporal / Temporal Cloud. Durable, retries, resume: yes (core product). Human approval: Signals plus wait_condition with optional timeout, no stated max wait [2]. Price: pay as you go from $50 per million actions, $150 credits for 90 days, no permanent free tier [3]. Self host: pricing page silent [3]; the open source server runs self hosted per Temporal docs (not verified here), but it needs a database and is heavy for a small VPS. Verdict: ADAPTER (right model, heaviest ops, cloud is metered with no forever free).
- Inngest. Durable steps, retries: yes. Free: $0, 50k executions, 5 concurrent steps, pauses at quota with no overage [4]. Pro $99/mo [4]. Open source, self hostable [4]. Event wait page not covered in what I fetched (not verified). Verdict: USE IT candidate (best free tier fit for one person).
- Trigger.dev. Long running tasks, wait.for/wait.until checkpoint after 5s and waiting time is not billed [5]. Free: $5/month credits, 20 concurrent runs; Hobby $10/mo [5]. Apache 2.0, self hostable [5]. Human approval primitive not stated on pricing page (not verified). Also runs your task code as compute, about $0.12/hr small 1x (calc) [5]. Verdict: USE IT candidate; a task can host the agent run itself.
- Restate. Pricing page returned no content [6]. Not verified. Verdict: NOT ASSESSED, low priority.
- Hatchet. Free 1M runs and 1M events per month, then $30 per million runs [7]. MIT open source, self hostable anytime [7]. Durable event waits referenced, detail not verified [7]. Verdict: USE IT candidate (generous free tier, MIT, Postgres based per general knowledge, not verified).
- DBOS. Open source Transact library "free forever"; Pro $99/mo, 1M checkpoints [8]. Runs on any platform, bring your own database [8]. Durable queues and external event waits not on pricing page (not verified). Verdict: ADAPTER (library in your own process, lightest, but I did not verify approval waits).
- Cloudflare Workflows. Included in Free and Paid Workers; free 3,000 steps/day, paid 500k steps/month included, $0.80 per extra 100k; state retained 3 days free, 30 days paid [9]. sleep is not billed as CPU [9]. waitForEvent and max sleep not verified. Not self hostable. Verdict: ADAPTER (3 day free retention is a real problem for waits of days).

## Sandboxes (can a coding agent run a repo toolchain and return a branch?)
General note: none of these pages mention git; all give a Linux VM or container, so cloning, installing the toolchain, and pushing a branch is something you script, not a product feature. Not verified per vendor.

- E2B. $100 one time credit; $0.000014/vCPU-s and $0.0000045/GiB-s, about $0.050 per vCPU-hr plus $0.016/GiB-hr (calc) [10]. Hobby caps a session at 1 hour, Pro ($150/mo) 24 hours [10]. Self host: not on pricing page [10]. Verdict: ADAPTER (1 hour cap kills free tier for hours long builds).
- Daytona (sponsor). $0.0504/vCPU-hr, $0.0162/GiB-hr, $200 free credit, per second billing [11]. Open source, self hostable [11]. A 2 vCPU 4 GiB box is about $0.17/hr (calc). Verdict: USE IT for the remote builder (cheap, open source).
- Modal (sponsor). Sandbox $0.00003942 per physical core-s (about $0.14/core-hr, calc) and $0.00000667/GiB-s; Starter plan $30/month free credits [12]. Not self hostable (not stated). Verdict: ADAPTER (credits cover about 100+ small hours a month, but Python first).
- Runloop. $50 trial credits; $0.108/CPU-hr, $0.0252/GB-hr; Pro $250/mo needed for suspend/resume and repo connections; 2 CPU 4 GB about $0.32/hr (calc) [13]. Verdict: NOT FOR THIS at one person scale ($250/mo for suspend).
- Blaxel. Up to $200 free credits; about $0.041 per GB RAM-hr active, suspends in 5s and resumes in 25ms, built for runs of days, suspended time costs only snapshot storage [14]. 4 GB is about $0.17/hr (calc) [14]. Verdict: ADAPTER (strongest fit for hours long idle waits, unproven here).
- Cloudflare Sandboxes. Priced as Containers plus Workers and Durable Objects, rates not on the page [15]. Verdict: ADAPTER (cost unknown, ties you to Cloudflare).

## Other sponsors checked (coordinator list)
- Union (Silver, Flyte). Durable ML/agent workflows, recovers from infra failure; Team $950/mo, 30 day trial, no free tier; self managed deploy into your Kubernetes cluster; approvals unverified (flyte.new_condition mentioned) [16]. Verdict: NOT FOR THIS (Kubernetes and $950/mo for one person).
- Sim (Gold). Open source visual agent builder, self hostable with Docker, no pricing or approval steps on page [17]. Verdict: NOT FOR THIS (agent building UI, not a dispatch/merge control loop).
- Runlayer (Gold). Enterprise agent security and MCP governance gateway, no sandbox [18]. Verdict: NOT FOR THIS.
- Flox (Silver). Reproducible locked toolchain environments, not a sandbox, no price listed [19]. Verdict: ADAPTER (could pin a repo's toolchain inside a Daytona box or on the laptop).
- Runpod, Supabase: not fetched. Supabase could host the Postgres for DBOS or Hatchet (not verified).

## Recommendation (5 lines)
1. Yes: write the control loop on a durable engine, not a state file. Hours long waits, CI waits, approval waits and resume per step are exactly what these engines do, and a hand rolled version will reinvent retries and idempotency badly.
2. Pick Inngest or Hatchet first: both have real free tiers (50k executions; 1M runs) and open source self hosting [4][7]. Hatchet is MIT; Inngest has the stronger hosted free plan. Run a one day spike for the approval wait on whichever you choose, since I could not verify it.
3. Temporal is the gold standard model [2] but costs $50 per million actions with only a 90 day credit [3] and heavy self hosting; take it only if interview signal matters more than cost, and note it is a sponsor so it carries career value.
4. Keep the per repository lease as a workflow concurrency key or one DB row; do not build it on the engine's queue alone unless the engine documents a per key limit (not verified).
5. Do not replace the laptop builder; add a sandbox for the unattended path. Daytona ($0.0504/vCPU-hr, open source) [11] or Blaxel (suspend while waiting) [14]; keep the laptop for machine bound tasks.

## Sources
[1] https://ai.engineer/nyc
[2] https://docs.temporal.io/develop/python/message-passing
[3] https://temporal.io/pricing
[4] https://www.inngest.com/pricing
[5] https://trigger.dev/pricing
[6] https://www.restate.dev/pricing
[7] https://hatchet.run/pricing
[8] https://www.dbos.dev/pricing
[9] https://developers.cloudflare.com/workflows/reference/pricing/
[10] https://e2b.dev/pricing
[11] https://www.daytona.io/pricing
[12] https://modal.com/pricing
[13] https://runloop.ai/pricing
[14] https://blaxel.ai/pricing
[15] https://developers.cloudflare.com/sandbox/platform/pricing/
[16] https://www.union.ai/pricing
[17] https://www.sim.ai
[18] https://www.runlayer.com
[19] https://flox.dev
