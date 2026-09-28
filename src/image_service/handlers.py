"""Lambda entry points. Each function is deployed as its own Lambda.

Handlers stay thin: parse the event, call the service, shape the response.
AWS clients and the service are created once per container and reused across
warm invocations (connection reuse matters for latency under load).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config

from .config import get_settings
from .http import (
    Event,
    Response,
    api_handler,
    caller_id,
    json_response,
    parse_json_body,
    path_param,
    query_params,
    redirect_response,
)
from .logging_config import configure_logging
from .models import ListQuery, UploadRequest
from .repository import ImageRepository
from .service import ImageService
from .storage import ImageStorage

configure_logging()
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_service() -> ImageService:
    settings = get_settings()
    retry = Config(retries={"max_attempts": 5, "mode": "adaptive"})
    session = boto3.session.Session(region_name=settings.region)

    table = session.resource("dynamodb", endpoint_url=settings.aws_endpoint_url, config=retry).Table(
        settings.table_name
    )
    s3 = session.client("s3", endpoint_url=settings.aws_endpoint_url, config=retry)
    presign_s3 = session.client(
        "s3",
        endpoint_url=settings.public_s3_endpoint,
        config=Config(
            signature_version="s3v4",
            # Path-style keeps presigned URLs working against localhost:4566.
            s3={"addressing_style": "path" if settings.public_s3_endpoint else "auto"},
        ),
    )
    return ImageService(
        repository=ImageRepository(table),
        storage=ImageStorage(s3, presign_s3, settings.bucket_name, settings.url_ttl_seconds),
        max_upload_bytes=settings.max_upload_bytes,
    )


def reset_dependencies() -> None:
    """Drop cached settings/clients (used by tests)."""
    get_service.cache_clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------- API routes
@api_handler
def upload_image(event: Event, context: Any) -> Response:
    """POST /images"""
    user_id = caller_id(event)
    request = UploadRequest.parse(parse_json_body(event), get_settings().max_inline_upload_bytes)
    result = get_service().create_image(user_id, request)
    status = 201 if request.is_inline else 202  # 202: accepted, bytes still to come
    return json_response(status, result)


@api_handler
def list_images(event: Event, context: Any) -> Response:
    """GET /images?user_id=&tag=&content_type=&created_from=&created_to=&limit=&next_token="""
    settings = get_settings()
    query = ListQuery.parse(query_params(event), settings.default_page_size, settings.max_page_size)
    return json_response(200, get_service().list_images(query))


@api_handler
def get_image(event: Event, context: Any) -> Response:
    """GET /images/{image_id}            -> metadata + presigned view/download URLs
    GET /images/{image_id}?download=true -> 302 redirect straight to the file
    """
    image_id = path_param(event, "image_id")
    params = query_params(event)
    download = (params.get("download") or "").lower()
    if download in ("true", "1", "yes"):
        return redirect_response(get_service().get_download_url(image_id, as_attachment=True))
    if (params.get("view") or "").lower() in ("true", "1", "yes"):
        return redirect_response(get_service().get_download_url(image_id, as_attachment=False))
    return json_response(200, get_service().get_image(image_id))


@api_handler
def delete_image(event: Event, context: Any) -> Response:
    """DELETE /images/{image_id} (owner only)"""
    user_id = caller_id(event)
    get_service().delete_image(user_id, path_param(event, "image_id"))
    return json_response(204)


# --------------------------------------------------------------- S3 trigger
def process_upload(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """S3 ObjectCreated trigger: verify presigned uploads and mark them AVAILABLE."""
    outcomes = []
    for record in event.get("Records", []):
        s3_info = record.get("s3", {})
        key = unquote_plus(s3_info.get("object", {}).get("key", ""))  # event keys are URL-encoded
        size = s3_info.get("object", {}).get("size")
        outcome = get_service().confirm_upload(key, size)
        outcomes.append({"key": key, "outcome": outcome})
        logger.info("processed s3 event", extra={"key": key, "outcome": outcome})
    return {"processed": outcomes}
