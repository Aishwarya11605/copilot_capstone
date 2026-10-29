"""HabitFlow's minimal local web application."""

from datetime import date, timedelta
from urllib.parse import urlsplit

from flask import Flask, redirect, render_template, request, url_for

from habitflow.habits import HabitManager
from habitflow.progress import (
    active_habit_ids_on,
    calculate_daily_progress,
    calculate_weekly_progress,
)
from habitflow.storage import (
    SQLiteStore,
    StorageError,
    StorageOperationError,
    StorageOutcomeUnknownError,
)

_LOAD_FAILURE_COPY = (
    "HabitFlow couldn't load a valid database. Existing data was not reset or "
    "replaced. Check database access and restart HabitFlow. Changes are disabled "
    "until the database can be loaded."
)
_ROLLED_BACK_COPY = (
    "HabitFlow couldn't save this change; the database confirmed it was rolled "
    "back. Check database access, then try again."
)
_UNKNOWN_OUTCOME_COPY = (
    "HabitFlow couldn't confirm whether this change was saved. Reload HabitFlow "
    "and check the current data before trying again."
)


def create_app(config: dict | None = None) -> Flask:
    """Create the server-rendered HabitFlow application."""
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = None
    if config:
        app.config.update(config)

    app.extensions["habitflow_storage"] = None
    app.extensions["habitflow_storage_state"] = None

    @app.before_request
    def reject_cross_origin_posts():
        if request.method != "POST":
            return None

        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source is None:
            return "Cannot verify the request origin.", 403

        try:
            source_origin = urlsplit(source)
            target_origin = urlsplit(request.host_url)
        except ValueError:
            return "Cross-origin data changes are not allowed.", 403

        if (
            source_origin.scheme.casefold(),
            source_origin.netloc.casefold(),
        ) != (
            target_origin.scheme.casefold(),
            target_origin.netloc.casefold(),
        ):
            return "Cross-origin data changes are not allowed.", 403
        return None

    def set_storage_error(exc: StorageError) -> dict:
        if isinstance(exc, StorageOutcomeUnknownError):
            message = _UNKNOWN_OUTCOME_COPY
        elif isinstance(exc, StorageOperationError):
            message = _ROLLED_BACK_COPY
        else:
            message = _LOAD_FAILURE_COPY
        state = {
            "status": "error",
            "data": None,
            "error": exc,
            "message": message,
            "data_changes_enabled": False,
        }
        app.extensions["habitflow_storage_state"] = state
        return state

    def load_storage_state() -> dict:
        store = app.extensions["habitflow_storage"]
        try:
            if store is None:
                store = SQLiteStore(app.config["DATABASE_PATH"])
                app.extensions["habitflow_storage"] = store
            data = store.load()
        except StorageError as exc:
            return set_storage_error(exc)

        state = {
            "status": "ready",
            "data": data,
            "error": None,
            "message": None,
            "data_changes_enabled": True,
        }
        app.extensions["habitflow_storage_state"] = state
        return state

    def current_week_start(today: date) -> date:
        return today - timedelta(days=today.weekday())

    def parse_week_start(value: str | None, today: date) -> date:
        latest_week = current_week_start(today)
        if value is None:
            return latest_week - timedelta(days=7)
        try:
            week_start = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Select a valid Monday for the displayed week.") from exc
        if week_start.isoformat() != value or week_start.weekday() != 0:
            raise ValueError("Select a valid Monday for the displayed week.")
        if week_start > latest_week:
            raise ValueError("Select a week no later than the current local week.")
        return week_start

    def redirect_to_selected_week():
        try:
            week_start = parse_week_start(
                request.form.get("week_start"), date.today()
            )
        except ValueError:
            return redirect(url_for("index"), code=303)
        return redirect(
            url_for("index", week_start=week_start.isoformat()),
            code=303,
        )

    def render_state(
        state: dict,
        *,
        form_error: str | None = None,
        create_value: str = "",
        edit_values=None,
        week_start: date | None = None,
        week_error: str | None = None,
        status: int = 200,
    ):
        if state["status"] != "ready":
            return render_template("index.html", state=state), 503

        today_date = date.today()
        current_week = current_week_start(today_date)
        selected_week = week_start or current_week - timedelta(days=7)
        daily_progress = calculate_daily_progress(state["data"], today_date)
        weekly_progress = calculate_weekly_progress(state["data"], selected_week)
        today = today_date.isoformat()
        completed_today_ids = {
            completion.habit_id
            for completion in state["data"].completions
            if completion.local_date == today
        }
        return (
            render_template(
                "index.html",
                state=state,
                form_error=form_error,
                create_value=create_value,
                edit_values=edit_values or {},
                today=today,
                completed_today_ids=completed_today_ids,
                active_habit_ids=active_habit_ids_on(state["data"], today_date),
                daily_progress=daily_progress,
                weekly_progress=weekly_progress,
                selected_week_start=selected_week,
                current_week_start=current_week,
                previous_week_start=selected_week - timedelta(days=7),
                next_week_start=selected_week + timedelta(days=7),
                can_select_next_week=selected_week < current_week,
                week_error=week_error,
            ),
            status,
        )

    def render_home(*, form_error: str | None = None, edit_values=None, status=200):
        state = load_storage_state()
        if state["status"] != "ready":
            return render_state(state)
        today = date.today()
        try:
            week_start = parse_week_start(
                request.args.get("week_start"),
                today,
            )
        except ValueError as exc:
            return render_state(
                state,
                form_error=form_error,
                edit_values=edit_values,
                week_start=current_week_start(today) - timedelta(days=7),
                week_error=str(exc),
                status=400,
            )
        return render_state(
            state,
            form_error=form_error,
            edit_values=edit_values,
            week_start=week_start,
            status=status,
        )

    load_storage_state()

    @app.get("/")
    def index():
        return render_home()

    @app.post("/habits")
    def create_habit():
        state = load_storage_state()
        if state["status"] != "ready":
            return render_state(state)

        try:
            HabitManager(app.extensions["habitflow_storage"]).create(
                request.form.get("name", "")
            )
        except ValueError as exc:
            return render_state(
                state,
                form_error=str(exc),
                create_value=request.form.get("name", ""),
                status=400,
            )
        except StorageError as exc:
            state = set_storage_error(exc)
            return render_state(state)
        return redirect_to_selected_week()

    @app.post("/habits/<habit_id>/edit")
    def edit_habit(habit_id: str):
        state = load_storage_state()
        if state["status"] != "ready":
            return render_state(state)

        name = request.form.get("name", "")
        try:
            HabitManager(app.extensions["habitflow_storage"]).edit(habit_id, name)
        except ValueError as exc:
            return render_state(
                state,
                form_error=str(exc),
                edit_values={habit_id: name},
                status=400,
            )
        except StorageError as exc:
            state = set_storage_error(exc)
            return render_state(state)
        return redirect_to_selected_week()

    @app.post("/habits/<habit_id>/delete")
    def delete_habit(habit_id: str):
        if request.form.get("cancel") == "yes":
            return redirect_to_selected_week()

        state = load_storage_state()
        if state["status"] != "ready":
            return render_state(state)
        if request.form.get("confirmed") != "yes":
            return render_state(
                state,
                form_error="Confirm deletion before deleting this habit.",
                status=400,
            )

        try:
            HabitManager(app.extensions["habitflow_storage"]).delete(
                habit_id, confirmed=True
            )
        except ValueError as exc:
            return render_state(state, form_error=str(exc), status=400)
        except StorageError as exc:
            state = set_storage_error(exc)
            return render_state(state)
        return redirect_to_selected_week()

    def change_completion(habit_id: str, *, undo: bool):
        state = load_storage_state()
        if state["status"] != "ready":
            return render_state(state)

        manager = HabitManager(app.extensions["habitflow_storage"])
        try:
            if undo:
                manager.undo_completion(habit_id)
            else:
                manager.mark_complete(habit_id)
        except ValueError as exc:
            return render_state(state, form_error=str(exc), status=400)
        except StorageError as exc:
            state = set_storage_error(exc)
            return render_state(state)
        return redirect_to_selected_week()

    @app.post("/habits/<habit_id>/complete")
    def complete_habit(habit_id: str):
        return change_completion(habit_id, undo=False)

    @app.post("/habits/<habit_id>/undo-completion")
    def undo_habit_completion(habit_id: str):
        return change_completion(habit_id, undo=True)

    @app.post("/habits/<habit_id>/status")
    def change_habit_status(habit_id: str):
        state = load_storage_state()
        if state["status"] != "ready":
            return render_state(state)

        if not any(habit.habit_id == habit_id for habit in state["data"].habits):
            return render_state(
                state,
                form_error="Habit not found.",
                status=404,
            )

        submitted_status = request.form.get("is_active")
        if submitted_status not in {"true", "false"}:
            return render_state(
                state,
                form_error="Select a valid habit status.",
                status=400,
            )

        try:
            HabitManager(app.extensions["habitflow_storage"]).set_active(
                habit_id, submitted_status == "true"
            )
        except ValueError as exc:
            return render_state(state, form_error=str(exc), status=400)
        except StorageError as exc:
            state = set_storage_error(exc)
            return render_state(state)
        return redirect_to_selected_week()

    return app
