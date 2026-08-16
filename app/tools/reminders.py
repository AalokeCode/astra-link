"""Native macOS Reminders tools implemented through AppleScript."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.config import load_config
from app.integrations.applescript import escape_applescript_string, run_applescript
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, registry


FIELD_SEPARATOR = "\x1f"
RECORD_SEPARATOR = "\x1e"
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_TIME_RE = re.compile(r"(?:^|\s+)at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)

# A date with no time attached means 09:00 — early enough to act on, late
# enough not to wake anyone. "tonight" is the one exception.
DEFAULT_CLOCK = time(9, 0)
DEFAULT_HOUR = DEFAULT_CLOCK.hour
TONIGHT_CLOCK = time(20, 0)

# Matches a bare calendar date with no time component.
_ISO_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


class CreateReminderArgs(BaseModel):
    title: str = Field(min_length=1, description="Reminder title.")
    due_at: str | None = Field(default=None, description="ISO-8601 or common natural-language due date.")
    notes: str | None = Field(default=None, description="Optional reminder notes.")
    list_name: str | None = Field(default=None, description="Target Reminders list; uses the default list when omitted.")


class ListRemindersArgs(BaseModel):
    list_name: str | None = Field(default=None, description="List to read; reads all lists when omitted.")
    include_completed: bool = Field(default=False, description="Include completed reminders.")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum reminders to return.")


class CompleteReminderArgs(BaseModel):
    title: str = Field(min_length=1, description="Exact title of the reminder to complete.")
    list_name: str | None = Field(default=None, description="List to search; searches all lists when omitted.")


class DeleteReminderArgs(BaseModel):
    title: str = Field(min_length=1, description="Exact title of the reminder to delete.")
    list_name: str | None = Field(default=None, description="List to search; searches all lists when omitted.")


class ListReminderListsArgs(BaseModel):
    pass


def _parse_clock(hour_text: str, minute_text: str | None, meridiem: str | None) -> time:
    hour = int(hour_text)
    minute = int(minute_text or "0")
    if minute > 59:
        raise ValueError("minute out of range")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError("12-hour time out of range")
        hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    elif not 0 <= hour <= 23:
        raise ValueError("24-hour time out of range")
    return time(hour, minute)


def _parse_error(value: str) -> ToolExecutionError:
    return ToolExecutionError(
        f"could not parse due date {value!r}; use ISO-8601, today/tomorrow/tonight, "
        "next weekday, 'in N minutes/hours/days', or 'at 5pm'"
    )


def _local_now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def parse_due_datetime(value: str, *, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Parse ISO-8601 and the small natural-language date set used by reminders."""
    raw = value.strip()
    if not raw:
        raise _parse_error(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        # A bare date ("2026-08-11") carries no time, and fromisoformat defaults
        # it to midnight — which would fire a reminder while the user is asleep.
        # Apply the same 09:00 default the relative forms ("tomorrow") use.
        # Detected on the string, not on the parsed value, so an explicit
        # "2026-08-11T00:00" still means midnight.
        if _ISO_DATE_ONLY.fullmatch(raw):
            parsed = parsed.replace(hour=DEFAULT_HOUR, minute=0, second=0, microsecond=0)
        return parsed

    current = now or _local_now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    normalized = re.sub(r"\s+", " ", raw.lower()).strip()

    time_match = _TIME_RE.search(normalized)
    clock: time | None = None
    base_text = normalized
    if time_match:
        try:
            clock = _parse_clock(time_match.group(1), time_match.group(2), time_match.group(3))
        except ValueError as exc:
            raise _parse_error(value) from exc
        base_text = normalized[: time_match.start()].strip()

    if not base_text and clock is not None:
        candidate = datetime.combine(current.date(), clock, tzinfo=tz)
        return candidate if candidate > current else candidate + timedelta(days=1)

    relative = re.fullmatch(r"in\s+(\d+)\s+(minute|minutes|hour|hours|day|days)", base_text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit.startswith("minute"):
            candidate = current + timedelta(minutes=amount)
        elif unit.startswith("hour"):
            candidate = current + timedelta(hours=amount)
        else:
            candidate = current + timedelta(days=amount)
        return datetime.combine(candidate.date(), clock, tzinfo=tz) if clock else candidate

    if base_text in {"today", "tomorrow", "tonight"}:
        offset = 1 if base_text == "tomorrow" else 0
        default_clock = TONIGHT_CLOCK if base_text == "tonight" else DEFAULT_CLOCK
        return datetime.combine(current.date() + timedelta(days=offset), clock or default_clock, tzinfo=tz)

    weekday = re.fullmatch(r"next\s+(" + "|".join(_WEEKDAYS) + r")", base_text)
    if weekday:
        target_weekday = _WEEKDAYS[weekday.group(1)]
        days = (target_weekday - current.weekday()) % 7
        if days == 0:
            days = 7
        return datetime.combine(current.date() + timedelta(days=days), clock or DEFAULT_CLOCK, tzinfo=tz)

    raise _parse_error(value)


def _list_reference(list_name: str | None) -> str:
    if list_name:
        return f'list "{escape_applescript_string(list_name)}"'
    return "default list"


def _date_script(due: datetime) -> str:
    return f"""set theDate to current date
set year of theDate to {due.year}
set month of theDate to {due.month}
set day of theDate to {due.day}
set hours of theDate to {due.hour}
set minutes of theDate to {due.minute}
set seconds of theDate to {due.second}"""


def _parse_records(output: str) -> list[dict]:
    reminders: list[dict] = []
    for record in output.split(RECORD_SEPARATOR):
        if not record:
            continue
        fields = record.split(FIELD_SEPARATOR)
        if len(fields) != 5:
            continue
        title, due_at, completed, list_name, notes = fields
        reminders.append(
            {"title": title, "due_at": due_at or None, "completed": completed.lower() == "true", "list": list_name, "notes": notes}
        )
    return reminders


def _date_formatter_handlers() -> str:
    return """on pad2(value)
    set rendered to value as text
    if (count rendered) is 1 then return "0" & rendered
    return rendered
end pad2

on isoDate(value)
    if value is missing value then return ""
    return (year of value as text) & "-" & my pad2(month of value as integer) & "-" & my pad2(day of value) & "T" & my pad2(hours of value) & ":" & my pad2(minutes of value) & ":" & my pad2(seconds of value)
end isoDate"""


@registry.tool(
    name="create_reminder",
    description="Create a native macOS reminder, optionally with a due date, notes, and list.",
    args_model=CreateReminderArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.REMINDERS,
)
def create_reminder(args: CreateReminderArgs) -> dict:
    title = escape_applescript_string(args.title)
    notes = escape_applescript_string(args.notes or "")
    due = parse_due_datetime(args.due_at, tz=load_config().timezone) if args.due_at else None
    properties = f'name:"{title}", body:"{notes}"'
    prefix = ""
    if due:
        prefix = _date_script(due) + "\n"
        properties += ", due date:theDate"
    script = f"""{prefix}tell application "Reminders"
    set targetList to {_list_reference(args.list_name)}
    make new reminder at end of reminders of targetList with properties {{{properties}}}
end tell"""
    run_applescript(script)
    return {"created": True, "title": args.title, "due_at": due.isoformat() if due else None, "list": args.list_name}


@registry.tool(
    name="list_reminders",
    description="List native macOS reminders, optionally filtered by list and completion state.",
    args_model=ListRemindersArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.REMINDERS,
)
def list_reminders(args: ListRemindersArgs) -> dict:
    completed_filter = "true" if args.include_completed else "false"
    if args.list_name:
        list_setup = f'set sourceLists to {{{_list_reference(args.list_name)}}}'
    else:
        list_setup = "set sourceLists to lists"
    script = f"""{_date_formatter_handlers()}

tell application "Reminders"
    {list_setup}
    set fieldSep to ASCII character 31
    set recordSep to ASCII character 30
    set output to ""
    set emitted to 0
    repeat with sourceList in sourceLists
        repeat with itemReminder in reminders of sourceList
            if {completed_filter} or completed of itemReminder is false then
                set dueText to my isoDate(due date of itemReminder)
                set notesText to body of itemReminder
                if notesText is missing value then set notesText to ""
                set output to output & name of itemReminder & fieldSep & dueText & fieldSep & (completed of itemReminder as text) & fieldSep & name of sourceList & fieldSep & notesText & recordSep
                set emitted to emitted + 1
                if emitted is {args.limit} then return output
            end if
        end repeat
    end repeat
    return output
end tell"""
    reminders = _parse_records(run_applescript(script))[: args.limit]
    return {"count": len(reminders), "reminders": reminders}


@registry.tool(
    name="complete_reminder",
    description="Complete the soonest-due incomplete reminder whose title exactly matches.",
    args_model=CompleteReminderArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.REMINDERS,
)
def complete_reminder(args: CompleteReminderArgs) -> dict:
    title = escape_applescript_string(args.title)
    source = f'{{{_list_reference(args.list_name)}}}' if args.list_name else "lists"
    script = f"""tell application "Reminders"
    set sourceLists to {source}
    set bestReminder to missing value
    set bestDate to missing value
    set matchedCount to 0
    repeat with sourceList in sourceLists
        repeat with itemReminder in reminders of sourceList
            if name of itemReminder is "{title}" and completed of itemReminder is false then
                set matchedCount to matchedCount + 1
                set itemDate to due date of itemReminder
                if bestReminder is missing value or (itemDate is not missing value and (bestDate is missing value or itemDate < bestDate)) then
                    set bestReminder to itemReminder
                    set bestDate to itemDate
                end if
            end if
        end repeat
    end repeat
    if bestReminder is missing value then return "0"
    set completed of bestReminder to true
    return matchedCount as text
end tell"""
    output = run_applescript(script)
    matched = int(output) if output.isdigit() else 0
    if matched == 0:
        raise ToolExecutionError(f"no incomplete reminder found with title {args.title!r}")
    return {"completed": True, "title": args.title, "matched": matched, "list": args.list_name}


@registry.tool(
    name="delete_reminder",
    description="Permanently delete the first reminder whose title exactly matches.",
    args_model=DeleteReminderArgs,
    risk=RiskLevel.HIGH_RISK,
    group=ToolGroup.REMINDERS,
    confirm_template="Delete the reminder {title!r} from macOS Reminders?",
)
def delete_reminder(args: DeleteReminderArgs) -> dict:
    title = escape_applescript_string(args.title)
    source = f'{{{_list_reference(args.list_name)}}}' if args.list_name else "lists"
    script = f"""tell application "Reminders"
    set sourceLists to {source}
    repeat with sourceList in sourceLists
        repeat with itemReminder in reminders of sourceList
            if name of itemReminder is "{title}" then
                delete itemReminder
                return "deleted"
            end if
        end repeat
    end repeat
    return "not found"
end tell"""
    if run_applescript(script) != "deleted":
        raise ToolExecutionError(f"no reminder found with title {args.title!r}")
    return {"deleted": True, "title": args.title, "list": args.list_name}


@registry.tool(
    name="list_reminder_lists",
    description="List the names of all native macOS Reminders lists.",
    args_model=ListReminderListsArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.REMINDERS,
)
def list_reminder_lists(args: ListReminderListsArgs) -> dict:
    script = """tell application "Reminders"
    set recordSep to ASCII character 30
    set output to ""
    repeat with sourceList in lists
        set output to output & name of sourceList & recordSep
    end repeat
    return output
end tell"""
    names = [name for name in run_applescript(script).split(RECORD_SEPARATOR) if name]
    return {"count": len(names), "lists": names}
