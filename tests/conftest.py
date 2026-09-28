"""Shared fixtures: every test runs against in-memory AWS (moto), never real AWS."""

from __future__ import annotations

from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from helpers import BUCKET, REGION, TABLE
from image_service import handlers
from image_service.schema import table_definition


@pytest.fixture(autouse=True)
def aws_env(monkeypatch: pytest.MonkeyPatch):
    for name in ("AWS_ENDPOINT_URL", "LOCALSTACK_HOSTNAME", "PUBLIC_S3_ENDPOINT", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("TABLE_NAME", TABLE)
    monkeypatch.setenv("BUCKET_NAME", BUCKET)
    handlers.reset_dependencies()
    yield
    handlers.reset_dependencies()


@pytest.fixture
def aws():
    """Create the real table (same definition as production) and bucket in moto."""
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(**table_definition(TABLE))
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        yield SimpleNamespace(
            s3=s3,
            table=boto3.resource("dynamodb", region_name=REGION).Table(TABLE),
        )


@pytest.fixture
def service(aws):
    return handlers.get_service()


