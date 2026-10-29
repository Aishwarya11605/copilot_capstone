import sqlite3

import pytest

from habitflow.storage import (
    CURRENT_SCHEMA_VERSION,
    InvalidStoreError,
    SQLiteStore,
    StorageOutcomeUnknownError,
    StorageOperationError,
    StorageUnavailableError,
    default_database_path,
)


def _insert_habit(connection, habit_id, name, created="2026-10-28"):
    connection.execute(
        "INSERT INTO habits (id, name, created_local_date) VALUES (?, ?, ?)",
        (habit_id, name, created),
    )
    connection.execute(
        """INSERT INTO habit_status_history
           (habit_id, local_date, is_active) VALUES (?, ?, 1)""",
        (habit_id, created),
    )


def test_missing_store_is_initialized_at_approved_local_app_data_path(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_database_path() == tmp_path / "HabitFlow" / "habitflow.sqlite3"

    store = SQLiteStore()
    data = store.load()

    assert data.habits == ()
    assert store.path.is_file()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            CURRENT_SCHEMA_VERSION
        )
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        } == {"habits", "completions", "habit_status_history"}


def test_missing_local_app_data_location_fails_without_fallback(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    with pytest.raises(StorageUnavailableError):
        SQLiteStore()


def test_current_schema_loads_duplicate_names_and_persists_ids_and_history(tmp_path):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _insert_habit(connection, "habit-one", "Read")
        _insert_habit(connection, "habit-two", "Read")
        connection.execute(
            "INSERT INTO completions VALUES (?, ?)",
            ("habit-one", "2026-10-28"),
        )
        connection.execute(
            """INSERT INTO habit_status_history
               (habit_id, local_date, is_active) VALUES (?, ?, 0)""",
            ("habit-one", "2026-10-29"),
        )

    reopened = SQLiteStore(store.path).load()

    assert [habit.habit_id for habit in reopened.habits] == [
        "habit-one",
        "habit-two",
    ]
    assert [habit.name for habit in reopened.habits] == ["Read", "Read"]
    assert [(item.habit_id, item.local_date) for item in reopened.completions] == [
        ("habit-one", "2026-10-28")
    ]
    assert [
        (item.habit_id, item.local_date, item.is_active)
        for item in reopened.active_status_history
    ] == [
        ("habit-one", "2026-10-28", True),
        ("habit-two", "2026-10-28", True),
        ("habit-one", "2026-10-29", False),
    ]


def test_completion_pair_is_durably_unique(tmp_path):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()

    with sqlite3.connect(store.path) as connection:
        _insert_habit(connection, "habit-one", "Read")
        connection.execute(
            "INSERT INTO completions VALUES (?, ?)",
            ("habit-one", "2026-10-28"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO completions VALUES (?, ?)",
                ("habit-one", "2026-10-28"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO habit_status_history
                   (habit_id, local_date, is_active) VALUES (?, ?, 0)""",
                ("habit-one", "2026-10-28"),
            )

    assert len(SQLiteStore(store.path).load().completions) == 1


def test_unknown_version_is_rejected_without_changing_existing_file(tmp_path):
    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE existing_data (value TEXT)")
        connection.execute("INSERT INTO existing_data VALUES ('preserve me')")
        connection.execute("PRAGMA user_version = 99")
    original_bytes = path.read_bytes()

    with pytest.raises(InvalidStoreError):
        SQLiteStore(path).load()

    assert path.read_bytes() == original_bytes


def test_existing_unversioned_sqlite_file_is_not_initialized(tmp_path):
    path = tmp_path / "existing-empty.sqlite3"
    with sqlite3.connect(path):
        pass
    original_bytes = path.read_bytes()

    with pytest.raises(InvalidStoreError):
        SQLiteStore(path).load()

    assert path.read_bytes() == original_bytes


def test_invalid_existing_file_is_rejected_without_changing_it(tmp_path):
    path = tmp_path / "invalid.sqlite3"
    original_bytes = b"not a sqlite database"
    path.write_bytes(original_bytes)

    with pytest.raises(InvalidStoreError):
        SQLiteStore(path).load()

    assert path.read_bytes() == original_bytes


def test_unavailable_store_path_fails_without_creating_a_database(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("unchanged", encoding="utf-8")
    database_path = blocker / "habitflow.sqlite3"

    with pytest.raises(StorageUnavailableError):
        SQLiteStore(database_path).load()

    assert blocker.read_text(encoding="utf-8") == "unchanged"
    assert not database_path.exists()


def test_current_version_with_invalid_schema_is_not_reinitialized(tmp_path):
    path = tmp_path / "bad-schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    original_bytes = path.read_bytes()

    with pytest.raises(InvalidStoreError):
        SQLiteStore(path).load()

    assert path.read_bytes() == original_bytes


def test_current_schema_without_status_history_is_rejected_unchanged(tmp_path):
    path = tmp_path / "missing-status-history.sqlite3"
    with sqlite3.connect(path) as connection:
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
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    original_bytes = path.read_bytes()

    with pytest.raises(InvalidStoreError):
        SQLiteStore(path).load()

    assert path.read_bytes() == original_bytes


def test_current_version_with_composite_unique_habit_constraint_is_rejected(
    tmp_path,
):
    path = tmp_path / "bad-unique-schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE habits (
                id TEXT PRIMARY KEY NOT NULL,
                name TEXT NOT NULL,
                created_local_date TEXT NOT NULL,
                UNIQUE (name, created_local_date)
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
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    original_bytes = path.read_bytes()

    with pytest.raises(InvalidStoreError, match="unapproved unique constraint"):
        SQLiteStore(path).load()

    assert path.read_bytes() == original_bytes


def test_current_version_with_standalone_unique_habit_index_is_rejected(tmp_path):
    store = SQLiteStore(tmp_path / "bad-index-schema.sqlite3")
    store.load()

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE UNIQUE INDEX unique_habit_name_date "
            "ON habits(name, created_local_date)"
        )
    original_bytes = store.path.read_bytes()

    with pytest.raises(InvalidStoreError):
        SQLiteStore(store.path).load()

    assert store.path.read_bytes() == original_bytes


class _FailingCommitConnection:
    """Connection proxy that simulates a failed durable commit."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self):
        raise sqlite3.OperationalError("simulated commit failure")


class _FailingRollbackConnection(_FailingCommitConnection):
    def rollback(self):
        raise sqlite3.OperationalError("simulated rollback failure")


class _CommitThenRaiseConnection(_FailingCommitConnection):
    def commit(self):
        self._connection.commit()
        raise sqlite3.OperationalError("simulated post-commit failure")


def _fail_next_commit(store, monkeypatch):
    connect = store._connect
    monkeypatch.setattr(
        store,
        "_connect",
        lambda: _FailingCommitConnection(connect()),
    )


def test_write_operations_preserve_scope_and_completion_retries_are_idempotent(
    tmp_path,
):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()
    store.create_habit("habit-one", "Read", "2026-10-28", True)
    store.create_habit("habit-two", "Read", "2026-10-28", True)

    assert store.create_completion("habit-one", "2026-10-28") is True
    assert store.create_completion("habit-one", "2026-10-28") is False
    assert store.create_completion("habit-two", "2026-10-28") is True

    store.update_habit_name("habit-one", " Read daily ")
    assert [
        (item.habit_id, item.local_date)
        for item in SQLiteStore(store.path).load().completions
    ] == [
        ("habit-one", "2026-10-28"),
        ("habit-two", "2026-10-28"),
    ]
    store.record_active_status("habit-one", "2026-10-29", False)
    store.record_active_status("habit-one", "2026-10-29", True)

    assert store.delete_completion("habit-one", "2026-10-28") is True
    assert store.delete_completion("habit-one", "2026-10-28") is False

    data = SQLiteStore(store.path).load()
    assert [(habit.habit_id, habit.name) for habit in data.habits] == [
        ("habit-one", " Read daily "),
        ("habit-two", "Read"),
    ]
    assert [(item.habit_id, item.local_date) for item in data.completions] == [
        ("habit-two", "2026-10-28")
    ]
    assert [
        (item.habit_id, item.local_date, item.is_active)
        for item in data.active_status_history
    ] == [
        ("habit-one", "2026-10-28", True),
        ("habit-two", "2026-10-28", True),
        ("habit-one", "2026-10-29", True),
    ]


def test_failed_habit_save_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()
    _fail_next_commit(store, monkeypatch)

    with pytest.raises(StorageOperationError):
        store.create_habit("retry-habit", "Retry", "2026-10-29", True)

    monkeypatch.undo()
    after_failure = SQLiteStore(store.path).load()
    assert after_failure.habits == ()
    assert after_failure.active_status_history == ()

    store.create_habit("retry-habit", "Retry", "2026-10-29", True)
    persisted = SQLiteStore(store.path).load()
    assert [(habit.habit_id, habit.name) for habit in persisted.habits] == [
        ("retry-habit", "Retry")
    ]
    assert len(persisted.active_status_history) == 1


def test_failed_rollback_reports_unknown_outcome(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()
    connect = store._connect
    monkeypatch.setattr(
        store,
        "_connect",
        lambda: _FailingRollbackConnection(connect()),
    )

    with pytest.raises(StorageOutcomeUnknownError, match="outcome is unknown"):
        store.create_habit("uncertain", "Uncertain", "2026-10-29", True)


def test_commit_that_takes_effect_before_raising_reports_unknown_outcome(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()
    connect = store._connect
    monkeypatch.setattr(
        store,
        "_connect",
        lambda: _CommitThenRaiseConnection(connect()),
    )

    with pytest.raises(StorageOutcomeUnknownError, match="outcome is unknown"):
        store.create_habit("committed", "Committed", "2026-10-29", True)

    persisted = SQLiteStore(store.path).load()
    assert [(habit.habit_id, habit.name) for habit in persisted.habits] == [
        ("committed", "Committed")
    ]


def test_failed_status_and_completion_saves_roll_back_and_can_be_retried(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()
    store.create_habit("habit", "Name", "2026-10-28", True)
    original = SQLiteStore(store.path).load()

    _fail_next_commit(store, monkeypatch)
    with pytest.raises(StorageOperationError):
        store.record_active_status("habit", "2026-10-29", False)
    monkeypatch.undo()

    assert SQLiteStore(store.path).load() == original
    store.record_active_status("habit", "2026-10-29", False)

    _fail_next_commit(store, monkeypatch)
    with pytest.raises(StorageOperationError):
        store.create_completion("habit", "2026-10-29")
    monkeypatch.undo()

    after_completion_failure = SQLiteStore(store.path).load()
    assert after_completion_failure.completions == ()
    assert store.create_completion("habit", "2026-10-29") is True
    assert store.create_completion("habit", "2026-10-29") is False


def test_failed_confirmed_deletion_rolls_back_related_data_and_can_be_retried(
    tmp_path, monkeypatch
):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()
    store.create_habit("selected", "Selected", "2026-10-28", True)
    store.create_habit("other", "Other", "2026-10-28", True)
    store.create_completion("selected", "2026-10-28")
    store.create_completion("other", "2026-10-28")
    store.record_active_status("selected", "2026-10-29", False)
    original = SQLiteStore(store.path).load()

    _fail_next_commit(store, monkeypatch)
    with pytest.raises(StorageOperationError):
        store.delete_habit("selected")

    monkeypatch.undo()
    after_failure = SQLiteStore(store.path).load()
    assert after_failure == original

    store.delete_habit("selected")
    after_retry = SQLiteStore(store.path).load()
    assert [(habit.habit_id, habit.name) for habit in after_retry.habits] == [
        ("other", "Other")
    ]
    assert [(item.habit_id, item.local_date) for item in after_retry.completions] == [
        ("other", "2026-10-28")
    ]
    assert [
        (item.habit_id, item.local_date, item.is_active)
        for item in after_retry.active_status_history
    ] == [("other", "2026-10-28", True)]


def test_writes_are_blocked_for_a_missing_or_unsafe_store_without_initializing_it(
    tmp_path,
):
    missing_path = tmp_path / "missing.sqlite3"
    missing_store = SQLiteStore(missing_path)
    with pytest.raises(StorageUnavailableError):
        missing_store.create_habit("habit", "Name", "2026-10-29", True)
    assert not missing_path.exists()

    unsafe_store = SQLiteStore(tmp_path / "unsafe.sqlite3")
    unsafe_store.load()
    unsafe_store.create_habit("habit", "Name", "2026-10-29", True)
    unsafe_store.path.write_bytes(b"not a sqlite database")
    invalid_bytes = unsafe_store.path.read_bytes()

    with pytest.raises(InvalidStoreError):
        unsafe_store.create_completion("habit", "2026-10-29")

    assert unsafe_store.path.read_bytes() == invalid_bytes


def test_writes_are_blocked_if_an_initialized_store_becomes_unavailable(tmp_path):
    store = SQLiteStore(tmp_path / "removed.sqlite3")
    store.load()
    store.path.unlink()

    with pytest.raises(StorageUnavailableError):
        store.create_habit("habit", "Name", "2026-10-29", True)

    assert not store.path.exists()
