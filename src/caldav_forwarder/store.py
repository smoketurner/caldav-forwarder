"""DynamoDB single-table access for event state and the outbox."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

_OUTBOX_TTL_SECONDS = 7 * 24 * 60 * 60
_serializer = TypeSerializer()
_deserializer = TypeDeserializer()
_client: Any = None


def _get_client() -> Any:
    global _client
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client


def _table_name() -> str:
    return os.environ["TABLE_NAME"]


def _to_item(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _serializer.serialize(value) for key, value in values.items()}


def _from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _deserializer.deserialize(value) for key, value in item.items()}


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
        for raw in response.get("Items", []):
            item = _from_item(raw)
            states[(item["uid"], item["instanceKey"])] = {
                "hash": item["contentHash"],
                "sequence": int(item["sequence"]),
                "payload": item["payload"],
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
    state_ttl: int | None = None,
    now: datetime | None = None,
) -> None:
    """Atomically write the event state and its outbox entry for one change."""
    uid, instance_key = event_key
    now = now or datetime.now(UTC)
    pk = f"EVENT#{uid}"
    outbox = {
        "Put": {
            "TableName": _table_name(),
            "Item": _to_item(
                {
                    "PK": pk,
                    "SK": f"OUTBOX#{instance_key}#{sequence:010d}",
                    "action": action,
                    "payload": {**payload, "sequence": sequence},
                    "ttl": int(now.timestamp()) + _OUTBOX_TTL_SECONDS,
                }
            ),
        }
    }
    _get_client().transact_write_items(
        TransactItems=[
            _state_op(pk, instance_key, uid, sequence, payload, content_hash, state_ttl, now),
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
    state_ttl: int | None,
    now: datetime,
) -> dict[str, Any]:
    key = {"PK": pk, "SK": f"STATE#{instance_key}"}
    if content_hash is None:
        return {"Delete": {"TableName": _table_name(), "Key": _to_item(key)}}
    item = {
        **key,
        "uid": uid,
        "instanceKey": instance_key,
        "contentHash": content_hash,
        "sequence": sequence,
        "payload": payload,
        "lastSeen": now.isoformat(),
    }
    if state_ttl is not None:
        item["ttl"] = state_ttl
    return {"Put": {"TableName": _table_name(), "Item": _to_item(item)}}
