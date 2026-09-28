"""DynamoDB persistence for image metadata."""

from __future__ import annotations

import base64
import binascii
import json
import logging
from functools import reduce
from typing import Any, Dict, List, Optional, Tuple

from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from botocore.exceptions import ClientError

from .errors import ValidationError
from .models import ImageRecord, ImageStatus, ListQuery
from .schema import USER_INDEX_NAME

logger = logging.getLogger(__name__)

# With FilterExpressions a DynamoDB page can come back sparse or empty, so one
# API request may need several reads. Bound it so a single request can never
# turn into an unbounded table walk; the caller just gets a next_token.
MAX_PAGES_PER_REQUEST = 10

_TABLE_KEY = ("image_id",)
_USER_INDEX_KEY = ("image_id", "user_id", "created_at")


def _is_conditional_failure(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def encode_token(key: Dict[str, Any]) -> str:
    """Opaque pagination cursor (clients must not depend on its contents)."""
    raw = json.dumps(key, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_token(token: str, expected_keys: Tuple[str, ...]) -> Dict[str, Any]:
    try:
        padded = token + "=" * (-len(token) % 4)
        key = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ValidationError("next_token is invalid", {"field": "next_token"}) from None
    if not isinstance(key, dict) or set(key) != set(expected_keys) or not all(
        isinstance(v, str) for v in key.values()
    ):
        # Also catches a token from a user-filtered listing being replayed without the filter.
        raise ValidationError(
            "next_token is invalid or does not match these filters", {"field": "next_token"}
        )
    return key


class ImageRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    # ------------------------------------------------------------------ writes
    def create(self, record: ImageRecord) -> None:
        self._table.put_item(
            Item=record.to_item(),
            ConditionExpression="attribute_not_exists(image_id)",  # never overwrite
        )

    def mark_available(self, image_id: str, size_bytes: int, updated_at: str) -> bool:
        """PENDING -> AVAILABLE. Returns False if the image is no longer pending.

        The condition makes this idempotent: S3 events are delivered at-least-once.
        """
        try:
            self._table.update_item(
                Key={"image_id": image_id},
                UpdateExpression="SET #s = :available, size_bytes = :size, updated_at = :now",
                ConditionExpression="attribute_exists(image_id) AND #s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":available": ImageStatus.AVAILABLE.value,
                    ":pending": ImageStatus.PENDING.value,
                    ":size": size_bytes,
                    ":now": updated_at,
                },
            )
            return True
        except ClientError as err:
            if _is_conditional_failure(err):
                return False
            raise

    def mark_rejected(self, image_id: str, reason: str, updated_at: str) -> bool:
        try:
            self._table.update_item(
                Key={"image_id": image_id},
                UpdateExpression="SET #s = :rejected, rejection_reason = :reason, updated_at = :now",
                ConditionExpression="attribute_exists(image_id) AND #s = :pending",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":rejected": ImageStatus.REJECTED.value,
                    ":pending": ImageStatus.PENDING.value,
                    ":reason": reason,
                    ":now": updated_at,
                },
            )
            return True
        except ClientError as err:
            if _is_conditional_failure(err):
                return False
            raise

    def delete_owned(self, image_id: str, user_id: str) -> Optional[ImageRecord]:
        """Delete only if `user_id` owns the image. Returns the deleted record or None.

        The ownership check is part of the write itself, so there is no
        read-check-delete race between concurrent requests.
        """
        try:
            resp = self._table.delete_item(
                Key={"image_id": image_id},
                ConditionExpression="attribute_exists(image_id) AND user_id = :uid",
                ExpressionAttributeValues={":uid": user_id},
                ReturnValues="ALL_OLD",
            )
        except ClientError as err:
            if _is_conditional_failure(err):
                return None
            raise
        attrs = resp.get("Attributes")
        return ImageRecord.from_item(attrs) if attrs else None

    # ------------------------------------------------------------------- reads
    def get(self, image_id: str) -> Optional[ImageRecord]:
        item = self._table.get_item(Key={"image_id": image_id}).get("Item")
        return ImageRecord.from_item(item) if item else None

    def list(self, query: ListQuery) -> Tuple[List[ImageRecord], Optional[str]]:
        filters: List[ConditionBase] = [Attr("status").eq(ImageStatus.AVAILABLE.value)]
        if query.tag:
            filters.append(Attr("tags").contains(query.tag))
        if query.content_type:
            filters.append(Attr("content_type").eq(query.content_type))

        kwargs: Dict[str, Any] = {}
        if query.user_id:
            # Efficient path: Query the GSI, date range goes into the key condition.
            key_cond = Key("user_id").eq(query.user_id)
            if query.created_from and query.created_to:
                key_cond &= Key("created_at").between(query.created_from, query.created_to)
            elif query.created_from:
                key_cond &= Key("created_at").gte(query.created_from)
            elif query.created_to:
                key_cond &= Key("created_at").lte(query.created_to)
            kwargs.update(
                IndexName=USER_INDEX_NAME,
                KeyConditionExpression=key_cond,
                ScanIndexForward=False,  # newest first
            )
            operation = self._table.query
            key_attrs = _USER_INDEX_KEY
        else:
            # Browse path: paginated Scan, date range becomes a filter.
            if query.created_from:
                filters.append(Attr("created_at").gte(query.created_from))
            if query.created_to:
                filters.append(Attr("created_at").lte(query.created_to))
            operation = self._table.scan
            key_attrs = _TABLE_KEY

        kwargs["FilterExpression"] = reduce(lambda a, b: a & b, filters)
        kwargs["Limit"] = query.limit
        start_key = decode_token(query.next_token, key_attrs) if query.next_token else None

        records: List[ImageRecord] = []
        for _ in range(MAX_PAGES_PER_REQUEST):
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            resp = operation(**kwargs)
            items = resp.get("Items", [])
            last_evaluated = resp.get("LastEvaluatedKey")

            for position, item in enumerate(items):
                records.append(ImageRecord.from_item(item))
                if len(records) == query.limit:
                    more_in_page = position < len(items) - 1
                    if more_in_page or last_evaluated:
                        # Resume right after the last item we returned, not after the
                        # whole DynamoDB page, so no item is ever skipped.
                        return records, encode_token({k: item[k] for k in key_attrs})
                    return records, None

            if not last_evaluated:
                return records, None
            start_key = last_evaluated

        # Hit the per-request read budget: return what we have plus a cursor.
        return records, encode_token({k: start_key[k] for k in key_attrs})
