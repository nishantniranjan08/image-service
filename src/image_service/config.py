"""Runtime configuration, read once per Lambda container from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

MB = 1024 * 1024


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _resolve_aws_endpoint() -> Optional[str]:
    """Endpoint used for AWS API calls made *from inside* the service.

    On real AWS this is None (boto3 default endpoints). On LocalStack the Lambda
    container receives AWS_ENDPOINT_URL (newer versions) or LOCALSTACK_HOSTNAME
    (older versions); both point at LocalStack from inside the Docker network.
    """
    explicit = os.environ.get("AWS_ENDPOINT_URL")
    if explicit:
        return explicit
    hostname = os.environ.get("LOCALSTACK_HOSTNAME")
    if hostname:
        port = os.environ.get("EDGE_PORT", "4566")
        return f"http://{hostname}:{port}"
    return None


@dataclass(frozen=True)
class Settings:
    table_name: str
    bucket_name: str
    region: str
    aws_endpoint_url: Optional[str]
    # Endpoint baked into presigned URLs. Must be reachable by the *client*,
    # which on LocalStack is the host machine (http://localhost:4566), not the
    # Docker-internal hostname the Lambda itself uses.
    public_s3_endpoint: Optional[str]
    url_ttl_seconds: int
    max_upload_bytes: int
    max_inline_upload_bytes: int
    default_page_size: int
    max_page_size: int

    @classmethod
    def from_env(cls) -> "Settings":
        aws_endpoint = _resolve_aws_endpoint()
        return cls(
            table_name=os.environ.get("TABLE_NAME", "images"),
            bucket_name=os.environ.get("BUCKET_NAME", "images-bucket"),
            region=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
            aws_endpoint_url=aws_endpoint,
            public_s3_endpoint=os.environ.get("PUBLIC_S3_ENDPOINT") or aws_endpoint,
            url_ttl_seconds=_int_env("URL_TTL_SECONDS", 900),
            max_upload_bytes=_int_env("MAX_UPLOAD_BYTES", 20 * MB),
            # API Gateway caps payloads at 10 MB and Lambda at 6 MB; base64 adds ~33%,
            # so ~4 MB of raw bytes is the safe ceiling for the inline upload path.
            max_inline_upload_bytes=_int_env("MAX_INLINE_UPLOAD_BYTES", 4 * MB),
            default_page_size=_int_env("DEFAULT_PAGE_SIZE", 20),
            max_page_size=_int_env("MAX_PAGE_SIZE", 100),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
