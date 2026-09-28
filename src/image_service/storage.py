"""S3 access for image bytes and presigned URLs."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from botocore.exceptions import ClientError

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_file_name(name: Optional[str], fallback: str) -> str:
    """Sanitise a user-supplied name for use in a Content-Disposition header."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", name or "").strip("._")
    return cleaned[:100] or fallback


class ImageStorage:
    def __init__(self, s3: Any, presign_s3: Any, bucket: str, url_ttl_seconds: int) -> None:
        self._s3 = s3
        # A separate client for presigning: its endpoint is the one the *caller*
        # can reach (see Settings.public_s3_endpoint). Signing is offline.
        self._presign_s3 = presign_s3
        self._bucket = bucket
        self._ttl = url_ttl_seconds

    @property
    def url_ttl_seconds(self) -> int:
        return self._ttl

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )

    def delete(self, key: str) -> None:
        # S3 DeleteObject is idempotent: deleting a missing key succeeds.
        self._s3.delete_object(Bucket=self._bucket, Key=key)

    def head(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            return self._s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def read_prefix(self, key: str, num_bytes: int = 16) -> bytes:
        """Read just the first bytes of an object (enough to check its magic number)."""
        resp = self._s3.get_object(Bucket=self._bucket, Key=key, Range=f"bytes=0-{num_bytes - 1}")
        return resp["Body"].read()

    def presigned_upload_url(self, key: str, content_type: str) -> str:
        # Content-Type is part of the signature: the client must send exactly this header.
        return self._presign_s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=self._ttl,
            HttpMethod="PUT",
        )

    def presigned_download_url(self, key: str, file_name: str, as_attachment: bool) -> str:
        disposition = "attachment" if as_attachment else "inline"
        return self._presign_s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ResponseContentDisposition": f'{disposition}; filename="{file_name}"',
            },
            ExpiresIn=self._ttl,
        )
