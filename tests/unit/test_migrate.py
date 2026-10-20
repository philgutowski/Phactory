"""U7 migration ledger logic that needs no database.

The integration suite (tests/integration/test_records.py) applies the real
files to Postgres; these tests hold the rules that decide what to apply.
"""

from pathlib import Path

import pytest

from slipway.records import migrate
from slipway.records.migrate import (
    NO_TRANSACTION_MARKER,
    ChecksumMismatch,
    MigrationError,
    checksum,
    discover,
    pending,
    split_statements,
)


def _schema(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_shipped_schema_is_discovered_in_order():
    found = discover()
    assert [m.version for m in found][:1] == ["001"]
    assert found[0].name == "001_init.sql"
    assert found[0].transactional


def test_shipped_sql_carries_no_double_hyphen():
    # SQL line comments start with two hyphens; the schema uses block comments
    # so the repository's punctuation rule holds in every file.
    for m in discover():
        assert "--" not in m.sql, m.name


def test_versions_sort_numerically(tmp_path):
    root = _schema(tmp_path, {"010_b.sql": "SELECT 1;", "002_a.sql": "SELECT 1;",
                              "001_init.sql": "SELECT 1;"})
    assert [m.version for m in discover(root)] == ["001", "002", "010"]


def test_badly_named_file_is_refused(tmp_path):
    root = _schema(tmp_path, {"init.sql": "SELECT 1;"})
    with pytest.raises(MigrationError, match="init.sql"):
        discover(root)


def test_duplicate_version_is_refused(tmp_path):
    root = _schema(tmp_path, {"001_a.sql": "SELECT 1;", "001_b.sql": "SELECT 2;"})
    with pytest.raises(MigrationError, match="version 001"):
        discover(root)


def test_applied_version_is_skipped(tmp_path):
    root = _schema(tmp_path, {"001_init.sql": "SELECT 1;", "002_more.sql": "SELECT 2;"})
    found = discover(root)
    ledger = {"001": found[0].checksum}
    assert [m.version for m in pending(found, ledger)] == ["002"]
    assert pending(found, {m.version: m.checksum for m in found}) == []


def test_changed_file_is_refused_with_both_checksums(tmp_path):
    root = _schema(tmp_path, {"001_init.sql": "SELECT 2;"})
    found = discover(root)
    old = checksum(b"SELECT 1;")
    with pytest.raises(ChecksumMismatch) as caught:
        pending(found, {"001": old})
    assert old in str(caught.value)
    assert found[0].checksum in str(caught.value)
    assert "001_init.sql" in str(caught.value)


def test_no_transaction_marker(tmp_path):
    root = _schema(tmp_path, {
        "001_init.sql": "CREATE TABLE t (a int);",
        "002_index.sql": f"{NO_TRANSACTION_MARKER}\n"
                         "CREATE INDEX CONCURRENTLY t_a ON t (a);\n"
                         "CREATE INDEX CONCURRENTLY t_a2 ON t (a);\n",
    })
    first, second = discover(root)
    assert first.transactional
    assert not second.transactional
    assert split_statements(second.sql) == [
        "CREATE INDEX CONCURRENTLY t_a ON t (a)",
        "CREATE INDEX CONCURRENTLY t_a2 ON t (a)",
    ]


def test_apply_needs_autocommit():
    class Conn:
        autocommit = False

    with pytest.raises(MigrationError, match="autocommit"):
        migrate.apply(Conn())
