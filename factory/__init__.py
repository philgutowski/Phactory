"""The factory: how Slipway builds itself until Slipway can.

Slipway's own cards are built by Claude Code cloud sessions and landed through
the repository's own merge path, which is the plan's Build Process section
made mechanical. The pieces live here, outside src/slipway, because they know
this repository by name and the product may not (R1):

    handoff.py       the ready rule and the handoff a cloud session starts from
    review.py        the model review that runs as the pull request's review check
    review-brief.md  the reviewer's brief, the written definition of a good review

The procedure that ties them together is the dispatch skill,
.claude/skills/dispatch/SKILL.md, and the explainer is
docs/explainers/build-loop.md. Once U14 and U25 land, Slipway's own watch and
dispatch replace handoff.py; the review check stays.
"""
