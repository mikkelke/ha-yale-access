"""Home Assistant schemas, websocket identity and login entity."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
from homeassistant.components.websocket_api.http import WebSocketHandler
from homeassistant.helpers.entity import EntityPlatformState

from custom_components.yale_access import CONFIG_SCHEMA, api
from custom_components.yale_access.const import DOMAIN
from custom_components.yale_access.health import LoginMonitor
from custom_components.yale_access.logging_filter import install_filter
from custom_components.yale_access.sensor import YaleLoginSensor
from custom_components.yale_access.service import MappedUser, UserMap

USER = "a" * 32
UNKNOWN = "b" * 32
CONFIG = {
    DOMAIN: {
        "lock_id": "LOCK_PLACEHOLDER",
        "notify": "notify.mobile_app_admin_device",
        "users": [{"ha_user_id": USER, "role": "admin"}],
    }
}
MESSAGES = {
    "yale_access/me": {},
    "yale_access/users": {},
    "yale_access/create": {"name": "Guest placeholder"},
    "yale_access/set_validity": {"user_id": "USER_PLACEHOLDER"},
    "yale_access/end": {"user_id": "USER_PLACEHOLDER"},
    "yale_access/pin": {"user_id": "USER_PLACEHOLDER"},
    "yale_access/set_pin": {"user_id": "USER_PLACEHOLDER", "pin": "654321"},
}


class FakeHass:
    def __init__(self):
        self.data = {}
        self.tasks = []

    def async_create_background_task(self, coro, name, eager_start=False):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task


class FakeConnection:
    def __init__(self, user_id):
        self.user = SimpleNamespace(id=user_id)
        self.results = []
        self.errors = []

    def send_result(self, msg_id, result=None):
        self.results.append(result)

    def send_error(self, msg_id, code, message):
        self.errors.append((code, message))


@pytest.fixture
def websocket():
    hass = FakeHass()
    service = SimpleNamespace(
        users=UserMap([MappedUser(USER, "admin")]),
        pin_length=6,
        me=AsyncMock(return_value={"role": "admin", "pin_length": 6, "own": None}),
        list_users=AsyncMock(return_value={"users": []}),
        create=AsyncMock(return_value={}),
        set_validity=AsyncMock(return_value={}),
        end=AsyncMock(return_value={"ok": True}),
        pin=AsyncMock(return_value={"pin": "654321"}),
        set_pin=AsyncMock(return_value={"user": {}}),
    )
    hass.data[DOMAIN] = service
    api.async_register_commands(hass)
    return hass, service


def test_config_entry_only_schema(caplog):
    assert CONFIG_SCHEMA({}) == {}
    CONFIG_SCHEMA(CONFIG)
    assert "does not support YAML setup" in caplog.text


@pytest.mark.parametrize("command", MESSAGES)
async def test_unknown_identity_forbidden_on_every_command(websocket, command):
    hass, service = websocket
    handler, schema = hass.data["websocket_api"][command]
    message = {"id": 1, "type": command, **MESSAGES[command]}
    message = schema(message) if schema is not False else message
    connection = FakeConnection(UNKNOWN)
    handler(hass, connection, message)
    await asyncio.gather(*hass.tasks)
    assert connection.errors == [("forbidden", "Not allowed")]
    assert not connection.results
    for method in (
        service.me,
        service.list_users,
        service.create,
        service.set_validity,
        service.end,
        service.pin,
        service.set_pin,
    ):
        method.assert_not_called()


@pytest.mark.parametrize("command", MESSAGES)
def test_schemas_reject_caller_identity_and_unexpected_pin(websocket, command):
    hass, _ = websocket
    _, schema = hass.data["websocket_api"][command]
    keys = ["ha_user_id", "role", "created_by"]
    if command not in {"yale_access/create", "yale_access/set_pin"}:
        keys.append("pin")
    for key in keys:
        message = {"id": 1, "type": command, **MESSAGES[command], key: "placeholder"}
        if schema is False:
            assert len(message) > 2
        else:
            with pytest.raises(vol.Invalid):
                schema(message)


async def test_only_connection_identity_is_used(websocket):
    hass, service = websocket
    handler, schema = hass.data["websocket_api"]["yale_access/create"]
    message = schema(
        {"id": 1, "type": "yale_access/create", "name": "Guest placeholder"}
    )
    handler(hass, FakeConnection(USER), message)
    await asyncio.gather(*hass.tasks)
    service.create.assert_awaited_once_with(
        MappedUser(USER, "admin"), "Guest placeholder", None, None, pin=None
    )


@pytest.mark.parametrize(
    "command", ["yale_access/create", "yale_access/set_pin", "yale_access/pin"]
)
async def test_pin_commands_dispatch_with_connection_identity(websocket, command):
    hass, service = websocket
    handler, schema = hass.data["websocket_api"][command]
    payload = {**MESSAGES[command]}
    if command == "yale_access/create":
        payload["pin"] = "654321"
    message = schema({"id": 1, "type": command, **payload})
    connection = FakeConnection(USER)
    handler(hass, connection, message)
    await asyncio.gather(*hass.tasks)
    assert connection.errors == []
    user = MappedUser(USER, "admin")
    if command == "yale_access/create":
        service.create.assert_awaited_once_with(
            user, payload["name"], None, None, pin="654321"
        )
    elif command == "yale_access/set_pin":
        service.set_pin.assert_awaited_once_with(user, payload["user_id"], "654321")
    else:
        service.pin.assert_awaited_once_with(user, payload["user_id"])


@pytest.mark.parametrize("digits", [None, 4, 6, 8])
async def test_me_returns_cached_pin_length_and_refreshes_if_not_ready(digits):
    from tests.test_service import ADMIN, UID_A, Client, make_service, record

    client = Client()
    service, _, _ = await make_service(client)
    if digits is not None:
        client.body["loaded"] = [record(UID_A, 1, pin="1" * digits)]
        await service.refresh()
    client.calls.clear()
    hass = FakeHass()
    hass.data[DOMAIN] = service
    api.async_register_commands(hass)
    handler, schema = hass.data["websocket_api"]["yale_access/me"]
    message = {"id": 1, "type": "yale_access/me"}
    if schema is not False:
        message = schema(message)
    connection = FakeConnection(ADMIN.ha_user_id)
    handler(hass, connection, message)
    await asyncio.gather(*hass.tasks)
    assert connection.results == [
        {"role": "admin", "pin_length": digits or 6, "own": None}
    ]
    assert connection.errors == []
    assert client.calls == ([] if digits is not None else [("detail",), ("pins",)])


@pytest.mark.parametrize("role", ["admin", "guests"])
async def test_me_websocket_returns_protected_own_row(role):
    from tests.test_service import UID_A, Client, make_service, record

    user = MappedUser(USER, role, own=UID_A)
    client = Client()
    client.body["loaded"] = [record(UID_A, 1, pin="654321")]
    service, _, _ = await make_service(client, users=[user], protected_users=[UID_A])
    hass = FakeHass()
    hass.data[DOMAIN] = service
    api.async_register_commands(hass)
    handler, schema = hass.data["websocket_api"]["yale_access/me"]
    message = {"id": 1, "type": "yale_access/me"}
    if schema is not False:
        message = schema(message)
    connection = FakeConnection(USER)
    handler(hass, connection, message)
    await asyncio.gather(*hass.tasks)
    assert connection.errors == []
    assert connection.results == [await service.me(user)]
    assert connection.results[0]["own"]["user_id"] == UID_A
    assert "654321" not in json.dumps(connection.results)


@pytest.mark.parametrize("role", ["admin", "owner", "assigned", "other"])
@pytest.mark.parametrize("protected", [False, True])
async def test_pin_websocket_authorization(role, protected, caplog):
    from tests.test_service import (
        ADMIN,
        GUEST_A,
        GUEST_B,
        UID_A,
        Client,
        make_service,
        ownership,
        record,
    )

    assigned = MappedUser("d" * 32, "guests", (UID_A,))
    user = {"admin": ADMIN, "owner": GUEST_A, "assigned": assigned, "other": GUEST_B}[
        role
    ]
    client = Client()
    client.body["loaded"] = [record(UID_A, 1, pin="654321")]
    service, _, store = await make_service(
        client,
        ownership(UID_A, GUEST_A),
        users=[ADMIN, GUEST_A, GUEST_B, assigned],
        protected_users=[UID_A] if protected else [],
    )
    hass = FakeHass()
    hass.data[DOMAIN] = service
    api.async_register_commands(hass)
    handler, schema = hass.data["websocket_api"]["yale_access/pin"]
    message = schema({"id": 1, "type": "yale_access/pin", "user_id": UID_A})
    connection = FakeConnection(user.ha_user_id)
    with caplog.at_level("DEBUG"):
        handler(hass, connection, message)
        await asyncio.gather(*hass.tasks)
    if protected or role == "other":
        expected = "protected" if protected and role == "admin" else "not_found"
        assert [error[0] for error in connection.errors] == [expected]
        assert connection.results == []
    else:
        assert connection.results == [{"pin": "654321"}]
        assert connection.errors == []
    assert "654321" not in caplog.text
    assert "654321" not in json.dumps(store.saves)


async def test_health_single_notify_and_reauth_and_recovery():
    session = SimpleNamespace(state="ok")
    notify = AsyncMock()
    reauth = Mock()
    changed = Mock()
    monitor = LoginMonitor(lambda: session.state, notify, reauth, changed)
    await monitor.update()
    assert not notify.called
    session.state = "expiring"
    await monitor.update()
    assert not reauth.called
    assert not notify.called
    session.state = "failed"
    await monitor.update()
    await monitor.update()
    reauth.assert_called_once()
    notify.assert_awaited_once_with(
        "Yale Access needs a new login. Open Settings → Devices & services to re-authenticate."
    )
    session.state = "ok"
    await monitor.update()
    session.state = "failed"
    await monitor.update()
    assert reauth.call_count == 2
    assert notify.await_count == 2


async def test_sensor_and_real_websocket_writer_never_publish_pin(hass, caplog):
    from tests.test_service import GUEST_A, make_service

    service, _, store = await make_service()
    result = await service.create(
        GUEST_A,
        "Guest Placeholder",
        "2026-10-30T12:00:00+01:00",
        "2026-10-31T12:00:00+01:00",
    )
    pin = result["pin"]
    assert pin not in json.dumps(store.saves)
    assert pin not in json.dumps(await service.list_users(GUEST_A))
    session = SimpleNamespace(state="ok", expires="2027-01-01T00:00:00Z")
    sensor = YaleLoginSensor(session)
    sensor.hass = hass
    sensor.entity_id = "sensor.yale_access_login"
    sensor._platform_state = EntityPlatformState.ADDED
    sensor._no_platform_reported = True
    events = []
    hass.bus.async_listen("state_changed", lambda event: events.append(event.as_dict()))
    sensor.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "ok"
    assert sensor.extra_state_attributes == {"expires": session.expires}
    session.state = "failed"
    sensor.async_write_ha_state()
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "failed"

    install_filter()
    logger = logging.getLogger("homeassistant.components.websocket_api.http.connection")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    frame = json.dumps(
        {"id": 1, "type": "result", "success": True, "result": result}
    ).encode()
    for coalesce in (False, True):
        queue = deque([frame, frame] if coalesce else [frame])
        sock = SimpleNamespace(closed=False)
        writer = SimpleNamespace(
            _message_queue=queue,
            _logger=logger,
            _wsock=sock,
            _loop=asyncio.get_running_loop(),
            _debug=True,
            _closing=False,
            description="placeholder connection",
            _cancel_peak_checker=Mock(),
        )
        sent = []

        async def send(message, sent=sent, sock=sock):
            sent.append(message)
            sock.closed = True

        await WebSocketHandler._writer(
            writer, SimpleNamespace(can_coalesce=coalesce), send
        )
        assert pin.encode() in sent[0]
    assert "Sensitive websocket message omitted" in caplog.text
    assert pin not in caplog.text
    assert pin not in json.dumps([state.as_dict() for state in hass.states.async_all()])
    assert pin not in json.dumps(events, default=lambda value: value.as_dict())


@pytest.mark.parametrize("command", MESSAGES)
async def test_websocket_without_entry_returns_not_ready(command):
    hass = FakeHass()
    api.async_register_commands(hass)
    handler, schema = hass.data["websocket_api"][command]
    message = {"id": 1, "type": command, **MESSAGES[command]}
    if schema is not False:
        message = schema(message)
    connection = FakeConnection(USER)
    handler(hass, connection, message)
    await asyncio.gather(*hass.tasks)
    assert connection.errors == [("not_ready", "Yale Access is not loaded")]


async def test_setup_registers_commands_once(hass, monkeypatch):
    import custom_components.yale_access as integration

    register = Mock(wraps=api.async_register_commands)
    monkeypatch.setattr(api, "async_register_commands", register)
    assert await integration.async_setup(hass, {})
    assert await integration.async_setup(hass, {})
    register.assert_called_once_with(hass)
    assert set(hass.data["websocket_api"]) == set(MESSAGES)


@pytest.mark.parametrize("country", ["US", "DK"])
async def test_entry_setup_unload_and_missing_session_reauth(
    hass, monkeypatch, country
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.yale_access as integration

    notifications = []

    async def notify(call):
        notifications.append(call.data)

    hass.services.async_register("notify", "mobile_app_admin_device", notify)
    captured = {}

    def make_service(client, users, store, display_name, *, protected_users):
        service = SimpleNamespace(client=client, load=AsyncMock(), refresh=AsyncMock())
        service.refresh.side_effect = client.detail
        captured.update(service=service, users=users, protected_users=protected_users)
        return service

    monkeypatch.setattr(integration, "YaleAccessService", make_service)
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload)
    reauth = Mock()
    monkeypatch.setattr(MockConfigEntry, "async_start_reauth", reauth)
    cancel = Mock()
    interval = Mock(return_value=cancel)
    monkeypatch.setattr(integration, "async_track_time_interval", interval)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Front door",
        data={
            "lock_id": "LOCK_PLACEHOLDER",
            "country": country,
            "install_id": "placeholder",
        },
        options={
            "users": [
                {
                    "ha_user_id": USER,
                    "role": "admin",
                    "yale_users": ["Guest_placeholder"],
                    "own_yale_user": "Resident_placeholder",
                }
            ],
            "protected_users": ["Admin_placeholder"],
            "notify": "notify.mobile_app_admin_device",
        },
    )
    entry.add_to_hass(hass)
    assert await integration.async_setup(hass, {})
    assert await integration.async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    service = captured["service"]
    service.load.assert_awaited_once()
    service.refresh.assert_awaited_once()
    assert hass.data[DOMAIN] is service
    assert captured["protected_users"] == ["Admin_placeholder"]
    assert captured["users"].require(USER).yale_users == ("Guest_placeholder",)
    assert captured["users"].require(USER).own == "Resident_placeholder"
    assert service.client.session_store.state == "failed"
    assert service.client._country == country
    assert service.client._session._retry_connection is False
    forward.assert_awaited_once()
    reauth.assert_called_once_with(hass)
    assert len(notifications) == 1
    interval.call_args.args[1]()
    await hass.async_block_till_done()
    assert len(notifications) == 1
    reauth.assert_called_once()
    assert await integration.async_unload_entry(hass, entry)
    await entry._async_process_on_unload(hass)
    cancel.assert_called_once()
    assert DOMAIN not in hass.data
    interval.call_args.args[1]()
    await hass.async_block_till_done()
    assert service.refresh.await_count == 2


async def test_entry_unload_cancels_refresh_in_progress(hass, monkeypatch):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.yale_access as integration

    began = asyncio.Event()
    cancelled = asyncio.Event()

    async def refresh():
        began.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service = SimpleNamespace(
        client=SimpleNamespace(session_store=SimpleNamespace(state_changed=None)),
        load=AsyncMock(),
        refresh=refresh,
    )
    monkeypatch.setattr(integration, "YaleAccessService", Mock(return_value=service))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(MockConfigEntry, "async_start_reauth", Mock())
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "lock_id": "LOCK_PLACEHOLDER",
            "country": "US",
            "install_id": "placeholder",
        },
    )
    entry.add_to_hass(hass)
    assert await integration.async_setup_entry(hass, entry)
    await began.wait()
    assert await integration.async_unload_entry(hass, entry)
    assert cancelled.is_set()
    await entry._async_process_on_unload(hass)


async def test_entry_options_update_reloads_and_removal_clears_session(
    hass, monkeypatch
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.yale_access as integration
    from custom_components.yale_access.session import SessionStore
    from tests.test_session import token

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    await integration.async_options_updated(hass, entry)
    reload.assert_awaited_once_with(entry.entry_id)
    await SessionStore(hass).rotate(token(60))
    await integration.async_remove_entry(hass, entry)
    assert await SessionStore(hass)._store.async_load() is None


async def test_real_entry_sensor_health_and_unload(hass, monkeypatch):
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.yale_access as integration
    from custom_components.yale_access.session import SessionStore
    from tests.test_session import token

    await SessionStore(hass).rotate(token(60))
    captured = {}

    def make_service(client, users, store, display_name, *, protected_users):
        service = SimpleNamespace(client=client, load=AsyncMock(), refresh=AsyncMock())
        captured["service"] = service
        return service

    monkeypatch.setattr(integration, "YaleAccessService", make_service)
    reauth = Mock()
    monkeypatch.setattr(MockConfigEntry, "async_start_reauth", reauth)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Front door",
        unique_id="LOCK_PLACEHOLDER",
        data={
            "lock_id": "LOCK_PLACEHOLDER",
            "country": "US",
            "install_id": "placeholder",
        },
        options={"users": [], "protected_users": [], "notify": None},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = "sensor.yale_access_login"
    assert hass.states.get(entity_id).state == "ok"
    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry.unique_id == "yale_access_login"
    assert registry_entry.config_entry_id == entry.entry_id
    service = captured["service"]
    assert hass.data[DOMAIN] is service
    reauth.assert_not_called()
    await service.client.session_store.mark_failed()
    await service.client.session_store.mark_failed()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "failed"
    reauth.assert_called_once_with(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unavailable"
    assert DOMAIN not in hass.data
