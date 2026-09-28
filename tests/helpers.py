"""Test helpers: sample images and API Gateway / S3 event builders."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from types import SimpleNamespace
from typing import Any, Dict, Optional


TABLE = "images-test"
BUCKET = "images-test-bucket"
REGION = "us-east-1"


def make_png(width: int = 1, height: int = 1) -> bytes:
    """A real, minimal PNG (valid signature, IHDR, IDAT, IEND)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
WEBP_BYTES = b"RIFF\x1a\x00\x00\x00WEBPVP8 " + b"\x00" * 16


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def api_event(
    method: str,
    path: str,
    *,
    body: Any = None,
    user: Optional[str] = None,
    path_params: Optional[Dict[str, str]] = None,
    query: Optional[Dict[str, str]] = None,
    raw_body: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an API Gateway REST (Lambda proxy) event."""
    headers = {"Content-Type": "application/json"}
    if user:
        headers["X-User-Id"] = user
    return {
        "httpMethod": method,
        "path": path,
        "headers": headers,
        "pathParameters": path_params,
        "queryStringParameters": query,
        "body": raw_body if raw_body is not None else (json.dumps(body) if body is not None else None),
        "isBase64Encoded": False,
        "requestContext": {},
    }


def s3_event(key: str, size: int = 0) -> Dict[str, Any]:
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": BUCKET}, "object": {"key": key, "size": size}},
            }
        ]
    }


def body_of(response: Dict[str, Any]) -> Any:
    return json.loads(response["body"]) if response["body"] else None


CONTEXT = SimpleNamespace(aws_request_id="req-test-123")
