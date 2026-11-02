"""Session store persistence and health."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.yale_access.errors import UpstreamUnavailable
from custom_components.yale_access.session import SessionStore


def token(days: int, label: str = "placeholder") -> str:
    payload = {
        "exp": int((datetime.now(UTC) + timedelta(days=days)).timestamp()),
        "placeholder": label,
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"placeholder.{encoded}.placeholder"


def make_session(value: str | None = None) -> SessionStore:
    store = Mock(
        async_load=AsyncMock(return_value={"access_token": value} if value else None),
        async_save=AsyncMock(),
        async_remove=AsyncMock(),
    )

    async def save(data):
        store.async_load.return_value = dict(data)

    store.async_save.side_effect = save
    with patch("custom_components.yale_access.session.Store", return_value=store):
        return SessionStore(Mock())


async def test_rotation_is_immediately_persisted_and_private(hass, hass_storage):
    original, rotated = token(60), token(120)
    session = SessionStore(hass)
    assert session._store._private
    await session.rotate(original)
    assert await session.load()
    await session.rotate(rotated)
    data = hass_storage["yale_access.session"]["data"]
    assert data["access_token"] == rotated
    assert set(data) == {"access_token", "saved_at", "exp"}
    assert rotated not in repr(session)


async def test_states_and_only_changed_callbacks():
    session = make_session(token(60))
    session.state_changed = AsyncMock()
    assert await session.load()
    assert session.state == "ok"
    assert session.expires.endswith("Z")
    assert session.state_changed.await_count == 1
    assert await session.load()
    assert session.state_changed.await_count == 1
    await session.rotate(token(7))
    assert session.state == "expiring"
    await session.mark_failed()
    assert session.state == "failed"
    await session.mark_failed()
    assert session.state_changed.await_count == 3
    assert not await session.load()
    assert session._store.async_load.await_count == 2
    await session.rotate(token(120))
    assert session.state == "ok"


@pytest.mark.parametrize("data", [None, [], {}, {"access_token": "invalid"}])
async def test_unreadable_or_malformed_session_is_failed(data):
    session = make_session()
    session._store.async_load.return_value = data
    assert not await session.load()
    assert session.state == "failed"
    assert session.expires is None


async def test_failed_persistence_is_sanitized():
    session = make_session(token(60))
    await session.load()
    replacement = token(120)
    session._store.async_save.side_effect = OSError(replacement)
    with pytest.raises(UpstreamUnavailable) as error:
        await session.rotate(replacement)
    assert replacement not in str(error.value)
    assert session.state == "failed"


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
async def test_malformed_expiry_is_sanitized(payload):
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    malformed = f"placeholder.{encoded}.placeholder"
    session = make_session(malformed)
    assert not await session.load()
    assert session.state == "failed"
    with pytest.raises(UpstreamUnavailable) as error:
        await session.rotate(malformed)
    assert malformed not in str(error.value)


async def test_remove_deletes_store(hass):
    session = SessionStore(hass)
    await session.rotate(token(60))
    await session.remove()
    assert await SessionStore(hass)._store.async_load() is None


@pytest.mark.parametrize("existing", [False, True])
async def test_store_swallowed_write_failure_does_not_accept_token(
    hass, hass_storage, monkeypatch, caplog, existing
):
    from homeassistant.util.file import WriteError

    session = SessionStore(hass)
    original = token(60) if existing else None
    if original is not None:
        await session.rotate(original)
    session.state_changed = AsyncMock()
    replacement = token(120)
    monkeypatch.setattr(
        session._store,
        "_async_write_data",
        AsyncMock(side_effect=WriteError("Storage unavailable")),
    )
    with pytest.raises(UpstreamUnavailable) as error:
        await session.rotate(replacement)
    assert str(error.value) == "Yale session could not be saved"
    assert session.state == "failed"
    assert session.token == original
    session.state_changed.assert_awaited_once()
    assert replacement not in caplog.text
    assert replacement not in str(error.value)
    if existing:
        assert hass_storage["yale_access.session"]["data"]["access_token"] == original
    else:
        assert "yale_access.session" not in hass_storage
