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
    OutOfOrder,
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
    assert [m.version for m in found][:3] == ["001", "002", "003"]
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
                         "CREATE INDEX CONCURRENTLY IF NOT EXISTS t_a ON t (a);\n"
                         "create index concurrently if not exists t_a2 on t (a);\n",
    })
    first, second = discover(root)
    assert first.transactional
    assert not second.transactional
    assert split_statements(second.sql) == [
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS t_a ON t (a)",
        "create index concurrently if not exists t_a2 on t (a)",
    ]


def test_no_transaction_statement_without_if_not_exists_is_refused(tmp_path):
    # A rerun after a partial failure starts the file from the top, so every
    # statement must be safe to run twice.
    root = _schema(tmp_path, {
        "001_init.sql": "CREATE TABLE t (a int);",
        "002_index.sql": f"{NO_TRANSACTION_MARKER}\n"
                         "CREATE INDEX CONCURRENTLY IF NOT EXISTS t_a ON t (a);\n"
                         "CREATE INDEX CONCURRENTLY t_b ON t (a);\n",
    })
    with pytest.raises(MigrationError, match="002_index.sql") as caught:
        discover(root)
    assert "IF NOT EXISTS" in str(caught.value)
    assert "t_b" in str(caught.value)


def test_transactional_file_needs_no_if_not_exists(tmp_path):
    root = _schema(tmp_path, {"001_init.sql": "CREATE TABLE t (a int);"})
    assert discover(root)[0].transactional


def test_lower_pending_version_is_refused_with_both_versions(tmp_path):
    root = _schema(tmp_path, {"001_init.sql": "SELECT 1;", "002_late.sql": "SELECT 2;",
                              "003_more.sql": "SELECT 3;"})
    found = discover(root)
    ledger = {"001": found[0].checksum, "003": found[2].checksum}
    with pytest.raises(OutOfOrder) as caught:
        pending(found, ledger)
    message = str(caught.value)
    assert "002_late.sql" in message
    assert "version 002" in message
    assert "version 003" in message


def test_highest_is_compared_as_a_number(tmp_path):
    # "10" sorts below "9" as text; as versions, 10 is the higher one.
    root = _schema(tmp_path, {"9_a.sql": "SELECT 1;", "10_b.sql": "SELECT 2;",
                              "11_c.sql": "SELECT 3;"})
    found = discover(root)
    ledger = {"9": found[0].checksum, "10": found[1].checksum}
    assert [m.version for m in pending(found, ledger)] == ["11"]
    with pytest.raises(OutOfOrder, match="version 10"):
        pending(found, {"10": found[1].checksum})


def test_empty_ledger_applies_everything(tmp_path):
    root = _schema(tmp_path, {"001_init.sql": "SELECT 1;", "002_more.sql": "SELECT 2;"})
    assert [m.version for m in pending(discover(root), {})] == ["001", "002"]


def test_apply_needs_autocommit():
    class Conn:
        autocommit = False

    with pytest.raises(MigrationError, match="autocommit"):
        migrate.apply(Conn())
