"""The onboarding questions, as data (U19, KTD15, R29).

Each Question is one thing onboard asks, in the order the list below gives.
The machine (machine.py) walks the list, asks the first question that is
missing or no longer true, validates the answer, and saves it. Nothing here
reads a file or a network; the machine passes in what a question needs.

A question carries:

    id             the name a caller answers it by, such as "board"
    text           what is asked, in one sentence
    why            one line on why it is asked
    kind           how an answer is read: TEXT, CHOICE, LIST, NUMBER, YES_NO
    field          the project file key it fills, dotted for a role
                   ("roles.builder.account"), or None when the answer stays
                   in onboard's own state (an attestation, say)
    choices_from   where its choices come from, or None for free text. A
                   source is either fixed (the Host enum), the operator file
                   (accounts by name), or live from the adapters (boards,
                   labels, statuses, builders), which the probe lists
    free_text      whether an answer outside the choices is taken
    default        what an empty answer means, or None when there is none.
                   For a LIST question with a default, "no" means the
                   default too, so an opt in question is declined in a word
    operator_only  KTD15: only the operator's subject answers it, and only
                   the operator is shown its choices
    required       False for an optional key: an empty answer leaves it out
    command        a command the project runs on its own host; Slipway stores
                   it and never runs it (KTD18)

An attestation is a statement a person makes and onboard records with who
made it and when, for a property Slipway cannot read for itself (R30).
"""

from collections.abc import Mapping
from dataclasses import dataclass

from slipway.config.operator import Host
from slipway.config.project import (
    DEFAULT_BUILDER_TOOLS,
    BuilderTool,
    Checkout,
    LandingPath,
    MergeMethod,
    TraceDetail,
    Tracker,
)

TEXT, CHOICE, LIST, NUMBER, YES_NO = "text", "choice", "list", "number", "yes_no"

# Choice sources. FIXED ones are enums; OPERATOR ones name a table of the
# operator file; LIVE ones are listed by the probe (checks.py).
HOSTS, TRACKERS = "hosts", "trackers"
LANDING_PATHS, MERGE_METHODS, TRACE_DETAILS = "landing_paths", "merge_methods", "trace_details"
CHECKOUTS, BUILDER_TOOLS = "checkouts", "builder_tools"
ACCOUNTS, LANGFUSE_PROJECTS = "accounts", "langfuse_projects"
TRACKER_CREDENTIALS, HOST_CREDENTIALS = "tracker_credentials", "host_credentials"
TRACKER_SITES, BUILDER_WORKERS = "tracker_sites", "builder_workers"
BOARDS, LABELS, STATUSES, BUILDERS = "boards", "labels", "statuses", "builders"

FIXED = {
    HOSTS: tuple(h.value for h in Host),
    TRACKERS: tuple(t.value for t in Tracker),
    LANDING_PATHS: tuple(p.value for p in LandingPath),
    MERGE_METHODS: tuple(m.value for m in MergeMethod),
    TRACE_DETAILS: tuple(d.value for d in TraceDetail),
    CHECKOUTS: tuple(c.value for c in Checkout),
    BUILDER_TOOLS: tuple(t.value for t in BuilderTool),
}
OPERATOR = (ACCOUNTS, LANGFUSE_PROJECTS, TRACKER_CREDENTIALS, HOST_CREDENTIALS, TRACKER_SITES,
            BUILDER_WORKERS)
LIVE = (BOARDS, LABELS, STATUSES, BUILDERS)

YES, NO = "yes", "no"


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    why: str
    kind: str = TEXT
    field: str | None = None
    choices_from: str | None = None
    free_text: bool = True
    default: object = None
    operator_only: bool = False
    required: bool = True
    command: bool = False
    # Asked only when a check names it, never in the walk (see EXTRA).
    on_demand: bool = False


def _q(id, text, why, **kw) -> Question:
    kw.setdefault("field", id)
    return Question(id, text, why, **kw)


def _role(role: str, budget: int, effort: str) -> list[Question]:
    name = f"roles.{role}"
    return [
        _q(f"{name}.account", f"Which operator account pays for the {role}?",
           "An account is spent by every session in this role; only the operator moves spend.",
           kind=CHOICE, choices_from=ACCOUNTS, free_text=False, operator_only=True),
        _q(f"{name}.model", f"Which model does the {role} run?",
           "The handoff names the model the session starts with."),
        _q(f"{name}.effort", f"What effort does the {role} run at?",
           "Effort trades time for depth; the binding records it.", default=effort),
        _q(f"{name}.time_budget_minutes", f"How many minutes may one {role} session take?",
           "The workflow ends a session that runs past its budget.", kind=NUMBER,
           default=budget),
    ]


# The walk, in the order the board lanes "up" checklist sets it up: where the
# code lives, where the cards live, how the board moves, how a change is
# proved, who builds and pays, how it lands, then what onboard cannot read.
QUESTIONS: tuple[Question, ...] = (
    _q("host", "Which host holds the repository?",
       "The host adapter opens pull requests and reads the gate there.",
       kind=CHOICE, choices_from=HOSTS, free_text=False, operator_only=True),
    _q("repository", "What is the repository, as owner/name (a workspace/slug on Bitbucket)?",
       "Every branch and pull request is made here.", operator_only=True),
    _q("host_credential", "Which host credential does Slipway use for this repository?",
       "The machine identity that pushes branches and opens pull requests (KTD6).",
       kind=CHOICE, choices_from=HOST_CREDENTIALS, free_text=False, operator_only=True),
    _q("tracker", "Which tracker holds the cards?",
       "The tracker adapter reads cards there and never writes.",
       kind=CHOICE, choices_from=TRACKERS, free_text=False, operator_only=True),
    _q("tracker_site", "What is the tracker's site, such as name.atlassian.net or github.com?",
       "A tracker credential is good for one site.", kind=CHOICE,
       choices_from=TRACKER_SITES, operator_only=True),
    _q("tracker_credential", "Which tracker credential reads the board?",
       "Slipway reads cards with it and nothing else.",
       kind=CHOICE, choices_from=TRACKER_CREDENTIALS, free_text=False, operator_only=True),
    _q("board", "Which board holds the cards?",
       "A Jira project key, or a GitHub Projects board as owner/number or number.",
       kind=CHOICE, choices_from=BOARDS, operator_only=True),
    _q("lane_label", "Which label marks a card for Slipway?",
       "A card is dispatched only when it carries this label and no other lane's.",
       kind=CHOICE, choices_from=LABELS),
    _q("ready_status", "Which status means a card is ready to build?",
       "Watch dispatches cards in this status.", kind=CHOICE, choices_from=STATUSES,
       default="Ready"),
    _q("in_progress_status", "Which status does a card move to once its branch exists?",
       "The board's own automation moves it there (KTD27).", kind=CHOICE,
       choices_from=STATUSES, default="In Progress"),
    _q("in_review_status", "Which status does a card move to once its pull request opens?",
       "The board's own automation moves it there (KTD27).", kind=CHOICE,
       choices_from=STATUSES, default="In Review"),
    _q("landed_status", "Which status means a card is done?",
       "Verify reads this status after the merge.", kind=CHOICE, choices_from=STATUSES,
       default="Done"),
    _q("other_dispatcher", "Does another system dispatch this board's cards today?",
       "Two dispatchers on one board build the same card twice; onboard will not finish "
       "while another one runs.", kind=YES_NO, field=None, free_text=False, default=NO),
    _q("gate_command", "What command does the project's gate run on its own host?",
       "Stored for the record and the handoff; Slipway never runs it (KTD18).",
       command=True),
    _q("allowed_builders", "Which builders may build this project's cards?",
       "Dispatch refuses a builder the project does not allow.",
       kind=LIST, choices_from=BUILDERS, free_text=False),
    *_role("builder", 90, "high"),
    *_role("judge", 10, "medium"),
    _q("builder_worker", "Which builder worker runs this project's laptop builds, if any?",
       "A worker is a machine the operator holds; leave empty for none.",
       kind=CHOICE, choices_from=BUILDER_WORKERS, free_text=False, operator_only=True,
       required=False),
    _q("checkout", "How does a laptop build get its working copy? Leave empty for a fresh "
       "clone.", "A worktree is a second working folder added from the laptop's own checkout.",
       kind=CHOICE, choices_from=CHECKOUTS, free_text=False, required=False),
    # The opt in (U27). The trade off is in the question itself, because the
    # answer hands a build the operator's own sign ins.
    _q("builder_tools", "A builder always has the headless browser, a browser with no window "
       "that runs inside its own session. Should a laptop build also drive your own signed in "
       "Chrome (operator_browser) or your screen (desktop)? Either one acts as you, outside "
       "the build's dedicated user, wherever you are signed in. Answer no to keep the "
       "headless browser alone, or list the tools to grant.",
       "Each is off unless you list it here, is refused for a container build, and is named "
       "on every build that has it.", kind=LIST, choices_from=BUILDER_TOOLS, free_text=False,
       default=DEFAULT_BUILDER_TOOLS, operator_only=True),
    _q("auto_merge", "May the host merge a pull request on its own once the gate is green?",
       "Auto merge lands without a person; without it a person approves each one.",
       kind=YES_NO, free_text=False, default=NO, operator_only=True),
    _q("landing_path", "Pin the landing path? Leave empty to read it from the host.",
       "Bitbucket exposes no queue setting, so a Bitbucket project pins it.",
       kind=CHOICE, choices_from=LANDING_PATHS, free_text=False, operator_only=True,
       required=False),
    _q("merge_method", "Pin the merge method? Leave empty to read it from the host.",
       "Verify relates the merged commit to the confirmed one by this method.",
       kind=CHOICE, choices_from=MERGE_METHODS, free_text=False, operator_only=True,
       required=False),
    _q("branch_template", "What should a card's branch be called? Use {key}, {slug}, and "
       "{build}; leave empty for slipway/{key}-{build}.",
       "The text before the first placeholder is the namespace the machine identity is held "
       "to (KTD6), and the board finds the card by {key}.", required=False),
    _q("pull_request_title", "What should a pull request be titled? Use {key} and {title}; "
       "leave empty for the card's title.", "The tracker's own token is still put in front "
       "when the title does not carry it.", required=False),
    _q("build_cap", "How many builds may run at once?",
       "The cap holds spend and review load.", kind=NUMBER, default=2, operator_only=True),
    _q("daily_dispatch_cap", "How many builds may start in one day (UTC)?",
       "A looping caller cannot spend a day's budget in an hour.", kind=NUMBER, default=10,
       operator_only=True),
    _q("trace_project", "Which Langfuse project receives the traces?",
       "A trace is exported under the operator's own keys.",
       kind=CHOICE, choices_from=LANGFUSE_PROJECTS, free_text=False, operator_only=True),
    _q("trace_detail", "How much of each trace is exported?",
       "summary keeps transcripts out of the third party store.",
       kind=CHOICE, choices_from=TRACE_DETAILS, free_text=False, default="summary",
       operator_only=True),
    _q("allowed_subjects", "Which subjects may use this project, besides the operator?",
       "A subject is the stable user id in a token; only these reach the project.",
       kind=LIST, operator_only=True),
    _q("evals_path", "Where do the project's evals live in the repository?",
       "Eval cards add cases there.", default="evals/"),
    _q("trivial_card_target", "Which file should the trivial onboarding card add?",
       "Onboard ends by landing one tiny card, so the first real card is not the first test.",
       default="docs/slipway-onboarding.md"),
    _q("runner_labels", "Which runner labels does the gate run on, if any?",
       "Only for a project that opts into Slipway's own gate runner; leave empty for none.",
       kind=LIST, required=False),
    _q("machine_bound_tests", "Which tests can only run on a particular machine, if any?",
       "They are skipped in a sandbox and named in the handoff; leave empty for none.",
       kind=LIST, required=False),
    _q("independence", "Can each card be built and landed on its own, with no other card's "
       "branch in it?", "Independent tasks is one of the four properties (R30); this is "
       "your attestation, recorded with your identity.", kind=YES_NO, field=None,
       free_text=False),
    _q("verification", "Does every card carry a written definition of done that a reviewer "
       "can check?", "A written definition of verification is one of the four properties "
       "(R30); this is your attestation, recorded with your identity.", kind=YES_NO,
       field=None, free_text=False),
)

# Asked only when a step names them: after a failing check, to confirm a
# property Slipway cannot read, and once the trivial card is filed.
RECHECK = Question(
    "recheck", "Ready for onboard to check again?",
    "Answer yes once the next step is done.", kind=YES_NO, free_text=False, default=YES,
    on_demand=True)
TRIVIAL_CARD = Question(
    "trivial_card", "What key did the tracker give the trivial card you filed?",
    "Onboard follows that card until it scores; then the project is verified.",
    on_demand=True)
ATTEST_PREFIX = "attest."

BY_ID: dict[str, Question] = {q.id: q for q in (*QUESTIONS, RECHECK, TRIVIAL_CARD)}


def attestation(check: str, what: str) -> Question:
    """The question that stands in for a probe the host gives no way to read."""
    return Question(
        f"{ATTEST_PREFIX}{check}", f"Slipway cannot read this on the host: {what}. "
        "Is it in place?", "Your attestation is recorded with your identity in place of the "
        "probe (R30).", kind=YES_NO, free_text=False, operator_only=True, on_demand=True)


def find(qid: str, attest: Mapping[str, str] | None = None) -> Question | None:
    """The question by id; an attestation question by its check name."""
    if qid in BY_ID:
        return BY_ID[qid]
    if qid.startswith(ATTEST_PREFIX) and attest and qid[len(ATTEST_PREFIX):] in attest:
        return attestation(qid[len(ATTEST_PREFIX):], attest[qid[len(ATTEST_PREFIX):]])
    return None


def by_field(field: str) -> Question | None:
    """The question that fills a project file key; a role's key matches its own question."""
    for question in QUESTIONS:
        if question.field and (field == question.field
                               or field.startswith(question.field + ".")):
            return question
    return None
