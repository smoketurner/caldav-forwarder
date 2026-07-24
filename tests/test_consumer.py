"""Consumer path: parse an outbox record, build the iMIP message, send via SES."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from caldav_forwarder import consumer

_ORGANIZER = "holds@example.com"
_DEST = "you@work.example.com"
_PK = "EVENT#timed-1@example.com"
_SK = "OUTBOX#MASTER#0000000000"


def _payload() -> dict[str, Any]:
    return {
        "uid": "timed-1@example.com",
        "sequence": 0,
        "recurrence_id": None,
        "dtstart": {"kind": "zoned", "tzid": "America/New_York", "value": "2026-01-15T09:00:00"},
        "dtend": {"kind": "zoned", "tzid": "America/New_York", "value": "2026-01-15T10:00:00"},
        "rrule": None,
        "exdate": [],
    }


def _record(message_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    new_image = {
        "PK": {"S": _PK},
        "SK": {"S": _SK},
        "action": {"S": action},
        "payload": {"S": json.dumps(payload)},
    }
    body = {"eventName": "INSERT", "dynamodb": {"NewImage": new_image}}
    return {"messageId": message_id, "body": json.dumps(body)}


@pytest.fixture
def ses(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("ORGANIZER_EMAIL", _ORGANIZER)
    monkeypatch.setenv("DEST_EMAIL", _DEST)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=os.environ["IDEMPOTENCY_TABLE_NAME"],
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client = boto3.client("ses", region_name="us-east-1")
        client.verify_email_identity(EmailAddress=_ORGANIZER)
        consumer._client = None
        yield client
        consumer._client = None


def test_create_record_is_sent(ses: Any, lambda_context: Any) -> None:
    result = consumer.handler({"Records": [_record("m0", "CREATE", _payload())]}, lambda_context)
    assert result == {"batchItemFailures": []}
    assert ses.get_send_quota()["SentLast24Hours"] >= 1.0


def test_cancel_record_is_sent(ses: Any, lambda_context: Any) -> None:
    payload = {**_payload(), "sequence": 2}
    result = consumer.handler({"Records": [_record("m0", "CANCEL", payload)]}, lambda_context)
    assert result == {"batchItemFailures": []}


def test_partial_batch_reports_only_failures(ses: Any, lambda_context: Any) -> None:
    good = _record("good", "CREATE", _payload())
    bad = {"messageId": "bad", "body": "not-json"}
    result = consumer.handler({"Records": [good, bad]}, lambda_context)
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    assert ses.get_send_quota()["SentLast24Hours"] >= 1.0


def test_duplicate_outbox_record_is_sent_once(ses: Any, lambda_context: Any) -> None:
    payload = _payload()
    consumer.handler({"Records": [_record("m0", "CREATE", payload)]}, lambda_context)
    after_first = ses.get_send_quota()["SentLast24Hours"]

    # Same outbox record redelivered by SQS (new messageId, identical PK#SK).
    consumer.handler({"Records": [_record("m1", "CREATE", payload)]}, lambda_context)
    after_second = ses.get_send_quota()["SentLast24Hours"]

    assert after_second == after_first  # idempotency suppressed the duplicate send
