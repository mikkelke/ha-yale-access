"""Session file persistence and health."""

from __future__ import annotations

import base64
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.yale_access.errors import UpstreamUnavailable
from custom_components.yale_access.session import SessionFile


def token(days: int, label: str = "placeholder") -> str:
    payload = {
        "exp": int((datetime.now(UTC) + timedelta(days=days)).timestamp()),
        "placeholder": label,
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"placeholder.{encoded}.placeholder"


def write_session(path, value: str) -> None:
    path.write_text(
        json.dumps(
            {
                "access_token": value,
                "install_id": "placeholder-install-id",
                "saved_at": "placeholder",
                "exp": 1,
            }
        )
    )


@pytest.mark.asyncio
async def test_rotation_is_atomic_and_mode_600(tmp_path):
    path = tmp_path / "session.json"
    original, rotated = token(60), token(120)
    write_session(path, original)
    session = SessionFile(str(path))
    assert await session.load_if_changed()
    inode = path.stat().st_ino
    await session.rotate(rotated)
    data = json.loads(path.read_text())
    assert data["access_token"] == rotated
    assert data["install_id"] == "placeholder-install-id"
    assert data["exp"] > 1
    assert set(data) == {"access_token", "install_id", "saved_at", "exp"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_ino != inode
    assert list(tmp_path.iterdir()) == [path]
    assert rotated not in repr(session)


@pytest.mark.asyncio
async def test_stale_rotation_leaves_relogin_for_next_load(tmp_path):
    path = tmp_path / "session.json"
    original = token(60)
    write_session(path, original)
    session = SessionFile(str(path))
    await session.load_if_changed()
    relogin = token(120, "relogin")
    replacement = tmp_path / "replacement"
    write_session(replacement, relogin)
    os.replace(replacement, path)
    await session.rotate(token(90, "stale-response"))
    assert session.token == original
    assert json.loads(path.read_text())["access_token"] == relogin
    assert await session.load_if_changed()
    assert session.token == relogin


@pytest.mark.asyncio
async def test_stale_auth_failure_leaves_relogin_for_next_load(tmp_path):
    path = tmp_path / "session.json"
    original = token(60)
    write_session(path, original)
    session = SessionFile(str(path))
    await session.load_if_changed()
    relogin = token(120, "relogin")
    replacement = tmp_path / "replacement"
    write_session(replacement, relogin)
    os.replace(replacement, path)
    await session.mark_failed()
    assert session.state == "ok"
    assert session.token == original
    assert json.loads(path.read_text())["access_token"] == relogin
    assert await session.load_if_changed()
    assert session.token == relogin
    assert session.state == "ok"


@pytest.mark.asyncio
async def test_states_and_only_changed_callbacks(tmp_path):
    path = tmp_path / "session.json"
    write_session(path, token(60))
    session = SessionFile(str(path))
    session.state_changed = AsyncMock()
    await session.load_if_changed()
    assert session.state == "ok"
    assert session.expires.endswith("Z")
    assert session.state_changed.await_count == 1
    await session.load_if_changed()
    assert session.state_changed.await_count == 1
    await session.rotate(token(7))
    assert session.state == "expiring"
    await session.mark_failed()
    assert session.state == "failed"
    await session.mark_failed()
    assert session.state_changed.await_count == 3
    assert not await session.load_if_changed()
    write_session(path, token(120))
    assert await session.load_if_changed()
    assert session.state == "ok"


@pytest.mark.asyncio
async def test_unreadable_or_malformed_session_is_failed(tmp_path):
    path = tmp_path / "session.json"
    session = SessionFile(str(path))
    assert not await session.load_if_changed()
    assert session.state == "failed"
    assert session.expires is None
    path.write_text("not json")
    assert not await session.load_if_changed()
    write_session(path, "invalid")
    assert not await session.load_if_changed()


@pytest.mark.asyncio
async def test_failed_persistence_is_sanitized(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    write_session(path, token(60))
    session = SessionFile(str(path))
    await session.load_if_changed()
    replacement = token(120)
    monkeypatch.setattr(
        "custom_components.yale_access.session.os.replace",
        lambda *args: (_ for _ in ()).throw(OSError(replacement)),
    )
    with pytest.raises(UpstreamUnavailable) as error:
        await session.rotate(replacement)
    assert replacement not in str(error.value)
    assert session.state == "failed"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"exp": float("nan")},
        {"exp": float("inf")},
        {"exp": True},
        [],
        {"exp": "placeholder"},
    ],
)
async def test_malformed_expiry_is_sanitized(tmp_path, payload):
    path = tmp_path / "session.json"
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    malformed = f"placeholder.{encoded}.placeholder"
    write_session(path, malformed)
    session = SessionFile(str(path))
    assert not await session.load_if_changed()
    assert session.state == "failed"
    with pytest.raises(UpstreamUnavailable) as error:
        await session.rotate(malformed)
    assert malformed not in str(error.value)
