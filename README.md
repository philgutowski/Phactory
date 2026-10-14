# Phactory

Phactory is the landing and grading authority between any coding agent builder and a customer's repository.

A card is filed on the project's tracker, from a phone if that is where the idea arrives. Phactory hands it to a builder through one fixed handoff, pushes the branch that comes back as a pull request into the host's own merge queue, verifies from git and the tracker that the card actually landed, stores the full trace, scores the result against the card's written definition of good, records who did each step, and shows one page with the things that need a person. Builders are adapters: Claude Managed Agents, a headless CLI session in a sandbox or on a laptop, and later Cursor, Grok Build, and Devin. Manager agents, one per customer, talk to the person by text or voice and drive every verb over a remote MCP server that sees only that customer's project.

Phactory does not build, does not merge, and does not write the tracker. Vendors sell those. What no vendor sells, and what Phactory owns, is the repo side across builders: the handoff, the project's gate truth, verify landed, the two evals, the in flight facts, the page, and the agent callable verbs.

Phactory is project agnostic. It runs against any repository with any tracker on any host, including ones that do not exist yet and ones that belong to someone else. Everything project specific is configuration a project supplies through a guided one question at a time intake, never code in the tool. That is the first law, and the test every change passes before any other.

It is built for a one person product engineering consultancy first, and for several humans each managing their own manager agents later, without a rebuild.

Status: brainstormed 2026-10-14. Nothing is built. The requirements are in `docs/plans/2026-10-14-1730-feat-phactory-first-version-plan.md`. The seed note is `docs/ideation/2026-10-14-software-factory.md`.
