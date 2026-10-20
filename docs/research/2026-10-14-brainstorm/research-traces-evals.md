# Slipway research: trace storage and evals (2026-10-14)

Caveat: the sponsor page is first party. Most pricing and licence claims come from third party aggregators and a few official docs pages, and several conflict. Verify prices on vendor pages before committing.

## Sponsor page (ai.engineer/nyc) [S1]
Presenting: Arize. Gold: Langfuse by ClickHouse, Comet (Opik). Silver: Judgment Labs, Sentry. Supporting: Coval. Braintrust was demoed in the Secure Software Factory show and tell (not a listed sponsor). Humanloop, Promptfoo, Weave, LangSmith, DeepEval are not listed.

## Per product
Terms: OTel = OpenTelemetry, the standard wire format for sending traces. Span = one timed step in a trace, such as one tool call.

**Langfuse (Gold sponsor)**
- Trace ingest: OTLP over HTTP at /api/public/otel (v3.22.0 and later), no gRPC; also a public API [S2].
- Judge evals: LLM as a judge with custom prompt and variables mapped to trace data; runs on live observations or experiment runs; all features MIT since 2025-06-04, including self hosted [S2]. Note: on v4, trace level judges are deprecated in favor of observation level ones [S2].
- Compare models: experiments and datasets. Not verified in this pass, check docs.
- Free tier: Hobby cloud, 50k units a month, 2 users, $0; Core $29, Pro $199 [S3]. A unit is a trace, observation or score.
- Self host: yes, MIT core, free, you pay infrastructure [S2][S3]. Hosted API: yes [S2].
- Verdict: USE IT.
**Arize Phoenix and AX (presenting sponsor)**
- Trace ingest: OTel native, Phoenix runs local, Docker or Kubernetes; covers tracing, evals, datasets, experiments [S4].
- Judge evals: yes, evals and experiments in Phoenix; AX adds online evals [S4]. Custom rubric detail not confirmed.
- Free: AX Free $0, about 25k spans a month, 15 day retention; AX Pro $50 a month (aggregator figures) [S4].
- Self host: Phoenix yes, but Elastic License 2.0 (source available, not OSI open source) per one source [S4][S5]. AX is SaaS. Hosted API: yes.
- Verdict: ADAPTER (strong second choice; OTel pipe means switching is cheap).
**Braintrust**
- Trace ingest: SDK and wrappers; no evidence of native OTel in the search results [S5]. Not confirmed either way.
- Judge evals and model comparison: this is its core strength (experiments, scorers), per general knowledge; not source checked here.
- Free: Starter, 1 GB data, 10k scores a month, 14 day retention; Pro $249 a month [S5].
- Self host: Enterprise only [S5]. Hosted API: yes.
- Verdict: NOT FOR THIS (closed, retention is short, self host gated).
**LangSmith**
- Trace ingest: SDK; OTel ingest on the free plan not confirmed [S6]. Docs show OTel export of its own telemetry only.
- Evals: online and offline evals included in Developer plan [S6].
- Free: Developer $0, 5k base traces a month, 1 seat; overage price disputed ($0.50 vs $2.50 per 1k) [S6].
- Self host: Enterprise only [S6]. Hosted API: yes.
- Verdict: NOT FOR THIS (tied to LangChain world, 5k trace cap, self host gated).
**Weights and Biases Weave**
- Trace ingest: SDK based; native OTel not confirmed [S7].
- Evals: Weave has evals; not source checked here.
- Free: free plan for personal non corporate use; limits conflict between sources [S7]. Pro about $50 to 60 a seat.
- Self host: Enterprise [S7]. Hosted API: yes.
- Verdict: NOT FOR THIS (a consultant using it for client work may breach the personal use limit).
**Promptfoo**
- Trace ingest: no. It is an eval runner, not a trace store.
- Judge evals: llm-rubric asserts a natural language rubric via a model grader; same tests across several providers to compare models; custom code graders in JS, Python, Ruby [S8]. Reported MIT, unverified [S8].
- Free: open source CLI, local. Self host: runs anywhere. Hosted API: not needed.
- Verdict: ADAPTER (a possible grader engine for the judged score, reads a diff plus a rubric file).
**Confident AI / DeepEval**
- Trace ingest: OTel export to Confident AI, plus its own confident-trace SDK [S9].
- Judge evals: DeepEval OSS has G-Eval (rubric style judge) and Pytest integration [S9]. Custom metrics reported paid only on the hosted free plan [S9].
- Free: DeepEval OSS $0; hosted Free $0; Starter $19.99 and Premium $49.99 (August 2026 snapshot, earlier snapshot differed) [S9].
- Self host: library yes, platform not confirmed. Hosted API: yes.
- Verdict: ADAPTER (use DeepEval library for G-Eval only if hand rolling a judge feels heavy).
**Humanloop**
- Shut down 2025-09-08 after the Anthropic team deal; platform and API gone [S10].
- Verdict: NOT FOR THIS (does not exist).
**Comet Opik (Gold sponsor)**
- Open source Apache 2.0, self host free with same code as hosted; Free Cloud $0, 10 members, 25k spans a month; Pro $19 flat [S11]. OTel and judge metric support not verified in this pass.
- Verdict: ADAPTER (credible Langfuse alternative; check OTel ingest first).
**Judgment Labs (Silver sponsor)**
- judgeval: OTel based tracing SDK, self hostable, licence unclear between sources (Apache 2.0 vs not stated); agent focused scoring [S12].
- Verdict: ADAPTER (watch; smaller, newer, licence to confirm).

**Sentry, Coval (sponsors)**: Sentry is error monitoring, Coval is voice and chat agent simulation. NOT FOR THIS.

## Recommendation
1. Adopt Langfuse (self hosted or Hobby cloud) as the trace store for stage one; send each task as an OTel trace, diff and transcript as span attributes.
2. Keep the deterministic eval (gate passed, diff inside declared paths, named test exists) in Slipway code; it reads git and CI, no vendor knows those facts.
3. Run the judged eval through Langfuse's custom judge, or a thin Slipway function (fresh model, rubric file, diff in, rubric JSON out) that writes its score back as a Langfuse score; both stay swappable.
4. Do not build a trace folder or dashboard UI; do build the Claude Code session to OTel exporter, the tracker, git and CI landing verifier, and the builder and model comparison view keyed by builder name.
5. Risk: Claude Code cloud transcripts may not expose every tool call over a clean API; test that capture first, because the store matters less than what you can ingest.

## Sources
- S1 https://ai.engineer/nyc
- S2 https://langfuse.com/integrations/native/opentelemetry (and the Langfuse docs results returned in the same search)
- S3 https://www.zenml.io/blog/langfuse-pricing , https://costbench.com/software/ai-observability/langfuse/
- S4 https://costbench.com/software/ai-observability/arize-phoenix/ , https://freetier.co/directory/products/arize
- S5 https://www.truefoundry.com/blog/braintrust-pricing , https://www.morphllm.com/comparisons/braintrust-alternatives
- S6 https://www.langchain.com/pricing-langsmith , https://docs.smith.langchain.com/self_hosting/observability
- S7 https://wandb.ai/pricing , https://futureagi.com/blog/wandb-weave-alternatives-2026/
- S8 https://www.promptfoo.dev/docs/configuration/expected-outputs/model-graded
- S9 https://www.confident-ai.com/docs/integrations/opentelemetry , https://costbench.com/software/ai-evaluation/deepeval/free-plan/
- S10 https://wandb.ai/wandb_fc/product-announcements-fc/reports/Humanloop-is-sunsetting-Migrate-to-Weights-Biases-as-an-alternative--VmlldzoxMzk4ODc1Nw
- S11 https://www.comet.com/site/pricing
- S12 https://aicoolies.com/tools/judgeval , https://docs.judgmentlabs.ai/changelog/2025-04-21
