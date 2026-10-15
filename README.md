# Phactory

Phactory is the landing and grading authority between any coding agent builder and a customer's repository.

A card is filed on the project's tracker, from a phone if that is where the idea arrives. Phactory hands it to a builder through one fixed handoff, opens the branch that comes back as a pull request into the host's own merge path, verifies from git and the tracker that the card actually landed, stores the full trace, scores the result against the card's written definition of good, records who did each step, and shows one page with the things that need a person. The card's own board column follows along, moved by the tracker's automation on the events Phactory causes, so the operator can watch twenty agents at once from the board.

Builders are adapters: Claude Managed Agents, a headless CLI session in a sandbox or on a laptop, and later Cursor, Grok Build, and Devin. Manager agents, one per customer, talk to the person by text or voice and drive every verb over a remote MCP server that sees only that customer's project.

Phactory does not build, does not merge, and does not write the tracker. Vendors sell those. What no vendor sells, and what Phactory owns, is the repo side across builders: the handoff, the project's gate truth, verify landed, the two evals, the in flight facts, the page, and the agent callable verbs.

Phactory is project agnostic. It runs against any repository with any tracker on any host, including ones that do not exist yet and ones that belong to someone else. Everything project specific is configuration a project supplies through a guided one question at a time intake, never code in the tool. That is the first law, and the test every change passes before any other.

## Gates on the operator's own runner

Project gates can run on the operator's instances of [zig-actions-runner](https://github.com/Lementknight/zig-actions-runner), a single binary replacement for GitHub's self hosted Actions runner. A project opts in by naming the instance's labels in its own workflow. The instances track the runner's main branch, build and test each new commit, swap it in when idle, and roll back when a gate fails for a reason of the runner's own. Phactory reads the runner's record after every gate and records findings; an agent process files them as issues on the runner's board, so using the runner is how it gets tested.

## How Phactory is built

Phactory is its own first project. Every unit of the plan is a card on this repository's GitHub Projects board. Cards a Claude Code cloud session can build are built that way, from the card text alone, and land as a pull request that auto merges on a green gate; changes to the gate, the deploy files, or anything that holds a credential wait for one review. Spikes, deploys, and the acceptance pass are operator cards run by hand. The plan's Build Process section has the card template and the lane rules.

## Status

Planned 2026-10-14, reviewed and deepened 2026-10-15. Nothing is built yet.

- The plan: `docs/plans/2026-10-14-1730-feat-phactory-first-version-plan.md`. Requirements R1 to R42, twenty eight technical decisions, twenty five units in six phases. Four spikes gate the build: the Managed Agents event log and push path, each host's merge path and machine identity, the MCP surface behind an outside identity provider, and the durability of the workflow engine.
- The seed note: `docs/ideation/2026-10-14-software-factory.md`.
- Research: `docs/research/2026-10-14-brainstorm/` (what was in the room at the brainstorm) and `docs/research/2026-10-14-plan/` (the planning research, the three deepening reviews, and the resolved document review).

The first card is the repository scaffold (U5). The four spikes run beside it.
