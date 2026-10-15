"""Pytest plugin that makes the integration suite required under CI.

An integration test is any test under tests/integration/ or marked
integration. It skips when its database is missing, which is right on a
laptop and wrong in the gate: a gate that skips the suite reports green
without having tested anything. So when the CI environment variable is set
(GitHub Actions sets CI=true) or the require integration option is passed,
a skipped integration test is reported as a failure instead.

pyproject.toml loads this module with "-p _gate".
"""

import os

import pytest

FLAG = "--require-integration"


def pytest_addoption(parser):
    parser.addoption(
        FLAG,
        action="store_true",
        default=False,
        help="fail any skipped integration test (on by default when CI is set)",
    )


def _required(config) -> bool:
    ci = os.environ.get("CI", "").strip().lower()
    return config.getoption(FLAG) or ci not in ("", "0", "false", "no")


def _is_integration(item) -> bool:
    return (
        item.get_closest_marker("integration") is not None
        or item.path.parent.name == "integration"
    )


def pytest_collection_modifyitems(config, items):
    for item in items:
        if _is_integration(item):
            item.add_marker(pytest.mark.integration)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.skipped and _required(item.config) and _is_integration(item):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
        report.outcome = "failed"
        report.longrepr = (
            f"integration test skipped while the integration suite is required: {reason}"
        )
