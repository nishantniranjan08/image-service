#!/usr/bin/env python3
"""End-to-end walkthrough of every endpoint against the deployed API.

Standard library only, so it runs anywhere:  python scripts/demo.py [API_URL]
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent


def sample_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x2b\x7c\xd8" * 4 for _ in range(4))  # 4x4 blue square
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # surface the 302 instead of following it
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def call(
    method: str, url: str, body: Any = None, headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None
) -> Tuple[int, Dict[str, str], Any]:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with _opener.open(request, timeout=60) as resp:
            status, resp_headers, payload = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        status, resp_headers, payload = err.code, dict(err.headers), err.read()
    try:
        parsed = json.loads(payload) if payload else None
    except ValueError:
        parsed = payload
    return status, resp_headers, parsed


def step(title: str) -> None:
    print(f"\n\033[1m▶ {title}\033[0m")


def show(status: int, body: Any) -> None:
    print(f"  HTTP {status}")
    if body is not None and not isinstance(body, bytes):
        text = json.dumps(body, indent=2)
        print("  " + "\n  ".join(text.splitlines()[:25]))


def expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"  ✗ {message}")
        sys.exit(1)
    print(f"  ✓ {message}")


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else (ROOT / ".api_url").read_text().strip()).rstrip("/")
    api = f"{base}/images"
    alice, bob = {"X-User-Id": "alice"}, {"X-User-Id": "bob"}
    png = sample_png()
    print(f"API: {api}")

    step("1. Upload an image inline (base64 JSON, small files)")
    status, _, body = call(
        "POST",
        api,
        {
            "image_base64": base64.b64encode(png).decode(),
            "title": "Blue square",
            "description": "Uploaded inline",
            "tags": ["demo", "blue"],
            "file_name": "blue.png",
        },
        alice,
    )
    show(status, body)
    expect(status == 201 and body["image"]["status"] == "AVAILABLE", "inline upload is immediately AVAILABLE")
    inline_id = body["image"]["image_id"]

    step("2. Upload via presigned URL (large files go straight to S3)")
    status, _, body = call("POST", api, {"content_type": "image/png", "title": "Via S3", "tags": ["demo"]}, bob)
    show(status, body)
    expect(status == 202 and body["image"]["status"] == "PENDING", "metadata created as PENDING")
    presigned_id = body["image"]["image_id"]
    status, _, _ = call("PUT", body["upload"]["url"], headers=body["upload"]["headers"], raw=png)
    expect(status == 200, "client PUT the bytes directly to S3")
    for _ in range(30):  # the S3 ObjectCreated trigger verifies the file asynchronously
        status, _, body = call("GET", f"{api}/{presigned_id}")
        if body and body.get("status") == "AVAILABLE":
            break
        time.sleep(1)
    expect(body.get("status") == "AVAILABLE", "S3 trigger verified the upload and marked it AVAILABLE")

    step("3. List images with filters")
    status, _, body = call("GET", f"{api}?user_id=alice")
    expect(status == 200 and inline_id in [i["image_id"] for i in body["items"]], "filter by user_id")
    status, _, body = call("GET", f"{api}?tag=demo&content_type=image/png&limit=1")
    show(status, body)
    expect(status == 200 and body["count"] == 1 and body["next_token"], "filter by tag + content_type, paginated")
    status, _, body = call("GET", f"{api}?tag=demo&content_type=image/png&limit=1&next_token={body['next_token']}")
    expect(status == 200 and body["count"] >= 1, "next page via next_token")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    status, _, body = call("GET", f"{api}?created_from={today}&user_id=bob")
    expect(status == 200 and presigned_id in [i["image_id"] for i in body["items"]], "filter by date range")

    step("4. View / download")
    status, _, body = call("GET", f"{api}/{inline_id}")
    show(status, body)
    expect(status == 200 and body["download_url"], "metadata with presigned view/download URLs")
    status, _, content = call("GET", body["download_url"])
    expect(status == 200 and content == png, "download URL returns the exact bytes uploaded")
    status, headers, _ = call("GET", f"{api}/{inline_id}?download=true")
    expect(status == 302 and "Location" in headers, "?download=true redirects to the file")

    step("5. Delete")
    status, _, body = call("DELETE", f"{api}/{inline_id}", headers=bob)
    show(status, body)
    expect(status == 403, "bob cannot delete alice's image")
    status, _, _ = call("DELETE", f"{api}/{inline_id}", headers=alice)
    expect(status == 204, "alice deletes her image")
    status, _, _ = call("GET", f"{api}/{inline_id}")
    expect(status == 404, "deleted image is gone")
    call("DELETE", f"{api}/{presigned_id}", headers=bob)

    step("6. Validation")
    status, _, body = call("POST", api, {"content_type": "application/pdf"}, alice)
    show(status, body)
    expect(status == 400, "unsupported file type rejected")
    status, _, _ = call("POST", api, {"content_type": "image/png"})
    expect(status == 401, "missing caller identity rejected")

    print("\n\033[1;32mAll endpoints working.\033[0m")


if __name__ == "__main__":
    main()
