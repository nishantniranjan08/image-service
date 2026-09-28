"""Minimal JSON logging so CloudWatch Logs Insights can query fields directly."""

from __future__ import annotations

import json
import logging
import os

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    formatter = JsonFormatter()
    if root.handlers:  # the Lambda runtime pre-installs a handler
        for handler in root.handlers:
            handler.setFormatter(formatter)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
    logging.getLogger("botocore").setLevel(logging.WARNING)
