"""Private, immediately persisted Yale app session."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import SESSION_STORAGE_KEY, STORAGE_VERSION
from .errors import UpstreamUnavailable


def _expiry(token: str) -> float:
    if not isinstance(token, str):
        raise TypeError
    encoded = token.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    exp = payload["exp"]
    if type(exp) not in (int, float) or exp <= 0:
        raise ValueError
    datetime.fromtimestamp(exp, UTC)
    return float(exp)


class SessionStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(
            hass, STORAGE_VERSION, SESSION_STORAGE_KEY, private=True, atomic_writes=True
        )
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {}
        self._loaded = False
        self._failed = True
        self._published: tuple[str, str | None] | None = None
        self.state_changed: Callable[[], Awaitable[None]] | None = None

    @property
    def token(self) -> str | None:
        return self._data.get("access_token")

    @property
    def expires(self) -> str | None:
        exp = self._data.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(exp, UTC).isoformat().replace("+00:00", "Z")

    @property
    def state(self) -> str:
        if self._failed or not self.token:
            return "failed"
        remaining = self._data["exp"] - datetime.now(UTC).timestamp()
        return "expiring" if remaining < timedelta(days=14).total_seconds() else "ok"

    async def _publish(self) -> None:
        current = self.state, self.expires
        if current != self._published:
            self._published = current
            if self.state_changed is not None:
                await self.state_changed()

    async def load(self) -> bool:
        async with self._lock:
            if not self._loaded:
                self._loaded = True
                try:
                    data = await self._store.async_load()
                    if not isinstance(data, dict):
                        raise TypeError
                    token = data["access_token"]
                    if not isinstance(token, str) or not token:
                        raise ValueError
                    self._data = {
                        "access_token": token,
                        "exp": _expiry(token),
                        "saved_at": data.get("saved_at"),
                    }
                    self._failed = False
                except Exception:  # noqa: BLE001
                    self._data = {}
                    self._failed = True
            loaded = bool(self.token) and not self._failed
        await self._publish()
        return loaded

    async def mark_failed(self) -> None:
        async with self._lock:
            self._failed = True
        await self._publish()

    async def rotate(self, token: str) -> None:
        try:
            exp = _expiry(token)
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            UnicodeError,
            OverflowError,
        ):
            raise UpstreamUnavailable("Yale returned an invalid session") from None
        async with self._lock:
            if token == self.token and not self._failed:
                return
            data = {
                "access_token": token,
                "saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "exp": exp,
            }
            try:
                await self._store.async_save(data)
                if await self._store.async_load() != data:
                    raise UpstreamUnavailable("Yale session could not be saved")
            except Exception:  # noqa: BLE001
                self._failed = True
                raise UpstreamUnavailable("Yale session could not be saved") from None
            else:
                self._data = data
                self._loaded = True
                self._failed = False
            finally:
                await self._publish()

    async def remove(self) -> None:
        await self._store.async_remove()
