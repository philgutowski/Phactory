"""U6 test scenarios for the project file, the operator file, and bindings.

Every test runs against a temporary SLIPWAY_HOME built from the fixtures in
tests/fixtures/config. HOME itself points at an empty temporary directory, so
a test that fell back to ~/.slipway would find nothing rather than read the
real home directory.
"""

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
    LandingPath,
    MergeMethod,
    TraceDetail,
    Tracker,
    check_names,
    load_project,
)

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
