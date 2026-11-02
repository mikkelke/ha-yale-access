"""Private, atomically persisted Yale app session."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import UpstreamUnavailable

Executor = Callable[..., Awaitable[Any]]
Fingerprint = tuple[int, int, int, int]


def _fingerprint(path: Path) -> Fingerprint:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size


def _expiry(token: str) -> float:
    encoded = token.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    exp = payload["exp"]
    if type(exp) not in (int, float) or exp <= 0:
        raise ValueError
    datetime.fromtimestamp(exp, UTC)
    return float(exp)


def _read(path: Path) -> tuple[dict[str, Any], Fingerprint]:
    before = _fingerprint(path)
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    after = _fingerprint(path)
    if before != after or not isinstance(data, dict):
        raise ValueError
    token, install_id = data.get("access_token"), data.get("install_id")
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(install_id, str)
        or not install_id
    ):
        raise ValueError
    return {
        "access_token": token,
        "install_id": install_id,
        "saved_at": data.get("saved_at"),
        "exp": _expiry(token),
    }, after


def _write(
    path: Path, data: dict[str, Any], expected: Fingerprint
) -> Fingerprint | None:
    temporary = None
    try:
        if _fingerprint(path) != expected:
            return None
        fd, temporary = tempfile.mkstemp(prefix=".yale-session-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        if _fingerprint(path) != expected:
            return None
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return _fingerprint(path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class SessionFile:
    def __init__(self, path: str, executor: Executor | None = None) -> None:
        self._path = Path(path)
        self._executor = executor or asyncio.to_thread
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {}
        self._fingerprint: Fingerprint | None = None
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

    async def _load(self) -> bool:
        try:
            fingerprint = await self._executor(_fingerprint, self._path)
            if fingerprint != self._fingerprint:
                self._data, self._fingerprint = await self._executor(_read, self._path)
                self._failed = False
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            UnicodeError,
            OverflowError,
        ):
            self._data = {}
            self._fingerprint = None
            self._failed = True
        return bool(self.token) and not self._failed

    async def load_if_changed(self) -> bool:
        async with self._lock:
            loaded = await self._load()
        await self._publish()
        return loaded

    async def mark_failed(self) -> None:
        async with self._lock:
            if not await self._unchanged():
                return
            self._failed = True
        await self._publish()

    async def _unchanged(self) -> bool:
        try:
            return await self._executor(_fingerprint, self._path) == self._fingerprint
        except OSError:
            return False

    async def rotate(self, token: str) -> None:
        if not isinstance(token, str) or not token:
            return
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
            try:
                if not await self._unchanged():
                    return
                if token == self.token:
                    return
                data = {
                    "access_token": token,
                    "install_id": self._data["install_id"],
                    "saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "exp": exp,
                }
                fingerprint = await self._executor(
                    _write, self._path, data, self._fingerprint
                )
                if fingerprint is not None:
                    self._data, self._fingerprint = data, fingerprint
                    self._failed = False
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                UnicodeError,
                OverflowError,
            ):
                self._failed = True
                raise UpstreamUnavailable("Yale session could not be saved") from None
            finally:
                await self._publish()
