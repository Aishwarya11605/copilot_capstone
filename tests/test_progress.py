from datetime import date, datetime

import pytest

from habitflow.habits import HabitManager
from habitflow.progress import (
    active_habit_ids_on,
    calculate_daily_progress,
    calculate_weekly_progress,
)
from habitflow.storage import (
    ActiveStatusRecord,
    CompletionRecord,
    HabitRecord,
    SQLiteStore,
    StoreData,
)


def _data(
    habits=(),
    completions=(),
    statuses=(),
):
    return StoreData(tuple(habits), tuple(completions), tuple(statuses))


def test_daily_progress_counts_only_active_habits_completed_on_current_date():
    data = _data(
        habits=(
            HabitRecord("active", "Active", "2026-10-26"),
            HabitRecord("not-completed", "Not completed", "2026-10-26"),
            HabitRecord("inactive", "Inactive", "2026-10-26"),
            HabitRecord("not-yet-created", "Future", "2026-10-30"),
        ),
        completions=(
            CompletionRecord("active", "2026-10-29"),
            CompletionRecord("inactive", "2026-10-29"),
            CompletionRecord("active", "2026-10-28"),
        ),
        statuses=(
            ActiveStatusRecord("active", "2026-10-26", True),
            ActiveStatusRecord("not-completed", "2026-10-26", True),
            ActiveStatusRecord("inactive", "2026-10-26", True),
            ActiveStatusRecord("inactive", "2026-10-29", False),
            ActiveStatusRecord("not-yet-created", "2026-10-30", True),
        ),
    )

    result = calculate_daily_progress(data, date(2026, 10, 29))

    assert result.has_habits is True
    assert result.active_habits == 2
    assert result.completed_active_habits == 1
    assert result.percentage == 50.0
    assert result.empty_progress is False


def test_daily_progress_distinguishes_empty_habits_from_zero_active_habits():
    no_habits = calculate_daily_progress(_data(), date(2026, 10, 29))
    assert no_habits.has_habits is False
    assert no_habits.percentage is None
    assert no_habits.empty_progress is False

    all_inactive = _data(
        habits=(HabitRecord("inactive", "Inactive", "2026-10-26"),),
        statuses=(ActiveStatusRecord("inactive", "2026-10-26", False),),
    )
    no_active_habits = calculate_daily_progress(all_inactive, date(2026, 10, 29))
    assert no_active_habits.has_habits is True
    assert no_active_habits.active_habits == 0
    assert no_active_habits.completed_active_habits == 0
    assert no_active_habits.percentage == 0.0
    assert no_active_habits.empty_progress is True


def test_active_habit_ids_use_final_status_on_explicit_local_date():
    data = _data(
        habits=(
            HabitRecord("active", "Active", "2026-10-26"),
            HabitRecord("inactive", "Inactive", "2026-10-26"),
            HabitRecord("future", "Future", "2026-10-30"),
        ),
        statuses=(
            ActiveStatusRecord("active", "2026-10-26", True),
            ActiveStatusRecord("active", "2026-10-29", False),
            ActiveStatusRecord("inactive", "2026-10-26", True),
            ActiveStatusRecord("inactive", "2026-10-29", False),
            ActiveStatusRecord("future", "2026-10-30", True),
        ),
    )

    assert active_habit_ids_on(data, date(2026, 10, 28)) == {"active", "inactive"}
    assert active_habit_ids_on(data, date(2026, 10, 29)) == set()


def test_weekly_progress_uses_monday_sunday_and_final_status_per_local_date():
    data = _data(
        habits=(
            HabitRecord("first", "First", "2026-10-26"),
            HabitRecord("second", "Second", "2026-10-28"),
        ),
        completions=(
            CompletionRecord("first", "2026-10-25"),
            CompletionRecord("first", "2026-10-26"),
            CompletionRecord("first", "2026-10-28"),
            CompletionRecord("first", "2026-10-29"),
            CompletionRecord("first", "2026-11-02"),
            CompletionRecord("second", "2026-10-31"),
            CompletionRecord("second", "2026-11-01"),
        ),
        statuses=(
            ActiveStatusRecord("first", "2026-10-26", True),
            ActiveStatusRecord("first", "2026-10-28", False),
            ActiveStatusRecord("first", "2026-10-29", True),
            ActiveStatusRecord("second", "2026-10-28", False),
            ActiveStatusRecord("second", "2026-10-29", True),
        ),
    )

    result = calculate_weekly_progress(data, date(2026, 10, 26))

    assert result.week_start == date(2026, 10, 26)
    assert result.week_end == date(2026, 11, 1)
    assert result.eligible_active_habit_days == 10
    assert result.completed_active_habit_days == 4
    assert result.percentage == 40.0


def test_weekly_progress_returns_zero_when_no_active_habit_days_are_eligible():
    data = _data(
        habits=(HabitRecord("inactive", "Inactive", "2026-10-26"),),
        statuses=(ActiveStatusRecord("inactive", "2026-10-26", False),),
    )

    result = calculate_weekly_progress(data, date(2026, 10, 26))

    assert result.eligible_active_habit_days == 0
    assert result.completed_active_habit_days == 0
    assert result.percentage == 0.0


def test_weekly_progress_uses_final_status_after_same_day_changes(tmp_path):
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()
    store.create_habit("habit", "Habit", "2026-10-26", True)
    store.create_completion("habit", "2026-10-26")
    store.create_completion("habit", "2026-10-29")
    store.record_active_status("habit", "2026-10-29", False)
    store.record_active_status("habit", "2026-10-29", True)
    store.record_active_status("habit", "2026-10-29", False)

    result = calculate_weekly_progress(store.load(), date(2026, 10, 26))

    assert result.eligible_active_habit_days == 3
    assert result.completed_active_habit_days == 1
    assert result.percentage == pytest.approx(100 / 3)


@pytest.mark.parametrize(
    ("function", "when"),
    [
        (calculate_daily_progress, date(2026, 10, 29)),
        (calculate_weekly_progress, date(2026, 10, 26)),
    ],
)
def test_progress_rejects_non_store_data(function, when):
    with pytest.raises(TypeError, match="StoreData"):
        function(None, when)


def test_daily_progress_rejects_invalid_date_arguments():
    with pytest.raises(ValueError, match="current_date"):
        calculate_daily_progress(_data(), "2026-10-29")

    with pytest.raises(ValueError, match="current_date"):
        calculate_daily_progress(_data(), datetime(2026, 10, 29))


@pytest.mark.parametrize("week_start", [date(2026, 10, 27), date(2026, 11, 1)])
def test_weekly_progress_requires_an_explicit_monday(week_start):
    with pytest.raises(ValueError, match="Monday"):
        calculate_weekly_progress(_data(), week_start)


def test_progress_recalculates_from_current_store_after_habit_edit_and_deletion(
    tmp_path, monkeypatch
):
    import habitflow.habits as habits_module

    class FixedDate:
        @staticmethod
        def today():
            return date(2026, 10, 29)

    monkeypatch.setattr(habits_module, "date", FixedDate)
    store = SQLiteStore(tmp_path / "habitflow.sqlite3")
    store.load()
    manager = HabitManager(store)
    habit_id = manager.create("Initial name")
    manager.mark_complete(habit_id)

    before_edit = calculate_daily_progress(store.load(), date(2026, 10, 29))
    manager.edit(habit_id, "Updated name")
    after_edit = calculate_daily_progress(store.load(), date(2026, 10, 29))
    assert after_edit == before_edit

    manager.delete(habit_id, confirmed=True)
    after_delete = calculate_daily_progress(store.load(), date(2026, 10, 29))
    assert after_delete.has_habits is False
    assert after_delete.percentage is None
