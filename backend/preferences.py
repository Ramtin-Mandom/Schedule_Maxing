"""
backend/preferences.py

Server-side resolution of a user's effective scheduling preferences, with
exactly the layers and rules scheduling uses on the desktop
(app/planning/preferences.py, resolve_day_preferences):

    built-in defaults -> the YAML template (config/task_preference.yaml,
    app.planning.preferences.default_preference_template) -> the user's
    stored user layer -> the user's stored layer of that date

Only the caller's own live layers are read. Reading the template needs
PyYAML (requirements-backend.txt); nothing else from the desktop is imported.
"""

from __future__ import annotations

import uuid
from datetime import date as date_

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.planning.preferences import (
    DayPreferences,
    PreferenceOverrides,
    default_preference_template,
    resolve_day_preferences,
)
from backend import models
from backend.record_mapping import preference_overrides


def stored_layers(session: Session, user_id: uuid.UUID, days: list[date_]) -> dict[str, PreferenceOverrides]:
    """The user's live layers keyed by scope_key ("user" or an ISO date), for the user layer and `days`."""
    keys = ["user", *(day.isoformat() for day in days)]
    rows = session.scalars(
        select(models.Preference).where(
            models.Preference.user_id == user_id,
            models.Preference.scope_key.in_(keys),
            models.Preference.deleted_at.is_(None),
        )
    )
    return {row.scope_key: preference_overrides(row) for row in rows}


def effective_day_preferences(
    session: Session, user_id: uuid.UUID, days: list[date_], timezone_name: str
) -> dict[date_, DayPreferences]:
    """Each date's effective preferences for `user_id`, resolved in `timezone_name`."""
    layers = stored_layers(session, user_id, days)
    template = default_preference_template()
    return {
        day: resolve_day_preferences(
            date=day, timezone=timezone_name, yaml_overrides=template,
            user_overrides=layers.get("user"), date_overrides=layers.get(day.isoformat()),
        )
        for day in days
    }
