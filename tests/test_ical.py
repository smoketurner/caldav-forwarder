"""Feed parsing: birthday/anniversary filtering, field extraction, stable hashing."""

from __future__ import annotations

import urllib.request
from datetime import UTC, datetime
from typing import Any

import pytest

from caldav_forwarder import ical

_CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)


def _by_uid(events: list[ical.SourceEvent]) -> dict[str, ical.SourceEvent]:
    return {e.uid: e for e in events}


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def test_fetch_normalizes_webcal_to_https(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        seen["url"] = request.full_url
        return _FakeResponse(b"BEGIN:VCALENDAR")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ical.fetch("webcal://example.com/a.ics") == b"BEGIN:VCALENDAR"
    assert seen["url"] == "https://example.com/a.ics"


def test_fetch_rejects_plain_http() -> None:
    with pytest.raises(ValueError, match="https or webcal"):
        ical.fetch("http://example.com/a.ics")


def test_is_upcoming_filters_past_one_off() -> None:
    payload = {"rrule": None, "dtend": {"kind": "date", "value": "2019-06-01"}}
    assert ical.is_upcoming(payload, _CUTOFF) is False


def test_is_upcoming_keeps_future_one_off() -> None:
    payload = {"rrule": None, "dtend": {"kind": "date", "value": "2026-06-01"}}
    assert ical.is_upcoming(payload, _CUTOFF) is True


def test_is_upcoming_keeps_open_ended_recurrence_from_the_past() -> None:
    payload = {"rrule": "FREQ=WEEKLY;BYDAY=MO", "dtend": {"kind": "date", "value": "2019-01-01"}}
    assert ical.is_upcoming(payload, _CUTOFF) is True


def test_is_upcoming_filters_expired_recurrence() -> None:
    payload = {
        "rrule": "FREQ=WEEKLY;UNTIL=20200101T000000Z",
        "dtend": {"kind": "date", "value": "2019-01-01"},
    }
    assert ical.is_upcoming(payload, _CUTOFF) is False


def test_is_upcoming_keeps_recurrence_with_future_until() -> None:
    payload = {
        "rrule": "FREQ=WEEKLY;UNTIL=20260601T000000Z",
        "dtend": {"kind": "zoned", "tzid": "America/New_York", "value": "2026-01-05T14:00:00"},
    }
    assert ical.is_upcoming(payload, _CUTOFF) is True


def test_skips_birthdays_and_anniversaries(sample_ics: bytes) -> None:
    uids = {e.uid for e in ical.parse(sample_ics)}
    assert uids == {
        "timed-1@example.com",
        "allday-1@example.com",
        "recurring-1@example.com",
        "cancelled-1@example.com",
    }


def test_keeps_timed_allday_and_recurring(sample_ics: bytes) -> None:
    events = _by_uid(ical.parse(sample_ics))

    assert events["timed-1@example.com"].payload["dtstart"] == {
        "kind": "zoned",
        "tzid": "America/New_York",
        "value": "2026-01-15T09:00:00",
    }
    assert events["allday-1@example.com"].payload["dtstart"] == {
        "kind": "date",
        "value": "2026-02-01",
    }
    recurring = events["recurring-1@example.com"]
    assert recurring.payload["rrule"] == "FREQ=WEEKLY;BYDAY=MO"
    assert len(recurring.payload["exdate"]) == 1
    assert recurring.instance_key == "MASTER"


def test_cancelled_event_is_flagged(sample_ics: bytes) -> None:
    events = _by_uid(ical.parse(sample_ics))
    assert events["cancelled-1@example.com"].cancelled is True
    assert events["timed-1@example.com"].cancelled is False


def test_payload_captures_summary_but_not_location(sample_ics: bytes) -> None:
    payload = _by_uid(ical.parse(sample_ics))["timed-1@example.com"].payload
    assert set(payload) == {
        "uid",
        "recurrence_id",
        "dtstart",
        "dtend",
        "rrule",
        "exdate",
        "summary",
    }
    assert payload["summary"] == "Strategy sync"
    assert "location" not in payload  # location is still dropped entirely


def test_content_hash_is_stable_across_parses(sample_ics: bytes) -> None:
    first = _by_uid(ical.parse(sample_ics))
    second = _by_uid(ical.parse(sample_ics))
    assert first["timed-1@example.com"].content_hash == second["timed-1@example.com"].content_hash


def test_content_hash_changes_when_time_changes(sample_ics: bytes) -> None:
    original = _by_uid(ical.parse(sample_ics))["timed-1@example.com"].content_hash
    moved = sample_ics.replace(b"20260115T100000", b"20260115T110000")
    changed = _by_uid(ical.parse(moved))["timed-1@example.com"].content_hash
    assert original != changed


def test_floating_datetime_has_no_zone() -> None:
    feed = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:floating@example.com
DTSTAMP:20260101T000000Z
DTSTART:20260115T090000
DTEND:20260115T100000
SUMMARY:Floating
END:VEVENT
END:VCALENDAR
"""
    payload = ical.parse(feed)[0].payload
    assert payload["dtstart"] == {"kind": "floating", "value": "2026-01-15T09:00:00"}


def test_unresolvable_tzid_falls_back_to_utc() -> None:
    feed = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VTIMEZONE
TZID:GMT-0400
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:-0400
TZOFFSETTO:-0400
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:offset-tz@example.com
DTSTAMP:20260101T000000Z
DTSTART;TZID=GMT-0400:20260115T090000
DTEND;TZID=GMT-0400:20260115T100000
SUMMARY:Offset tz meeting
END:VEVENT
END:VCALENDAR
"""
    payload = ical.parse(feed)[0].payload
    assert payload["dtstart"] == {"kind": "utc", "value": "2026-01-15T13:00:00"}
    assert ical.is_upcoming(payload, _CUTOFF) is True  # must not raise on the fallback


def test_missing_dtend_defaults_to_one_hour() -> None:
    feed = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:no-end@example.com
DTSTART:20260115T090000Z
SUMMARY:Open ended
END:VEVENT
END:VCALENDAR
"""
    payload = ical.parse(feed)[0].payload
    assert payload["dtstart"] == {"kind": "utc", "value": "2026-01-15T09:00:00"}
    assert payload["dtend"] == {"kind": "utc", "value": "2026-01-15T10:00:00"}
