"""Keep one-time PIN websocket results out of Home Assistant debug logs."""

from __future__ import annotations

import logging
import re

_SECRET_FIELD = re.compile(
    r"[\"'](?:pin|access_token|x-access-token)[\"']\s*:", re.IGNORECASE
)


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _SECRET_FIELD.search(record.getMessage()):
            record.msg = "Sensitive websocket message omitted"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def install_filter() -> None:
    logger = logging.getLogger("homeassistant.components.websocket_api.http.connection")
    if not any(isinstance(item, SecretFilter) for item in logger.filters):
        logger.addFilter(SecretFilter())
