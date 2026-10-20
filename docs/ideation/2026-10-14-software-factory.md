# Slipway: the software factory

Created: 2026-10-14
Status: ideation for a new system. It does not exist yet. Nothing here is built.

## What it is

A system where agents take tasks, do the work in isolated environments, and every run is
recorded and graded so the setup keeps improving. It is project agnostic: it has to run against
any repository with any tracker on any host, including ones that do not exist yet and ones that
belong to someone else. It carries no knowledge of any one project. Everything project specific
is configuration a project supplies, never code in the tool.

It is a fresh start. Two earlier systems on this machine, a task runner and a lanes script, were
built for specific boards and grew around them. What they taught is kept below as rules. Their
code, their vocabulary and their history are not carried over. The earlier runner is the factory
that builds this one: its queue takes this system's cards like any other project's.

## The six principles

1. **Off the laptop, isolated, one place.** Each task runs in its own sandbox, on a hosted agent
   by default (a Claude Code cloud session, a Gemini agent, a Grok agent) or in a local worktree
   when a task must run on the operator's machine. Every task flows through one place.
2. **Every run leaves a trace with three parts.** Transcript, everything said by the person and
   the agent. Actions, every tool call, file read and write, shell command and outside
   connection. Output, the diff or branch, saved so it can be graded.
3. **Every output is graded.** An eval scores whether the task was done right from start to
   finish, against a written definition of good, not just whether the code runs.
4. **Model per job in config.** One line per agent names its model and effort. Swapping is a
   config edit, and because every run is scored, swaps can be compared.
5. **Thin harness.** The tool is the outer loop and the records. The model does the work. What
   lasts is the tasks, the evals and the definitions of good, so every unit of harness has to
   name the grade, trace or task it exists for.
6. **A person approves anything risky.** Merging, pushing, sending messages, touching real
   data. The tool prepares, a person releases.

## Rules learned, carried over without their context

From the runner:

- **Never trust the agent's own report.** The agent's closing claim is a claim. Landing is
  decided from git and the tracker, read by the tool itself.
- **One fresh process per task.** Nothing carries between tasks except what landed in git and on
  the tracker. If an outcome lives anywhere else, the next task cannot see it.
- **Halt rather than continue past an outcome the tool cannot confirm.**
- **The project owns verification.** The tool names one gate command it runs before a merge.
  What else a unit must pass, and what it records, comes from the project's own instructions.
- **A project needs four properties before the tool runs against it.** Independent tasks,
  durable state between tasks, an external gate that refuses broken changes, and a written
  definition of verification. A project missing one gets it first.
- **The tool never writes the tracker on its own.** Every tracker write goes through an agent
  process with instructions, so a defect in the tool cannot move a card.
- **One lease per repository.** Two runs never merge into the same repository at once, and a
  lease expires on its own if its holder dies.

From the lanes:

- **Builders can be anywhere.** The shape is a handoff with a fixed form (base commit, branch
  name, rules, files another build is editing, the done line) and a branch that comes back.
- **A remote builder reads nothing on the operator's machine.** The handoff carries every rule
  it needs, including which tests are bound to the operator's machine and will fail elsewhere.
- **Verify in a scratch join and land the exact verified commit.** Merge onto main in a
  throwaway worktree with its own environment, run the gate there, fast forward main to that
  commit. Never re merge.
- **One pusher per repository.** Pushing is by hash, by one party, on a cadence.
- **The gate runs on a machine that matches production.** A hosted VM running as root in a
  different timezone without the project's native tools fails tests that pass at home. The
  builder can be remote; the gate is where the truth is.
- **Remote builders never poll.** A new comment on their card has to be delivered to them.
- **A label added while the loop runs must be honoured on the next cycle**, not after a restart.
- **A file every unit edits at the same place serialises every lane.** One fragment file per
  unit, numbers assigned at landing, never at build.
- **Route at filing, not by hand.** A card carries its lane from the moment it is filed, by a
  label chosen from a few plain words. Nobody routes.
- **Two systems landing the same card is the worst failure.** A card owned by one lane is
  invisible to every other.

From operating both:

- **The harness grew and the grading never started.** Twenty thousand lines of runner, two
  thousand tests, zero evals. The new system inverts that.
- **State for engineers is not state for operators.** A page that says "4 being built, 2
  waiting to land, 1 needs you, here is what to do" is the product surface. Branches and hashes
  are the debug view.

## The simplest architecture

```
  card                   a task on the project's tracker, carrying its definition of good
    |
    v
  dispatch               the one place; picks the lane the card's label names
    |
    v
  build                  a hosted agent or a local worktree; isolated; one fresh process
    |
    v
  return                 a branch at a commit, plus the trace (transcript, actions, output)
    |
    v
  land                   scratch join, gate, fast forward to the verified commit
    |
    v
  eval                   score the landed diff against the definition of good; informational
  trace                  one folder per task
  learning               did this task teach something worth keeping
    |
    v
  person                 reads the page, releases the push, writes the next definition of good
```

Objects: Project, Card, Lane, Handoff, Landing, Eval, Trace, Learning.

Verbs: `onboard`, `dispatch`, `status`, `land`, `eval`, `trace`, `watch`.

Configuration, all outside any repository: one file per project (repo, tracker, gate, lanes
allowed, providers and accounts allowed, where the project keeps its evals), one file per
operator (credentials, which providers they hold accounts on). A customer's project file and
secrets are never visible to another customer's runs.

Where the definition of good lives: with the card and the project, never in the tool. A card
carries a `good:` block written by whoever filed it. A project that wants deterministic checks
keeps them in its own tree. The tool runs the stage; the project owns the standard.

Two scores kept apart: a deterministic one (the gate passed, the diff stayed inside its declared
paths, the test the card named exists and runs) and a judged one (a fresh model, on its own
model, given the definition of good and the diff, returning a rubric result).

## Decisions settled on 2026-10-14

- The eval runs after landing and is informational. It never blocks a landing while the grader
  is young.
- The name is Slipway, chosen 2026-10-14 before any code. It is a word a customer will hear.
- Slipway starts as a fresh repository. Modules from the two earlier systems that are generic
  and already verified are lifted as files when a unit needs them, the way the runner was
  itself forked. From the runner: the tracker adapters, verify landed, the lease. From the
  lanes script: the land path (scratch join with its own environment, gate, fast forward to the
  exact verified commit), the handoff shape, and the status read. The lanes script's board
  specific parts, its hard coded paths, its session bring up and its one board's configuration,
  do not come. No docs, issues, sidecars or vocabulary come with any of it.
- Hosted agents are the default lane. Local is the exception, for tasks bound to the machine.
- The tool is project agnostic and is a practice asset: it onboards customers' systems. Its
  repository is public; every project's configuration is private and lives outside it.

## Open

- The first task to grade: any landed task from any existing repository, by replay, since its
  diff and transcript already exist. No new build is needed to write and tune the first eval.
- Whether a hosted provider's transcript can be pulled to disk after a session ends. If not, the
  builder writes its own action log into the branch and the trace reads that. This is the one
  principle hosted agents put at risk and the first spike.
- Which trackers and hosts the first version must speak: at least GitHub and Jira, and the
  first Bitbucket repository is already in view.

## Acceptance targets

The first version is done when it can onboard, from a cold start and with no code change,
three kinds of project: one on GitHub with a GitHub project board, one on Bitbucket with Jira,
and one it has never seen, and land one scored card on each.

## Next step

A brainstorm producing the requirements for the first version. Then a
plan. Then the existing runner builds it from a card queue.
