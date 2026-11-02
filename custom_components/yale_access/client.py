"""Single-attempt Yale app HTTP requests without response-body logging."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout
from yarl import URL

from .const import DEFAULT_COUNTRY
from .errors import (
    InvalidRequest,
    NotFound,
    Pending,
    RequestFailed,
    UpstreamUnavailable,
)
from .session import SessionFile

if TYPE_CHECKING:
    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.aaecosystem.com"
APP_API_KEY = "2fcb39a8-40b1-4f4e-bc58-1aeecc0670e0"
USER_AGENT = "Yale Home/2026.11.0 261013398 (Android; SDK 35; Home Assistant)"
REQUEST_TIMEOUT = 25
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{8,128}")


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise InvalidRequest("Invalid Yale identifier")
    return quote(value, safe="")


class YaleClient:
    def __init__(
        self,
        session: ClientSession,
        session_file: SessionFile,
        lock_id: str,
        *,
        country: str = DEFAULT_COUNTRY,
    ) -> None:
        if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
            raise InvalidRequest("Invalid country code (two uppercase letters)")
        self._session = session
        self._session_file = session_file
        self._country = country
        self.lock_id = lock_id
        self._lock_path = "/locks/" + _identifier(lock_id)
        self._request_lock = asyncio.Lock()
        if hasattr(session, "_retry_connection"):
            session._retry_connection = False

    @property
    def session_file(self) -> SessionFile:
        return self._session_file

    async def detail(self) -> dict[str, Any]:
        return await self._send("GET", self._lock_path, "lock detail")

    async def pins(self) -> dict[str, Any]:
        return await self._send("GET", self._lock_path + "/pins", "pins")

    async def profile(self, user_id: str) -> dict[str, Any]:
        return await self._send(
            "GET", "/users/" + _identifier(user_id), "profile", version="2.0.0"
        )

    async def activities(self, house_id: str, end: str | None = None) -> dict[str, Any]:
        params = {"limit": "100"}
        if end is not None:
            if not isinstance(end, str) or not end or len(end) > 2048:
                raise InvalidRequest("Invalid activity cursor")
            params["end"] = end
        return await self._send(
            "GET",
            "/houses/" + _identifier(house_id) + "/activities",
            "activities",
            version="4.0.0",
            params=params,
        )

    async def create_user(self, name: str) -> str:
        first_name, *rest = name.split(maxsplit=1)
        body = {
            "lockID": self.lock_id,
            "credentialType": "pin",
            "firstName": first_name,
            "lastName": rest[0] if rest else ".",
        }
        payload = await self._send("POST", "/unverifiedusers", "create user", body=body)
        candidates = set()
        invalid = False
        for key in ("id", "userID", "UserID"):
            if key not in payload:
                continue
            value = payload[key]
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                invalid = True
            else:
                candidates.add(value)
        if invalid or len(candidates) != 1:
            raise Pending(
                "User creation outcome is unresolved. Inspect the Yale app before retrying; do not create the user again blindly."
            )
        user_id = candidates.pop()
        try:
            _identifier(user_id)
        except InvalidRequest:
            raise Pending(
                "User creation outcome is unresolved. Inspect the Yale app before retrying; do not create the user again blindly."
            ) from None
        return user_id

    async def intent(self, user_id: str, body: dict[str, str]) -> None:
        await self._send(
            "PUT",
            self._lock_path + "/users/" + _identifier(user_id) + "/credentials",
            "credential intent",
            body=body,
        )

    async def sync(self) -> None:
        await self._send(
            "PUT",
            self._lock_path + "/credentials/sync",
            "credential sync",
            params={"notificationType": "websocket"},
            body={"hideActivityLog": False},
        )

    async def _send(
        self,
        method: str,
        path: str,
        step: str,
        *,
        version: str = "0.0.1",
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._request_lock:
            if not await self._session_file.load_if_changed():
                raise UpstreamUnavailable("Yale login is unavailable")
            headers = {
                "x-api-key": APP_API_KEY,
                "x-branding": "yale",
                "x-country": self._country,
                "accept-version": version,
                "User-Agent": USER_AGENT,
                "content-type": "application/json",
                "x-access-token": self._session_file.token,
            }
            url = BASE_URL + path
            if params:
                url += "?" + "&".join(
                    f"{quote(k, safe='')}={quote(v, safe='')}"
                    for k, v in params.items()
                )
            status = None
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    async with self._session.request(
                        method,
                        URL(url, encoded=True),
                        headers=headers,
                        json=body,
                        allow_redirects=False,
                        timeout=ClientTimeout(total=REQUEST_TIMEOUT),
                    ) as response:
                        status = response.status
                        rotated = response.headers.get("x-access-token")
                        if rotated:
                            await self._session_file.rotate(rotated)
                        if status in (401, 403) and (method == "GET" or status == 401):
                            await self._session_file.mark_failed()
                        if not 200 <= status < 300:
                            if status == 404:
                                raise NotFound
                            if status >= 500 or status in (408, 429):
                                if method != "GET":
                                    raise Pending(
                                        "Yale write outcome is unresolved. Inspect the Yale app before retrying; do not repeat the request blindly."
                                    )
                                raise UpstreamUnavailable
                            if status == 403 and method != "GET":
                                raise RequestFailed("Yale denied write permission")
                            raise RequestFailed
                        raw = await response.read()
                        if not raw:
                            return {}
                        try:
                            payload = json.loads(raw)
                        except (ValueError, TypeError, UnicodeError):
                            if method != "GET":
                                raise Pending(
                                    "Yale write outcome is unresolved. Inspect the Yale app before retrying; do not repeat the request blindly."
                                ) from None
                            raise UpstreamUnavailable(
                                "Yale returned an unexpected response"
                            ) from None
                        if not isinstance(payload, dict):
                            if method != "GET":
                                raise Pending(
                                    "Yale write outcome is unresolved. Inspect the Yale app before retrying; do not repeat the request blindly."
                                )
                            raise UpstreamUnavailable(
                                "Yale returned an unexpected response"
                            )
                        return payload
            except (TimeoutError, ClientError, OSError):
                if method != "GET":
                    raise Pending(
                        "Yale write outcome is unresolved. The one-time PIN was not delivered and cannot be recovered here. Inspect the Yale app before retrying; do not repeat the request blindly."
                    ) from None
                raise UpstreamUnavailable from None
            finally:
                _LOGGER.debug("%s: %s", step, status)
