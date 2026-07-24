"""Parser behavior against an anonymized real iCloud published feed.

The fixture keeps iCloud's actual serialization of dates/recurrence (TZID times,
VALUE=DATE all-day, RRULE with UNTIL, EXDATE, RECURRENCE-ID overrides) while all
identifying content has been replaced or removed.
"""

from __future__ import annotations

from caldav_forwarder import ical


def _by_uid(events: list[ical.SourceEvent]) -> dict[str, ical.SourceEvent]:
    return {e.uid: e for e in events}


def test_birthday_and_anniversary_are_skipped(icloud_ics: bytes) -> None:
    uids = {e.uid for e in ical.parse(icloud_ics)}
    assert "evt-birthday" not in uids
    assert "evt-anniversary" not in uids


def test_timed_event_uses_source_timezone(icloud_ics: bytes) -> None:
    event = _by_uid(ical.parse(icloud_ics))["evt-timed"]
    assert event.payload["dtstart"] == {
        "kind": "zoned",
        "tzid": "America/New_York",
        "value": "2021-01-17T12:00:00",
    }


def test_allday_event_is_a_date(icloud_ics: bytes) -> None:
    event = _by_uid(ical.parse(icloud_ics))["evt-allday"]
    assert event.payload["dtstart"]["kind"] == "date"


def test_recurring_event_keeps_rrule_and_exdate(icloud_ics: bytes) -> None:
    event = _by_uid(ical.parse(icloud_ics))["evt-recurring"]
    assert event.payload["rrule"] == "FREQ=WEEKLY;UNTIL=20220618T150000Z"
    assert len(event.payload["exdate"]) == 1


def test_recurring_series_master_and_override_are_separate(icloud_ics: bytes) -> None:
    series = [e for e in ical.parse(icloud_ics) if e.uid == "evt-series"]
    keys = {e.instance_key for e in series}

    assert len(series) == 2
    assert "MASTER" in keys
    assert any(k != "MASTER" for k in keys)
    master = next(e for e in series if e.instance_key == "MASTER")
    assert master.payload["rrule"] == "FREQ=WEEKLY;UNTIL=20251205T153000Z;INTERVAL=2"
