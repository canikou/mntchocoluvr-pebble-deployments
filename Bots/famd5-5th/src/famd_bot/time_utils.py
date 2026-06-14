from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DB_DT_FMT = "%Y-%m-%dT%H:%M:%S%z"


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_db_dt(value: str) -> datetime:
    return ensure_utc(datetime.strptime(value, DB_DT_FMT))


def format_db_dt(value: datetime) -> str:
    return ensure_utc(value).strftime(DB_DT_FMT)


def localize(value: datetime, timezone_name: str) -> datetime:
    return ensure_utc(value).astimezone(timezone_for(timezone_name))


def timezone_for(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name == "Asia/Manila":
            return timezone(timedelta(hours=8), "Asia/Manila")
        raise


def format_display_dt(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return ""
    local = localize(value, timezone_name)
    return f"{local.month}/{local.day}/{local.year} {format_display_time(value, timezone_name)}"


def format_display_time(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return ""
    local = localize(value, timezone_name)
    return local.strftime("%I:%M%p").lstrip("0")


def format_duration(total_minutes: int) -> str:
    hours, minutes = divmod(max(0, int(total_minutes)), 60)
    if hours == 0 and minutes == 0:
        return "0h0m"
    return f"{hours}h{minutes:02d}m"


def rendered_minutes(start_utc: datetime, end_utc: datetime) -> int:
    return max(0, int((ensure_utc(end_utc) - ensure_utc(start_utc)).total_seconds() // 60))


def parse_user_datetime(value: str, timezone_name: str) -> datetime:
    raw = " ".join(value.strip().split())
    if not raw:
        raise ValueError("Time cannot be blank.")
    parsed: datetime | None = None
    formats = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %I:%M%p",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %I:%M%p",
        "%m/%d/%y %H:%M",
        "%m/%d/%y %I:%M %p",
        "%m/%d/%y %I:%M%p",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw.upper(), fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("Use YYYY-MM-DD HH:MM, YYYY-MM-DD h:mm AM/PM, or M/D/YYYY h:mm AM/PM.")
    return parsed.replace(tzinfo=timezone_for(timezone_name)).astimezone(UTC)


def week_start_for(value: date, week_start: str = "sunday") -> date:
    normalized = week_start.strip().lower()
    if normalized == "monday":
        return value - timedelta(days=value.weekday())
    return value - timedelta(days=(value.weekday() + 1) % 7)


def split_shift_segments(start: datetime, end: datetime, timezone_name: str) -> list[tuple[datetime, datetime]]:
    start_local = localize(start, timezone_name)
    end_local = localize(end, timezone_name)
    if end_local < start_local:
        raise ValueError("Clock-out cannot be earlier than time-in.")
    if start_local.date() == end_local.date():
        return [(ensure_utc(start), ensure_utc(end))]

    segments: list[tuple[datetime, datetime]] = []
    zone = timezone_for(timezone_name)
    first_end = datetime.combine(start_local.date(), time(23, 59), zone)
    segments.append((start_local.astimezone(UTC), first_end.astimezone(UTC)))

    current_day = start_local.date() + timedelta(days=1)
    while current_day < end_local.date():
        seg_start = datetime.combine(current_day, time(0, 0), zone)
        seg_end = datetime.combine(current_day, time(23, 59), zone)
        segments.append((seg_start.astimezone(UTC), seg_end.astimezone(UTC)))
        current_day += timedelta(days=1)

    final_start = datetime.combine(end_local.date(), time(0, 0), zone)
    segments.append((final_start.astimezone(UTC), end_local.astimezone(UTC)))
    return segments
