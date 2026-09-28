"""Domain model, request parsing and validation.

Validation is hand-rolled rather than using Pydantic so the Lambda package has
zero third-party runtime dependencies (boto3 ships with the Lambda runtime).
That keeps the deployment artifact tiny and cold starts fast.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from .errors import PayloadTooLargeError, ValidationError

# content type -> file extension used for the S3 object key
ALLOWED_CONTENT_TYPES: Dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IMAGE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TAG_PATTERN = re.compile(r"^[a-z0-9_-]{1,30}$")

MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2000
MAX_TAGS = 10
MAX_FILE_NAME_LENGTH = 255

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class ImageStatus(str, Enum):
    PENDING = "PENDING"  # metadata created, waiting for the client to PUT the bytes to S3
    AVAILABLE = "AVAILABLE"  # bytes are in S3 and verified
    REJECTED = "REJECTED"  # bytes failed verification (size / type) and were removed


def format_timestamp(value: datetime) -> str:
    """UTC, fixed width, lexicographically sortable -> safe to use as a DynamoDB sort key."""
    return value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def detect_content_type(data: bytes) -> Optional[str]:
    """Identify the image format from its magic bytes (never trust the declared type)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# --------------------------------------------------------------------------- #
# Stored record
# --------------------------------------------------------------------------- #
@dataclass
class ImageRecord:
    image_id: str
    user_id: str
    s3_key: str
    content_type: str
    status: str
    created_at: str
    updated_at: str
    title: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    file_name: Optional[str] = None
    size_bytes: Optional[int] = None

    def to_item(self) -> Dict[str, Any]:
        """Serialise for DynamoDB (omit empty values; DynamoDB rejects empty sets/strings in keys)."""
        item = {k: v for k, v in asdict(self).items() if v not in (None, "", [])}
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> "ImageRecord":
        size = item.get("size_bytes")
        return cls(
            image_id=item["image_id"],
            user_id=item["user_id"],
            s3_key=item["s3_key"],
            content_type=item["content_type"],
            status=item["status"],
            created_at=item["created_at"],
            updated_at=item.get("updated_at", item["created_at"]),
            title=item.get("title", ""),
            description=item.get("description", ""),
            tags=list(item.get("tags", [])),
            file_name=item.get("file_name"),
            size_bytes=int(size) if isinstance(size, (int, Decimal)) else None,
        )

    def to_public_dict(self) -> Dict[str, Any]:
        """API representation. Internal storage details (the S3 key) are not exposed."""
        return {
            "image_id": self.image_id,
            "user_id": self.user_id,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "content_type": self.content_type,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# --------------------------------------------------------------------------- #
# Field validators
# --------------------------------------------------------------------------- #
def validate_user_id(value: Any) -> str:
    if not isinstance(value, str) or not USER_ID_PATTERN.match(value):
        raise ValidationError(
            "user_id must be 1-64 characters of letters, digits, '-' or '_'",
            {"field": "user_id"},
        )
    return value


def validate_image_id(value: Any) -> str:
    if not isinstance(value, str) or not IMAGE_ID_PATTERN.match(value):
        raise ValidationError("image_id must be a 32-character hex string", {"field": "image_id"})
    return value


def _optional_str(body: Mapping[str, Any], name: str, max_len: int) -> str:
    value = body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string", {"field": name})
    value = value.strip()
    if len(value) > max_len:
        raise ValidationError(f"{name} must be at most {max_len} characters", {"field": name})
    return value


def normalise_tag(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("tags must be strings", {"field": "tags"})
    tag = value.strip().lower()
    if not TAG_PATTERN.match(tag):
        raise ValidationError(
            f"invalid tag '{value}': use 1-30 characters of a-z, 0-9, '-' or '_'",
            {"field": "tags"},
        )
    return tag


def _parse_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("tags must be a list of strings", {"field": "tags"})
    tags: List[str] = []
    for raw in value:
        tag = normalise_tag(raw)
        if tag not in tags:  # de-duplicate, keep order
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValidationError(f"at most {MAX_TAGS} tags are allowed", {"field": "tags"})
    return tags


def validate_content_type(value: Any) -> str:
    if not isinstance(value, str) or value.lower() not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            "content_type must be one of: " + ", ".join(sorted(ALLOWED_CONTENT_TYPES)),
            {"field": "content_type"},
        )
    return value.lower()


def _parse_timestamp(value: str, name: str, end_of_day: bool) -> str:
    """Accept a date (2026-09-01) or an ISO-8601 datetime; normalise to our sort-key format."""
    try:
        if len(value) == 10:  # plain date
            day = datetime.strptime(value, "%Y-%m-%d").date()
            parsed = datetime.combine(day, time.max if end_of_day else time.min, tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValidationError(
            f"{name} must be an ISO-8601 date or datetime (e.g. 2026-09-01 or 2026-09-01T10:00:00Z)",
            {"field": name},
        ) from None
    return format_timestamp(parsed)


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
@dataclass
class UploadRequest:
    """Body of POST /images.

    Two modes:
      * inline   - `image_base64` is present; the Lambda writes the bytes to S3 (small images).
      * presigned - no image bytes; the service returns a presigned S3 PUT URL and the
                    client uploads directly to S3 (any size up to MAX_UPLOAD_BYTES).
    """

    content_type: str
    title: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    file_name: Optional[str] = None
    image_bytes: Optional[bytes] = None

    @property
    def is_inline(self) -> bool:
        return self.image_bytes is not None

    @classmethod
    def parse(cls, body: Mapping[str, Any], max_inline_bytes: int) -> "UploadRequest":
        if not isinstance(body, Mapping):
            raise ValidationError("request body must be a JSON object")

        title = _optional_str(body, "title", MAX_TITLE_LENGTH)
        description = _optional_str(body, "description", MAX_DESCRIPTION_LENGTH)
        tags = _parse_tags(body.get("tags"))
        file_name = _optional_str(body, "file_name", MAX_FILE_NAME_LENGTH) or None

        declared_type = body.get("content_type")
        image_bytes: Optional[bytes] = None
        raw_b64 = body.get("image_base64")

        if raw_b64 is not None:
            if not isinstance(raw_b64, str) or not raw_b64:
                raise ValidationError("image_base64 must be a non-empty base64 string", {"field": "image_base64"})
            if "," in raw_b64[:100] and raw_b64.startswith("data:"):  # tolerate data URLs
                raw_b64 = raw_b64.split(",", 1)[1]
            # Reject oversize payloads before spending CPU on decoding them.
            if len(raw_b64) * 3 // 4 > max_inline_bytes + 3:
                raise PayloadTooLargeError(
                    f"inline uploads are limited to {max_inline_bytes} bytes; "
                    "omit image_base64 to receive a presigned upload URL instead",
                    {"max_bytes": max_inline_bytes},
                )
            try:
                image_bytes = base64.b64decode(raw_b64, validate=True)
            except (binascii.Error, ValueError):
                raise ValidationError("image_base64 is not valid base64", {"field": "image_base64"}) from None
            if not image_bytes:
                raise ValidationError("image_base64 decodes to an empty file", {"field": "image_base64"})
            if len(image_bytes) > max_inline_bytes:
                raise PayloadTooLargeError(
                    f"inline uploads are limited to {max_inline_bytes} bytes",
                    {"max_bytes": max_inline_bytes},
                )

            detected = detect_content_type(image_bytes)
            if detected is None:
                raise ValidationError(
                    "file is not a supported image (jpeg, png, gif, webp)", {"field": "image_base64"}
                )
            if declared_type is not None and validate_content_type(declared_type) != detected:
                raise ValidationError(
                    f"content_type '{declared_type}' does not match the file contents ({detected})",
                    {"field": "content_type"},
                )
            content_type = detected
        else:
            if declared_type is None:
                raise ValidationError(
                    "content_type is required when image_base64 is not provided", {"field": "content_type"}
                )
            content_type = validate_content_type(declared_type)

        return cls(
            content_type=content_type,
            title=title,
            description=description,
            tags=tags,
            file_name=file_name,
            image_bytes=image_bytes,
        )


@dataclass
class ListQuery:
    """Query-string parameters of GET /images."""

    limit: int
    user_id: Optional[str] = None
    tag: Optional[str] = None
    content_type: Optional[str] = None
    created_from: Optional[str] = None
    created_to: Optional[str] = None
    next_token: Optional[str] = None

    @classmethod
    def parse(cls, params: Mapping[str, str], default_limit: int, max_limit: int) -> "ListQuery":
        known = {"limit", "user_id", "tag", "content_type", "created_from", "created_to", "next_token"}
        unknown = sorted(set(params) - known)
        if unknown:
            raise ValidationError(
                "unknown query parameter(s): " + ", ".join(unknown), {"allowed": sorted(known)}
            )

        raw_limit = params.get("limit")
        if raw_limit is None or raw_limit == "":
            limit = default_limit
        else:
            try:
                limit = int(raw_limit)
            except ValueError:
                raise ValidationError("limit must be an integer", {"field": "limit"}) from None
            if not 1 <= limit <= max_limit:
                raise ValidationError(f"limit must be between 1 and {max_limit}", {"field": "limit"})

        user_id = params.get("user_id") or None
        if user_id is not None:
            validate_user_id(user_id)

        tag = params.get("tag") or None
        if tag is not None:
            tag = normalise_tag(tag)

        content_type = params.get("content_type") or None
        if content_type is not None:
            content_type = validate_content_type(content_type)

        created_from = params.get("created_from") or None
        created_to = params.get("created_to") or None
        if created_from:
            created_from = _parse_timestamp(created_from, "created_from", end_of_day=False)
        if created_to:
            created_to = _parse_timestamp(created_to, "created_to", end_of_day=True)
        if created_from and created_to and created_from > created_to:
            raise ValidationError("created_from must be before created_to", {"field": "created_from"})

        return cls(
            limit=limit,
            user_id=user_id,
            tag=tag,
            content_type=content_type,
            created_from=created_from,
            created_to=created_to,
            next_token=params.get("next_token") or None,
        )
