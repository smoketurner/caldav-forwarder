"""iMIP message building: Busy stripping, REQUEST/CANCEL semantics, recurrence."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message

from caldav_forwarder import invite

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_ORGANIZER = "holds@example.com"
_DEST = "you@work.example.com"


def _create_payload() -> dict:
    return {
        "uid": "recurring-1@example.com",
        "sequence": 0,
        "recurrence_id": None,
        "dtstart": {"kind": "zoned", "tzid": "America/New_York", "value": "2026-01-05T14:00:00"},
        "dtend": {"kind": "zoned", "tzid": "America/New_York", "value": "2026-01-05T14:30:00"},
        "rrule": "FREQ=WEEKLY;BYDAY=MO",
        "exdate": [{"kind": "zoned", "tzid": "America/New_York", "value": "2026-01-19T14:00:00"}],
    }


def _calendar_part(message: Message) -> Message:
    for part in message.walk():
        if part.get_content_type() == "text/calendar":
            return part
    raise AssertionError("no text/calendar part")


def _ics(payload: dict, action: str = "CREATE") -> str:
    message = invite.build_message(action, payload, _ORGANIZER, _DEST, now=_NOW)
    body = _calendar_part(message).get_payload(decode=True)
    assert isinstance(body, bytes)
    return body.decode("utf-8")


def test_request_is_busy_and_stripped() -> None:
    ics = _ics(_create_payload())
    assert "METHOD:REQUEST" in ics
    assert "SUMMARY:Busy" in ics
    assert "TRANSP:OPAQUE" in ics
    assert "CLASS:PRIVATE" in ics
    assert "STATUS:CONFIRMED" in ics
    assert "SEQUENCE:0" in ics
    assert "LOCATION" not in ics
    assert "DESCRIPTION" not in ics


def test_uid_is_namespaced_and_recurrence_preserved() -> None:
    ics = _ics(_create_payload())
    assert "UID:caldav-forwarder-recurring-1@example.com" in ics
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in ics
    assert "EXDATE" in ics


def test_zoned_event_includes_vtimezone() -> None:
    ics = _ics(_create_payload())
    assert "BEGIN:VTIMEZONE" in ics
    assert "TZID:America/New_York" in ics


def test_organizer_and_attendee_present() -> None:
    ics = _ics(_create_payload())
    assert f'ORGANIZER;CN="Calendar Hold":MAILTO:{_ORGANIZER}' in ics
    assert f"MAILTO:{_DEST}" in ics
    assert "RSVP=TRUE" in ics


def test_private_body_carries_original_title() -> None:
    ics = _ics({**_create_payload(), "summary": "Dentist appointment"})
    assert "SUMMARY:Busy" in ics  # subject stays generic
    assert "CLASS:PRIVATE" in ics  # body hidden from everyone but the owner
    assert "DESCRIPTION:Dentist appointment" in ics


def test_cancel_uses_cancel_method_and_status() -> None:
    payload = _create_payload()
    payload["sequence"] = 3
    ics = _ics(payload, action="CANCEL")
    assert "METHOD:CANCEL" in ics
    assert "STATUS:CANCELLED" in ics
    assert "SEQUENCE:3" in ics


def test_calendar_mime_part_declares_method() -> None:
    message = invite.build_message("CREATE", _create_payload(), _ORGANIZER, _DEST, now=_NOW)
    part = _calendar_part(message)
    assert part.get_param("method") == "REQUEST"
    assert message["Subject"] == "Busy"
