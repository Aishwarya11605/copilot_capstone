"""Local SQLite storage setup and startup loading for HabitFlow."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypeVar

CURRENT_SCHEMA_VERSION = 1
_TABLES_CURRENT = {"habits", "completions", "habit_status_history"}
_WriteResult = TypeVar("_WriteResult")


class StorageError(Exception):
    """Base error for a store that could not be safely loaded."""


class StorageUnavailableError(StorageError):
    """The approved local store path could not be accessed."""


class InvalidStoreError(StorageError):
    """An existing database is invalid or has an unsupported schema."""


class StorageOperationError(StorageError):
    """A persistence operation failed and its transaction was rolled back."""


class StorageOutcomeUnknownError(StorageError):
    """A persistence operation may have committed and must be reconciled."""


@dataclass(frozen=True)
class HabitRecord:
    habit_id: str
    name: str
    created_local_date: str


@dataclass(frozen=True)
class CompletionRecord:
    habit_id: str
    local_date: str


@dataclass(frozen=True)
class ActiveStatusRecord:
    habit_id: str
    local_date: str
    is_active: bool


@dataclass(frozen=True)
class StoreData:
    habits: tuple[HabitRecord, ...]
    completions: tuple[CompletionRecord, ...]
    active_status_history: tuple[ActiveStatusRecord, ...]


def default_database_path() -> Path:
    """Return the approved per-user database path, without a fallback."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise StorageUnavailableError(
            "The Windows local application-data location is unavailable."
        )
    return Path(local_app_data) / "HabitFlow" / "habitflow.sqlite3"


def _validate_date(value: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidStoreError("The database contains an invalid local date.") from exc
    if parsed.isoformat() != value:
        raise InvalidStoreError("The database contains an invalid local date.")


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _table_info(connection: sqlite3.Connection, table: str) -> list[tuple]:
    return list(connection.execute(f"PRAGMA table_info({table})"))


def _column_layout(connection: sqlite3.Connection, table: str) -> tuple[tuple, ...]:
    return tuple(
        (row[1], row[2].upper(), row[3])
        for row in _table_info(connection, table)
    )


def _primary_key_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    columns = _table_info(connection, table)
    return tuple(
        row[1]
        for row in sorted((row for row in columns if row[5]), key=lambda row: row[5])
    )


def _has_foreign_key(
    connection: sqlite3.Connection, table: str, column: str
) -> bool:
    return any(
        row[2] == "habits"
        and row[3] == column
        and row[4] == "id"
        and row[6] == "CASCADE"
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    )


def _has_check(connection: sqlite3.Connection, table: str, *fragments: str) -> bool:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if row is None or row[0] is None:
        return False
    sql = " ".join(row[0].lower().split())
    return all(fragment in sql for fragment in fragments)


def _validate_base_schema(connection: sqlite3.Connection) -> None:
    if _table_names(connection) != _TABLES_CURRENT:
        raise InvalidStoreError("The database schema is invalid.")
    unexpected_schema_objects = connection.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type IN ('index', 'trigger', 'view') AND sql IS NOT NULL
           LIMIT 1"""
    ).fetchone()
    if unexpected_schema_objects:
        raise InvalidStoreError("The database schema is invalid.")

    if _column_layout(connection, "habits") != (
        ("id", "TEXT", 1),
        ("name", "TEXT", 1),
        ("created_local_date", "TEXT", 1),
    ):
        raise InvalidStoreError("The database schema is invalid.")
    if _primary_key_columns(connection, "habits") != ("id",):
        raise InvalidStoreError("The database schema is invalid.")

    if _column_layout(connection, "completions") != (
        ("habit_id", "TEXT", 1),
        ("local_date", "TEXT", 1),
    ):
        raise InvalidStoreError("The database schema is invalid.")
    if _primary_key_columns(connection, "completions") != ("habit_id", "local_date"):
        raise InvalidStoreError("The database schema is invalid.")
    if not _has_foreign_key(connection, "completions", "habit_id"):
        raise InvalidStoreError("The database schema is invalid.")
    for table in _TABLES_CURRENT:
        for index in connection.execute(f"PRAGMA index_list({table})"):
            if index[2] and index[3] != "pk":
                raise InvalidStoreError(
                    "The database schema contains an unapproved unique constraint."
                )

    if _column_layout(connection, "habit_status_history") != (
        ("habit_id", "TEXT", 1),
        ("local_date", "TEXT", 1),
        ("is_active", "INTEGER", 1),
    ):
        raise InvalidStoreError("The database schema is invalid.")
    if _primary_key_columns(connection, "habit_status_history") != (
        "habit_id",
        "local_date",
    ):
        raise InvalidStoreError("The database schema is invalid.")
    if not _has_foreign_key(connection, "habit_status_history", "habit_id"):
        raise InvalidStoreError("The database schema is invalid.")
    if not _has_check(
        connection,
        "habit_status_history",
        "check (is_active in (0, 1))",
    ):
        raise InvalidStoreError("The database schema is invalid.")


def _validate_rows(connection: sqlite3.Connection) -> None:
    habits = list(
        connection.execute("SELECT id, name, created_local_date FROM habits")
    )
    habit_ids = {row[0] for row in habits}
    for habit_id, name, created_date in habits:
        if not isinstance(habit_id, str) or not habit_id:
            raise InvalidStoreError("The database contains an invalid habit identity.")
        if not isinstance(name, str):
            raise InvalidStoreError("The database contains an invalid habit name.")
        _validate_date(created_date)

    completions = list(
        connection.execute("SELECT habit_id, local_date FROM completions")
    )
    for habit_id, local_date in completions:
        if habit_id not in habit_ids:
            raise InvalidStoreError("The database contains an orphaned completion.")
        _validate_date(local_date)

    statuses = list(
        connection.execute(
            "SELECT habit_id, local_date, is_active FROM habit_status_history"
        )
    )
    statuses_by_habit: dict[str, set[str]] = {
        habit_id: set() for habit_id in habit_ids
    }
    for habit_id, local_date, is_active in statuses:
        if habit_id not in habit_ids or is_active not in (0, 1):
            raise InvalidStoreError("The database contains invalid status history.")
        _validate_date(local_date)
        statuses_by_habit[habit_id].add(local_date)
    if any(
        created_date not in statuses_by_habit[habit_id]
        for habit_id, _, created_date in habits
    ):
        raise InvalidStoreError("The database is missing initial status history.")


def _create_current_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE habits (
            id TEXT PRIMARY KEY NOT NULL,
            name TEXT NOT NULL,
            created_local_date TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE completions (
            habit_id TEXT NOT NULL,
            local_date TEXT NOT NULL,
            PRIMARY KEY (habit_id, local_date),
            FOREIGN KEY (habit_id) REFERENCES habits(id) ON DELETE CASCADE
        )"""
    )
    connection.execute(
        """CREATE TABLE habit_status_history (
            habit_id TEXT NOT NULL,
            local_date TEXT NOT NULL,
            is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
            PRIMARY KEY (habit_id, local_date),
            FOREIGN KEY (habit_id) REFERENCES habits(id) ON DELETE CASCADE
        )"""
    )


class SQLiteStore:
    """Initialize and load the approved local database without unsafe recovery."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else default_database_path()

    def load(self) -> StoreData:
        """Initialize only a missing file, then validate and load persisted data."""
        created_new = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.stat()
            except FileNotFoundError:
                try:
                    with self.path.open("xb"):
                        pass
                    created_new = True
                except FileExistsError:
                    pass

            connection = self._connect()
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                if created_new:
                    self._initialize_new(connection)
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version != CURRENT_SCHEMA_VERSION:
                    raise InvalidStoreError(
                        f"Unsupported database schema version: {version}."
                    )
                _validate_base_schema(connection)
                self._validate_integrity(connection)
                _validate_rows(connection)
                return self._read_data(connection)
            finally:
                connection.close()
        except StorageError:
            if created_new:
                self._remove_new_file()
            raise
        except (OSError, sqlite3.Error) as exc:
            if created_new:
                self._remove_new_file()
            if isinstance(exc, sqlite3.DatabaseError) and any(
                marker in str(exc).lower()
                for marker in (
                    "file is not a database",
                    "database disk image is malformed",
                    "malformed database schema",
                    "no such table",
                    "no such column",
                )
            ):
                raise InvalidStoreError(
                    "The existing HabitFlow database is invalid."
                ) from exc
            raise StorageUnavailableError(
                "The local HabitFlow database could not be accessed or loaded."
            ) from exc

    def create_habit(
        self,
        habit_id: str,
        name: str,
        created_local_date: str,
        is_active: bool,
    ) -> None:
        """Persist a habit and its initial status row atomically.

        The caller supplies the initial status; domain defaults and name rules
        belong to the habit-management layer.
        """
        self._validate_habit_id(habit_id)
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        self._validate_operation_date(created_local_date)
        if type(is_active) is not bool:
            raise ValueError("is_active must be a boolean")

        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO habits (id, name, created_local_date)
                   VALUES (?, ?, ?)""",
                (habit_id, name, created_local_date),
            )
            connection.execute(
                """INSERT INTO habit_status_history
                   (habit_id, local_date, is_active) VALUES (?, ?, ?)""",
                (habit_id, created_local_date, int(is_active)),
            )

        self._write(insert)

    def update_habit_name(self, habit_id: str, name: str) -> None:
        """Persist a name change without changing associated history."""
        self._validate_habit_id(habit_id)
        if not isinstance(name, str):
            raise ValueError("name must be a string")

        def update(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "UPDATE habits SET name = ? WHERE id = ?", (name, habit_id)
            )
            if cursor.rowcount != 1:
                raise StorageOperationError("The requested habit does not exist.")

        self._write(update)

    def record_active_status(
        self, habit_id: str, local_date: str, is_active: bool
    ) -> None:
        """Insert or replace a habit's status for the supplied local date."""
        self._validate_habit_id(habit_id)
        self._validate_operation_date(local_date)
        if type(is_active) is not bool:
            raise ValueError("is_active must be a boolean")

        def record(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """INSERT INTO habit_status_history
                   (habit_id, local_date, is_active) VALUES (?, ?, ?)
                   ON CONFLICT (habit_id, local_date)
                   DO UPDATE SET is_active = excluded.is_active""",
                (habit_id, local_date, int(is_active)),
            )
            if cursor.rowcount != 1:
                raise StorageOperationError("The requested habit does not exist.")

        self._write(record)

    def create_completion(self, habit_id: str, local_date: str) -> bool:
        """Create a completion idempotently; return whether a row was inserted."""
        self._validate_habit_id(habit_id)
        self._validate_operation_date(local_date)

        def create(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """INSERT INTO completions (habit_id, local_date)
                   VALUES (?, ?)
                   ON CONFLICT (habit_id, local_date) DO NOTHING""",
                (habit_id, local_date),
            )
            return cursor.rowcount == 1

        return self._write(create)

    def delete_completion(self, habit_id: str, local_date: str) -> bool:
        """Delete only the supplied habit/date completion pair."""
        self._validate_habit_id(habit_id)
        self._validate_operation_date(local_date)

        def delete(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM completions WHERE habit_id = ? AND local_date = ?",
                (habit_id, local_date),
            )
            return cursor.rowcount == 1

        return self._write(delete)

    def delete_habit(self, habit_id: str) -> None:
        """Delete one previously authorized habit and its cascaded history.

        Authorization/confirmation is the caller's responsibility. The habit
        and related completion/status rows are removed in the same transaction.
        """
        self._validate_habit_id(habit_id)

        def delete(connection: sqlite3.Connection) -> None:
            cursor = connection.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
            if cursor.rowcount != 1:
                raise StorageOperationError("The requested habit does not exist.")

        self._write(delete)

    @staticmethod
    def _validate_habit_id(habit_id: str) -> None:
        if not isinstance(habit_id, str) or not habit_id:
            raise ValueError("habit_id must be a non-empty string")

    @staticmethod
    def _validate_operation_date(local_date: str) -> None:
        try:
            _validate_date(local_date)
        except InvalidStoreError as exc:
            raise ValueError("local_date must be a valid ISO date") from exc

    def _write(
        self, operation: Callable[[sqlite3.Connection], _WriteResult]
    ) -> _WriteResult:
        """Run one operation only against a validated existing store."""
        connection = None
        commit_attempted = False
        try:
            connection = self._connect()
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != CURRENT_SCHEMA_VERSION:
                raise InvalidStoreError(
                    f"Unsupported database schema version: {version}."
                )
            _validate_base_schema(connection)
            self._validate_integrity(connection)
            result = operation(connection)
            commit_attempted = True
            connection.commit()
            return result
        except StorageError as exc:
            transaction_active = self._transaction_is_active(connection)
            rollback_confirmed = self._rollback(connection)
            if not rollback_confirmed or (
                commit_attempted and transaction_active is not True
            ):
                raise StorageOutcomeUnknownError(
                    "The operation outcome is unknown; reload the database before retrying."
                ) from exc
            raise
        except (OSError, sqlite3.Error) as exc:
            transaction_active = self._transaction_is_active(connection)
            rollback_confirmed = self._rollback(connection)
            if not rollback_confirmed or (
                commit_attempted and transaction_active is not True
            ):
                raise StorageOutcomeUnknownError(
                    "The operation outcome is unknown; reload the database before retrying."
                ) from exc
            if isinstance(exc, sqlite3.DatabaseError) and any(
                marker in str(exc).lower()
                for marker in (
                    "file is not a database",
                    "database disk image is malformed",
                    "malformed database schema",
                    "no such table",
                    "no such column",
                )
            ):
                raise InvalidStoreError(
                    "The existing HabitFlow database is invalid."
                ) from exc
            if "unable to open database file" in str(exc).lower():
                raise StorageUnavailableError(
                    "The local HabitFlow database is unavailable."
                ) from exc
            raise StorageOperationError(
                "The database operation failed and was rolled back."
            ) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _transaction_is_active(
        connection: sqlite3.Connection | None,
    ) -> bool | None:
        if connection is None:
            return False
        try:
            return connection.in_transaction
        except sqlite3.Error:
            return None

    @staticmethod
    def _rollback(connection: sqlite3.Connection | None) -> bool:
        if connection is None:
            return True
        try:
            if connection.in_transaction:
                connection.rollback()
            return not connection.in_transaction
        except sqlite3.Error:
            return False

    def _remove_new_file(self) -> None:
        """Remove only a file exclusively created by this initialization attempt."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.path.resolve().as_uri()}?mode=rw"
        return sqlite3.connect(uri, uri=True)

    @staticmethod
    def _initialize_new(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _create_current_schema(connection)
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise

    @staticmethod
    def _validate_integrity(connection: sqlite3.Connection) -> None:
        results = list(connection.execute("PRAGMA quick_check"))
        if results != [("ok",)]:
            raise InvalidStoreError("The database failed its integrity check.")
        if list(connection.execute("PRAGMA foreign_key_check")):
            raise InvalidStoreError("The database contains broken references.")
        _validate_rows(connection)

    @staticmethod
    def _read_data(connection: sqlite3.Connection) -> StoreData:
        habits = tuple(
            HabitRecord(*row)
            for row in connection.execute(
                "SELECT id, name, created_local_date FROM habits ORDER BY rowid"
            )
        )
        completions = tuple(
            CompletionRecord(*row)
            for row in connection.execute(
                "SELECT habit_id, local_date FROM completions "
                "ORDER BY local_date, habit_id"
            )
        )
        statuses = tuple(
            ActiveStatusRecord(habit_id, local_date, bool(is_active))
            for habit_id, local_date, is_active in connection.execute(
                """SELECT habit_id, local_date, is_active
                   FROM habit_status_history ORDER BY local_date, habit_id"""
            )
        )
        return StoreData(habits, completions, statuses)

    @staticmethod
    def new_habit_id() -> str:
        """Create a stable unique identifier for future habit operations."""
        return str(uuid.uuid4())
