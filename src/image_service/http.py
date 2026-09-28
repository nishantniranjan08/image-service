"""API Gateway (REST, Lambda proxy integration) request/response helpers."""

from __future__ import annotations

import base64
import functools
import json
import logging
import time
from typing import Any, Callable, Dict, Mapping, Optional

from .errors import AppError, UnauthorizedError, ValidationError
from .models import validate_user_id

logger = logging.getLogger(__name__)

Event = Dict[str, Any]
Response = Dict[str, Any]

_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-User-Id,Authorization",
    "Cache-Control": "no-store",
}


def json_response(status_code: int, body: Any = None, headers: Optional[Dict[str, str]] = None) -> Response:
    return {
        "statusCode": status_code,
        "headers": {**_BASE_HEADERS, **(headers or {})},
        "body": "" if body is None else json.dumps(body, default=str),
    }


def redirect_response(location: str) -> Response:
    return json_response(302, None, {"Location": location})


def error_response(err: AppError, request_id: Optional[str]) -> Response:
    body = {"error": err.to_dict()}
    if request_id:
        body["error"]["request_id"] = request_id
    return json_response(err.status_code, body)


def get_header(event: Event, name: str) -> Optional[str]:
    headers = event.get("headers") or {}
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def path_param(event: Event, name: str) -> str:
    value = (event.get("pathParameters") or {}).get(name)
    if not value:
        raise ValidationError(f"missing path parameter '{name}'")
    return value


def query_params(event: Event) -> Mapping[str, str]:
    return event.get("queryStringParameters") or {}


def parse_json_body(event: Event) -> Dict[str, Any]:
    raw = event.get("body")
    if raw is None or raw == "":
        raise ValidationError("request body is required")
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except ValueError:
        raise ValidationError("request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    return body


def caller_id(event: Event) -> str:
    """Identify the caller.

    Production: identity comes from an API Gateway authorizer (e.g. Cognito
    `sub` claim), which API Gateway verifies before the Lambda runs.
    Local/demo: the `X-User-Id` header stands in for that authorizer.
    """
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = authorizer.get("claims") or {}
    user_id = claims.get("sub") or authorizer.get("principalId") or get_header(event, "X-User-Id")
    if not user_id:
        raise UnauthorizedError("missing caller identity: send the X-User-Id header")
    try:
        return validate_user_id(user_id)
    except ValidationError as err:
        raise UnauthorizedError(err.message) from None


def api_handler(func: Callable[[Event, Any], Response]) -> Callable[[Event, Any], Response]:
    """Wrap a Lambda handler: structured access log + uniform error responses."""

    @functools.wraps(func)
    def wrapper(event: Event, context: Any) -> Response:
        started = time.perf_counter()
        request_id = getattr(context, "aws_request_id", None)
        try:
            response = func(event, context)
        except AppError as err:
            response = error_response(err, request_id)
        except Exception:  # never leak stack traces to clients
            logger.exception("unhandled error", extra={"request_id": request_id})
            response = error_response(AppError("internal server error"), request_id)
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": event.get("httpMethod"),
                "path": event.get("path"),
                "status": response["statusCode"],
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response

    return wrapper
