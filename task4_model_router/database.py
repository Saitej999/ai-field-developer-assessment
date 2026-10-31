"""On-disk SQLite storage and atomic transactions for token reservations."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class RateLimiterStorageError(RuntimeError):
    """Storage is unavailable; callers must fail closed."""


class SQLiteUsageStore:
    """Open one SQLite connection per operation, safe across threads/processes."""

    def __init__(self, path: str | Path, *, busy_timeout_seconds: float = 5.0) -> None:
        self.path = Path(path)
        if str(self.path) == ":memory:":
            raise ValueError("An on-disk SQLite path is required")
        if busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be positive")
        self.busy_timeout_seconds = busy_timeout_seconds
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
        return connection

    def _initialize(self) -> None:
        try:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS token_reservations (
                        reservation_id TEXT PRIMARY KEY,
                        tenant_hash TEXT NOT NULL,
                        request_id TEXT,
                        created_at REAL NOT NULL,
                        reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens > 0),
                        usage_tokens INTEGER NOT NULL CHECK (usage_tokens >= 0),
                        actual_tokens INTEGER CHECK (actual_tokens >= 0),
                        status TEXT NOT NULL CHECK (status IN ('active', 'settled', 'cancelled')),
                        execution_state TEXT NOT NULL DEFAULT 'unclaimed',
                        request_fingerprint TEXT,
                        UNIQUE (tenant_hash, request_id)
                    )"""
                )
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(token_reservations)")
                }
                if "execution_state" not in columns:
                    connection.execute(
                        "ALTER TABLE token_reservations ADD COLUMN execution_state "
                        "TEXT NOT NULL DEFAULT 'unclaimed'"
                    )
                if "request_fingerprint" not in columns:
                    connection.execute(
                        "ALTER TABLE token_reservations ADD COLUMN request_fingerprint TEXT"
                    )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_token_reservations_tenant_time "
                    "ON token_reservations (tenant_hash, created_at)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_token_reservations_time "
                    "ON token_reservations (created_at)"
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        except sqlite3.Error:
            raise RateLimiterStorageError("Rate limiter storage unavailable") from None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize read-check-write operations with BEGIN IMMEDIATE."""
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
        except sqlite3.Error:
            raise RateLimiterStorageError("Rate limiter storage unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def cleanup_expired(connection: sqlite3.Connection, cutoff: float) -> int:
        cursor = connection.execute(
            "DELETE FROM token_reservations WHERE created_at <= ?", (cutoff,)
        )
        return cursor.rowcount

    @staticmethod
    def usage_for_tenant(connection: sqlite3.Connection, tenant_hash: str, cutoff: float) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(usage_tokens), 0) AS total "
            "FROM token_reservations WHERE tenant_hash = ? AND created_at > ?",
            (tenant_hash, cutoff),
        ).fetchone()
        return int(row["total"])

    @staticmethod
    def get_by_request_id(connection: sqlite3.Connection, tenant_hash: str, request_id: str):
        return connection.execute(
            "SELECT * FROM token_reservations WHERE tenant_hash = ? AND request_id = ?",
            (tenant_hash, request_id),
        ).fetchone()

    @staticmethod
    def get_by_reservation_id(connection: sqlite3.Connection, tenant_hash: str, reservation_id: str):
        return connection.execute(
            "SELECT * FROM token_reservations WHERE tenant_hash = ? AND reservation_id = ?",
            (tenant_hash, reservation_id),
        ).fetchone()
