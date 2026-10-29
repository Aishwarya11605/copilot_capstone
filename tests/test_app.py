import errno
from datetime import date
from html import unescape
from pathlib import Path

import pytest

import habitflow as habitflow_module
import habitflow.__main__ as launcher
import habitflow.habits as habits_module
from habitflow import create_app
from habitflow.habits import HabitManager
from habitflow.storage import (
    SQLiteStore,
    StorageOperationError,
    StorageOutcomeUnknownError,
)

LOAD_FAILURE_COPY = (
    "HabitFlow couldn't load a valid database. Existing data was not reset or "
    "replaced. Check database access and restart HabitFlow. Changes are disabled "
    "until the database can be loaded."
)
ROLLED_BACK_COPY = (
    "HabitFlow couldn't save this change; the database confirmed it was rolled "
    "back. Check database access, then try again."
)
UNKNOWN_OUTCOME_COPY = (
    "HabitFlow couldn't confirm whether this change was saved. Reload HabitFlow "
    "and check the current data before trying again."
)


def _post_same_origin(client, *args, **kwargs):
    base_url = kwargs.setdefault("base_url", "http://localhost")
    headers = kwargs.setdefault("headers", {})
    headers.setdefault("Origin", base_url.rstrip("/"))
    return client.post(*args, **kwargs)


def test_home_page_is_server_rendered_and_shows_local_privacy_notice(tmp_path):
    app = create_app({"DATABASE_PATH": tmp_path / "habitflow.sqlite3"})
    response = app.test_client().get("/")

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert b"<h1>HabitFlow</h1>" in response.data
    assert b"does not send your data to external services" in response.data
    assert b"does not protect your data from others with access" in response.data
    assert b"You don't have any habits yet" in response.data
    assert b"Create your first habit below" in response.data
    assert b"Completed today" not in response.data
    assert b"Today's progress" not in response.data
    assert b"Weekly completion" not in response.data
    assert app.extensions["habitflow_storage_state"]["status"] == "ready"
    assert app.extensions["habitflow_storage_state"]["data_changes_enabled"] is True
    assert app.extensions["habitflow_storage_state"]["data"].habits == ()


def test_server_uses_only_the_approved_loopback_address_and_port(monkeypatch, tmp_path):
    captured = {}
    app = create_app({"DATABASE_PATH": tmp_path / "habitflow.sqlite3"})

    class FakeServer:
        def __init__(self, host, port, app):
            captured.update(host=host, port=port, app=app)

    monkeypatch.setattr(launcher, "_ExclusiveLoopbackServer", FakeServer)
    monkeypatch.setattr(launcher, "create_app", lambda: app)

    assert launcher.create_server() is not None
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 5000
    assert captured["app"].test_client().get("/").status_code == 200


def test_exclusive_server_disables_reusable_address():
    assert launcher._ExclusiveLoopbackServer.allow_reuse_address is False


def test_busy_port_reports_clear_error_without_trying_another_port(
    monkeypatch, capsys, tmp_path
):
    calls = []
    app = create_app({"DATABASE_PATH": tmp_path / "habitflow.sqlite3"})
    monkeypatch.setattr(launcher, "create_app", lambda: app)

    def raise_address_in_use(server):
        calls.append((server.host, server.port))
        raise OSError(errno.EADDRINUSE, "address already in use")

    monkeypatch.setattr(launcher.BaseWSGIServer, "server_bind", raise_address_in_use)

    with pytest.raises(SystemExit) as exc_info:
        launcher.main()

    assert exc_info.value.code == 1
    assert calls == [("127.0.0.1", 5000)]
    assert "127.0.0.1:5000 is already in use" in capsys.readouterr().err


def test_readme_documents_same_device_and_account_privacy_limit():
    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8").lower()

    assert "does **not** protect data" in text
    assert "same device or operating-system account" in text


def test_startup_keeps_storage_failure_explicit_and_does_not_load_empty_data(tmp_path):
    invalid_database = tmp_path / "invalid.sqlite3"
    invalid_database.write_bytes(b"not a sqlite database")
    original_bytes = invalid_database.read_bytes()

    app = create_app({"DATABASE_PATH": invalid_database})
    state = app.extensions["habitflow_storage_state"]

    assert state["status"] == "error"
    assert state["data"] is None
    assert state["error"] is not None
    assert state["data_changes_enabled"] is False
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 503
    assert LOAD_FAILURE_COPY in unescape(response.get_data(as_text=True))
    assert b"You don&#39;t have any habits yet" not in response.data
    assert b"Today's progress" not in response.data
    assert b"Weekly completion" not in response.data
    assert b'name="name"' not in response.data
    assert _post_same_origin(
        client, "/habits", data={"name": "Blocked"}
    ).status_code == 503
    assert _post_same_origin(client, "/habits/selected/complete").status_code == 503
    assert (
        _post_same_origin(
            client,
            "/habits/selected/status",
            data={"is_active": "true"},
        ).status_code
        == 503
    )
    assert invalid_database.read_bytes() == original_bytes


def test_storage_error_can_be_retried_after_database_becomes_available(tmp_path):
    database_path = tmp_path / "invalid.sqlite3"
    database_path.write_bytes(b"not a sqlite database")
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()

    assert client.get("/").status_code == 503
    database_path.unlink()
    SQLiteStore(database_path).load()

    recovered = client.get("/")
    assert recovered.status_code == 200
    assert b"You don't have any habits yet" in recovered.data
    assert app.extensions["habitflow_storage_state"]["status"] == "ready"


def test_missing_local_app_data_keeps_app_in_explicit_storage_error_state(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    app = create_app()
    state = app.extensions["habitflow_storage_state"]

    assert state["status"] == "error"
    assert state["data"] is None
    assert state["data_changes_enabled"] is False


def test_habit_routes_create_list_and_edit_without_losing_history(tmp_path):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()
    store = SQLiteStore(database_path)

    created = _post_same_origin(client, "/habits", data={"name": "  Read  daily "})
    assert created.status_code == 303
    selected_id = store.load().habits[0].habit_id
    store.create_completion(selected_id, "2026-10-28")
    other_created = _post_same_origin(client, "/habits", data={"name": "Read  daily"})
    assert other_created.status_code == 303
    other_id = next(
        habit.habit_id for habit in store.load().habits if habit.habit_id != selected_id
    )

    page = client.get("/")
    assert b"Read  daily" in page.data
    assert page.data.count(b"Save name") == 2
    assert page.data.count(b"Confirm deletion of") == 2

    edited = _post_same_origin(
        client,
        f"/habits/{selected_id}/edit",
        data={"name": "  Renamed  habit  "},
    )
    assert edited.status_code == 303
    data = store.load()
    assert [(habit.habit_id, habit.name) for habit in data.habits] == [
        (selected_id, "Renamed  habit"),
        (other_id, "Read  daily"),
    ]
    assert [(item.habit_id, item.local_date) for item in data.completions] == [
        (selected_id, "2026-10-28")
    ]


def test_habit_names_are_html_escaped_in_the_list_and_edit_form(tmp_path):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    HabitManager(SQLiteStore(database_path)).create('<script>alert("x")</script>')

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"&lt;script&gt;" in response.data
    assert b"&#34;x&#34;" in response.data
    assert b"<script>" not in response.data


def test_create_rejects_blank_names_without_writing(tmp_path):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})

    response = _post_same_origin(
        app.test_client(), "/habits", data={"name": " \t "}
    )

    assert response.status_code == 400
    assert b"Habit name cannot be blank." in response.data
    assert SQLiteStore(database_path).load().habits == ()


def test_delete_requires_confirmation_and_cancel_leaves_all_data_unchanged(
    tmp_path,
):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()
    store = SQLiteStore(database_path)
    manager = HabitManager(store)
    selected_id = manager.create("Selected")
    other_id = manager.create("Other")
    store.create_completion(selected_id, "2026-10-28")
    store.create_completion(other_id, "2026-10-28")
    original = store.load()

    missing_confirmation = _post_same_origin(
        client, f"/habits/{selected_id}/delete"
    )
    assert missing_confirmation.status_code == 400
    assert b"Confirm deletion before deleting this habit." in missing_confirmation.data
    assert store.load() == original

    cancelled = _post_same_origin(
        client,
        f"/habits/{selected_id}/delete",
        data={"cancel": "yes"},
    )
    assert cancelled.status_code == 303
    assert store.load() == original

    deleted = _post_same_origin(
        client,
        f"/habits/{selected_id}/delete",
        data={"confirmed": "yes"},
    )
    assert deleted.status_code == 303
    data = store.load()
    assert [(habit.habit_id, habit.name) for habit in data.habits] == [
        (other_id, "Other")
    ]
    assert [(item.habit_id, item.local_date) for item in data.completions] == [
        (other_id, "2026-10-28")
    ]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (StorageOperationError("operation rolled back"), ROLLED_BACK_COPY),
        (StorageOutcomeUnknownError("operation outcome unknown"), UNKNOWN_OUTCOME_COPY),
    ],
)
def test_write_failures_show_approved_copy_and_never_report_success(
    tmp_path, monkeypatch, failure, message
):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = app.extensions["habitflow_storage"]

    def fail_write(*args, **kwargs):
        raise failure

    monkeypatch.setattr(store, "create_habit", fail_write)
    response = _post_same_origin(
        app.test_client(), "/habits", data={"name": "Habit"}
    )

    assert response.status_code == 503
    assert message in unescape(response.get_data(as_text=True))
    assert app.extensions["habitflow_storage_state"]["data_changes_enabled"] is False
    assert SQLiteStore(database_path).load().habits == ()


@pytest.mark.parametrize(
    ("action", "storage_method"),
    [
        ("edit", "update_habit_name"),
        ("delete", "delete_habit"),
    ],
)
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (StorageOperationError("operation rolled back"), ROLLED_BACK_COPY),
        (StorageOutcomeUnknownError("operation outcome unknown"), UNKNOWN_OUTCOME_COPY),
    ],
)
def test_edit_and_delete_failures_show_full_approved_copy(
    tmp_path, monkeypatch, action, storage_method, failure, message
):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = app.extensions["habitflow_storage"]
    habit_id = HabitManager(store).create("Habit")
    before = SQLiteStore(database_path).load()

    def fail_write(*args, **kwargs):
        raise failure

    monkeypatch.setattr(store, storage_method, fail_write)
    if action == "edit":
        response = _post_same_origin(
            app.test_client(),
            f"/habits/{habit_id}/edit",
            data={"name": "Changed"},
        )
    else:
        response = _post_same_origin(
            app.test_client(),
            f"/habits/{habit_id}/delete",
            data={"confirmed": "yes"},
        )

    assert response.status_code == 503
    assert response.headers.get("Location") is None
    assert message in unescape(response.get_data(as_text=True))
    assert app.extensions["habitflow_storage_state"]["data_changes_enabled"] is False
    if isinstance(failure, StorageOperationError):
        assert SQLiteStore(database_path).load() == before


def test_completion_controls_use_action_date_and_only_change_selected_habit(
    tmp_path, monkeypatch
):
    class ChangingDate:
        current = date(2026, 10, 29)

        @classmethod
        def today(cls):
            return cls.current

        @staticmethod
        def fromisoformat(value):
            return date.fromisoformat(value)

    monkeypatch.setattr(habitflow_module, "date", ChangingDate)
    monkeypatch.setattr(habits_module, "date", ChangingDate)
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()
    store = SQLiteStore(database_path)
    manager = HabitManager(store)
    selected_id = manager.create("Selected")
    other_id = manager.create("Other")

    initial_page = client.get("/")
    assert b"Mark Selected complete for 2026-10-29" in initial_page.data
    assert b"Mark Other complete for 2026-10-29" in initial_page.data

    ChangingDate.current = date(2026, 10, 30)
    marked = _post_same_origin(
        client,
        f"/habits/{selected_id}/complete",
        data={"local_date": "2026-10-29"},
    )
    assert marked.status_code == 303
    assert manager.mark_complete(selected_id) is False
    assert [
        (item.habit_id, item.local_date) for item in store.load().completions
    ] == [(selected_id, "2026-10-30")]

    _post_same_origin(client, f"/habits/{other_id}/complete")
    page = client.get("/")
    assert b"Undo Selected completion for 2026-10-30" in page.data
    assert b"Undo Other completion for 2026-10-30" in page.data

    undone = _post_same_origin(client, f"/habits/{selected_id}/undo-completion")
    assert undone.status_code == 303
    assert [
        (item.habit_id, item.local_date) for item in store.load().completions
    ] == [(other_id, "2026-10-30")]


def test_status_controls_use_action_date_isolate_habits_and_refresh_progress(
    tmp_path, monkeypatch
):
    class ChangingDate:
        current = date(2026, 10, 29)

        @classmethod
        def today(cls):
            return cls.current

        @staticmethod
        def fromisoformat(value):
            return date.fromisoformat(value)

    monkeypatch.setattr(habitflow_module, "date", ChangingDate)
    monkeypatch.setattr(habits_module, "date", ChangingDate)
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    client = app.test_client()
    store = SQLiteStore(database_path)
    manager = HabitManager(store)
    selected_id = manager.create("Selected")
    other_id = manager.create("Other")
    manager.mark_complete(selected_id)

    initial = client.get("/?week_start=2026-10-26")
    assert b"Status: Active" in initial.data
    assert b"Mark Selected inactive" in initial.data
    assert b"Mark Other inactive" in initial.data

    ChangingDate.current = date(2026, 10, 30)
    deactivated = _post_same_origin(
        client,
        f"/habits/{selected_id}/status",
        data={
            "is_active": "false",
            "local_date": "2026-10-29",
            "week_start": "2026-10-26",
        },
        base_url="http://127.0.0.1:5000",
    )
    assert deactivated.status_code == 303
    assert deactivated.headers["Location"] == "/?week_start=2026-10-26"
    after_deactivation = client.get(deactivated.headers["Location"])
    assert b"Status: Inactive" in after_deactivation.data
    assert b"Reactivate Selected" in after_deactivation.data
    assert b"Completed active habits: 0 of 1" in after_deactivation.data
    assert b"Eligible active habit-days: 5" in after_deactivation.data
    assert b"Completed active habit-days: 1" in after_deactivation.data
    assert b"Weekly completion: 20.0%" in after_deactivation.data

    reactivated = _post_same_origin(
        client,
        f"/habits/{selected_id}/status",
        data={"is_active": "true", "week_start": "2026-10-26"},
    )
    assert reactivated.status_code == 303
    after_reactivation = client.get(reactivated.headers["Location"])
    assert b"Status: Active" in after_reactivation.data
    assert b"Mark Selected inactive" in after_reactivation.data
    assert b"Completed active habits: 0 of 2" in after_reactivation.data
    assert b"Eligible active habit-days: 8" in after_reactivation.data
    assert b"Completed active habit-days: 1" in after_reactivation.data
    assert b"Weekly completion: 12.5%" in after_reactivation.data

    statuses = store.load().active_status_history
    assert [
        (status.local_date, status.is_active)
        for status in statuses
        if status.habit_id == selected_id
    ] == [("2026-10-29", True), ("2026-10-30", True)]
    assert [
        (status.local_date, status.is_active)
        for status in statuses
        if status.habit_id == other_id
    ] == [("2026-10-29", True)]


@pytest.mark.parametrize("submitted_status", [None, "yes", "1"])
def test_status_route_rejects_missing_or_invalid_status_without_writing(
    tmp_path, submitted_status
):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = app.extensions["habitflow_storage"]
    habit_id = HabitManager(store).create("Habit")
    before = store.load()
    form = {} if submitted_status is None else {"is_active": submitted_status}

    response = _post_same_origin(
        app.test_client(), f"/habits/{habit_id}/status", data=form
    )

    assert response.status_code == 400
    assert b"Select a valid habit status." in response.data
    assert store.load() == before


def test_status_route_returns_not_found_without_disabling_storage(tmp_path):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = app.extensions["habitflow_storage"]
    HabitManager(store).create("Habit")
    before = store.load()

    response = _post_same_origin(
        app.test_client(),
        "/habits/unknown/status",
        data={"is_active": "false"},
    )

    assert response.status_code == 404
    assert b"Habit not found." in response.data
    assert store.load() == before
    assert app.extensions["habitflow_storage_state"]["status"] == "ready"
    assert app.extensions["habitflow_storage_state"]["data_changes_enabled"] is True


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/habits/{habit_id}/status", {"is_active": "false"}),
        ("/habits/{habit_id}/complete", {}),
        ("/habits", {"name": "Forged"}),
    ],
)
@pytest.mark.parametrize("source_header", ["Origin", "Referer"])
def test_cross_origin_posts_are_rejected_without_changing_data(
    tmp_path, path, data, source_header
):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = app.extensions["habitflow_storage"]
    habit_id = HabitManager(store).create("Habit")
    before = store.load()

    response = app.test_client().post(
        path.format(habit_id=habit_id),
        data=data,
        headers={source_header: "https://attacker.example"},
        base_url="http://127.0.0.1:5000",
    )

    assert response.status_code == 403
    assert b"Cross-origin data changes are not allowed." in response.data
    assert store.load() == before


def test_posts_without_origin_or_referer_are_rejected(tmp_path):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = app.extensions["habitflow_storage"]
    habit_id = HabitManager(store).create("Habit")
    before = store.load()

    response = app.test_client().post(
        f"/habits/{habit_id}/status",
        data={"is_active": "false"},
        base_url="http://127.0.0.1:5000",
    )

    assert response.status_code == 403
    assert b"Cannot verify the request origin." in response.data
    assert store.load() == before


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (StorageOperationError("operation rolled back"), ROLLED_BACK_COPY),
        (StorageOutcomeUnknownError("operation outcome unknown"), UNKNOWN_OUTCOME_COPY),
    ],
)
def test_status_write_failures_show_approved_copy_and_block_changes(
    tmp_path, monkeypatch, failure, message
):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = app.extensions["habitflow_storage"]
    habit_id = HabitManager(store).create("Habit")

    def fail_write(*args, **kwargs):
        raise failure

    monkeypatch.setattr(store, "record_active_status", fail_write)
    response = _post_same_origin(
        app.test_client(),
        f"/habits/{habit_id}/status",
        data={"is_active": "false"},
    )

    assert response.status_code == 503
    assert response.headers.get("Location") is None
    assert message in unescape(response.get_data(as_text=True))
    assert app.extensions["habitflow_storage_state"]["data_changes_enabled"] is False


def test_weekly_dashboard_defaults_navigates_and_preserves_selection_after_change(
    tmp_path, monkeypatch
):
    class FixedDate:
        @staticmethod
        def today():
            return date(2026, 10, 29)

        @staticmethod
        def fromisoformat(value):
            return date.fromisoformat(value)

    monkeypatch.setattr(habitflow_module, "date", FixedDate)
    monkeypatch.setattr(habits_module, "date", FixedDate)
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = SQLiteStore(database_path)
    store.create_habit("habit", "Read", "2026-10-19", True)
    store.create_completion("habit", "2026-10-19")
    client = app.test_client()

    default_week = client.get("/")
    assert b"Weekly progress (2026-10-19 to 2026-10-25)" in default_week.data
    assert b"Weekly completion: 14.3%" in default_week.data
    assert b'href="/?week_start=2026-10-26">Next week</a>' in default_week.data

    current_week = client.get("/?week_start=2026-10-26")
    assert b"Weekly progress (2026-10-26 to 2026-11-01)" in current_week.data
    assert b"Completed active habit-days: 0" in current_week.data
    assert b'<button type="button" disabled>Next week</button>' in current_week.data
    assert b'href="/?week_start=2026-11-02"' not in current_week.data

    marked = _post_same_origin(
        client,
        "/habits/habit/complete",
        data={"week_start": "2026-10-26"},
    )
    assert marked.status_code == 303
    assert marked.headers["Location"] == "/?week_start=2026-10-26"
    updated = client.get(marked.headers["Location"])
    assert b"Daily completion: 100.0%" in updated.data
    assert b"Completed active habit-days: 1" in updated.data


@pytest.mark.parametrize(
    ("week_start", "message"),
    [
        ("2026-10-27", b"Select a valid Monday for the displayed week."),
        ("2026-11-02", b"Select a week no later than the current local week."),
    ],
)
def test_week_selector_rejects_invalid_and_future_weeks(
    tmp_path, monkeypatch, week_start, message
):
    class FixedDate:
        @staticmethod
        def today():
            return date(2026, 10, 29)

        @staticmethod
        def fromisoformat(value):
            return date.fromisoformat(value)

    monkeypatch.setattr(habitflow_module, "date", FixedDate)
    app = create_app({"DATABASE_PATH": tmp_path / "habitflow.sqlite3"})

    response = app.test_client().get(f"/?week_start={week_start}")

    assert response.status_code == 400
    assert message in response.data


def test_dashboard_recalculates_daily_and_weekly_progress_after_status_change(
    tmp_path, monkeypatch
):
    class FixedDate:
        @staticmethod
        def today():
            return date(2026, 10, 29)

        @staticmethod
        def fromisoformat(value):
            return date.fromisoformat(value)

    monkeypatch.setattr(habitflow_module, "date", FixedDate)
    monkeypatch.setattr(habits_module, "date", FixedDate)
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = SQLiteStore(database_path)
    manager = HabitManager(store)
    habit_id = manager.create("Read")
    manager.mark_complete(habit_id)
    client = app.test_client()

    before = client.get("/?week_start=2026-10-26")
    assert b"Daily completion: 100.0%" in before.data
    assert b"Eligible active habit-days: 4" in before.data

    manager.set_active(habit_id, False)
    after = client.get("/?week_start=2026-10-26")
    assert b"Completed active habits: 0 of 0" in after.data
    assert b"Daily completion: 0.0%" in after.data
    assert b"There are no active habits to track today." in after.data
    assert b"Eligible active habit-days: 0" in after.data
    assert b"Weekly completion: 0.0%" in after.data


@pytest.mark.parametrize(
    ("storage_method", "endpoint", "seed_completion"),
    [
        ("create_completion", "complete", False),
        ("delete_completion", "undo-completion", True),
    ],
)
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (StorageOperationError("operation rolled back"), ROLLED_BACK_COPY),
        (StorageOutcomeUnknownError("operation outcome unknown"), UNKNOWN_OUTCOME_COPY),
    ],
)
def test_completion_write_failures_show_approved_copy(
    tmp_path,
    monkeypatch,
    storage_method,
    endpoint,
    seed_completion,
    failure,
    message,
):
    database_path = tmp_path / "habitflow.sqlite3"
    app = create_app({"DATABASE_PATH": database_path})
    store = app.extensions["habitflow_storage"]
    habit_id = HabitManager(store).create("Habit")
    if seed_completion:
        store.create_completion(habit_id, date.today().isoformat())

    def fail_write(*args, **kwargs):
        raise failure

    monkeypatch.setattr(store, storage_method, fail_write)
    response = _post_same_origin(
        app.test_client(), f"/habits/{habit_id}/{endpoint}"
    )

    assert response.status_code == 503
    assert response.headers.get("Location") is None
    assert message in unescape(response.get_data(as_text=True))
    assert app.extensions["habitflow_storage_state"]["data_changes_enabled"] is False
