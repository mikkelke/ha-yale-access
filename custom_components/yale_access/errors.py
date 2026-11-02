"""Errors returned to websocket clients as stable codes."""

from __future__ import annotations


class YaleAccessError(Exception):
    code = "unknown_error"
    default_message = "Something went wrong"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class Forbidden(YaleAccessError):
    code = "forbidden"
    default_message = "Not allowed"


class Protected(YaleAccessError):
    code = "protected"
    default_message = "This user's access cannot be changed here"


class InvalidRequest(YaleAccessError):
    code = "invalid_format"
    default_message = "Invalid request"


class NotFound(YaleAccessError):
    code = "not_found"
    default_message = "Not found"


class UpstreamUnavailable(YaleAccessError):
    code = "upstream_unavailable"
    default_message = "Yale is unavailable"


class RequestFailed(YaleAccessError):
    code = "request_failed"
    default_message = "Yale could not handle the request"


class Pending(YaleAccessError):
    code = "pending"
    default_message = (
        "The change is pending and will be reconciled on refresh. "
        "The one-time PIN was not delivered and cannot be recovered here. "
        "Do not create the user again; check its status and end it before replacing it."
    )


class ReadOnly(YaleAccessError):
    code = "read_only"
    default_message = "Recurring access is read-only"
