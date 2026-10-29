"""Daily and weekly progress calculations for validated HabitFlow data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from habitflow.storage import StoreData


@dataclass(frozen=True)
class DailyProgress:
    has_habits: bool
    completed_active_habits: int
    active_habits: int
    percentage: float | None

    @property
    def empty_progress(self) -> bool:
        return self.has_habits and self.active_habits == 0


@dataclass(frozen=True)
class WeeklyProgress:
    week_start: date
    week_end: date
    completed_active_habit_days: int
    eligible_active_habit_days: int
    percentage: float


def calculate_daily_progress(data: StoreData, current_date: date) -> DailyProgress:
    """Calculate today's active-habit count and percentage.

    An explicit date keeps local-date selection with the caller. A missing
    habit list has no statistics; a non-empty list with no active habits is 0%.
    """
    _validate_data(data)
    _validate_date_argument(current_date, "current_date")

    if not data.habits:
        return DailyProgress(False, 0, 0, None)

    completions = {
        (completion.habit_id, _stored_date(completion.local_date))
        for completion in data.completions
    }
    active_habit_ids = _active_habit_ids_on(data, current_date)
    completed = sum(
        (habit_id, current_date) in completions for habit_id in active_habit_ids
    )
    percentage = (
        100.0 * completed / len(active_habit_ids) if active_habit_ids else 0.0
    )
    return DailyProgress(True, completed, len(active_habit_ids), percentage)


def active_habit_ids_on(data: StoreData, current_date: date) -> frozenset[str]:
    """Return habit IDs active on an explicit local date."""
    _validate_data(data)
    _validate_date_argument(current_date, "current_date")
    return frozenset(_active_habit_ids_on(data, current_date))


def calculate_weekly_progress(data: StoreData, week_start: date) -> WeeklyProgress:
    """Calculate progress for an explicit Monday-to-Sunday local-date week."""
    _validate_data(data)
    _validate_date_argument(week_start, "week_start")
    if week_start.weekday() != 0:
        raise ValueError("week_start must be a Monday.")

    week_end = week_start + timedelta(days=6)
    statuses = _statuses_by_habit(data)
    completions = {
        (completion.habit_id, _stored_date(completion.local_date))
        for completion in data.completions
    }
    eligible = 0
    completed = 0

    for habit in data.habits:
        created = _stored_date(habit.created_local_date)
        first_day = max(week_start, created)
        habit_statuses = statuses.get(habit.habit_id, ())
        day = first_day
        while day <= week_end:
            if _is_active_on(created, habit_statuses, day):
                eligible += 1
                if (habit.habit_id, day) in completions:
                    completed += 1
            day += timedelta(days=1)

    percentage = 100.0 * completed / eligible if eligible else 0.0
    return WeeklyProgress(week_start, week_end, completed, eligible, percentage)


def _validate_data(data: StoreData) -> None:
    if not isinstance(data, StoreData):
        raise TypeError("data must be a StoreData instance.")


def _validate_date_argument(value: date, name: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{name} must be a date.")


def _stored_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Habit data contains an invalid local date.") from exc
    if parsed.isoformat() != value:
        raise ValueError("Habit data contains an invalid local date.")
    return parsed


def _statuses_by_habit(data: StoreData) -> dict[str, tuple[tuple[date, bool], ...]]:
    statuses: dict[str, list[tuple[date, bool]]] = {}
    for status in data.active_status_history:
        statuses.setdefault(status.habit_id, []).append(
            (_stored_date(status.local_date), status.is_active)
        )
    return {
        habit_id: tuple(sorted(events, key=lambda event: event[0]))
        for habit_id, events in statuses.items()
    }


def _active_habit_ids_on(data: StoreData, current_date: date) -> set[str]:
    statuses = _statuses_by_habit(data)
    return {
        habit.habit_id
        for habit in data.habits
        if _is_active_on(
            _stored_date(habit.created_local_date),
            statuses.get(habit.habit_id, ()),
            current_date,
        )
    }


def _is_active_on(
    created_date: date,
    statuses: tuple[tuple[date, bool], ...],
    target_date: date,
) -> bool:
    if target_date < created_date:
        return False

    is_active = False
    for effective_date, status in statuses:
        if effective_date > target_date:
            break
        is_active = status
    return is_active
