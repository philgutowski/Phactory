"""Apply the SQL files under records/schema to the slipway database (KTD16).

A migration is one numbered SQL file, such as 001_init.sql. The ledger is the
schema_migrations table: one row per applied file with its version, its
checksum (the SHA 256 of the file's bytes), and when it was applied.

Rules, in the order apply() enforces them:

1. Files are applied in version order, each at most once. A version already
   in the ledger is skipped, which makes a second run a no op.
2. Migrations are forward only. A file whose version is absent from the
   ledger but below the highest version the ledger holds is refused with
   both versions named, so a lower numbered file that merged after a higher
   one was applied is never run out of order. Renumber it above the highest.
3. A version in the ledger whose file has changed since is refused with both
   checksums named. A fix is a new file.
4. Each file runs in one transaction with a short lock_timeout, so a file
   that waits on a busy table fails fast instead of queueing every query
   behind it, and the ledger row commits with the file or not at all.
5. A file whose first line is NO_TRANSACTION_MARKER runs statement by
   statement outside a transaction, which CREATE INDEX CONCURRENTLY needs.
   Its ledger row is written after its last statement. Such a file is not
   atomic: when its second statement fails, the first stays applied and no
   ledger row is written, so the next run starts the file again from the
   top. Every statement in it must therefore say IF NOT EXISTS, which
   discover() checks, so the rerun skips what already landed. One caveat:
   a CREATE INDEX CONCURRENTLY that fails leaves an invalid index behind
   under its name, which IF NOT EXISTS would then skip; drop that index by
   hand before the rerun.
6. A session advisory lock serialises two migrators on one database.

Slipway's records live in their own database beside the engine's. apply()
takes the database name it is meant for and refuses any other, so a
connection string pointed at the engine's database cannot be migrated by
mistake.
"""

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
NO_TRANSACTION_MARKER = "/* slipway: no transaction */"
LOCK_TIMEOUT = "5s"
MIGRATE_LOCK_KEY = 0x70686163  # an arbitrary constant naming the migrator's advisory lock

_NAME = re.compile(r"^(\d+)_[a-z0-9_]+\.sql$")
_IF_NOT_EXISTS = re.compile(r"\bIF\s+NOT\s+EXISTS\b", re.IGNORECASE)

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    name text NOT NULL,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    """A migration could not be applied as written."""


class ChecksumMismatch(MigrationError):
    """An applied migration's file no longer matches the checksum in the ledger."""


class OutOfOrder(MigrationError):
    """A pending migration is numbered below one the ledger already holds."""


class WrongDatabase(MigrationError):
    """The connection points at a database the migrator is not meant for."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str
    checksum: str

    @property
    def transactional(self) -> bool:
        first = self.sql.lstrip().splitlines()[0] if self.sql.strip() else ""
        return first.strip() != NO_TRANSACTION_MARKER


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover(schema_dir: Path = SCHEMA_DIR) -> list[Migration]:
    """Every migration file under schema_dir, in version order."""
    found: dict[str, Migration] = {}
    for path in sorted(schema_dir.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise MigrationError(f"{path.name} is not named <number>_<name>.sql")
        version = match.group(1)
        if version in found:
            raise MigrationError(f"version {version} is used by {found[version].name} "
                                 f"and {path.name}")
        data = path.read_bytes()
        m = Migration(version, path.name, data.decode(), checksum(data))
        if not m.transactional:
            _require_if_not_exists(m)
        found[version] = m
    return sorted(found.values(), key=lambda m: int(m.version))


def _require_if_not_exists(m: Migration) -> None:
    for statement in split_statements(m.sql):
        if not _IF_NOT_EXISTS.search(statement):
            first = statement.splitlines()[0]
            raise MigrationError(f"{m.name} runs outside a transaction, so every statement "
                                 f"must say IF NOT EXISTS; this one does not: {first}")


def pending(migrations: list[Migration], ledger: dict[str, str]) -> list[Migration]:
    """The migrations still to apply, given the ledger as {version: checksum}.

    Raises ChecksumMismatch for an applied version whose file has changed,
    and OutOfOrder for an unapplied version below the ledger's highest.
    """
    highest = max(ledger, key=int, default=None)
    todo = []
    for m in migrations:
        recorded = ledger.get(m.version)
        if recorded is None:
            if highest is not None and int(m.version) < int(highest):
                raise OutOfOrder(
                    f"migration {m.name} (version {m.version}) is not applied but the ledger "
                    f"already holds version {highest}; migrations are forward only, so "
                    f"renumber it above {highest}")
            todo.append(m)
        elif recorded != m.checksum:
            raise ChecksumMismatch(
                f"migration {m.name} was applied with checksum {recorded} "
                f"but the file now has checksum {m.checksum}; write a new migration instead")
    return todo


def split_statements(sql: str) -> list[str]:
    """Split a no transaction file into statements on a semicolon ending a line.

    Such files hold index builds and similar single statements; they carry no
    function bodies, so this simple split is enough.
    """
    body = sql.replace(NO_TRANSACTION_MARKER, "", 1)
    parts = re.split(r";[ \t]*(?:\n|$)", body)
    return [p.strip() for p in parts if p.strip()]


def apply(conn, database: str | None = None,
          schema_dir: Path = SCHEMA_DIR) -> list[str]:
    """Apply pending migrations on an autocommit psycopg connection.

    Returns the versions applied by this call. With database set, refuses a
    connection whose current database has another name.
    """
    if not conn.autocommit:
        raise MigrationError("apply needs an autocommit connection")
    if database is not None:
        current = conn.execute("SELECT current_database()").fetchone()[0]
        if current != database:
            raise WrongDatabase(f"refusing to migrate database {current!r}; "
                                f"expected {database!r}")
    migrations = discover(schema_dir)
    conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATE_LOCK_KEY,))
    try:
        conn.execute(LEDGER_DDL)
        ledger = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
        applied = []
        for m in pending(migrations, ledger):
            _apply_one(conn, m)
            applied.append(m.version)
        return applied
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATE_LOCK_KEY,))


def _record(conn, m: Migration) -> None:
    conn.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                 (m.version, m.name, m.checksum))


def _apply_one(conn, m: Migration) -> None:
    if m.transactional:
        with conn.transaction():
            conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            conn.execute(m.sql)
            _record(conn, m)
        return
    conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    try:
        for statement in split_statements(m.sql):
            conn.execute(statement)
        _record(conn, m)
    finally:
        conn.execute("RESET lock_timeout")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m slipway.records.migrate",
                                     description="Apply Slipway's record migrations.")
    parser.add_argument("dsn", help="connection string for the records database")
    parser.add_argument("--database", default="slipway",
                        help="refuse unless the connection's database has this name")
    args = parser.parse_args(argv)
    import psycopg

    with psycopg.connect(args.dsn, autocommit=True) as conn:
        try:
            applied = apply(conn, database=args.database)
        except MigrationError as exc:
            print(f"migrate: {exc}", file=sys.stderr)
            return 1
    print("applied: " + (", ".join(applied) if applied else "nothing, ledger is current"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
