"""Pure and mocked tests for macOS Reminders integration."""

from __future__ import annotations

import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.integrations.applescript import escape_applescript_string, run_applescript
from app.tools.registry import RiskLevel, ToolExecutionError, registry
from app.tools.reminders import (
    CreateReminderArgs,
    DeleteReminderArgs,
    ListRemindersArgs,
    _parse_records,
    create_reminder,
    delete_reminder,
    list_reminders,
    parse_due_datetime,
)


TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 10, 14, 0, tzinfo=TZ)  # Monday


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("tomorrow at 9am", datetime(2026, 8, 11, 9, 0, tzinfo=TZ)),
        ("in 30 minutes", datetime(2026, 8, 10, 14, 30, tzinfo=TZ)),
        ("next friday", datetime(2026, 8, 14, 9, 0, tzinfo=TZ)),
        ("today at 21:00", datetime(2026, 8, 10, 21, 0, tzinfo=TZ)),
        ("in 3 days at 7:30 pm", datetime(2026, 8, 13, 19, 30, tzinfo=TZ)),
    ],
)
def test_parse_natural_due_dates(value, expected):
    assert parse_due_datetime(value, tz=TZ, now=NOW) == expected


def test_parse_iso_due_date_preserves_offset():
    parsed = parse_due_datetime("2026-08-11T09:00:00+05:30", tz=TZ, now=NOW)
    assert parsed == datetime(2026, 8, 11, 9, 0, tzinfo=TZ)


def test_bare_time_uses_today_when_future_and_tomorrow_when_past():
    assert parse_due_datetime("at 5pm", tz=TZ, now=NOW) == datetime(2026, 8, 10, 17, 0, tzinfo=TZ)
    after_five = NOW.replace(hour=18)
    assert parse_due_datetime("at 5pm", tz=TZ, now=after_five) == datetime(2026, 8, 11, 17, 0, tzinfo=TZ)


def test_garbage_due_date_raises_actionable_error():
    with pytest.raises(ToolExecutionError, match="ISO-8601"):
        parse_due_datetime("whenever the moon is blue", tz=TZ, now=NOW)


def test_escape_applescript_quotes_backslashes_and_controls():
    assert escape_applescript_string('He said "hi" \\ bye') == 'He said \\"hi\\" \\\\ bye'
    assert escape_applescript_string("first\nsecond\tthird\x00") == "firstsecondthird"


def test_run_applescript_uses_argv_and_strips_stdout(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv == ["osascript", "-e", 'return "ok"']
        assert kwargs["timeout"] == 3
        assert kwargs["text"] is True
        return subprocess.CompletedProcess(argv, 0, " ok \n", "")

    monkeypatch.setattr("app.integrations.applescript.subprocess.run", fake_run)
    assert run_applescript('return "ok"', timeout=3) == "ok"


def test_run_applescript_translates_permission_error(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.applescript.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "execution error: Not authorized (-1743)"),
    )
    with pytest.raises(ToolExecutionError, match="System Settings"):
        run_applescript("script")


def test_create_reminder_builds_locale_independent_date(monkeypatch):
    scripts = []
    monkeypatch.setattr("app.tools.reminders.run_applescript", lambda script: scripts.append(script) or "")
    monkeypatch.setattr("app.tools.reminders.parse_due_datetime", lambda *args, **kwargs: datetime(2026, 8, 11, 9, 30, tzinfo=TZ))

    result = create_reminder(CreateReminderArgs(title='Submit "work"', due_at="tomorrow", notes="Bring \\ notes", list_name="School"))

    script = scripts[0]
    assert "set year of theDate to 2026" in script
    assert "set month of theDate to 8" in script
    assert "set day of theDate to 11" in script
    assert "set hours of theDate to 9" in script
    assert "set minutes of theDate to 30" in script
    assert 'name:"Submit \\"work\\""' in script
    assert 'set targetList to list "School"' in script
    assert result["due_at"] == "2026-08-11T09:30:00+05:30"


def test_list_reminders_parses_delimited_output(monkeypatch):
    output = "Task\x1f2026-08-11T09:00:00\x1ffalse\x1fSchool\x1fNotes\x1e"
    monkeypatch.setattr("app.tools.reminders.run_applescript", lambda script: output)
    result = list_reminders(ListRemindersArgs(limit=5))
    assert result == {"count": 1, "reminders": [{"title": "Task", "due_at": "2026-08-11T09:00:00", "completed": False, "list": "School", "notes": "Notes"}]}


def test_parse_records_skips_malformed_record():
    assert _parse_records("broken\x1erecord") == []


def test_delete_reminder_is_high_risk(monkeypatch):
    tool = registry.get("delete_reminder")
    assert tool is not None and tool.risk is RiskLevel.HIGH_RISK
    monkeypatch.setattr("app.tools.reminders.run_applescript", lambda script: "deleted")
    assert delete_reminder(DeleteReminderArgs(title="Task"))["deleted"] is True
