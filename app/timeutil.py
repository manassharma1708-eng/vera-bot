"""Small helpers for parsing the ISO timestamps the judge sends."""
from datetime import datetime, timedelta, timezone


def parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def plus_seconds(value, seconds: float) -> datetime | None:
    dt = parse_iso(value)
    return dt + timedelta(seconds=seconds) if dt else None
