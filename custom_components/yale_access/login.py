"""Yale Home sign-in and verification without retaining passwords or codes."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .client import APP_API_KEY, BASE_URL, REQUEST_TIMEOUT, USER_AGENT
from .session import _expiry


class LoginError(Exception):
    def __init__(self, code: str = "unknown") -> None:
        self.code = code
        super().__init__(code)


class YaleLogin:
    def __init__(self, session: ClientSession, country: str, install_id: str) -> None:
        self._session = session
        self._country = country
        self._install_id = install_id
        self._method = ""
        self._identifier = ""
        self._step_token: str | None = None
        self.token: str | None = None

    @staticmethod
    def _validate_token(token: str | None) -> str:
        if not isinstance(token, str) or not token:
            raise LoginError
        try:
            _expiry(token)
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            UnicodeError,
            OverflowError,
        ):
            raise LoginError from None
        return token

    async def _request(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        authenticated: bool = False,
        rejection: str = "invalid_auth",
    ) -> tuple[dict[str, Any], str | None, str | None]:
        headers = {
            "x-api-key": APP_API_KEY,
            "x-branding": "yale",
            "x-country": self._country,
            "accept-version": "0.0.1",
            "User-Agent": USER_AGENT,
            "content-type": "application/json",
        }
        if authenticated:
            headers["x-access-token"] = self._validate_token(self.token)
        elif self._step_token:
            headers["x-step-token"] = self._step_token
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.request(
                    "GET" if authenticated else "POST",
                    BASE_URL + path,
                    headers=headers,
                    json=body,
                    allow_redirects=False,
                    timeout=ClientTimeout(total=REQUEST_TIMEOUT),
                ) as response:
                    if response.status >= 500 or response.status in (408, 429):
                        raise LoginError("cannot_connect")
                    if response.status in (400, 401, 403, 422):
                        raise LoginError(rejection)
                    if not 200 <= response.status < 300:
                        raise LoginError
                    raw = await response.read()
                    try:
                        payload = json.loads(raw) if raw else {}
                    except (ValueError, TypeError, UnicodeError):
                        raise LoginError from None
                    if not isinstance(payload, dict):
                        raise LoginError
                    rotated = response.headers.get("x-access-token")
                    if rotated and authenticated:
                        self.token = self._validate_token(rotated)
                    return payload, response.headers.get("x-step-token"), rotated
        except (ClientError, TimeoutError, OSError):
            raise LoginError("cannot_connect") from None

    async def signin(self, method: str, identifier: str, password: str) -> bool:
        if method not in {"email", "phone"}:
            raise LoginError("invalid_auth")
        self._method = method
        self._identifier = identifier
        self._step_token = None
        self.token = None
        result, step_token, access_token = await self._request(
            "/v2/session/signin",
            {
                "identifierType": method,
                "identifier": identifier,
                "credential": password,
                "installID": self._install_id,
            },
        )
        if result.get("needIdentifier"):
            raise LoginError("invalid_auth")
        if result.get("needVerify") is False:
            self.token = self._validate_token(access_token)
            return True
        if result.get("needVerify") is not True or not step_token:
            raise LoginError
        self._step_token = step_token
        _, self._step_token, _ = await self._request(
            "/v2/validation/" + method,
            {"identifier": identifier, "channel": "sms"},
        )
        if not self._step_token:
            raise LoginError
        return False

    async def verify(self, code: str) -> None:
        code = code.strip()
        if not re.fullmatch(r"[0-9]{6}", code):
            raise LoginError("invalid_code")
        if not self._step_token:
            raise LoginError
        _, _, access_token = await self._request(
            "/v2/validate/" + self._method,
            {"identifier": self._identifier, "code": code},
            rejection="invalid_code",
        )
        self.token = self._validate_token(access_token)
        self._step_token = None
        self._identifier = ""

    async def locks(self) -> dict[str, str]:
        result, _, _ = await self._request("/users/locks/mine", authenticated=True)
        locks = {}
        for lock_id, summary in result.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", lock_id) or not isinstance(
                summary, dict
            ):
                raise LoginError
            name = summary.get("LockName", summary.get("lockName"))
            locks[lock_id] = (
                name.strip() if isinstance(name, str) and name.strip() else lock_id
            )
        return locks
