# U8 explainer: card contract and tracker adapters

A card is one unit of work filed on a project's tracker. Slipway needs four things from a tracker: which cards are ready to build, what one card says, whether a card is done, and what a pull request must carry so the tracker's own automation finds the card. This card builds the code that answers those four questions for GitHub Projects and Jira, and the parser for the block a card carries in its body.

## Words used below

- **Tracker.** Where a project's cards live. Here, a GitHub Projects board or a Jira project.
- **Adapter.** A class that hides one tracker behind a shared set of methods, so the rest of Slipway never knows which tracker it is reading.
- **Board column.** On GitHub Projects, the value of a card's single select Status field. On Jira, the card's workflow status.
- **Lane label.** The label that says who builds a card, such as `cloud` (R9). A project dispatches only cards carrying its own lane label.
- **slipway block.** A fenced code block in the card body, opened with three backticks and the word `slipway`, holding a JSON object with `good`, `paths`, `test`, and `lane` (KTD5).
- **Ready rule.** The test a card must pass before dispatch may pick it up.
- **Closing reference.** The text a pull request carries in its title, body, or branch name so the tracker links it to the card and moves the card when it merges.
- **Transport.** The thing that actually talks to the tracker: the `gh` command line tool for GitHub, an HTTP opener from `urllib` for Jira.
- **Injection.** Passing the transport into the adapter's constructor instead of building it inside, so a test can pass a fake.
- **GraphQL point.** GitHub's charge for one GraphQL query, the API `gh` uses under the hood. Each account gets 5,000 points an hour, shared by every token and every process that account runs.
- **Skip.** A read that could not complete. It carries a reason and the halt class `tracker_unreadable` (KTD11), never a guess.
- **ADF.** Atlassian Document Format, the nested JSON Jira stores rich text in.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/cards/contract.py` | `parse(body)` reads the slipway block into a `Block` (good, paths, test, lane, what is absent, or an error). `ready(card, project, other_lanes)` applies the ready rule and returns a `Readiness` with a reason when someone must act. |
| `src/slipway/trackers/base.py` | The shapes every adapter returns: `Card`, `TrackerStatus`, `ClosingReference`, the `skipped` helper, `TRACKER_UNREADABLE`, the network timeout, and `INTERFACE`, the only public methods an adapter may have. |
| `src/slipway/trackers/github.py` | `GitHubTracker`, derived from native-relay's GitHub adapter. Reads through `gh`, injected as `run`. Never lists a board. |
| `src/slipway/trackers/jira.py` | `JiraTracker`, derived from native-relay's Jira adapter. Reads the Jira REST API through `urllib`, injected as `opener`, with the credential from `resolve_tracker_credential` (U6). |
| `tests/unit/test_contract.py` | The block parser and the ready rule. |
| `tests/unit/test_trackers.py` | Both adapters against fixtures, the board listing ban, the budget skip, the network guard, and the import rule for these two packages. |
| `tests/fixtures/tracker/` | The six native-relay fixtures, unchanged, plus six new ones for labels, the per issue membership answer, the rate limited answer, and a ready search. |

## The card contract

The block parser looks for exactly one fenced block whose info string is `slipway`:

````
```slipway
{"good": "The page renders.", "paths": ["src/page.py"], "test": "tests/test_page.py", "lane": "cloud"}
```
````

| Body | Result |
|:-|:-|
| No block | Parses. All three of `good`, `paths`, `test` are listed as absent. |
| Block with empty values (the template as filed) | Parses. The empty ones are absent. |
| Block with some fields | Parses. The missing ones are absent. |
| Block that is not JSON, not an object, or has a wrong type | Error. The card is not ready, and the reason names the problem. |
| Two blocks | Error, so nobody wonders which one counts. |

Absent is not a failure. Absent `paths` contributes nothing to in flight files until push, absent `good` records "no rubric", absent `test` records "none named". Those are recorded later by the scorer (U16); this parser only reports which are absent.

## The ready rule

`ready(card, project, other_lanes=())` runs these checks in order. The first one that fails decides.

```
card.status equals project.ready_status (ignoring case)?
  no  -> not ready, silent            (a card in Done, In progress, or Backlog)
  yes
   |
lane labels on the card: project.lane_label plus other_lanes
  more than one -> not ready, reason  "carries 2 lane labels (cloud, operator); keep one"
  none          -> not ready, reason  "carries no lane label"
  one, not ours -> not ready, silent  (another lane's card)
  one, ours
   |
parse(card.body)
  error                     -> not ready, reason
  block names another lane  -> not ready, reason
  otherwise                 -> ready, with the block and its absent fields
```

Silent means nothing is wrong: the card is simply not this project's to build now. A reason means the card sits in the ready column and cannot move until someone fixes it, so it belongs on the needs you list.

The contract reads only a card's `key`, `body`, `status`, and `labels` attributes and imports no tracker, so the adapters and the contract stay independent of each other.

## The adapter interface

```
candidates()            -> (cards, reason)   open cards with the lane label, no status read
ready()                 -> (cards, reason)   the same cards, each with its board column
read(key)               -> Card              title, body, status, labels; skipped on failure
status(key)             -> TrackerStatus     status, terminal, skipped (halt class tracker_unreadable)
closing_reference(key)  -> ClosingReference  title_token, body_line, branch_token
```

`ready()` returns candidates for the ready rule, not a verdict; the caller (the watch cycle, U25) runs `contract.ready` on each. A failed read returns a reason beside an empty list instead of raising, because "the tracker could not be read" and "nothing is ready" lead to different places. A test holds the public methods of both classes to exactly this list, so no closeout, filing, or comment search method from the origin can creep back.

## How each adapter is injected so tests never reach the network

| Adapter | Constructor | Real transport | Test transport |
|:-|:-|:-|:-|
| GitHub | `GitHubTracker(project, run=None)` | `make_run()`, which calls `gh` through `subprocess.run` with a 30 second timeout | `DispatchRun`, which answers each `gh` subcommand from a fixture and records every call |
| Jira | `JiraTracker(project, opener=None, credential=None, ...)` | `urllib.request.build_opener()` | `FakeOpener`, which routes a URL fragment to a fixture and records every request with its headers |

The network guard (`tests/_nonet.py`) is loaded for every test. Two tests build an adapter with no fake and check that its first read raises `NetworkReached`, which proves the guard would catch a test that forgot its fake.

## GitHub: never list a board

The native-relay adapter read a card's column by listing the whole project board with `gh project item-list`. On a 313 card board that costs 405 GraphQL points a call, and the budget is 5,000 points an hour for the whole account, shared by every project and process. A few watch cycles would spend it.

So this adapter asks the other way around. For one issue it asks which project items the issue belongs to:

```
repository(owner, name).issue(number) {
  state
  projectItems(first: 20) { project { number owner { login } }  fieldValueByName("Status") { name } }
}
```

That is one point per card, whatever the board's size. It keeps the item whose project number and owner match the project file's `board` (written `owner/number`, or just `number` for a board the repository's owner holds). An issue on more than twenty boards pages, up to five pages, then reports a reason.

When the budget is spent, `gh` exits with "API rate limit exceeded" or the answer carries an error of type `RATE_LIMITED`. Either becomes a skip whose reason starts "GitHub GraphQL budget exhausted", with halt class `tracker_unreadable`. It never raises. A test asserts that no call is ever `gh project ...` and that no query names an `items` connection other than the issue's own `projectItems`.

`status(key)` is terminal when the issue is closed or its column equals the project's `landed_status`. `read(key)` uses `gh issue view` and returns the issue state as its status, with labels.

## Jira: the credential comes from the operator file

`JiraTracker` calls `resolve_tracker_credential(project.slug)` when it is not handed a credential. That function (U6) reads the project file's `tracker_credential` name, finds the entry in the operator file, refuses an entry whose site differs from the project's, and reads the email and token from the environment variables the entry names. The adapter builds a Basic authorization header from those two values and keeps only the header. The project file holds the entry's name and nothing else, and a test checks the header against the environment values and checks that neither value nor variable name appears in the project file.

`ready()` builds its JQL (Jira's query language) from the project file: `project = "<board>" AND status = "<ready_status>" AND labels = "<lane_label>"`. `status(key)` is terminal when the status is one of the done statuses, which default to the project's `landed_status`. A failed request is a skip with the HTTP code, never a crash.

## The closing reference per tracker

| Tracker | title_token | body_line | branch_token | Why |
|:-|:-|:-|:-|:-|
| GitHub | none | `Closes #12` | none | GitHub closes the issue when a merged pull request's body says so, and the project's own workflow moves the item. |
| Jira | `ABC-83` | none | `ABC-83` | Jira's development integration links a pull request by the upper case key in its title or branch, and its automation moves the card. |

The host adapter (U9) takes this object when it opens the pull request.

## The read path, from tracker to card

```
           watch cycle (U25)
                 |
                 v
     tracker.ready()  --------------------------------------------.
       |                                                          |
       | GitHub                                                   | Jira
       v                                                          v
  gh issue list --label <lane_label>                  GET /rest/api/3/search/jql
       |   (labels live on the issue)                   jql: board, ready status, lane label
       v                                                 Basic auth from the operator file
  for each issue:                                                 |
  gh api graphql  issue(number).projectItems                      |
       |   one point per card, never a board listing              |
       |   budget spent -> reason, tracker_unreadable             |
       v                                                          v
  Card(key, title, body, status = board column, labels)   Card(key, title, body, status, labels)
       |                                                          |
       '-------------------------.  .-----------------------------'
                                 v  v
                 contract.ready(card, project)
                                 |
              .------------------+------------------.
              v                  v                   v
        ready + Block      not ready, reason    not ready, silent
       (absent recorded)    (needs you)          (not ours now)
```

## Where this differs from the plan

- **`ready()` takes no argument.** The plan writes `ready(project)`; the adapter is already built from the project, so passing it again would invite two projects to disagree.
- **The Jira JQL is built from the project file's `board`, `ready_status`, and `lane_label`.** The project file has no JQL field and no `done_statuses`, and this card does not change config. `done_statuses` defaults to the project's `landed_status` and can be widened by a constructor argument.
- **GitHub's board is the project file's `board`, written `owner/number` or `number`.** The project file has no separate board owner or number field.
- **`candidates()` stays public** on both adapters, as the plan's "keep" list says, and on GitHub it now lists issues by the lane label instead of listing the board.
- **The ready rule takes `other_lanes`.** The project file names one lane label, so "two lane labels" needs the other lane words from the caller. A block whose `lane` disagrees with the label is also not ready.
- **Six new fixtures.** The native-relay fixtures carry no labels and no per issue membership answer, so those are new files beside the six copied unchanged.
- **The card shape lives in `trackers/base.py`** and the contract reads it by attribute, so `cards` imports nothing from `trackers`.
