"""Fetch and parse the source iCal feed into normalized event records."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar

_SKIP_CATEGORIES = {"birthday", "anniversary"}
_SKIP_SUMMARY_TOKENS = ("birthday", "anniversary", "🎂")
_FETCH_TIMEOUT_SECONDS = 30
_DEFAULT_TIMED_DURATION = timedelta(hours=1)
_DEFAULT_ALLDAY_DURATION = timedelta(days=1)
_STATE_RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class SourceEvent:
    """A feed VEVENT reduced to the fields we forward, plus a change hash."""

    uid: str
    instance_key: str
    cancelled: bool
    payload: dict[str, Any]
    content_hash: str


def fetch(url: str) -> bytes:
    """GET the iCal feed over HTTPS.

    ``webcal://`` / ``webcals://`` share links (as handed out by iCloud) are normalized
    to ``https://``.

    Raises:
        ValueError: if the URL is not an https or webcal URL.
        urllib.error.URLError: on network/HTTP failure.
    """
    url = _https_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "caldav-forwarder/1.0"})  # noqa: S310 -- scheme normalized to https
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
        return response.read()


def _https_url(url: str) -> str:
    scheme, separator, remainder = url.partition("://")
    if separator and scheme.lower() in ("webcal", "webcals"):
        return f"https://{remainder}"
    if separator and scheme.lower() == "https":
        return url
    raise ValueError(f"SOURCE_ICAL_URL must be an https or webcal URL, got: {url!r}")


def parse(ics_bytes: bytes) -> list[SourceEvent]:
    """Parse feed bytes into forwardable events, skipping birthdays/anniversaries."""
    calendar = Calendar.from_ical(ics_bytes)
    events: list[SourceEvent] = []
    for component in calendar.walk("VEVENT"):
        if _is_skippable(component):
            continue
        event = _to_source_event(component)
        if event is not None:
            events.append(event)
    return events


def _is_skippable(component: Any) -> bool:
    if _categories(component) & _SKIP_CATEGORIES:
        return True
    summary = str(component.get("SUMMARY", "")).lower()
    return any(token in summary for token in _SKIP_SUMMARY_TOKENS)


def _categories(component: Any) -> set[str]:
    raw = component.get("CATEGORIES")
    if raw is None:
        return set()
    props = raw if isinstance(raw, list) else [raw]
    result: set[str] = set()
    for prop in props:
        values = getattr(prop, "cats", None) or [prop]
        for value in values:
            result.add(str(value).strip().lower())
    return result


def _to_source_event(component: Any) -> SourceEvent | None:
    uid = component.get("UID")
    dtstart_prop = component.get("DTSTART")
    if uid is None or dtstart_prop is None:
        return None
    uid = str(uid)
    recurrence = component.get("RECURRENCE-ID")
    instance_key = recurrence.to_ical().decode() if recurrence is not None else "MASTER"
    cancelled = str(component.get("STATUS", "")).upper() == "CANCELLED"
    payload: dict[str, Any] = {
        "uid": uid,
        "recurrence_id": _dt_payload(recurrence) if recurrence is not None else None,
        "dtstart": _dt_payload(dtstart_prop),
        "dtend": _dtend_payload(component, dtstart_prop),
        "rrule": _prop_ical(component, "RRULE"),
        "exdate": _exdates(component),
        "summary": str(component.get("SUMMARY", "")) or None,
    }
    return SourceEvent(
        uid=uid,
        instance_key=instance_key,
        cancelled=cancelled,
        payload=payload,
        content_hash=_hash(cancelled, payload),
    )


def _dt_payload(prop: Any) -> dict[str, Any]:
    return _dt_payload_from_value(prop.dt, prop.params.get("TZID"))


@cache
def _resolvable_tzid(tzid: str) -> bool:
    """Whether ``tzid`` is an IANA zone ZoneInfo can load (feeds also carry GMT-offset
    and Windows names, which cannot)."""
    try:
        ZoneInfo(tzid)
    except ZoneInfoNotFoundError, ValueError:  # PEP 758 unparenthesized multi-except (3.14+)
        return False
    return True


def _dt_payload_from_value(value: date | datetime, tzid: str | None) -> dict[str, Any]:
    if isinstance(value, datetime):
        if tzid and _resolvable_tzid(str(tzid)):
            return {
                "kind": "zoned",
                "tzid": str(tzid),
                "value": value.replace(tzinfo=None).isoformat(),
            }
        if value.tzinfo is not None:
            utc = value.astimezone(UTC).replace(tzinfo=None)
            return {"kind": "utc", "value": utc.isoformat()}
        return {"kind": "floating", "value": value.isoformat()}
    return {"kind": "date", "value": value.isoformat()}


def _dtend_payload(component: Any, dtstart_prop: Any) -> dict[str, Any]:
    dtend_prop = component.get("DTEND")
    if dtend_prop is not None:
        return _dt_payload(dtend_prop)
    start = dtstart_prop.dt
    tzid = dtstart_prop.params.get("TZID")
    duration = component.get("DURATION")
    if duration is not None:
        return _dt_payload_from_value(start + duration.dt, tzid)
    default = _DEFAULT_TIMED_DURATION if isinstance(start, datetime) else _DEFAULT_ALLDAY_DURATION
    return _dt_payload_from_value(start + default, tzid)


def _prop_ical(component: Any, name: str) -> str | None:
    prop = component.get(name)
    return prop.to_ical().decode() if prop is not None else None


def _exdates(component: Any) -> list[dict[str, Any]]:
    raw = component.get("EXDATE")
    if raw is None:
        return []
    props = raw if isinstance(raw, list) else [raw]
    result: list[dict[str, Any]] = []
    for prop in props:
        tzid = prop.params.get("TZID")
        for item in prop.dts:
            result.append(_dt_payload_from_value(item.dt, tzid))
    return result


def _hash(cancelled: bool, payload: dict[str, Any]) -> str:
    canonical = json.dumps({"cancelled": cancelled, "payload": payload}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def is_upcoming(payload: dict[str, Any], cutoff: datetime) -> bool:
    """True if the event still occurs at or after ``cutoff``.

    Recurring events are kept unless their RRULE ``UNTIL`` has passed; an RRULE with no
    ``UNTIL`` recurs indefinitely and is always upcoming. One-off events are kept while
    their end is at or after ``cutoff``. This filters out the years of past events an
    iCloud feed carries so only current/future holds are forwarded.
    """
    rrule = payload.get("rrule")
    if rrule:
        until = _rrule_until(rrule)
        return until is None or until >= cutoff
    return _end_utc(payload["dtend"]) >= cutoff


def _rrule_until(rrule: str) -> datetime | None:
    for part in rrule.split(";"):
        key, _, value = part.partition("=")
        if key.upper() == "UNTIL":
            return _parse_until(value.strip())
    return None


def _parse_until(value: str) -> datetime:
    value = value.removesuffix("Z")
    fmt = "%Y%m%dT%H%M%S" if "T" in value else "%Y%m%d"
    return datetime.strptime(value, fmt).replace(tzinfo=UTC)


def _end_utc(dtend: dict[str, Any]) -> datetime:
    if dtend["kind"] == "date":
        day = date.fromisoformat(dtend["value"])
        return datetime(day.year, day.month, day.day, tzinfo=UTC)
    naive = datetime.fromisoformat(dtend["value"])
    if dtend["kind"] == "zoned":
        return naive.replace(tzinfo=ZoneInfo(dtend["tzid"])).astimezone(UTC)
    return naive.replace(tzinfo=UTC)


def state_ttl(payload: dict[str, Any]) -> int | None:
    """DynamoDB TTL epoch for a STATE row: ~a month past the event's end (or its RRULE
    ``UNTIL``). Returns ``None`` for open-ended recurrences, which must persist while the
    series is active — otherwise the row would expire and be re-created as a duplicate.
    """
    rrule = payload.get("rrule")
    if rrule:
        until = _rrule_until(rrule)
        if until is None:
            return None
        end = until
    else:
        end = _end_utc(payload["dtend"])
    return int((end + _STATE_RETENTION).timestamp())
