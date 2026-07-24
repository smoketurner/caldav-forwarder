"""Build the outbound iMIP 'Busy' calendar message from an outbox payload."""

from __future__ import annotations

from datetime import UTC, date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, vCalAddress, vRecur, vText

_PRODID = "-//smoketurner//caldav-forwarder//EN"
_UID_NAMESPACE = "caldav-forwarder"


def build_message(
    action: str,
    payload: dict[str, Any],
    organizer_email: str,
    dest_email: str,
    *,
    now: datetime | None = None,
) -> MIMEMultipart:
    """Build the raw MIME iMIP message carrying a Busy hold for the given change."""
    calendar = _build_calendar(action, payload, organizer_email, dest_email, now=now)
    ics = calendar.to_ical().decode("utf-8")
    method = "CANCEL" if action == "CANCEL" else "REQUEST"

    message = MIMEMultipart("mixed")
    message["Subject"] = "Canceled: Busy" if action == "CANCEL" else "Busy"
    message["From"] = organizer_email
    message["To"] = dest_email
    message.attach(MIMEText("Automated calendar hold.", "plain", "utf-8"))

    calendar_part = MIMEText(ics, "calendar", "utf-8")
    calendar_part.set_param("method", method)
    calendar_part.set_param("name", "invite.ics")
    message.attach(calendar_part)
    return message


def _build_calendar(
    action: str,
    payload: dict[str, Any],
    organizer_email: str,
    dest_email: str,
    *,
    now: datetime | None,
) -> Calendar:
    is_cancel = action == "CANCEL"
    now = now or datetime.now(UTC)

    calendar = Calendar()
    calendar.add("prodid", _PRODID)
    calendar.add("version", "2.0")
    calendar.add("method", "CANCEL" if is_cancel else "REQUEST")

    event = Event()
    event.add("uid", f"{_UID_NAMESPACE}-{payload['uid']}")
    event.add("dtstamp", now)
    event.add("sequence", int(payload["sequence"]))
    event.add("dtstart", _to_dt(payload["dtstart"]))
    event.add("dtend", _to_dt(payload["dtend"]))
    if payload.get("rrule"):
        event.add("rrule", vRecur.from_ical(payload["rrule"]))
    for exdate in payload.get("exdate") or []:
        event.add("exdate", _to_dt(exdate))
    if payload.get("recurrence_id"):
        event.add("recurrence-id", _to_dt(payload["recurrence_id"]))
    event.add("summary", "Busy")
    if payload.get("summary"):
        # Real title goes in the body, not the subject; CLASS:PRIVATE keeps it hidden
        # from anyone but the calendar owner.
        event.add("description", payload["summary"])
    event.add("transp", "OPAQUE")
    event.add("class", "PRIVATE")
    event.add("status", "CANCELLED" if is_cancel else "CONFIRMED")
    event["organizer"] = _address(organizer_email, "Calendar Hold")
    event.add("attendee", _attendee(dest_email))

    calendar.add_component(event)
    calendar.add_missing_timezones()
    return calendar


def _address(email: str, common_name: str) -> vCalAddress:
    address = vCalAddress(f"MAILTO:{email}")
    address.params["cn"] = vText(common_name)
    return address


def _attendee(email: str) -> vCalAddress:
    attendee = _address(email, email)
    attendee.params["role"] = vText("REQ-PARTICIPANT")
    attendee.params["partstat"] = vText("NEEDS-ACTION")
    attendee.params["rsvp"] = vText("TRUE")
    return attendee


def _to_dt(spec: dict[str, Any]) -> date | datetime:
    kind = spec["kind"]
    if kind == "date":
        return date.fromisoformat(spec["value"])
    naive = datetime.fromisoformat(spec["value"])
    if kind == "zoned":
        return naive.replace(tzinfo=ZoneInfo(spec["tzid"]))
    if kind == "utc":
        return naive.replace(tzinfo=UTC)
    return naive
