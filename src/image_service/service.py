"""Business logic. Knows nothing about HTTP or Lambda events."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from .errors import ConflictError, ForbiddenError, NotFoundError
from .models import (
    ALLOWED_CONTENT_TYPES,
    ImageRecord,
    ImageStatus,
    ListQuery,
    UploadRequest,
    detect_content_type,
    format_timestamp,
    validate_image_id,
)
from .repository import ImageRepository
from .storage import ImageStorage, safe_file_name

logger = logging.getLogger(__name__)

KEY_PREFIX = "images/"


def build_s3_key(user_id: str, image_id: str, content_type: str) -> str:
    # Prefixing by user keeps each user's objects together (per-user lifecycle
    # rules, bulk export/erasure) and spreads load across S3 key prefixes.
    return f"{KEY_PREFIX}{user_id}/{image_id}{ALLOWED_CONTENT_TYPES[content_type]}"


def image_id_from_key(key: str) -> Optional[str]:
    if not key.startswith(KEY_PREFIX):
        return None
    name = key.rsplit("/", 1)[-1]
    return name.split(".", 1)[0] or None


class ImageService:
    def __init__(
        self,
        repository: ImageRepository,
        storage: ImageStorage,
        max_upload_bytes: int,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._repo = repository
        self._storage = storage
        self._max_upload_bytes = max_upload_bytes
        self._clock = clock
        self._new_id = id_factory

    def _now(self) -> str:
        return format_timestamp(self._clock())

    # ----------------------------------------------------------------- upload
    def create_image(self, user_id: str, request: UploadRequest) -> Dict[str, Any]:
        image_id = self._new_id()
        now = self._now()
        key = build_s3_key(user_id, image_id, request.content_type)
        record = ImageRecord(
            image_id=image_id,
            user_id=user_id,
            s3_key=key,
            content_type=request.content_type,
            status=ImageStatus.PENDING.value,
            created_at=now,
            updated_at=now,
            title=request.title,
            description=request.description,
            tags=request.tags,
            file_name=request.file_name,
        )

        if request.is_inline:
            assert request.image_bytes is not None
            # Bytes first, then metadata: a crash in between leaves an unreferenced
            # object (cheap, removable by lifecycle rule), never metadata that
            # points at nothing.
            self._storage.put(key, request.image_bytes, request.content_type)
            record.status = ImageStatus.AVAILABLE.value
            record.size_bytes = len(request.image_bytes)
            try:
                self._repo.create(record)
            except Exception:
                self._storage.delete(key)  # best-effort compensation
                raise
            logger.info("image uploaded inline", extra={"image_id": image_id, "user_id": user_id})
            return {"image": record.to_public_dict()}

        self._repo.create(record)
        logger.info("presigned upload issued", extra={"image_id": image_id, "user_id": user_id})
        return {
            "image": record.to_public_dict(),
            "upload": {
                "method": "PUT",
                "url": self._storage.presigned_upload_url(key, request.content_type),
                "headers": {"Content-Type": request.content_type},
                "expires_in": self._storage.url_ttl_seconds,
                "max_bytes": self._max_upload_bytes,
            },
        }

    def confirm_upload(self, key: str, size_bytes: Optional[int] = None) -> str:
        """Handle an S3 ObjectCreated event: verify the bytes and publish the image.

        Returns the outcome (useful for logs and tests). Safe to call repeatedly.
        """
        image_id = image_id_from_key(key)
        record = self._repo.get(image_id) if image_id else None
        if record is None or record.s3_key != key:
            logger.warning("object without matching metadata", extra={"key": key})
            return "ignored"
        if record.status != ImageStatus.PENDING.value:
            return "already_processed"  # e.g. inline uploads, or a redelivered event

        head = self._storage.head(key)
        if head is None:
            return "ignored"  # object was deleted before we got here
        actual_size = int(head.get("ContentLength", size_bytes or 0))

        reason = None
        if actual_size > self._max_upload_bytes:
            reason = f"file exceeds {self._max_upload_bytes} bytes"
        elif actual_size == 0:
            reason = "file is empty"
        elif detect_content_type(self._storage.read_prefix(key)) != record.content_type:
            reason = f"file contents are not {record.content_type}"

        if reason:
            self._storage.delete(key)
            self._repo.mark_rejected(image_id, reason, self._now())  # type: ignore[arg-type]
            logger.warning("upload rejected", extra={"image_id": image_id, "reason": reason})
            return "rejected"

        self._repo.mark_available(image_id, actual_size, self._now())  # type: ignore[arg-type]
        logger.info("upload confirmed", extra={"image_id": image_id, "size_bytes": actual_size})
        return "available"

    # ------------------------------------------------------------------- read
    def list_images(self, query: ListQuery) -> Dict[str, Any]:
        records, next_token = self._repo.list(query)
        return {
            "items": [r.to_public_dict() for r in records],
            "count": len(records),
            "next_token": next_token,
        }

    def _get_record(self, image_id: str) -> ImageRecord:
        validate_image_id(image_id)
        record = self._repo.get(image_id)
        if record is None or record.status == ImageStatus.REJECTED.value:
            raise NotFoundError(f"image '{image_id}' not found")
        return record

    def get_image(self, image_id: str) -> Dict[str, Any]:
        record = self._get_record(image_id)
        body = record.to_public_dict()
        body["view_url"] = None
        body["download_url"] = None
        if record.status == ImageStatus.AVAILABLE.value:
            body["view_url"] = self._download_url(record, as_attachment=False)
            body["download_url"] = self._download_url(record, as_attachment=True)
            body["url_expires_in"] = self._storage.url_ttl_seconds
        return body

    def get_download_url(self, image_id: str, as_attachment: bool = True) -> str:
        record = self._get_record(image_id)
        if record.status != ImageStatus.AVAILABLE.value:
            raise ConflictError(f"image '{image_id}' is still being uploaded")
        return self._download_url(record, as_attachment)

    def _download_url(self, record: ImageRecord, as_attachment: bool) -> str:
        fallback = record.image_id + ALLOWED_CONTENT_TYPES[record.content_type]
        return self._storage.presigned_download_url(
            record.s3_key, safe_file_name(record.file_name, fallback), as_attachment
        )

    # ----------------------------------------------------------------- delete
    def delete_image(self, user_id: str, image_id: str) -> None:
        validate_image_id(image_id)
        deleted = self._repo.delete_owned(image_id, user_id)
        if deleted is None:
            # Distinguish "not yours" from "doesn't exist" for a clear API contract.
            existing = self._repo.get(image_id)
            if existing is None:
                raise NotFoundError(f"image '{image_id}' not found")
            raise ForbiddenError("you can only delete your own images")
        # Metadata first, then bytes: once metadata is gone the image is invisible
        # to every API. If this S3 call fails, the orphaned object is harmless.
        try:
            self._storage.delete(deleted.s3_key)
        except Exception:
            logger.exception("failed to delete S3 object", extra={"key": deleted.s3_key})
        logger.info("image deleted", extra={"image_id": image_id, "user_id": user_id})
