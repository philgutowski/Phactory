# Document review, resolved (2026-10-14)

Reviewed: `docs/plans/2026-10-14-1730-feat-phactory-first-version-plan.md` after the deepening pass. Mode non interactive. Team: coherence (11 findings), feasibility (17), scope guardian (2), security lens (11), adversarial (12). Cross model pass skipped by live user prohibition: Phillip's standing rule forbids sending content to an outside service without his explicit approval, and the pass would send the full plan to another provider; each judgment lens kept its in process reviewer. No persona output was malformed. Dropped at anchors 0 and 25: none. Edit authority: the planning session's grant to correct the implementation and verification sections to satisfy the Product Contract, plus the author's own confirmation of the synthesis; two Product Contract wordings (R20, R37) were corrected because the plan's own mechanism contradicted them, and both are recorded in the preservation note.

Phillip added R42 (live board status) during the review; it is integrated with KTD27 and AE13 and was not a reviewer finding.

## Applied (one fix may resolve several merged findings)

1. Verify's commit relation (coherence C6, adversarial 1, feasibility 9, 10, 11). Under a merge queue the host lands a commit it created; under squash or rebase there is no second parent. R20 restored to "the commit the gate tested", with the tested commit carrying the confirmed tree; KTD7 and U10 made path and method aware; `tested_commit` recorded from the queue's checks requested event; the deterministic check posted on the queue's commit (feasibility 12); `hand_landing` demoted to evidence (feasibility 9); `tree_of` named as new code and the two missing `gitread` functions dropped (feasibility 18); U2 records the merge method per repository.
2. Builder expiry halt with no exit (coherence C1). Build outcomes end as `returned_empty` with the class as reason; only verify halts wait. KTD11, KTD22, the state diagram, U10, U14. Budget clock starts at `building` (adversarial 12, folded in).
3. Page buckets overlapping and leaving states unassigned (coherence C2). Every state in exactly one bucket; board columns mapped.
4. Success criterion unmeetable before U22 (coherence C3). Reworded.
5. Sequencing diagram missing the Phase 4 to Phase 5 edge (coherence C4). Added.
6. U22 and U23 circular on rollback, and observation only after landing so red gates never roll back (coherence C5, feasibility 1). Rollback evaluator in U22, called by U23's observe; `gate_observed` seam in U14 on every gate completion.
7. KTD23 excluded `onboard` which surfaces import (coherence C7). Added.
8. Builder protocol method outside the primitive table, and no archive or delete (coherence C8, feasibility 4). `poll` carries the result; `release` and `delete` under Environment.
9. Deterministic score labels for an absent block (coherence C9). Fixed.
10. U18 in two Definition of Done rows (coherence C10). One row.
11. `deploy/runner.Dockerfile` missing from Output Structure (coherence C11). Added, with `deploy/README.md` and `templates/`.
12. Onboard over MCP could rewrite accounts, subjects, caps, and enumerate every customer's accounts (security 1, 11). Operator only questions in KTD15 and U19.
13. Jira adapter read credential environment variable names from the project file (security 2). Credentials by operator file name, site checked; U6, U8, KTD8, R2, R3.
14. No secret storage or per service placement (security 3) and ports unstated with Hatchet's default admin (security 4). KTD28, U4, U6, U22, System-Wide Impact.
15. Laptop builder beside worker credentials (security 5). Dedicated user, session as a second user, `laptop_build` returns the result and the VPS writes records; KTD12, U13, U14.
16. Runner registration tokens unaddressed (security 6). Fine grained administration write per repository, named in the operator file, runner container only; KTD20, U2, U22.
17. Filing posted customer log excerpts to a public tracker unscrubbed (security 7). Findings added to the redaction call sites; customer projects file structured fields only; KTD20, KTD24, U23.
18. Review paths omitted builders, redact, trace; deploy from main unstated; live smoke units auto merged (security 8, adversarial 11). KTD25 path list extended; deploy by hand; Verification Contract rows.
19. Status page had no operator subject or login flow (security 9). `operator_subject` in the operator file; authorization code flow with a session cookie; U6, U20, KTD13.
20. Land override reachable from a project token (security 10). Operator only; KTD11, KTD14, U25, U18.
21. Claim to run id crash window (adversarial 2). `attach_run` compare and set; KTD3, U7, U14.
22. Rollback fired on any project red (adversarial 3). Runner class finding required; R37, KTD20, F6, AE11, U22.
23. Gate never ran the integration tests (adversarial 4). Local Postgres in CI, skips fail under CI, Hatchet smoke operator run; KTD19, U5, Verification Contract.
24. One hour installation token expires inside long builds (adversarial 5, feasibility 6). Refreshable credential; KTD6, KTD26, U1, U11, U12, U13.
25. Four parallel cards editing one function (adversarial 7). Seams as a registry; U14, U15, U16, U17, U23.
26. U4 tested a literal cap and asserted the opposite of KTD4; engine unpinned (adversarial 8, feasibility 8). Rewritten; engine pinned at or above 0.106.0.
27. Verify raced tracker automation (adversarial 9). Ten minute bounded retry; KTD11, U14.
28. Per project mounts at startup forced a restart per customer (adversarial 10). Parameterised route; KTD13, U18.
29. Runner read API carries no runner commit (feasibility 2). Attribution by activation interval; KTD16, KTD20, U7, U23.
30. Runner is one job per process and one repository per process (feasibility 3). Supervisor model; KTD20, U22.
31. Failed and abandoned builds never exported or deleted sessions (feasibility 5). `post_build` seam on every exit from building; KTD17, U14, U15.
32. Hatchet default timeouts cancel long children and queued laptop builds (feasibility 7). KTD22, U4, U14.
33. U15 and U16 did not depend on U14 (scope 1, feasibility 13); U1 did not depend on U2 (feasibility 14). Unit index and cards.
34. Transcript path ignored the empty configuration directory (feasibility 15). Fixed in U13.
35. Phactory's repository must be public for the runner (feasibility 16). The ideation note already decided it; recorded in KTD19 and Dependencies.
36. Langfuse read back lags asynchronous ingestion (feasibility 17, anchor 50). Bounded retry in KTD17 and U15, folded in by the author.
37. Project delete, pruning, and extra CLI commands exceeded any requirement (scope 2, anchor 50). Moved to Considered and not built and Deferred to Follow-Up Work by the author; cascade keys kept.
38. Postgres as the record past Langfuse's thirty day window (the applicable half of adversarial 6). Attempts carry account, model, effort, cost; scores carry the rubric result; KTD16, KTD17, U15.

## Decision, resolved by Phillip on 2026-10-15

- Langfuse Hobby keeps data thirty days (adversarial 6). Phillip chose the side reference in git over paying for Langfuse Core. Applied: R22 widened to every builder with a user directed Key Decision; KTD17 and U15 commit the mapped event log to the card's side reference in `post_build`; the Outstanding Questions entry removed.

## Residual concerns kept

- Postgres runs one instance with two databases; roles per database, encryption at rest, and backup of the `phactory` database and the environment file are not specified, and a backup of the environment file is a second copy of every credential (security).
- The Claude app and Grok hold connectors at account level, so tokens for several customer projects sit in one client account; isolation between manager agents is client side discipline (adversarial).
- The Dagger engine on the VPS needs a listener a runner container without a Docker socket can reach, and it runs job steps with engine privileges (feasibility).
- A week long wait replays a long durable event log on resume; Hatchet documents no cap and U4 waits through one hour, not seven days (adversarial).

## Deferred questions kept

- Is `Lementknight` an organization, which decides merge queue availability for the runner repository (feasibility). U2 answers it.

Restated and suppressed as duplicates of findings or plan text: 7 (runner main as supply chain, redaction limits, deploy watcher, laptop listener, manager agent subject minting, merge method per repository, Hatchet engine version).
