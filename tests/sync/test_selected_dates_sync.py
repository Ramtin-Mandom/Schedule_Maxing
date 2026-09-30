"""The page-selected real date and a date's own Day Window through synchronization: a task added
on a Week page's selected day reaches the server with that calendar date (never a day number),
and a per-date window override travels as that user's date preference layer to a second device,
where the default still applies to every other date."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.planning.preferences import DayWindowSpec, PreferenceOverrides
from app.ui.calendar_controller import CalendarController
from app.ui.day_window import DayWindowController
from app.ui.task_form_model import TaskDraft
from tests.sync.conftest import MON, InProcessTransport

WED = MON + timedelta(days=2)


@pytest.fixture
def pair(alice_server, make_device):
    a = make_device("a", InProcessTransport(alice_server.client))
    b = make_device("b", InProcessTransport(alice_server.client))
    return alice_server, a, b


def test_selected_date_and_day_window_reach_the_server_and_another_device(pair) -> None:
    server, a, b = pair
    a.sign_in("alice@example.com")
    week = CalendarController(a.controller, mode="week", selected=MON, timezone="UTC", today=lambda: MON)
    week.select(WED)
    saved = week.save_draft(TaskDraft(name="Essay", duration="45 min", date=week.form_date.isoformat()))
    assert saved.ok, saved.error
    user = a.controller.set_user_overrides(PreferenceOverrides(day_window=DayWindowSpec(start_minute=420,
                                                                                         end_minute=1320)))
    assert user.ok, user.error
    window = DayWindowController(a.controller)
    assert window.save(WED, "9:00 AM", "5:30 PM", expected_version=None).ok
    assert a.sync_now().status == "ok"

    tasks = server.get("alice@example.com", "/tasks")["items"]
    assert [(task["name"], task["preferred_dates"]) for task in tasks] == [("Essay", [WED.isoformat()])]

    b.sign_in("alice@example.com")
    assert b.sync_now().status == "ok"
    other = DayWindowController(b.controller)
    wednesday, tuesday = other.state(WED).value, other.state(MON + timedelta(days=1)).value
    assert (wednesday.start_minute, wednesday.end_minute, wednesday.overridden) == (540, 1050, True)
    assert (tuesday.start_minute, tuesday.end_minute, tuesday.overridden) == (420, 1320, False)
    assert b.planning.list_tasks()[0].preferred_dates == [WED]
