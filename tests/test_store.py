"""End-to-end poll behavior over a moto DynamoDB table: create, update, cancel, idempotency."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from caldav_forwarder import poller, store

# Fixed clock so the sample feed's Jan/Feb 2026 events count as upcoming.
_DEFAULT_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _outbox_items(dynamodb: Any) -> list[dict[str, Any]]:
    items = dynamodb.scan(
        TableName="caldav-forwarder-test",
        FilterExpression="begins_with(SK, :s)",
        ExpressionAttributeValues={":s": {"S": "OUTBOX#"}},
    )["Items"]
    return sorted(items, key=lambda i: i["SK"]["S"])


def _run_poll(
    monkeypatch: pytest.MonkeyPatch, feed: bytes, context: Any, now: datetime = _DEFAULT_NOW
) -> dict[str, int]:
    monkeypatch.setenv("SOURCE_ICAL_URL", "https://example.com/cal.ics")
    monkeypatch.setattr("caldav_forwarder.ical.fetch", lambda _url: feed)
    monkeypatch.setattr("caldav_forwarder.poller._now", lambda: now)
    return poller.handler({}, context)


def test_first_poll_creates_non_cancelled_events(
    monkeypatch: pytest.MonkeyPatch, dynamodb: Any, sample_ics: bytes, lambda_context: Any
) -> None:
    result = _run_poll(monkeypatch, sample_ics, lambda_context)

    assert result == {"events": 4, "upcoming": 3, "changes": 3}
    assert set(store.scan_states()) == {
        ("timed-1@example.com", "MASTER"),
        ("allday-1@example.com", "MASTER"),
        ("recurring-1@example.com", "MASTER"),
    }
    outbox = _outbox_items(dynamodb)
    assert len(outbox) == 3
    assert {i["action"]["S"] for i in outbox} == {"CREATE"}


def test_second_identical_poll_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, dynamodb: Any, sample_ics: bytes, lambda_context: Any
) -> None:
    _run_poll(monkeypatch, sample_ics, lambda_context)
    result = _run_poll(monkeypatch, sample_ics, lambda_context)

    assert result["changes"] == 0
    assert len(_outbox_items(dynamodb)) == 3


def test_time_change_emits_update_with_next_sequence(
    monkeypatch: pytest.MonkeyPatch, dynamodb: Any, sample_ics: bytes, lambda_context: Any
) -> None:
    _run_poll(monkeypatch, sample_ics, lambda_context)
    moved = sample_ics.replace(b"20260115T100000", b"20260115T113000")

    result = _run_poll(monkeypatch, moved, lambda_context)

    assert result["changes"] == 1
    updates = [i for i in _outbox_items(dynamodb) if i["action"]["S"] == "UPDATE"]
    assert len(updates) == 1
    assert updates[0]["SK"]["S"].endswith("#0000000001")
    assert store.scan_states()[("timed-1@example.com", "MASTER")]["sequence"] == 1


def test_removed_event_emits_cancel_and_deletes_state(
    monkeypatch: pytest.MonkeyPatch, dynamodb: Any, sample_ics: bytes, lambda_context: Any
) -> None:
    _run_poll(monkeypatch, sample_ics, lambda_context)
    without_recurring = sample_ics.replace(
        b"""BEGIN:VEVENT
UID:recurring-1@example.com
DTSTAMP:20260101T000000Z
DTSTART;TZID=America/New_York:20260105T140000
DTEND;TZID=America/New_York:20260105T143000
RRULE:FREQ=WEEKLY;BYDAY=MO
EXDATE;TZID=America/New_York:20260119T140000
SUMMARY:Weekly 1:1
END:VEVENT
""",
        b"",
    )

    result = _run_poll(monkeypatch, without_recurring, lambda_context)

    assert result["changes"] == 1
    cancels = [i for i in _outbox_items(dynamodb) if i["action"]["S"] == "CANCEL"]
    assert len(cancels) == 1
    assert ("recurring-1@example.com", "MASTER") not in store.scan_states()


def test_cancel_payload_retains_dtstart_for_the_hold(
    monkeypatch: pytest.MonkeyPatch, dynamodb: Any, sample_ics: bytes, lambda_context: Any
) -> None:
    import json

    _run_poll(monkeypatch, sample_ics, lambda_context)
    without_timed = sample_ics.replace(
        b"""BEGIN:VEVENT
UID:timed-1@example.com
DTSTAMP:20260101T000000Z
DTSTART;TZID=America/New_York:20260115T090000
DTEND;TZID=America/New_York:20260115T100000
SUMMARY:Strategy sync
LOCATION:Room 5
DESCRIPTION:Confidential agenda
END:VEVENT
""",
        b"",
    )

    _run_poll(monkeypatch, without_timed, lambda_context)

    cancel = next(i for i in _outbox_items(dynamodb) if i["action"]["S"] == "CANCEL")
    payload = json.loads(cancel["payload"]["S"])
    assert payload["uid"] == "timed-1@example.com"
    assert payload["dtstart"]["value"] == "2026-01-15T09:00:00"


_MIXED_FEED = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:past-1@example.com
DTSTAMP:20190101T000000Z
DTSTART;TZID=America/New_York:20190115T090000
DTEND;TZID=America/New_York:20190115T100000
SUMMARY:Old meeting
END:VEVENT
BEGIN:VEVENT
UID:future-1@example.com
DTSTAMP:20260101T000000Z
DTSTART;TZID=America/New_York:20260215T090000
DTEND;TZID=America/New_York:20260215T100000
SUMMARY:Upcoming meeting
END:VEVENT
END:VCALENDAR
"""


def test_past_events_are_not_forwarded(
    monkeypatch: pytest.MonkeyPatch, dynamodb: Any, lambda_context: Any
) -> None:
    result = _run_poll(monkeypatch, _MIXED_FEED, lambda_context)

    assert result == {"events": 2, "upcoming": 1, "changes": 1}
    assert set(store.scan_states()) == {("future-1@example.com", "MASTER")}


def test_events_that_aged_into_the_past_are_not_cancelled(
    monkeypatch: pytest.MonkeyPatch, dynamodb: Any, sample_ics: bytes, lambda_context: Any
) -> None:
    _run_poll(monkeypatch, sample_ics, lambda_context, now=datetime(2026, 1, 1, tzinfo=UTC))

    # Same feed, but now the Jan/Feb one-off events are in the past.
    result = _run_poll(
        monkeypatch, sample_ics, lambda_context, now=datetime(2026, 6, 1, tzinfo=UTC)
    )

    assert result["changes"] == 0
    assert not [i for i in _outbox_items(dynamodb) if i["action"]["S"] == "CANCEL"]
    assert ("timed-1@example.com", "MASTER") in store.scan_states()
