"""Cross-component tests for Flask routes, domain operations, and SQLite."""

from datetime import date
from html import unescape

import habitflow as habitflow_module
import habitflow.habits as habits_module
from habitflow import create_app
from habitflow.storage import SQLiteStore, StorageUnavailableError

LOAD_FAILURE_COPY = (
    "HabitFlow couldn't load a valid database. Existing data was not reset or "
    "replaced. Check database access and restart HabitFlow. Changes are disabled "
    "until the database can be loaded."
)
ROLLED_BACK_COPY = (
    "HabitFlow couldn't save this change; the database confirmed it was rolled "
    "back. Check database access, then try again."
)


class _MutableDate:
    current = date(2026, 10, 29)

    @classmethod
    def today(cls):
        return cls.current

    @staticmethod
    def fromisoformat(value):
        return date.fromisoformat(value)


class _FailingCommitConnection:
    """Connection proxy that fails commit while retaining SQLite rollback."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self):
        raise OSError("simulated commit failure")


def _set_date(monkeypatch, current):
    _MutableDate.current = current
    monkeypatch.setattr(habitflow_module, "date", _MutableDate)
    monkeypatch.setattr(habits_module, "date", _MutableDate)


def _post(client, path, *, data=None):
    return client.post(
        path,
        data=data,
        headers={"Origin": "http://localhost"},
        base_url="http://localhost",
    )


def test_routes_persist_management_completion_and_progress_across_reopen(
    tmp_path, monkeypatch
):
    _set_date(monkeypatch, date(2026, 10, 29))
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()
    store = SQLiteStore(database_path)

    created = _post(client, "/habits", data={"name": "  Read  daily  "})
    duplicate = _post(client, "/habits", data={"name": "Read  daily"})
    assert created.status_code == duplicate.status_code == 303
    initial_data = store.load()
    assert [habit.name for habit in initial_data.habits] == [
        "Read  daily",
        "Read  daily",
    ]
    first_id, second_id = (habit.habit_id for habit in initial_data.habits)

    page = client.get("/?week_start=2026-10-26")
    assert page.status_code == 200
    assert b"Mark Read  daily complete for 2026-10-29" in page.data
    assert b"Completed active habits: 0 of 2" in page.data
    assert b"Daily completion: 0.0%" in page.data
    assert b"Eligible active habit-days: 8" in page.data

    assert _post(client, f"/habits/{first_id}/complete").status_code == 303
    assert _post(client, f"/habits/{first_id}/complete").status_code == 303
    completed_page = client.get("/?week_start=2026-10-26")
    assert b"Undo Read  daily completion for 2026-10-29" in completed_page.data
    assert b"Completed active habits: 1 of 2" in completed_page.data
    assert b"Daily completion: 50.0%" in completed_page.data
    assert b"Completed active habit-days: 1" in completed_page.data
    assert b"Weekly completion: 12.5%" in completed_page.data

    persisted = SQLiteStore(database_path).load()
    assert [
        (item.habit_id, item.local_date) for item in persisted.completions
    ] == [(first_id, "2026-10-29")]
    assert second_id != first_id

    reopened_app = create_app({"DATABASE_PATH": database_path})
    reopened_client = reopened_app.test_client()
    reopened_page = reopened_client.get("/?week_start=2026-10-26")
    assert b"Undo Read  daily completion for 2026-10-29" in reopened_page.data
    assert b"Daily completion: 50.0%" in reopened_page.data

    second_marked = _post(reopened_client, f"/habits/{second_id}/complete")
    assert second_marked.status_code == 303
    with_second_completion = reopened_client.get("/?week_start=2026-10-26")
    assert b"Completed active habits: 2 of 2" in with_second_completion.data
    assert b"Daily completion: 100.0%" in with_second_completion.data
    assert b"Completed active habit-days: 2" in with_second_completion.data
    assert b"Weekly completion: 25.0%" in with_second_completion.data

    second_undone = _post(reopened_client, f"/habits/{second_id}/undo-completion")
    assert second_undone.status_code == 303
    after_undo = reopened_client.get("/?week_start=2026-10-26")
    assert b"Completed active habits: 1 of 2" in after_undo.data
    assert b"Daily completion: 50.0%" in after_undo.data
    assert b"Completed active habit-days: 1" in after_undo.data
    assert b"Weekly completion: 12.5%" in after_undo.data

    edited = _post(
        reopened_client,
        f"/habits/{first_id}/edit",
        data={"name": "  Read  every day  "},
    )
    assert edited.status_code == 303
    after_edit = SQLiteStore(database_path).load()
    assert [(habit.habit_id, habit.name) for habit in after_edit.habits] == [
        (first_id, "Read  every day"),
        (second_id, "Read  daily"),
    ]
    assert [(item.habit_id, item.local_date) for item in after_edit.completions] == [
        (first_id, "2026-10-29")
    ]
    assert b"Completed active habits: 1 of 2" in reopened_client.get(
        "/?week_start=2026-10-26"
    ).data


def test_status_ui_uses_action_date_and_recalculates_daily_weekly_progress(
    tmp_path, monkeypatch
):
    _set_date(monkeypatch, date(2026, 10, 29))
    database_path = tmp_path / "habitflow.sqlite3"
    store = SQLiteStore(database_path)
    store.load()
    store.create_habit("selected", "Selected", "2026-10-26", True)
    store.create_habit("other", "Other", "2026-10-26", True)
    store.create_completion("selected", "2026-10-29")
    store.create_completion("other", "2026-10-26")

    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()
    initial = client.get("/?week_start=2026-10-26")
    assert b"Status: Active" in initial.data
    assert b"Completed active habits: 1 of 2" in initial.data
    assert b"Eligible active habit-days: 14" in initial.data
    assert b"Weekly completion: 14.3%" in initial.data

    # Two changes on one local date are collapsed to that date's final value.
    deactivated_same_day = _post(
        client,
        "/habits/selected/status",
        data={"is_active": "false", "local_date": "2026-10-26"},
    )
    assert deactivated_same_day.status_code == 303
    after_same_day_deactivation = SQLiteStore(database_path).load()
    assert [
        (item.local_date, item.is_active)
        for item in after_same_day_deactivation.active_status_history
        if item.habit_id == "selected"
    ] == [("2026-10-26", True), ("2026-10-29", False)]
    reactivated_same_day = _post(
        client,
        "/habits/selected/status",
        data={"is_active": "true", "local_date": "2026-10-26"},
    )
    assert reactivated_same_day.status_code == 303
    same_day_data = SQLiteStore(database_path).load()
    assert [
        (item.local_date, item.is_active)
        for item in same_day_data.active_status_history
        if item.habit_id == "selected"
    ] == [("2026-10-26", True), ("2026-10-29", True)]
    same_day_page = client.get("/?week_start=2026-10-26")
    assert b"Status: Active" in same_day_page.data
    assert b"Completed active habits: 1 of 2" in same_day_page.data
    assert b"Weekly completion: 14.3%" in same_day_page.data

    _MutableDate.current = date(2026, 10, 30)
    deactivated = _post(
        client,
        "/habits/selected/status",
        data={"is_active": "false", "local_date": "2026-10-26"},
    )
    assert deactivated.status_code == 303
    after_deactivation = client.get("/?week_start=2026-10-26")
    assert b"Status: Inactive" in after_deactivation.data
    assert b"Reactivate Selected" in after_deactivation.data
    assert b"Completed active habits: 0 of 1" in after_deactivation.data
    assert b"Eligible active habit-days: 11" in after_deactivation.data
    assert b"Completed active habit-days: 2" in after_deactivation.data
    assert b"Weekly completion: 18.2%" in after_deactivation.data

    persisted = SQLiteStore(database_path).load()
    assert [(item.habit_id, item.local_date) for item in persisted.completions] == [
        ("other", "2026-10-26"),
        ("selected", "2026-10-29"),
    ]
    assert [
        (item.local_date, item.is_active)
        for item in persisted.active_status_history
        if item.habit_id == "selected"
    ] == [
        ("2026-10-26", True),
        ("2026-10-29", True),
        ("2026-10-30", False),
    ]
    reopened = create_app({"DATABASE_PATH": database_path}).test_client().get(
        "/?week_start=2026-10-26"
    )
    assert b"Status: Inactive" in reopened.data
    assert b"Weekly completion: 18.2%" in reopened.data


def test_empty_and_zero_active_states_are_distinct(tmp_path, monkeypatch):
    _set_date(monkeypatch, date(2026, 10, 29))
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()

    empty = client.get("/")
    assert b"You don't have any habits yet" in empty.data
    assert b"Create your first habit below" in empty.data
    assert b"Today's progress" not in empty.data
    assert b"Weekly completion" not in empty.data

    assert _post(client, "/habits", data={"name": "Rest"}).status_code == 303
    created = SQLiteStore(database_path).load()
    habit_id = created.habits[0].habit_id
    assert _post(
        client,
        f"/habits/{habit_id}/status",
        data={"is_active": "false"},
    ).status_code == 303

    zero_active = client.get("/")
    assert b"Today's progress" in zero_active.data
    assert b"Daily completion: 0.0%" in zero_active.data
    assert b"There are no active habits to track today." in zero_active.data
    assert b"Weekly completion: 0.0%" in zero_active.data


def test_invalid_store_is_not_overwritten_or_treated_as_empty(tmp_path):
    database_path = tmp_path / "invalid.sqlite3"
    original_bytes = b"existing data that is not a database"
    database_path.write_bytes(original_bytes)
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()

    response = client.get("/")
    assert response.status_code == 503
    assert LOAD_FAILURE_COPY in unescape(response.get_data(as_text=True))
    assert b"You don't have any habits yet" not in response.data
    assert b"Today's progress" not in response.data
    assert _post(client, "/habits", data={"name": "Blocked"}).status_code == 503
    assert database_path.read_bytes() == original_bytes


def test_unavailable_store_blocks_changes_then_allows_safe_retry(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("preserve this file", encoding="utf-8")
    database_path = blocker / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()

    unavailable = client.get("/")
    assert unavailable.status_code == 503
    assert LOAD_FAILURE_COPY in unescape(unavailable.get_data(as_text=True))
    assert b"You don't have any habits yet" not in unavailable.data
    assert _post(client, "/habits", data={"name": "Blocked"}).status_code == 503
    assert blocker.read_text(encoding="utf-8") == "preserve this file"
    assert not database_path.exists()

    blocker.unlink()
    retried = client.get("/")
    assert retried.status_code == 200
    assert b"You don't have any habits yet" in retried.data
    assert _post(client, "/habits", data={"name": "Available"}).status_code == 303
    assert [habit.name for habit in SQLiteStore(database_path).load().habits] == [
        "Available"
    ]


def test_failed_load_shows_error_then_recovers_on_a_fresh_load(tmp_path, monkeypatch):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()
    store = app.extensions["habitflow_storage"]
    store.create_habit("existing", "Existing", date.today().isoformat(), True)
    real_load = store.load

    def fail_load():
        raise StorageUnavailableError("simulated load failure")

    monkeypatch.setattr(store, "load", fail_load)
    failed = client.get("/")
    assert failed.status_code == 503
    assert LOAD_FAILURE_COPY in unescape(failed.get_data(as_text=True))
    assert b"You don't have any habits yet" not in failed.data
    assert _post(client, "/habits", data={"name": "Blocked"}).status_code == 503

    monkeypatch.setattr(store, "load", real_load)
    recovered = client.get("/")
    assert recovered.status_code == 200
    assert b"Existing" in recovered.data
    assert b"Today's progress" in recovered.data


def test_failed_save_rolls_back_and_retry_creates_one_habit(tmp_path, monkeypatch):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()
    store = app.extensions["habitflow_storage"]
    original_connect = store._connect
    monkeypatch.setattr(
        store,
        "_connect",
        lambda: _FailingCommitConnection(original_connect()),
    )

    failed = _post(client, "/habits", data={"name": "Retry me"})
    assert failed.status_code == 503
    assert failed.headers.get("Location") is None
    assert ROLLED_BACK_COPY in unescape(failed.get_data(as_text=True))
    assert SQLiteStore(database_path).load().habits == ()

    monkeypatch.setattr(store, "_connect", original_connect)
    retried = _post(client, "/habits", data={"name": "Retry me"})
    assert retried.status_code == 303
    persisted = SQLiteStore(database_path).load()
    assert [habit.name for habit in persisted.habits] == ["Retry me"]
    assert len(persisted.active_status_history) == 1


def test_confirmed_delete_failure_rolls_back_history_and_retry_is_scoped(
    tmp_path, monkeypatch
):
    _set_date(monkeypatch, date(2026, 10, 29))
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()
    store = app.extensions["habitflow_storage"]
    selected_id = "selected"
    other_id = "other"
    store.create_habit(selected_id, "Selected", "2026-10-26", True)
    store.create_habit(other_id, "Other", "2026-10-26", True)
    store.create_completion(selected_id, "2026-10-28")
    store.create_completion(other_id, "2026-10-28")
    store.record_active_status(selected_id, "2026-10-29", False)
    original_data = SQLiteStore(database_path).load()

    cancelled = _post(
        client,
        f"/habits/{selected_id}/delete",
        data={"cancel": "yes"},
    )
    assert cancelled.status_code == 303
    assert SQLiteStore(database_path).load() == original_data

    original_connect = store._connect
    monkeypatch.setattr(
        store,
        "_connect",
        lambda: _FailingCommitConnection(original_connect()),
    )
    failed = _post(
        client,
        f"/habits/{selected_id}/delete",
        data={"confirmed": "yes"},
    )
    assert failed.status_code == 503
    assert failed.headers.get("Location") is None
    assert ROLLED_BACK_COPY in unescape(failed.get_data(as_text=True))
    assert SQLiteStore(database_path).load() == original_data

    monkeypatch.setattr(store, "_connect", original_connect)
    deleted = _post(
        client,
        f"/habits/{selected_id}/delete",
        data={"confirmed": "yes"},
    )
    assert deleted.status_code == 303
    after_delete = SQLiteStore(database_path).load()
    assert [(habit.habit_id, habit.name) for habit in after_delete.habits] == [
        (other_id, "Other")
    ]
    assert [(item.habit_id, item.local_date) for item in after_delete.completions] == [
        (other_id, "2026-10-28")
    ]
    assert [
        item.habit_id for item in after_delete.active_status_history
    ] == [other_id]
    updated_dashboard = client.get("/?week_start=2026-10-26")
    assert b"Completed active habits: 0 of 1" in updated_dashboard.data
    assert b"Completed active habit-days: 1" in updated_dashboard.data
