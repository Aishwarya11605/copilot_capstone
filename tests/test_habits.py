import pytest

import habitflow.habits as habits_module
from habitflow.habits import HabitManager
from habitflow.storage import SQLiteStore, StorageOperationError


class _FixedDate:
    @staticmethod
    def today():
        from datetime import date

        return date(2026, 10, 29)


def _manager(tmp_path):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()
    return HabitManager(store), store


@pytest.mark.parametrize("name", [None, 42, b"habit"])
def test_create_rejects_non_string_names_without_writing(tmp_path, name):
    manager, store = _manager(tmp_path)

    with pytest.raises(ValueError, match="must be a string"):
        manager.create(name)

    assert store.load().habits == ()


@pytest.mark.parametrize("name", ["", " ", "\t\n\r"])
def test_create_and_edit_reject_blank_names(tmp_path, name):
    manager, store = _manager(tmp_path)

    with pytest.raises(ValueError, match="cannot be blank"):
        manager.create(name)

    habit_id = manager.create("Keep this")
    with pytest.raises(ValueError, match="cannot be blank"):
        manager.edit(habit_id, name)
    assert [(habit.habit_id, habit.name) for habit in manager.list()] == [
        (habit_id, "Keep this")
    ]


def test_edit_rejects_non_string_name_without_changing_habit(tmp_path):
    manager, _ = _manager(tmp_path)
    habit_id = manager.create("Keep this")

    with pytest.raises(ValueError, match="must be a string"):
        manager.edit(habit_id, None)

    assert [(habit.habit_id, habit.name) for habit in manager.list()] == [
        (habit_id, "Keep this")
    ]


def test_create_trims_only_outer_whitespace_allows_duplicates_and_starts_active(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(habits_module, "date", _FixedDate)
    manager, store = _manager(tmp_path)

    first_id = manager.create("  Read  a\tbook \n")
    second_id = manager.create("Read  a\tbook")
    third_id = manager.create("read  a\tbook")

    assert len({first_id, second_id, third_id}) == 3
    assert [(habit.habit_id, habit.name) for habit in manager.list()] == [
        (first_id, "Read  a\tbook"),
        (second_id, "Read  a\tbook"),
        (third_id, "read  a\tbook"),
    ]
    assert {
        status.habit_id: (status.local_date, status.is_active)
        for status in store.load().active_status_history
    } == {
        first_id: ("2026-10-29", True),
        second_id: ("2026-10-29", True),
        third_id: ("2026-10-29", True),
    }


def test_listing_returns_all_persisted_habits(tmp_path):
    manager, _ = _manager(tmp_path)
    first_id = manager.create("One")
    second_id = manager.create("Two")

    assert {habit.habit_id for habit in manager.list()} == {first_id, second_id}


def test_edit_changes_only_name_and_preserves_completion_and_status_history(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(habits_module, "date", _FixedDate)
    manager, store = _manager(tmp_path)
    selected_id = manager.create("Old name")
    other_id = manager.create("Other")
    store.create_completion(selected_id, "2026-10-28")
    store.create_completion(other_id, "2026-10-28")
    manager.set_active(selected_id, False)

    manager.edit(selected_id, "  New  name  ")

    data = store.load()
    assert [(habit.habit_id, habit.name) for habit in data.habits] == [
        (selected_id, "New  name"),
        (other_id, "Other"),
    ]
    assert {
        (item.habit_id, item.local_date) for item in data.completions
    } == {
        (selected_id, "2026-10-28"),
        (other_id, "2026-10-28"),
    }
    assert {
        item.habit_id: (item.local_date, item.is_active)
        for item in data.active_status_history
    } == {
        selected_id: ("2026-10-29", False),
        other_id: ("2026-10-29", True),
    }


def test_status_changes_use_local_action_date_and_final_same_day_value(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(habits_module, "date", _FixedDate)
    manager, store = _manager(tmp_path)
    habit_id = manager.create("Habit")

    manager.set_active(habit_id, False)
    manager.set_active(habit_id, True)
    manager.set_active(habit_id, False)

    status_history = store.load().active_status_history
    assert [
        (item.habit_id, item.local_date, item.is_active) for item in status_history
    ] == [(habit_id, "2026-10-29", False)]


def test_status_history_records_each_local_date(monkeypatch, tmp_path):
    from datetime import date

    class ChangingDate:
        current = date(2026, 10, 29)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr(habits_module, "date", ChangingDate)
    manager, store = _manager(tmp_path)
    habit_id = manager.create("Habit")

    ChangingDate.current = date(2026, 10, 30)
    manager.set_active(habit_id, False)
    manager.set_active(habit_id, True)
    manager.set_active(habit_id, False)

    assert [
        (item.habit_id, item.local_date, item.is_active)
        for item in store.load().active_status_history
    ] == [
        (habit_id, "2026-10-29", True),
        (habit_id, "2026-10-30", False),
    ]


def test_completion_mark_and_undo_use_current_local_date_and_are_scoped(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(habits_module, "date", _FixedDate)
    manager, store = _manager(tmp_path)
    selected_id = manager.create("Selected")
    other_id = manager.create("Other")

    assert manager.mark_complete(selected_id) is True
    assert manager.mark_complete(selected_id) is False
    assert manager.mark_complete(other_id) is True
    assert {
        (item.habit_id, item.local_date) for item in store.load().completions
    } == {
        (selected_id, "2026-10-29"),
        (other_id, "2026-10-29"),
    }

    assert manager.undo_completion(selected_id) is True
    assert manager.undo_completion(selected_id) is False
    assert [
        (item.habit_id, item.local_date) for item in store.load().completions
    ] == [(other_id, "2026-10-29")]


def test_completion_dates_follow_local_date_changes_without_reinterpreting_history(
    tmp_path, monkeypatch
):
    from datetime import date

    class ChangingDate:
        current = date(2026, 10, 29)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr(habits_module, "date", ChangingDate)
    manager, store = _manager(tmp_path)
    habit_id = manager.create("Habit")

    assert manager.mark_complete(habit_id) is True
    ChangingDate.current = date(2026, 10, 30)
    assert manager.mark_complete(habit_id) is True

    assert [
        (item.habit_id, item.local_date) for item in store.load().completions
    ] == [
        (habit_id, "2026-10-29"),
        (habit_id, "2026-10-30"),
    ]
    assert manager.undo_completion(habit_id) is True
    assert [
        (item.habit_id, item.local_date) for item in store.load().completions
    ] == [(habit_id, "2026-10-29")]


@pytest.mark.parametrize("method_name", ["mark_complete", "undo_completion"])
@pytest.mark.parametrize("habit_id", ["", None, 42])
def test_completion_actions_reject_invalid_habit_ids_without_storage_calls(
    method_name, habit_id
):
    class StoreThatMustNotBeUsed:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected storage call: {name}")

    manager = HabitManager(StoreThatMustNotBeUsed())
    with pytest.raises(ValueError, match="non-empty string"):
        getattr(manager, method_name)(habit_id)


def test_unconfirmed_delete_makes_no_storage_call_or_change(tmp_path, monkeypatch):
    manager, store = _manager(tmp_path)
    habit_id = manager.create("Keep this")
    original_load = store.load
    before = original_load()
    calls = []

    def record_call(method_name):
        original_method = getattr(store, method_name)

        def tracked(*args, **kwargs):
            calls.append(method_name)
            return original_method(*args, **kwargs)

        monkeypatch.setattr(store, method_name, tracked)

    for method_name in (
        "create_habit",
        "update_habit_name",
        "record_active_status",
        "delete_habit",
    ):
        record_call(method_name)

    assert manager.delete(habit_id, confirmed=False) is False
    assert calls == []
    assert original_load() == before


def test_confirmed_delete_removes_only_selected_habit_and_its_history(tmp_path):
    manager, store = _manager(tmp_path)
    selected_id = manager.create("Selected")
    other_id = manager.create("Other")
    store.create_completion(selected_id, "2026-10-28")
    store.create_completion(other_id, "2026-10-28")

    assert manager.delete(selected_id, confirmed=True) is True

    data = store.load()
    assert [(habit.habit_id, habit.name) for habit in data.habits] == [
        (other_id, "Other")
    ]
    assert [(item.habit_id, item.local_date) for item in data.completions] == [
        (other_id, "2026-10-28")
    ]
    assert [item.habit_id for item in data.active_status_history] == [other_id]


@pytest.mark.parametrize("confirmation", [None, 1, "yes"])
def test_delete_requires_a_boolean_confirmation(confirmation):
    class StoreThatMustNotBeUsed:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected storage call: {name}")

    manager = HabitManager(StoreThatMustNotBeUsed())
    with pytest.raises(ValueError, match="must be a boolean"):
        manager.delete("selected-id", confirmed=confirmation)


def test_storage_failures_are_not_silently_caught():
    failure = StorageOperationError("storage unavailable")

    class FailingStore:
        @staticmethod
        def new_habit_id():
            return "habit-id"

        def create_habit(self, *args):
            raise failure

    manager = HabitManager(FailingStore())
    with pytest.raises(StorageOperationError) as exc_info:
        manager.create("Habit")
    assert exc_info.value is failure


def test_storage_failures_propagate_from_list_edit_status_and_confirmed_delete():
    failure = StorageOperationError("storage unavailable")

    class FailingStore:
        def load(self):
            raise failure

        def update_habit_name(self, *args):
            raise failure

        def record_active_status(self, *args):
            raise failure

        def delete_habit(self, *args):
            raise failure

    manager = HabitManager(FailingStore())
    actions = (
        manager.list,
        lambda: manager.edit("habit-id", "Name"),
        lambda: manager.set_active("habit-id", False),
        lambda: manager.delete("habit-id", confirmed=True),
    )

    for action in actions:
        with pytest.raises(StorageOperationError) as exc_info:
            action()
        assert exc_info.value is failure


def test_storage_failures_propagate_from_completion_mark_and_undo(monkeypatch):
    monkeypatch.setattr(habits_module, "date", _FixedDate)
    failure = StorageOperationError("storage unavailable")

    class FailingStore:
        def create_completion(self, *args):
            raise failure

        def delete_completion(self, *args):
            raise failure

    manager = HabitManager(FailingStore())
    actions = (
        lambda: manager.mark_complete("habit-id"),
        lambda: manager.undo_completion("habit-id"),
    )

    for action in actions:
        with pytest.raises(StorageOperationError) as exc_info:
            action()
        assert exc_info.value is failure
