"""DynamoDB single-table access for event state and the outbox."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import boto3

_TTL_SECONDS = 7 * 24 * 60 * 60
_client: Any = None


def _get_client() -> Any:
    global _client
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client


def _table_name() -> str:
    return os.environ["TABLE_NAME"]


def scan_states() -> dict[tuple[str, str], dict[str, Any]]:
    """Return every stored event state keyed by (uid, instance_key)."""
    client = _get_client()
    kwargs: dict[str, Any] = {
        "TableName": _table_name(),
        "FilterExpression": "begins_with(SK, :s)",
        "ExpressionAttributeValues": {":s": {"S": "STATE#"}},
    }
    states: dict[tuple[str, str], dict[str, Any]] = {}
    while True:
        response = client.scan(**kwargs)
        for item in response.get("Items", []):
            key = (item["uid"]["S"], item["instanceKey"]["S"])
            states[key] = {
                "hash": item["contentHash"]["S"],
                "sequence": int(item["sequence"]["N"]),
                "payload": json.loads(item["payload"]["S"]),
            }
        start = response.get("LastEvaluatedKey")
        if not start:
            return states
        kwargs["ExclusiveStartKey"] = start


def write_change(
    event_key: tuple[str, str],
    action: str,
    sequence: int,
    payload: dict[str, Any],
    *,
    content_hash: str | None = None,
    now: datetime | None = None,
) -> None:
    """Atomically write the event state and its outbox entry for one change."""
    uid, instance_key = event_key
    now = now or datetime.now(UTC)
    pk = f"EVENT#{uid}"
    ttl = int(now.timestamp()) + _TTL_SECONDS

    outbox = {
        "Put": {
            "TableName": _table_name(),
            "Item": {
                "PK": {"S": pk},
                "SK": {"S": f"OUTBOX#{instance_key}#{sequence:010d}"},
                "action": {"S": action},
                "payload": {"S": json.dumps({**payload, "sequence": sequence})},
                "ttl": {"N": str(ttl)},
            },
        }
    }
    _get_client().transact_write_items(
        TransactItems=[
            _state_op(pk, instance_key, uid, sequence, payload, content_hash, now),
            outbox,
        ]
    )


def _state_op(
    pk: str,
    instance_key: str,
    uid: str,
    sequence: int,
    payload: dict[str, Any],
    content_hash: str | None,
    now: datetime,
) -> dict[str, Any]:
    key = {"PK": {"S": pk}, "SK": {"S": f"STATE#{instance_key}"}}
    if content_hash is None:
        return {"Delete": {"TableName": _table_name(), "Key": key}}
    return {
        "Put": {
            "TableName": _table_name(),
            "Item": {
                **key,
                "uid": {"S": uid},
                "instanceKey": {"S": instance_key},
                "contentHash": {"S": content_hash},
                "sequence": {"N": str(sequence)},
                "payload": {"S": json.dumps(payload)},
                "lastSeen": {"S": now.isoformat()},
            },
        }
    }
