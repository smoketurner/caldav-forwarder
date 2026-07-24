"""SQS handler: turn outbox records into iMIP 'Busy' messages and send via SES."""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.batch.types import PartialItemFailureResponse
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import DynamoDBRecord
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.typing import LambdaContext

from caldav_forwarder import invite

logger = Logger()
processor = BatchProcessor(event_type=EventType.SQS)
persistence = DynamoDBPersistenceLayer(table_name=os.environ["IDEMPOTENCY_TABLE_NAME"])
idempotency_config = IdempotencyConfig(event_key_jmespath="idempotency_key")
_client: Any = None


def _ses_client() -> Any:
    global _client
    if _client is None:
        _client = boto3.client("ses")
    return _client


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> PartialItemFailureResponse:
    """Send one Busy invite per outbox record; report failures for SQS redrive."""
    idempotency_config.register_lambda_context(context)
    organizer = os.environ["ORGANIZER_EMAIL"]
    dest = os.environ["DEST_EMAIL"]

    def record_handler(record: SQSRecord) -> None:
        _process(record, organizer, dest)

    return process_partial_response(event, record_handler, processor, context)


def _process(record: SQSRecord, organizer: str, dest: str) -> None:
    stream = DynamoDBRecord(json.loads(record.body)).dynamodb
    if stream is None or stream.new_image is None:
        raise ValueError("outbox stream record has no NewImage")
    new_image = stream.new_image
    record_data = {
        "idempotency_key": f"{new_image['PK']}#{new_image['SK']}",
        "action": str(new_image["action"]),
        "payload": json.loads(new_image["payload"]),
    }
    _send_invite(record_data=record_data, organizer=organizer, dest=dest)


@idempotent_function(
    data_keyword_argument="record_data",
    persistence_store=persistence,
    config=idempotency_config,
)
def _send_invite(record_data: dict[str, Any], organizer: str, dest: str) -> None:
    action = record_data["action"]
    payload = record_data["payload"]
    message = invite.build_message(action, payload, organizer, dest)
    _ses_client().send_raw_email(
        Source=organizer,
        Destinations=[dest],
        RawMessage={"Data": message.as_string()},
    )
    logger.info(
        "sent invite",
        extra={"action": action, "uid": payload.get("uid"), "sequence": payload.get("sequence")},
    )
