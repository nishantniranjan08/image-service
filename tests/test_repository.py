"""Repository-level tests for DynamoDB edge cases: races, idempotency, the read budget."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from image_service import repository as repo_module
from image_service.models import ImageRecord, ImageStatus, ListQuery
from image_service.repository import ImageRepository, decode_token, encode_token


def record(image_id: str, user: str = "alice", status: str = "PENDING", created: str = "2026-09-01") -> ImageRecord:
    return ImageRecord(
        image_id=image_id,
        user_id=user,
        s3_key=f"images/{user}/{image_id}.png",
        content_type="image/png",
        status=status,
        created_at=f"{created}T00:00:00.000000Z",
        updated_at=f"{created}T00:00:00.000000Z",
        tags=["x"],
    )


@pytest.fixture
def repo(aws):
    return ImageRepository(aws.table)


def test_create_never_overwrites(repo):
    repo.create(record("a" * 32))
    with pytest.raises(ClientError):
        repo.create(record("a" * 32, user="mallory"))
    assert repo.get("a" * 32).user_id == "alice"


def test_status_transitions_only_from_pending(repo):
    repo.create(record("a" * 32))
    assert repo.mark_available("a" * 32, 10, "now") is True
    # a late/duplicate event can neither re-publish nor reject a finished image
    assert repo.mark_available("a" * 32, 99, "later") is False
    assert repo.mark_rejected("a" * 32, "late", "later") is False
    stored = repo.get("a" * 32)
    assert stored.status == ImageStatus.AVAILABLE.value and stored.size_bytes == 10


def test_transitions_on_missing_image_are_no_ops(repo):
    assert repo.mark_available("b" * 32, 1, "now") is False
    assert repo.mark_rejected("b" * 32, "x", "now") is False
    assert repo.get("b" * 32) is None  # update_item did not create a phantom item


def test_delete_owned_is_conditional(repo):
    repo.create(record("a" * 32))
    assert repo.delete_owned("a" * 32, "mallory") is None
    assert repo.get("a" * 32) is not None
    assert repo.delete_owned("a" * 32, "alice").image_id == "a" * 32
    assert repo.delete_owned("a" * 32, "alice") is None


def test_user_query_with_full_date_range(repo):
    for i, day in enumerate(["2026-09-01", "2026-09-05", "2026-09-10"]):
        repo.create(record(str(i) * 32, status="AVAILABLE", created=day))
    query = ListQuery(
        limit=10,
        user_id="alice",
        created_from="2026-09-02T00:00:00.000000Z",
        created_to="2026-09-09T00:00:00.000000Z",
    )
    items, token = repo.list(query)
    assert [r.image_id for r in items] == ["1" * 32] and token is None


def test_read_budget_returns_resumable_cursor(repo, monkeypatch):
    # 5 images, only the last one (by scan order) matches the tag filter
    for i in range(5):
        r = record(str(i) * 32, status="AVAILABLE")
        r.tags = ["rare"] if i == 4 else ["common"]
        repo.create(r)
    monkeypatch.setattr(repo_module, "MAX_PAGES_PER_REQUEST", 1)

    found, token, calls = [], None, 0
    while True:
        calls += 1
        items, token = repo.list(ListQuery(limit=1, tag="rare", next_token=token))
        found += items
        if not token:
            break
    assert [r.image_id for r in found] == ["4" * 32]
    assert calls > 1  # budget forced several round-trips, none lost the item


def test_unexpected_dynamodb_errors_propagate():
    table = MagicMock()
    error = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    table.update_item.side_effect = error
    table.delete_item.side_effect = error
    repo = ImageRepository(table)
    with pytest.raises(ClientError):
        repo.mark_available("a" * 32, 1, "now")
    with pytest.raises(ClientError):
        repo.mark_rejected("a" * 32, "x", "now")
    with pytest.raises(ClientError):
        repo.delete_owned("a" * 32, "alice")


def test_token_round_trip():
    key = {"image_id": "a" * 32, "user_id": "alice", "created_at": "2026"}
    assert decode_token(encode_token(key), tuple(key)) == key
