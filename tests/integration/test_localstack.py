"""End-to-end tests against the real LocalStack deployment (API Gateway -> Lambda -> S3/DynamoDB).

Run with:  make up deploy test-integration
Skipped automatically when no deployment is available.
"""

from __future__ import annotations

import base64
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from demo import call, sample_png  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def api() -> str:
    url_file = ROOT / ".api_url"
    if not url_file.exists():
        pytest.skip("no deployment found: run `make up deploy` first")
    base = url_file.read_text().strip()
    try:
        urllib.request.urlopen(f"{base}/images?limit=1", timeout=30)
    except urllib.error.HTTPError:
        pass
    except OSError:
        pytest.skip("LocalStack is not reachable")
    return f"{base}/images"


def test_inline_upload_view_and_delete(api):
    png = sample_png()
    status, _, body = call(
        "POST", api, {"image_base64": base64.b64encode(png).decode(), "tags": ["it"]}, {"X-User-Id": "it-user"}
    )
    assert status == 201
    image_id = body["image"]["image_id"]

    status, _, meta = call("GET", f"{api}/{image_id}")
    assert status == 200
    status, _, content = call("GET", meta["download_url"])
    assert status == 200 and content == png

    status, _, listing = call("GET", f"{api}?user_id=it-user&tag=it")
    assert image_id in [i["image_id"] for i in listing["items"]]

    assert call("DELETE", f"{api}/{image_id}", headers={"X-User-Id": "someone-else"})[0] == 403
    assert call("DELETE", f"{api}/{image_id}", headers={"X-User-Id": "it-user"})[0] == 204
    assert call("GET", f"{api}/{image_id}")[0] == 404


def test_presigned_upload_is_confirmed_by_s3_trigger(api):
    status, _, body = call("POST", api, {"content_type": "image/png"}, {"X-User-Id": "it-user"})
    assert status == 202
    image_id = body["image"]["image_id"]
    assert call("PUT", body["upload"]["url"], headers=body["upload"]["headers"], raw=sample_png())[0] == 200

    deadline = time.time() + 60
    status_value = None
    while time.time() < deadline:
        status_value = call("GET", f"{api}/{image_id}")[2]["status"]
        if status_value == "AVAILABLE":
            break
        time.sleep(1)
    assert status_value == "AVAILABLE"
    call("DELETE", f"{api}/{image_id}", headers={"X-User-Id": "it-user"})


def test_validation_errors(api):
    assert call("POST", api, {"content_type": "text/plain"}, {"X-User-Id": "it-user"})[0] == 400
    assert call("POST", api, {"content_type": "image/png"})[0] == 401
    assert call("GET", f"{api}?limit=0")[0] == 400
    assert call("GET", f"{api}/{'0' * 32}")[0] == 404
