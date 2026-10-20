"""The integration suite's first member: the local Postgres answers.

Every later integration test reads the same SLIPWAY_TEST_DSN. It skips when
the variable or psql is missing, and tests/_gate.py turns that skip into a
failure under CI, so the gate never passes without a database.
"""

import os
import shutil
import subprocess

import pytest


@pytest.fixture
def dsn() -> str:
    value = os.environ.get("SLIPWAY_TEST_DSN", "")
    if not value:
        pytest.skip("SLIPWAY_TEST_DSN is not set")
    if shutil.which("psql") is None:
        pytest.skip("psql is not installed")
    return value


def test_postgres_answers(dsn):
    out = subprocess.run(
        ["psql", dsn, "-X", "-t", "-A", "-c", "select 1 + 1"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert out == "2"
