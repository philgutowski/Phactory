"""U6 test scenarios for the project file, the operator file, and bindings.

Every test runs against a temporary SLIPWAY_HOME built from the fixtures in
tests/fixtures/config. HOME itself points at an empty temporary directory, so
a test that fell back to ~/.slipway would find nothing rather than read the
real home directory.
"""

import re
import shutil
from pathlib import Path

import pytest

from slipway.config import ConfigError, home
from slipway.config.bindings import (
    BindingInvalid,
    resolve_binding,
    resolve_tracker_credential,
)
from slipway.config.operator import AccountKind, Host, load_operator
from slipway.config.project import (
    DEFAULT_BRANCH_TEMPLATE,
    DEFAULT_PULL_REQUEST_TITLE,
    Checkout,
    LandingPath,
    MergeMethod,
    TraceDetail,
    Tracker,
    branch_namespace,
    check_names,
    load_project,
)
from slipway.hosts.base import PINNED, pinned_landing

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "config"
SLUG = "example"

# The R2 fields, by their names on Project. Each must be populated.
R2_FIELDS = [
    "repository", "host", "tracker", "board", "lane_label", "ready_status",
    "in_progress_status", "in_review_status", "landed_status", "gate_command",
    "allowed_builders", "roles", "auto_merge", "landing_path", "build_cap",
    "daily_dispatch_cap", "trivial_card_target", "trace_project", "tracker_credential",
    "host_credential", "allowed_subjects", "evals_path",
]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    fake_user_home = tmp_path / "user"
    fake_user_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_user_home))
    root = tmp_path / "slipway"
    (root / "projects").mkdir(parents=True)
    shutil.copy(FIXTURES / "operator.toml", root / "operator.toml")
    shutil.copy(FIXTURES / "project.toml", root / "projects" / f"{SLUG}.toml")
    monkeypatch.setenv("SLIPWAY_HOME", str(root))
    return root


def project_file(root: Path) -> Path:
    return root / "projects" / f"{SLUG}.toml"


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1))


def failure(call) -> ConfigError:
    with pytest.raises(ConfigError) as caught:
        call()
    return caught.value


# Where the files live.

def test_home_reads_slipway_home(isolated_home):
    assert home() == isolated_home


def test_home_defaults_under_the_user_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SLIPWAY_HOME")
    assert home() == tmp_path / "user" / ".slipway"


# Loading.

def test_complete_project_file_loads_with_every_r2_field(isolated_home):
    project = load_project(SLUG)
    for name in R2_FIELDS:
        value = getattr(project, name)
        assert value not in (None, "", (), {}), name
    assert project.slug == SLUG
    assert project.host is Host.GITHUB
    assert project.tracker is Tracker.JIRA
    assert project.landing_path is LandingPath.AUTO_MERGE_STRICT
    assert project.merge_method is MergeMethod.SQUASH
    assert project.trace_detail is TraceDetail.SUMMARY
    assert project.auto_merge is True
    assert set(project.roles) == {"builder", "judge"}
    assert project.roles["builder"].time_budget_minutes == 90
    assert project.runner_labels == ("self-hosted", "linux")
    assert project.machine_bound_tests == ("tests/test_usb_device.py",)
    assert project.builder_worker == "laptop"


def test_operator_fixture_loads(isolated_home):
    operator = load_operator()
    assert operator.operator_subject == "user_operator_example"
    assert operator.accounts["max-sub"].kind is AccountKind.SUBSCRIPTION
    assert operator.accounts["api-main"].kind is AccountKind.API_KEY
    assert operator.tracker_credentials["example-github"].email_env is None
    assert operator.runner_repository.version_file == "VERSION"
    instance = operator.runner_instances["vps"]
    assert instance.repositories[0].previous_token_env == "EXAMPLE_RUNNER_REGISTRATION_PREVIOUS"
    assert operator.webhook_secrets["example-org/example-app"].previous_env
    assert operator.builder_workers["laptop"].label == "laptop-builder"


def test_fixture_names_all_resolve_against_the_operator(isolated_home):
    check_names(load_project(SLUG), load_operator())


# Validation names the offending field.

def test_missing_lane_label_names_the_field(isolated_home):
    edit(project_file(isolated_home), 'lane_label = "cloud"\n', "")
    error = failure(lambda: load_project(SLUG))
    assert error.field == "lane_label"
    assert "lane_label" in str(error) and "missing" in str(error)


def test_token_pattern_names_the_key(isolated_home):
    token = "ghp_" + "a1B2c3D4" * 5
    edit(project_file(isolated_home), 'board = "EX"', f'board = "{token}"')
    error = failure(lambda: load_project(SLUG))
    assert error.field == "board"
    assert token not in str(error)


def test_token_inside_a_nested_table_names_the_dotted_key(isolated_home):
    edit(project_file(isolated_home), 'model = "model-large"',
         'model = "sk-ant-' + "x9" * 20 + '"')
    assert failure(lambda: load_project(SLUG)).field == "roles.builder.model"


def test_env_var_name_as_a_credential_names_the_key(isolated_home):
    edit(project_file(isolated_home), 'tracker_credential = "example-jira"',
         'tracker_credential = "EXAMPLE_JIRA_TOKEN"')
    error = failure(lambda: load_project(SLUG))
    assert error.field == "tracker_credential"
    assert "environment variable" in error.message


def test_status_labels_in_capitals_are_accepted(isolated_home):
    """Issue #37: a Jira workflow with TO_DO and IN_REVIEW statuses must validate."""
    path = project_file(isolated_home)
    edit(path, 'ready_status = "Ready"', 'ready_status = "TO_DO"')
    edit(path, 'in_review_status = "In review"', 'in_review_status = "IN_REVIEW"')
    project = load_project(SLUG)
    assert project.ready_status == "TO_DO"
    assert project.in_review_status == "IN_REVIEW"


@pytest.mark.parametrize(("old", "new", "field"), [
    ('host_credential = "example-app"', 'host_credential = "EXAMPLE_GH_TOKEN"',
     "host_credential"),
    ('trace_project = "example-traces"', 'trace_project = "LANGFUSE_SECRET_KEY"',
     "trace_project"),
    ('builder_worker = "laptop"', 'builder_worker = "LAPTOP_WORKER"', "builder_worker"),
    ('account = "max-sub"', 'account = "ANTHROPIC_API_KEY"', "roles.builder.account"),
])
def test_env_var_name_in_any_name_field_is_refused(isolated_home, old, new, field):
    edit(project_file(isolated_home), old, new)
    error = failure(lambda: load_project(SLUG))
    assert error.field == field
    assert "environment variable" in error.message


def test_unknown_key_names_the_key(isolated_home):
    edit(project_file(isolated_home), 'board = "EX"', 'board = "EX"\nboard_number = 7')
    assert failure(lambda: load_project(SLUG)).field == "board_number"


def test_bad_enum_names_the_key_and_the_choices(isolated_home):
    edit(project_file(isolated_home), 'landing_path = "auto_merge_strict"',
         'landing_path = "by_hand"')
    error = failure(lambda: load_project(SLUG))
    assert error.field == "landing_path"
    assert "merge_queue" in error.message


def test_project_file_may_pin_plain_merge(isolated_home):
    edit(project_file(isolated_home), 'landing_path = "auto_merge_strict"',
         'landing_path = "plain_merge"')
    assert load_project(SLUG).landing_path is LandingPath.PLAIN_MERGE


def test_wrong_type_names_the_key(isolated_home):
    edit(project_file(isolated_home), "build_cap = 3", 'build_cap = "three"')
    assert failure(lambda: load_project(SLUG)).field == "build_cap"


def test_missing_required_role_names_it(isolated_home):
    path = project_file(isolated_home)
    text = path.read_text()
    path.write_text(text[: text.index("[roles.judge]")])
    assert failure(lambda: load_project(SLUG)).field == "roles.judge"


def test_name_absent_from_operator_file_names_the_key(isolated_home):
    edit(project_file(isolated_home), 'host_credential = "example-app"',
         'host_credential = "someone-else"')
    error = failure(lambda: check_names(load_project(SLUG), load_operator()))
    assert error.field == "host_credential"


def test_tracker_credential_for_another_site_names_the_key(isolated_home):
    # The same file halts binding_invalid at resolve, so check_names must refuse it too.
    edit(project_file(isolated_home), 'tracker_site = "example.atlassian.net"',
         'tracker_site = "other.atlassian.net"')
    error = failure(lambda: check_names(load_project(SLUG), load_operator()))
    assert error.field == "tracker_credential"
    assert "other.atlassian.net" in error.message


@pytest.mark.parametrize(
    "old, new, field",
    [
        ('token_env = "EXAMPLE_JIRA_TOKEN"', 'tokn_env = "EXAMPLE_JIRA_TOKEN"',
         "tracker_credentials.example-jira.tokn_env"),
        ('client_id = "client_example"', 'client_id = "client_example"\nclient = "x"',
         "identity_provider.client"),
        ('kind = "subscription"', 'kind = "subscription"\nenvv = "X"',
         "accounts.max-sub.envv"),
        ('version_file = "VERSION"', 'version_file = "VERSION"\nbranch = "main"',
         "runner_repository.branch"),
        ('previous_token_env = "EXAMPLE_RUNNER_REGISTRATION_PREVIOUS"',
         'previous_token_env = "EXAMPLE_RUNNER_REGISTRATION_PREVIOUS"\ntoken = "X"',
         "runner_instances.vps.repositories[0].token"),
        ('previous_env = "EXAMPLE_APP_WEBHOOK_SECRET_PREVIOUS"',
         'previous_env = "EXAMPLE_APP_WEBHOOK_SECRET_PREVIOUS"\nrepo = "x"',
         "webhook_secrets[0].repo"),
    ],
)
def test_unknown_key_in_a_nested_operator_table_names_it(isolated_home, old, new, field):
    edit(isolated_home / "operator.toml", old, new)
    error = failure(load_operator)
    assert error.field == field
    assert "not a known key" in error.message


def test_operator_file_refuses_a_secret_value(isolated_home):
    edit(isolated_home / "operator.toml", 'env = "SLIPWAY_ACCOUNT_MAX_SUB"',
         'env = "ghp_' + "Z" * 36 + '"')
    assert failure(load_operator).field == "accounts.max-sub.env"


def test_operator_file_refuses_a_value_that_is_not_a_variable_name(isolated_home):
    edit(isolated_home / "operator.toml", 'token_env = "EXAMPLE_JIRA_TOKEN"',
         'token_env = "example jira token"')
    assert failure(load_operator).field == "tracker_credentials.example-jira.token_env"


def test_invalid_slug_is_refused(isolated_home):
    assert failure(lambda: load_project("../operator")).field == "(file)"


# Resolving at call time.

def test_binding_resolves_account_model_effort_budget(isolated_home):
    binding = resolve_binding(SLUG, "builder")
    assert binding.account.name == "max-sub"
    assert binding.account.env == "SLIPWAY_ACCOUNT_MAX_SUB"
    assert (binding.model, binding.effort, binding.time_budget_minutes) == (
        "model-large", "high", 90,
    )


def test_binding_to_an_unknown_account_is_binding_invalid(isolated_home):
    edit(project_file(isolated_home), 'account = "max-sub"', 'account = "borrowed"')
    with pytest.raises(BindingInvalid) as caught:
        resolve_binding(SLUG, "builder")
    assert caught.value.halt_class == "binding_invalid"
    assert caught.value.field == "roles.builder.account"


def test_unbound_role_is_binding_invalid(isolated_home):
    with pytest.raises(BindingInvalid) as caught:
        resolve_binding(SLUG, "helper")
    assert caught.value.field == "roles.helper"


def test_invalid_project_file_at_resolve_is_binding_invalid(isolated_home):
    edit(project_file(isolated_home), 'lane_label = "cloud"\n', "")
    with pytest.raises(BindingInvalid) as caught:
        resolve_binding(SLUG, "builder")
    assert caught.value.field == "lane_label"


def test_editing_the_model_on_disk_changes_the_next_resolve(isolated_home):
    assert resolve_binding(SLUG, "builder").model == "model-large"
    edit(project_file(isolated_home), 'model = "model-large"', 'model = "model-small"')
    assert resolve_binding(SLUG, "builder").model == "model-small"


def test_tracker_credential_resolves_from_the_environment(isolated_home):
    environ = {"EXAMPLE_JIRA_EMAIL": "ops@example.test", "EXAMPLE_JIRA_TOKEN": "t" * 24}
    credential = resolve_tracker_credential(SLUG, environ=environ)
    assert credential.site == "example.atlassian.net"
    assert credential.email == "ops@example.test"
    assert credential.token == "t" * 24
    assert "t" * 24 not in repr(credential)


def test_tracker_credential_for_another_site_is_binding_invalid(isolated_home):
    edit(project_file(isolated_home), 'tracker_site = "example.atlassian.net"',
         'tracker_site = "other.atlassian.net"')
    environ = {"EXAMPLE_JIRA_EMAIL": "ops@example.test", "EXAMPLE_JIRA_TOKEN": "t" * 24}
    with pytest.raises(BindingInvalid) as caught:
        resolve_tracker_credential(SLUG, environ=environ)
    assert caught.value.halt_class == "binding_invalid"
    assert caught.value.field == "tracker_credential"


def test_unset_tracker_token_is_binding_invalid(isolated_home):
    with pytest.raises(BindingInvalid) as caught:
        resolve_tracker_credential(SLUG, environ={"EXAMPLE_JIRA_EMAIL": "ops@example.test"})
    assert caught.value.field == "tracker_credentials.example-jira.token_env"


def test_env_name_refusal_does_not_echo_the_value(tmp_path):
    """KTD24: a pasted credential in an env slot is reported by hash and length only."""
    from slipway.config import ConfigError, Reader

    secret = "hunter2-app-password-from-bitbucket"
    reader = Reader(tmp_path / "operator.toml", {"token_env": secret}, "")
    with pytest.raises(ConfigError) as info:
        reader.env("token_env")
    assert secret not in str(info.value)
    assert "length 35" in str(info.value)


# U26: the first project's lane. lander.toml is shaped like the first
# project's file, with every value invented.

LANDER = "lander"
SRC = Path(__file__).resolve().parents[2] / "src" / "slipway"


@pytest.fixture
def lander(isolated_home):
    shutil.copy(FIXTURES / "lander.toml", isolated_home / "projects" / f"{LANDER}.toml")
    return isolated_home / "projects" / f"{LANDER}.toml"


def test_an_external_lander_project_file_loads_and_pins_the_landing(lander):
    project = load_project(LANDER)
    assert project.landing_path is LandingPath.EXTERNAL_LANDER
    assert project.checkout is Checkout.WORKTREE
    assert project.ready_status == "Backlog"
    # No host setting reveals an outside lander, so the value comes from the
    # file, which the probe takes in place of anything it could read.
    landing = pinned_landing(project)
    assert landing.path == LandingPath.EXTERNAL_LANDER and landing.path_source == PINNED


def test_the_branch_template_renders_the_projects_own_names(lander):
    project = load_project(LANDER)
    assert project.branch_for("XY-12", "Fix export", 7) == "XY-12-fix-export"
    assert project.branch_for("XY-12", "Fix the CSV export: dates & times!", 7) == (
        "XY-12-fix-the-csv-export-dates-times")
    assert project.branch_for("XY-12", "", 7) == "XY-12-card"
    assert project.title_for("XY-12", "Export keeps dates") == "XY-12: Export keeps dates"
    assert branch_namespace(project.branch_template) == ""


def test_a_long_title_is_cut_at_a_word(lander):
    project = load_project(LANDER)
    branch = project.branch_for("XY-1", "word " * 30, 1)
    assert branch.startswith("XY-1-word-word") and not branch.endswith("-")
    assert len(branch) <= len("XY-1-") + 40


def test_defaults_keep_todays_names(isolated_home):
    project = load_project(SLUG)
    assert project.branch_template == DEFAULT_BRANCH_TEMPLATE
    assert project.branch_for("EX-12", "Anything", 31) == "slipway/EX-12-31"
    assert project.pull_request_title == DEFAULT_PULL_REQUEST_TITLE
    assert project.title_for("EX-12", "Add the page") == "Add the page"
    assert project.checkout is Checkout.CLONE
    assert branch_namespace(project.branch_template) == "slipway/"


@pytest.mark.parametrize(("template", "says"), [
    ("{slug}", "must carry {key}"),
    ("feature/{slug}-{build}", "must carry {key}"),
    ("{key} {slug}", "not a legal branch name"),
    ("lane..{key}", "not a legal branch name"),
    ("{key}.lock", "not a legal branch name"),
    ("/{key}", "not a legal branch name"),
    ("-{key}", "not a legal branch name"),
    ("{key}-{title}", "may use only {key}, {slug}, {build}"),
    ("{key!r}", "may use only"),
    ("{key", "is not a template"),
])
def test_a_branch_template_without_key_or_illegal_is_refused_at_load(lander, template, says):
    edit(lander, 'branch_template = "{key}-{slug}"', f'branch_template = "{template}"')
    error = failure(lambda: load_project(LANDER))
    assert error.field == "branch_template"
    assert says in error.message


def test_a_title_template_without_the_title_is_refused(lander):
    edit(lander, 'pull_request_title = "{key}: {title}"', 'pull_request_title = "{key}"')
    error = failure(lambda: load_project(LANDER))
    assert error.field == "pull_request_title" and "{title}" in error.message


def test_an_unknown_checkout_names_the_key(lander):
    edit(lander, 'checkout = "worktree"', 'checkout = "copy"')
    assert failure(lambda: load_project(LANDER)).field == "checkout"


def test_no_value_of_the_first_projects_file_is_in_the_code(lander):
    # R1: the fixture holds the project's values; src/slipway holds none of them.
    project = load_project(LANDER)
    values = [project.repository, project.board + "-12", project.lane_label,
              project.gate_command, project.host_credential, "example-lander"]
    # And no word naming the first project or its customer, by whole word,
    # so "integration" is not a hit.
    names = re.compile(r"\b(integra|workbench|integrel)\b", re.IGNORECASE)
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        for value in values:
            assert value not in text, f"{value!r} in {path}"
        assert not names.search(text), f"{names.search(text)[0]!r} in {path}"
