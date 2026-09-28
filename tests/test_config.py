"""Configuration and small infrastructure helpers."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from image_service.config import Settings
from image_service.logging_config import JsonFormatter
from image_service.storage import ImageStorage, safe_file_name


def test_settings_defaults(monkeypatch):
    for name in ("MAX_UPLOAD_BYTES", "URL_TTL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.aws_endpoint_url is None  # real AWS: boto3 default endpoints
    assert settings.public_s3_endpoint is None
    assert settings.max_upload_bytes == 20 * 1024 * 1024
    assert settings.url_ttl_seconds == 900


def test_settings_localstack_hostname(monkeypatch):
    monkeypatch.setenv("LOCALSTACK_HOSTNAME", "localstack")
    settings = Settings.from_env()
    assert settings.aws_endpoint_url == "http://localstack:4566"


def test_public_endpoint_overrides_internal_one(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://172.17.0.2:4566")
    monkeypatch.setenv("PUBLIC_S3_ENDPOINT", "http://localhost:4566")
    settings = Settings.from_env()
    assert settings.aws_endpoint_url == "http://172.17.0.2:4566"
    assert settings.public_s3_endpoint == "http://localhost:4566"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("holiday photo.png", "holiday_photo.png"),
        ('evil"; filename=x.exe', "evil_filename_x.exe"),
        ("../../etc/passwd", "etc_passwd"),
        (None, "fallback.png"),
        ("...", "fallback.png"),
    ],
)
def test_safe_file_name(name, expected):
    assert safe_file_name(name, "fallback.png") == expected


def test_head_propagates_unexpected_errors():
    s3 = MagicMock()
    s3.head_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
    with pytest.raises(ClientError):
        ImageStorage(s3, s3, "bucket", 60).head("key")


def test_json_log_format_includes_extra_fields():
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.image_id = "abc"
    payload = json.loads(JsonFormatter().format(record))
    assert payload == {"level": "INFO", "logger": "svc", "message": "hello world", "image_id": "abc"}
