"""Shared fixtures: env, a moto-backed DynamoDB table, and the sample feed."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_TABLE_NAME = "caldav-forwarder-test"
_IDEMPOTENCY_TABLE_NAME = "caldav-forwarder-idempotency-test"

# Set before importing the package: consumer.py builds a DynamoDBPersistenceLayer at
# import time, which reads IDEMPOTENCY_TABLE_NAME and needs a region for its boto3 client.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("TABLE_NAME", _TABLE_NAME)
os.environ.setdefault("IDEMPOTENCY_TABLE_NAME", _IDEMPOTENCY_TABLE_NAME)

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from caldav_forwarder import store  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("TABLE_NAME", _TABLE_NAME)
    monkeypatch.setenv("IDEMPOTENCY_TABLE_NAME", _IDEMPOTENCY_TABLE_NAME)


@pytest.fixture
def dynamodb() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        store._client = None
        yield client
        store._client = None


@pytest.fixture
def sample_ics() -> bytes:
    return (_FIXTURES / "sample.ics").read_bytes()


@pytest.fixture
def icloud_ics() -> bytes:
    """Anonymized subset of a real iCloud published feed (structure preserved, PII stripped)."""
    return (_FIXTURES / "icloud.ics").read_bytes()


@pytest.fixture
def lambda_context() -> Any:
    """Minimal object satisfying Powertools' inject_lambda_context and idempotency."""
    return SimpleNamespace(
        function_name="test",
        memory_limit_in_mb=128,
        invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:test",
        aws_request_id="test-request-id",
        get_remaining_time_in_millis=lambda: 30000,
    )
