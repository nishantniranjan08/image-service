"""Unit tests for request parsing and validation (no AWS involved)."""

from __future__ import annotations

import pytest

from helpers import GIF_BYTES, JPEG_BYTES, WEBP_BYTES, b64, make_png
from image_service.errors import PayloadTooLargeError, ValidationError
from image_service.models import (
    ImageRecord,
    ListQuery,
    UploadRequest,
    detect_content_type,
    validate_image_id,
    validate_user_id,
)

MAX_INLINE = 1024


class TestDetectContentType:
    @pytest.mark.parametrize(
        "data, expected",
        [
            (make_png(), "image/png"),
            (JPEG_BYTES, "image/jpeg"),
            (GIF_BYTES, "image/gif"),
            (WEBP_BYTES, "image/webp"),
            (b"%PDF-1.7 not an image", None),
            (b"", None),
        ],
    )
    def test_detects_by_magic_bytes(self, data, expected):
        assert detect_content_type(data) == expected


class TestUploadRequest:
    def test_inline_upload_detects_type_and_normalises_fields(self):
        req = UploadRequest.parse(
            {
                "image_base64": b64(make_png()),
                "title": "  Sunset  ",
                "tags": ["Travel", "beach", "travel"],
                "file_name": "sunset.png",
            },
            MAX_INLINE,
        )
        assert req.is_inline
        assert req.content_type == "image/png"
        assert req.title == "Sunset"
        assert req.tags == ["travel", "beach"]  # lower-cased, de-duplicated, order kept
        assert req.file_name == "sunset.png"

    def test_accepts_data_url_prefix(self):
        req = UploadRequest.parse({"image_base64": "data:image/png;base64," + b64(make_png())}, MAX_INLINE)
        assert req.content_type == "image/png"

    def test_presigned_mode_requires_content_type(self):
        with pytest.raises(ValidationError, match="content_type is required"):
            UploadRequest.parse({"title": "x"}, MAX_INLINE)

    def test_presigned_mode(self):
        req = UploadRequest.parse({"content_type": "IMAGE/JPEG"}, MAX_INLINE)
        assert not req.is_inline
        assert req.content_type == "image/jpeg"

    @pytest.mark.parametrize(
        "body, message",
        [
            ({"content_type": "application/pdf"}, "content_type must be one of"),
            ({"image_base64": "!!!not-base64!!!"}, "not valid base64"),
            ({"image_base64": ""}, "non-empty base64"),
            ({"image_base64": 123}, "non-empty base64"),
            ({"image_base64": b64(b"%PDF-1.7 hello")}, "not a supported image"),
            ({"image_base64": b64(make_png()), "content_type": "image/jpeg"}, "does not match"),
            ({"content_type": "image/png", "title": "x" * 201}, "title must be at most"),
            ({"content_type": "image/png", "title": 5}, "title must be a string"),
            ({"content_type": "image/png", "tags": "travel"}, "tags must be a list"),
            ({"content_type": "image/png", "tags": ["has space"]}, "invalid tag"),
            ({"content_type": "image/png", "tags": [1]}, "tags must be strings"),
            ({"content_type": "image/png", "tags": [f"t{i}" for i in range(11)]}, "at most 10 tags"),
        ],
    )
    def test_rejects_invalid_input(self, body, message):
        with pytest.raises(ValidationError, match=message):
            UploadRequest.parse(body, MAX_INLINE)

    def test_rejects_non_object_body(self):
        with pytest.raises(ValidationError):
            UploadRequest.parse(["not", "an", "object"], MAX_INLINE)  # type: ignore[arg-type]

    def test_rejects_oversized_inline_image_before_decoding(self):
        big = b64(make_png() + b"\x00" * (MAX_INLINE * 2))
        with pytest.raises(PayloadTooLargeError):
            UploadRequest.parse({"image_base64": big}, MAX_INLINE)

    def test_rejects_oversized_inline_image_after_decoding(self):
        data = make_png() + b"\x00" * (MAX_INLINE - len(make_png()) + 2)  # just over the limit
        with pytest.raises(PayloadTooLargeError):
            UploadRequest.parse({"image_base64": b64(data)}, MAX_INLINE)


class TestListQuery:
    def parse(self, **params):
        return ListQuery.parse(params, default_limit=20, max_limit=100)

    def test_defaults(self):
        q = self.parse()
        assert q.limit == 20 and q.user_id is None and q.next_token is None

    def test_all_filters(self):
        q = self.parse(
            user_id="alice",
            tag="Travel",
            content_type="image/png",
            created_from="2026-09-01",
            created_to="2026-09-30",
            limit="5",
        )
        assert q.limit == 5
        assert q.tag == "travel"
        assert q.created_from == "2026-09-01T00:00:00.000000Z"
        assert q.created_to == "2026-09-30T23:59:59.999999Z"  # date-only 'to' is inclusive

    def test_accepts_iso_datetime_with_offset(self):
        q = self.parse(created_from="2026-09-01T10:00:00+05:30")
        assert q.created_from == "2026-09-01T04:30:00.000000Z"

    @pytest.mark.parametrize(
        "params, message",
        [
            ({"limit": "0"}, "between 1 and 100"),
            ({"limit": "101"}, "between 1 and 100"),
            ({"limit": "ten"}, "must be an integer"),
            ({"user_id": "bad id!"}, "user_id"),
            ({"content_type": "text/plain"}, "content_type"),
            ({"created_from": "yesterday"}, "ISO-8601"),
            ({"created_from": "2026-09-10", "created_to": "2026-09-01"}, "before created_to"),
            ({"colour": "red"}, "unknown query parameter"),
        ],
    )
    def test_rejects_invalid(self, params, message):
        with pytest.raises(ValidationError, match=message):
            self.parse(**params)


class TestIdentifiers:
    def test_valid_ids(self):
        assert validate_user_id("alice_01-x") == "alice_01-x"
        assert validate_image_id("a" * 32) == "a" * 32

    @pytest.mark.parametrize("value", ["", "a" * 65, "a/b", None, 7])
    def test_invalid_user_ids(self, value):
        with pytest.raises(ValidationError):
            validate_user_id(value)

    @pytest.mark.parametrize("value", ["abc", "../../etc/passwd", "G" * 32, None])
    def test_invalid_image_ids(self, value):
        with pytest.raises(ValidationError):
            validate_image_id(value)


def test_record_round_trip_drops_empty_values():
    record = ImageRecord(
        image_id="a" * 32,
        user_id="alice",
        s3_key="images/alice/x.png",
        content_type="image/png",
        status="AVAILABLE",
        created_at="t",
        updated_at="t",
    )
    item = record.to_item()
    assert "title" not in item and "tags" not in item and "size_bytes" not in item
    assert ImageRecord.from_item(item) == record
