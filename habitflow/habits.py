"""Habit-domain operations backed by the approved storage boundary."""

from __future__ import annotations

from datetime import date

from habitflow.storage import HabitRecord, SQLiteStore


def _validated_name(name: object) -> str:
    if not isinstance(name, str):
        raise ValueError("Habit name must be a string.")
    normalized = name.strip()
    if not normalized:
        raise ValueError("Habit name cannot be blank.")
    return normalized


def _validated_habit_id(habit_id: object) -> str:
    if not isinstance(habit_id, str) or not habit_id:
        raise ValueError("Habit ID must be a non-empty string.")
    return habit_id


class HabitManager:
    """Apply approved habit rules while delegating persistence to SQLiteStore."""

    def __init__(self, store: SQLiteStore):
        self._store = store

    def create(self, name: str) -> str:
        """Create an active habit and return its stable ID."""
        normalized_name = _validated_name(name)
        habit_id = self._store.new_habit_id()
        self._store.create_habit(
            habit_id,
            normalized_name,
            date.today().isoformat(),
            True,
        )
        return habit_id

    def list(self) -> tuple[HabitRecord, ...]:
        """Return every currently stored habit."""
        return self._store.load().habits

    def edit(self, habit_id: str, name: str) -> None:
        """Rename one habit without altering its completion or status history."""
        stable_id = _validated_habit_id(habit_id)
        normalized_name = _validated_name(name)
        self._store.update_habit_name(stable_id, normalized_name)

    def delete(self, habit_id: str, *, confirmed: bool) -> bool:
        """Delete only the selected habit after explicit confirmation.

        Return False for cancellation without making any storage call.
        """
        stable_id = _validated_habit_id(habit_id)
        if type(confirmed) is not bool:
            raise ValueError("Deletion confirmation must be a boolean.")
        if not confirmed:
            return False
        self._store.delete_habit(stable_id)
        return True

    def set_active(self, habit_id: str, is_active: bool) -> None:
        """Set a habit's status effective on the device's current local date."""
        stable_id = _validated_habit_id(habit_id)
        if type(is_active) is not bool:
            raise ValueError("Active status must be a boolean.")
        self._store.record_active_status(
            stable_id,
            date.today().isoformat(),
            is_active,
        )

    def mark_complete(self, habit_id: str) -> bool:
        """Mark a habit complete for the device's current local date.

        Return whether a completion was newly recorded.
        """
        stable_id = _validated_habit_id(habit_id)
        return self._store.create_completion(stable_id, date.today().isoformat())

    def undo_completion(self, habit_id: str) -> bool:
        """Undo only this habit's completion for the device's current local date.

        Return whether a completion was removed.
        """
        stable_id = _validated_habit_id(habit_id)
        return self._store.delete_completion(stable_id, date.today().isoformat())
