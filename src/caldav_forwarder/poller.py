"""EventBridge Scheduler handler: poll the feed and emit outbox change events."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from caldav_forwarder import ical, store

logger = Logger()

# Only forward events ending within this grace of "now" or later, so the years of past
# events an iCloud feed carries are never turned into holds.
_FORWARD_GRACE = timedelta(days=1)


def _now() -> datetime:
    return datetime.now(UTC)


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    """Fetch the feed, diff upcoming events against stored state, write outbox changes."""
    raw = ical.fetch(os.environ["SOURCE_ICAL_URL"])
    events = ical.parse(raw)
    states = store.scan_states()
    now = _now()
    cutoff = now - _FORWARD_GRACE

    desired = {
        (e.uid, e.instance_key): e
        for e in events
        if not e.cancelled and ical.is_upcoming(e.payload, cutoff)
    }
    changes = _apply_creates_and_updates(desired, states, now)
    changes += _apply_cancels(desired, states, now, cutoff)

    logger.info(
        "poll complete",
        extra={"events": len(events), "upcoming": len(desired), "changes": changes},
    )
    return {"events": len(events), "upcoming": len(desired), "changes": changes}


def _apply_creates_and_updates(
    desired: dict[tuple[str, str], ical.SourceEvent],
    states: dict[tuple[str, str], dict[str, Any]],
    now: datetime,
) -> int:
    changes = 0
    for key, event in desired.items():
        existing = states.get(key)
        if existing is None:
            store.write_change(
                key, "CREATE", 0, event.payload, content_hash=event.content_hash, now=now
            )
            changes += 1
        elif existing["hash"] != event.content_hash:
            sequence = existing["sequence"] + 1
            store.write_change(
                key, "UPDATE", sequence, event.payload, content_hash=event.content_hash, now=now
            )
            changes += 1
    return changes


def _apply_cancels(
    desired: dict[tuple[str, str], ical.SourceEvent],
    states: dict[tuple[str, str], dict[str, Any]],
    now: datetime,
    cutoff: datetime,
) -> int:
    changes = 0
    for key, existing in states.items():
        if key in desired:
            continue
        if not ical.is_upcoming(existing["payload"], cutoff):
            continue  # leave holds for events that have already passed — never cancel them
        store.write_change(key, "CANCEL", existing["sequence"] + 1, existing["payload"], now=now)
        changes += 1
    return changes
