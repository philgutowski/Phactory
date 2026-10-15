# Deepening review: architecture (2026-10-14)

Reviewer: architecture pass over the first version plan, dispatched by the planning session after the initial write. Disposition of every finding: accepted and integrated on 2026-10-14 (KTD3, KTD4, KTD8, KTD9, KTD11, KTD17, KTD18, KTD20, KTD23, KTD26, U4, U5, U6, U8, U10, U11, U14, U15, U17, U22, new U25). Nothing found shows a settled decision cannot work; two Hatchet assumptions were unverified and are now U4 facts.

1. Card ownership has one record, and it is not the engine. KTD16 said the engine's history is never the record while KTD3 put ownership in the engine's idempotency key, and the engines research documents no run dedupe primitive for Hatchet. The invariant is at most one build row per (project, card key) outside the ownership terminal set, enforced by a partial unique index, with a claim function called by both watch and the dispatch verb so the database decides the race. KTD3's terminal set was also wrong against the state diagram: gate_red and halted keep a live workflow.

2. Both concurrency keys as written serialised the whole card. A limit applies to the unit the engine counts, which is a run; a repository key on the durable parent would mean one card per repository from dispatch to scored, and a project key on the parent would count cards waiting days for approval. Keys move to child tasks; U4 proves the layout and whether a waiting parent counts against a key.

3. U14 could not be built from its text. It cited units that depend on it (U15, U16, U17, U23), pointed at a pattern that did not exist (U4 had no code file), and mixed the engine program with the ownership layer. U14 narrows to the workflow with a post_landed child whose steps are no ops the later units fill; halt becomes a wait on a card.resume event; U25 holds ownership, watch, and the verbs in a service layer; U18, U20, U21 depend on U25; U4 commits its task file.

4. The builder contract was "what the two adapters share", not six primitives. Four gaps would force a contract change for a third adapter: two schedule shapes, two budget enforcers, provider events mapped in the trace package per adapter, and a side reference decided downstream. The contract gains a primitive table with one carrier each, poll instead of wait, a declared schedule shape, adapters yielding shape records, the side reference convention fixed in U11, and a credential object injected by the adapter.

5. The closing reference belongs to the tracker adapter, and the landing path is a live probe. The lifted comment search read was dead on arrival since Phactory never writes the comment it searched for; the landing path as a per repository table in code was the first project agnosticism violation in the tree. The tracker adapter renders the closing reference object, the host adapter places it, the landing path is probed live and may be pinned in the project file, and verify depends on U5 and U8 only.

6. Thin harness: runner build, swap, and rollback are deploy scripts, not package code; replay runs as a Langfuse dataset and experiment; the judge stays a Phactory function only because R13 binds it per project, stated in KTD18; the trace shape is an OpenTelemetry GenAI span builder, not a model of its own.

7. Project and operator values that would have landed in code: the runner repository coordinates and version file, the Langfuse project mapping, the laptop worker label, and the replay's source repository. All moved to the configuration files.

8. The layering the Output Structure invited: tools importing the workflow package, the CLI importing the MCP layer, the composed status with no home, the page reaching into the MCP auth module. One dependency rule (KTD23), a service layer, a top level auth module, and an import graph test in CI.

Checked and found sound: the state machine and page bucket mapping; the split of credentials; Phactory's own webhook endpoint with the durable sleep as fallback; the closed halt set; no tool accepting an approval; the absence rules shared by check_card and the deterministic score; per project Langfuse projects and token audience as project identity; the deadline step on the merge wait.
