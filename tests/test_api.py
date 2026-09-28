"""API tests: Lambda handlers + service + DynamoDB + S3, with AWS mocked by moto."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from helpers import BUCKET, CONTEXT, JPEG_BYTES, api_event, b64, body_of, make_png
from image_service import handlers


def upload(user="alice", **fields):
    body = {"image_base64": b64(make_png()), "title": "A photo", **fields}
    return handlers.upload_image(api_event("POST", "/images", body=body, user=user), CONTEXT)


def get(image_id, query=None):
    return handlers.get_image(
        api_event("GET", f"/images/{image_id}", path_params={"image_id": image_id}, query=query), CONTEXT
    )


def delete(image_id, user="alice"):
    return handlers.delete_image(
        api_event("DELETE", f"/images/{image_id}", path_params={"image_id": image_id}, user=user), CONTEXT
    )


def list_images(**query):
    return handlers.list_images(api_event("GET", "/images", query=query or None), CONTEXT)


# --------------------------------------------------------------------- upload
class TestUpload:
    def test_inline_upload_stores_bytes_and_metadata(self, aws):
        resp = upload(tags=["Travel"], description="golden hour", file_name="sunset.png")

        assert resp["statusCode"] == 201
        image = body_of(resp)["image"]
        assert image["status"] == "AVAILABLE"
        assert image["user_id"] == "alice"
        assert image["tags"] == ["travel"]
        assert image["size_bytes"] == len(make_png())
        assert "s3_key" not in image  # internal detail not exposed

        item = aws.table.get_item(Key={"image_id": image["image_id"]})["Item"]
        stored = aws.s3.get_object(Bucket=BUCKET, Key=item["s3_key"])
        assert stored["Body"].read() == make_png()
        assert stored["ContentType"] == "image/png"
        assert item["s3_key"] == f"images/alice/{image['image_id']}.png"

    def test_presigned_upload_returns_url_and_pending_record(self, aws):
        resp = handlers.upload_image(
            api_event("POST", "/images", body={"content_type": "image/jpeg", "title": "big"}, user="bob"),
            CONTEXT,
        )
        assert resp["statusCode"] == 202
        body = body_of(resp)
        assert body["image"]["status"] == "PENDING"
        upload_info = body["upload"]
        assert upload_info["method"] == "PUT"
        assert upload_info["headers"] == {"Content-Type": "image/jpeg"}
        url = urlparse(upload_info["url"])
        assert f"images/bob/{body['image']['image_id']}.jpg" in url.path + url.netloc
        assert "X-Amz-Signature" in parse_qs(url.query)

    def test_requires_caller_identity(self, aws):
        resp = handlers.upload_image(api_event("POST", "/images", body={"content_type": "image/png"}), CONTEXT)
        assert resp["statusCode"] == 401
        assert body_of(resp)["error"]["code"] == "UNAUTHORIZED"

    def test_rejects_malformed_identity(self, aws):
        resp = upload(user="not a valid id")
        assert resp["statusCode"] == 401

    def test_uses_authorizer_identity_when_present(self, aws):
        event = api_event("POST", "/images", body={"image_base64": b64(make_png())}, user="spoofed")
        event["requestContext"] = {"authorizer": {"claims": {"sub": "cognito-user-1"}}}
        resp = handlers.upload_image(event, CONTEXT)
        assert body_of(resp)["image"]["user_id"] == "cognito-user-1"

    @pytest.mark.parametrize(
        "raw_body, message",
        [(None, "body is required"), ("{not json", "valid JSON"), ("[1, 2]", "JSON object")],
    )
    def test_rejects_bad_bodies(self, aws, raw_body, message):
        event = api_event("POST", "/images", user="alice")
        event["body"] = raw_body
        resp = handlers.upload_image(event, CONTEXT)
        assert resp["statusCode"] == 400
        assert message in body_of(resp)["error"]["message"]

    def test_accepts_base64_encoded_body(self, aws):
        import base64
        import json

        event = api_event("POST", "/images", user="alice")
        event["body"] = base64.b64encode(json.dumps({"image_base64": b64(make_png())}).encode()).decode()
        event["isBase64Encoded"] = True
        assert handlers.upload_image(event, CONTEXT)["statusCode"] == 201

    def test_validation_error_shape(self, aws):
        resp = handlers.upload_image(
            api_event("POST", "/images", body={"content_type": "text/html"}, user="alice"), CONTEXT
        )
        assert resp["statusCode"] == 400
        error = body_of(resp)["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"] == {"field": "content_type"}
        assert error["request_id"] == "req-test-123"
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_oversized_inline_upload_returns_413(self, aws, monkeypatch):
        monkeypatch.setenv("MAX_INLINE_UPLOAD_BYTES", "10")
        handlers.reset_dependencies()
        assert upload()["statusCode"] == 413

    def test_s3_failure_returns_500_without_leaking_details(self, aws, monkeypatch):
        service = handlers.get_service()

        def boom(*args, **kwargs):
            raise RuntimeError("secret internal detail")

        monkeypatch.setattr(service._storage, "put", boom)
        resp = upload()
        assert resp["statusCode"] == 500
        assert "secret" not in resp["body"]
        assert aws.table.scan()["Count"] == 0  # no metadata written for a failed upload

    def test_metadata_failure_removes_uploaded_object(self, aws, monkeypatch):
        service = handlers.get_service()

        def boom(record):
            raise RuntimeError("dynamodb down")

        monkeypatch.setattr(service._repo, "create", boom)
        assert upload()["statusCode"] == 500
        assert aws.s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0  # compensated


# ----------------------------------------------------------------------- view
class TestView:
    def test_returns_metadata_and_presigned_urls(self, aws):
        image_id = body_of(upload(file_name="my photo!.png"))["image"]["image_id"]
        resp = get(image_id)
        assert resp["statusCode"] == 200
        body = body_of(resp)
        assert body["image_id"] == image_id
        assert body["url_expires_in"] == 900
        download = parse_qs(urlparse(body["download_url"]).query)
        assert download["response-content-disposition"] == ['attachment; filename="my_photo_.png"']
        view = parse_qs(urlparse(body["view_url"]).query)
        assert view["response-content-disposition"][0].startswith("inline")

    def test_download_redirects_to_file(self, aws):
        image_id = body_of(upload())["image"]["image_id"]
        resp = get(image_id, {"download": "true"})
        assert resp["statusCode"] == 302
        assert "X-Amz-Signature" in resp["headers"]["Location"]
        assert "attachment" in parse_qs(urlparse(resp["headers"]["Location"]).query)[
            "response-content-disposition"
        ][0]

    def test_view_redirect_is_inline(self, aws):
        image_id = body_of(upload())["image"]["image_id"]
        resp = get(image_id, {"view": "true"})
        assert resp["statusCode"] == 302
        assert "inline" in resp["headers"]["Location"]

    def test_presigned_url_serves_the_bytes(self, aws):
        """Follow the presigned URL through moto to prove it really resolves to the image."""
        import requests

        image_id = body_of(upload())["image"]["image_id"]
        url = body_of(get(image_id))["download_url"]
        response = requests.get(url, timeout=5)
        assert response.status_code == 200
        assert response.content == make_png()

    def test_unknown_image_returns_404(self, aws):
        resp = get("f" * 32)
        assert resp["statusCode"] == 404
        assert body_of(resp)["error"]["code"] == "NOT_FOUND"

    def test_malformed_id_returns_400(self, aws):
        assert get("../../secrets")["statusCode"] == 400

    def test_missing_path_parameter_returns_400(self, aws):
        resp = handlers.get_image(api_event("GET", "/images/"), CONTEXT)
        assert resp["statusCode"] == 400

    def test_pending_image_has_no_urls_and_cannot_be_downloaded(self, aws):
        resp = handlers.upload_image(
            api_event("POST", "/images", body={"content_type": "image/png"}, user="alice"), CONTEXT
        )
        image_id = body_of(resp)["image"]["image_id"]
        body = body_of(get(image_id))
        assert body["status"] == "PENDING"
        assert body["download_url"] is None
        conflict = get(image_id, {"download": "true"})
        assert conflict["statusCode"] == 409


# --------------------------------------------------------------------- delete
class TestDelete:
    def test_owner_can_delete(self, aws):
        image_id = body_of(upload())["image"]["image_id"]
        key = aws.table.get_item(Key={"image_id": image_id})["Item"]["s3_key"]

        resp = delete(image_id)

        assert resp["statusCode"] == 204
        assert resp["body"] == ""
        assert "Item" not in aws.table.get_item(Key={"image_id": image_id})
        assert aws.s3.list_objects_v2(Bucket=BUCKET, Prefix=key).get("KeyCount", 0) == 0
        assert get(image_id)["statusCode"] == 404

    def test_other_users_cannot_delete(self, aws):
        image_id = body_of(upload(user="alice"))["image"]["image_id"]
        resp = delete(image_id, user="mallory")
        assert resp["statusCode"] == 403
        assert get(image_id)["statusCode"] == 200  # still there

    def test_deleting_missing_image_returns_404(self, aws):
        assert delete("a" * 32)["statusCode"] == 404

    def test_second_delete_returns_404(self, aws):
        image_id = body_of(upload())["image"]["image_id"]
        assert delete(image_id)["statusCode"] == 204
        assert delete(image_id)["statusCode"] == 404

    def test_requires_identity(self, aws):
        image_id = body_of(upload())["image"]["image_id"]
        event = api_event("DELETE", f"/images/{image_id}", path_params={"image_id": image_id})
        assert handlers.delete_image(event, CONTEXT)["statusCode"] == 401

    def test_s3_delete_failure_still_removes_metadata(self, aws, monkeypatch):
        image_id = body_of(upload())["image"]["image_id"]
        service = handlers.get_service()

        def boom(key):
            raise RuntimeError("s3 unavailable")

        monkeypatch.setattr(service._storage, "delete", boom)
        assert delete(image_id)["statusCode"] == 204
        assert get(image_id)["statusCode"] == 404


# ----------------------------------------------------------------------- list
@pytest.fixture
def gallery(aws, monkeypatch):
    """Seed images with deterministic timestamps: alice x3, bob x2, one pending."""
    service = handlers.get_service()
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    clock = iter(base + timedelta(days=i) for i in range(100))
    monkeypatch.setattr(service, "_clock", lambda: next(clock))

    ids = {}
    seed = [
        ("a1", "alice", ["travel", "beach"], make_png()),
        ("a2", "alice", ["food"], JPEG_BYTES),
        ("b1", "bob", ["travel"], make_png()),
        ("a3", "alice", ["travel"], make_png()),
        ("b2", "bob", ["food"], JPEG_BYTES),
    ]
    for name, user, tags, data in seed:
        resp = handlers.upload_image(
            api_event("POST", "/images", body={"image_base64": b64(data), "title": name, "tags": tags}, user=user),
            CONTEXT,
        )
        ids[name] = body_of(resp)["image"]["image_id"]
    # a pending (not yet uploaded) image must never be listed
    handlers.upload_image(
        api_event("POST", "/images", body={"content_type": "image/png", "tags": ["travel"]}, user="alice"),
        CONTEXT,
    )
    return ids


def titles(resp):
    return [item["title"] for item in body_of(resp)["items"]]


class TestList:
    def test_lists_all_available_images(self, gallery):
        resp = list_images()
        assert resp["statusCode"] == 200
        body = body_of(resp)
        assert sorted(titles(resp)) == ["a1", "a2", "a3", "b1", "b2"]
        assert body["count"] == 5 and body["next_token"] is None

    def test_filter_by_user_newest_first(self, gallery):
        assert titles(list_images(user_id="alice")) == ["a3", "a2", "a1"]

    def test_filter_by_tag(self, gallery):
        assert sorted(titles(list_images(tag="travel"))) == ["a1", "a3", "b1"]

    def test_filter_by_content_type(self, gallery):
        assert sorted(titles(list_images(content_type="image/jpeg"))) == ["a2", "b2"]

    def test_filter_by_date_range(self, gallery):
        # created on Sep 1..5 in seed order; Sep 2..4 -> a2, b1, a3
        resp = list_images(created_from="2026-09-02", created_to="2026-09-04")
        assert sorted(titles(resp)) == ["a2", "a3", "b1"]

    def test_user_with_date_range_uses_key_condition(self, gallery):
        assert titles(list_images(user_id="alice", created_from="2026-09-02")) == ["a3", "a2"]
        assert titles(list_images(user_id="alice", created_to="2026-09-02")) == ["a2", "a1"]

    def test_combined_filters(self, gallery):
        assert titles(list_images(user_id="alice", tag="travel")) == ["a3", "a1"]
        assert titles(list_images(user_id="bob", tag="beach")) == []

    def test_unknown_user_returns_empty_list(self, gallery):
        body = body_of(list_images(user_id="nobody"))
        assert body == {"items": [], "count": 0, "next_token": None}

    def test_pagination_by_user_returns_every_item_exactly_once(self, gallery):
        first = body_of(list_images(user_id="alice", limit="2"))
        assert [i["title"] for i in first["items"]] == ["a3", "a2"]
        assert first["next_token"]
        second = body_of(list_images(user_id="alice", limit="2", next_token=first["next_token"]))
        assert [i["title"] for i in second["items"]] == ["a1"]
        assert second["next_token"] is None

    def test_pagination_with_sparse_filter_pages(self, gallery):
        """Filters make DynamoDB pages sparse; the service keeps reading so no item is lost."""
        seen, token = [], None
        for _ in range(10):
            query = {"tag": "travel", "limit": "1"}
            if token:
                query["next_token"] = token
            body = body_of(list_images(**query))
            seen += [i["title"] for i in body["items"]]
            token = body["next_token"]
            if not token:
                break
        assert sorted(seen) == ["a1", "a3", "b1"]

    def test_pagination_across_all_images(self, gallery):
        seen, token = [], None
        while True:
            query = {"limit": "2", **({"next_token": token} if token else {})}
            body = body_of(list_images(**query))
            seen += [i["title"] for i in body["items"]]
            token = body["next_token"]
            if not token:
                break
        assert sorted(seen) == ["a1", "a2", "a3", "b1", "b2"]

    @pytest.mark.parametrize("token", ["garbage!!", "e30"])  # e30 = base64("{}")
    def test_invalid_next_token(self, gallery, token):
        resp = list_images(next_token=token)
        assert resp["statusCode"] == 400

    def test_token_cannot_be_reused_with_different_filters(self, gallery):
        token = body_of(list_images(user_id="alice", limit="1"))["next_token"]
        assert list_images(next_token=token)["statusCode"] == 400

    def test_invalid_query_returns_400(self, gallery):
        assert list_images(limit="1000")["statusCode"] == 400
        assert list_images(sort="asc")["statusCode"] == 400


# ------------------------------------------------------------------ S3 events
class TestPresignedUploadConfirmation:
    def _start(self, content_type="image/png", user="alice"):
        resp = handlers.upload_image(
            api_event("POST", "/images", body={"content_type": content_type}, user=user), CONTEXT
        )
        return body_of(resp)["image"]["image_id"]

    def _key(self, aws, image_id):
        return aws.table.get_item(Key={"image_id": image_id})["Item"]["s3_key"]

    def test_valid_upload_becomes_available(self, aws):
        from helpers import s3_event

        image_id = self._start()
        key = self._key(aws, image_id)
        aws.s3.put_object(Bucket=BUCKET, Key=key, Body=make_png(), ContentType="image/png")

        result = handlers.process_upload(s3_event(key), CONTEXT)

        assert result["processed"][0]["outcome"] == "available"
        body = body_of(get(image_id))
        assert body["status"] == "AVAILABLE"
        assert body["size_bytes"] == len(make_png())
        assert body["download_url"]

    def test_presigned_url_upload_end_to_end(self, aws):
        """PUT to the presigned URL (as a real client would), then fire the S3 event."""
        import requests

        from helpers import s3_event

        resp = handlers.upload_image(
            api_event("POST", "/images", body={"content_type": "image/png"}, user="alice"), CONTEXT
        )
        body = body_of(resp)
        put = requests.put(body["upload"]["url"], data=make_png(), headers=body["upload"]["headers"], timeout=5)
        assert put.status_code == 200

        key = self._key(aws, body["image"]["image_id"])
        handlers.process_upload(s3_event(key), CONTEXT)
        assert body_of(get(body["image"]["image_id"]))["status"] == "AVAILABLE"

    def test_redelivered_event_is_idempotent(self, aws):
        from helpers import s3_event

        image_id = self._start()
        key = self._key(aws, image_id)
        aws.s3.put_object(Bucket=BUCKET, Key=key, Body=make_png())
        handlers.process_upload(s3_event(key), CONTEXT)
        again = handlers.process_upload(s3_event(key), CONTEXT)
        assert again["processed"][0]["outcome"] == "already_processed"

    def test_wrong_file_type_is_rejected_and_removed(self, aws):
        from helpers import s3_event

        image_id = self._start("image/png")
        key = self._key(aws, image_id)
        aws.s3.put_object(Bucket=BUCKET, Key=key, Body=b"#!/bin/sh\necho pwned")

        result = handlers.process_upload(s3_event(key), CONTEXT)

        assert result["processed"][0]["outcome"] == "rejected"
        assert aws.s3.list_objects_v2(Bucket=BUCKET, Prefix=key).get("KeyCount", 0) == 0
        item = aws.table.get_item(Key={"image_id": image_id})["Item"]
        assert item["status"] == "REJECTED"
        assert "not image/png" in item["rejection_reason"]
        assert get(image_id)["statusCode"] == 404  # rejected images are invisible

    def test_oversized_file_is_rejected(self, aws, monkeypatch):
        from helpers import s3_event

        monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
        handlers.reset_dependencies()
        image_id = self._start()
        key = self._key(aws, image_id)
        aws.s3.put_object(Bucket=BUCKET, Key=key, Body=make_png())
        assert handlers.process_upload(s3_event(key), CONTEXT)["processed"][0]["outcome"] == "rejected"

    def test_empty_file_is_rejected(self, aws):
        from helpers import s3_event

        image_id = self._start()
        key = self._key(aws, image_id)
        aws.s3.put_object(Bucket=BUCKET, Key=key, Body=b"")
        assert handlers.process_upload(s3_event(key), CONTEXT)["processed"][0]["outcome"] == "rejected"

    def test_inline_upload_event_is_a_no_op(self, aws):
        from helpers import s3_event

        image_id = body_of(upload())["image"]["image_id"]
        key = self._key(aws, image_id)
        assert handlers.process_upload(s3_event(key), CONTEXT)["processed"][0]["outcome"] == "already_processed"

    @pytest.mark.parametrize("key", ["images/alice/" + "0" * 32 + ".png", "other/prefix.png", "images/"])
    def test_unknown_objects_are_ignored(self, aws, key):
        from helpers import s3_event

        assert handlers.process_upload(s3_event(key), CONTEXT)["processed"][0]["outcome"] == "ignored"

    def test_object_deleted_before_event_is_ignored(self, aws):
        from helpers import s3_event

        image_id = self._start()
        key = self._key(aws, image_id)
        assert handlers.process_upload(s3_event(key), CONTEXT)["processed"][0]["outcome"] == "ignored"

    def test_url_encoded_keys_are_decoded(self, aws, monkeypatch):
        from helpers import s3_event

        seen = []
        service = handlers.get_service()
        monkeypatch.setattr(service, "confirm_upload", lambda key, size=None: seen.append(key) or "ignored")
        handlers.process_upload(s3_event("images/my+user/a%2Bb.png"), CONTEXT)
        assert seen == ["images/my user/a+b.png"]
