"""Home Assistant schemas, websocket identity and login entity."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
from homeassistant.components.websocket_api.http import WebSocketHandler
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityPlatformState

from custom_components.yale_access import CONFIG_SCHEMA, api
from custom_components.yale_access.const import DOMAIN, RELOGIN_COMMAND
from custom_components.yale_access.logging_filter import install_filter
from custom_components.yale_access.login import LoginMonitor
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


def test_config_defaults_and_validation():
    result = CONFIG_SCHEMA(copy.deepcopy(CONFIG))[DOMAIN]
    assert result["session_file"] == "/config/.yale_app_session.json"
    assert result["country"] == "US"
    assert result["protected_users"] == []
    assert result["users"][0]["yale_users"] == []
    assert CONFIG_SCHEMA({}) == {}
    for changes in (
        {"users": []},
        {"users": [{"ha_user_id": "<placeholder>", "role": "admin"}]},
        {"users": [{"ha_user_id": USER, "role": "owner"}]},
        {"users": CONFIG[DOMAIN]["users"] * 2},
        {"notify": "light.turn_on"},
        {"lock_id": "../lock"},
        {"unexpected": True},
        {"session_file": ""},
    ):
        config = copy.deepcopy(CONFIG)
        config[DOMAIN].update(changes)
        with pytest.raises(vol.Invalid):
            CONFIG_SCHEMA(config)


@pytest.mark.parametrize("country", ["US", "DK", "GB"])
def test_config_accepts_country(country):
    config = copy.deepcopy(CONFIG)
    config[DOMAIN]["country"] = country
    assert CONFIG_SCHEMA(config)[DOMAIN]["country"] == country


@pytest.mark.parametrize(
    "country", ["dk", "Dk", "USA", "U", " US", "US\n", "ÜS", "12", "", None, 12]
)
def test_config_rejects_invalid_country(country):
    config = copy.deepcopy(CONFIG)
    config[DOMAIN]["country"] = country
    with pytest.raises(vol.Invalid, match="two uppercase letters"):
        CONFIG_SCHEMA(config)


@pytest.mark.parametrize("ids", [[], ["Admin_placeholder-1"], ["a" * 8, "Z" * 128]])
@pytest.mark.parametrize("key", ["protected_users", "yale_users"])
def test_config_accepts_yale_user_ids(ids, key):
    config = copy.deepcopy(CONFIG)
    target = config[DOMAIN] if key == "protected_users" else config[DOMAIN]["users"][0]
    target[key] = ids
    result = CONFIG_SCHEMA(config)[DOMAIN]
    result = result if key == "protected_users" else result["users"][0]
    assert result[key] == ids


@pytest.mark.parametrize(
    "ids",
    [
        "Admin_placeholder",
        None,
        [12345678],
        ["short"],
        ["a" * 129],
        ["../user-id"],
        ["user id!!"],
        ["valid_user\n"],
    ],
)
@pytest.mark.parametrize("key", ["protected_users", "yale_users"])
def test_config_rejects_invalid_yale_user_ids(ids, key):
    config = copy.deepcopy(CONFIG)
    target = config[DOMAIN] if key == "protected_users" else config[DOMAIN]["users"][0]
    target[key] = ids
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(config)


def test_config_rejects_yale_user_assigned_to_multiple_ha_users():
    config = copy.deepcopy(CONFIG)
    config[DOMAIN]["users"][0]["yale_users"] = ["Guest_placeholder"]
    config[DOMAIN]["users"].append(
        {
            "ha_user_id": UNKNOWN,
            "role": "guests",
            "yale_users": ["Guest_placeholder"],
        }
    )
    with pytest.raises(vol.Invalid, match="only be assigned to one HA user"):
        CONFIG_SCHEMA(config)


@pytest.mark.parametrize("uid", ["Resident_placeholder-1", "a" * 8, "Z" * 128])
def test_config_accepts_own_yale_user(uid):
    config = copy.deepcopy(CONFIG)
    config[DOMAIN]["users"][0]["own_yale_user"] = uid
    assert CONFIG_SCHEMA(config)[DOMAIN]["users"][0]["own_yale_user"] == uid


@pytest.mark.parametrize(
    "uid", [None, 12345678, "short", "a" * 129, "../user-id", "user id!!"]
)
def test_config_rejects_invalid_own_yale_user(uid):
    config = copy.deepcopy(CONFIG)
    config[DOMAIN]["users"][0]["own_yale_user"] = uid
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(config)


@pytest.mark.parametrize(
    "assignments",
    [
        [{"own_yale_user": "Resident_placeholder"}] * 2,
        [
            {
                "own_yale_user": "Resident_placeholder",
                "yale_users": ["Resident_placeholder"],
            }
        ],
        [
            {"own_yale_user": "Resident_placeholder"},
            {"yale_users": ["Resident_placeholder"]},
        ],
        [
            {"yale_users": ["Resident_placeholder"]},
            {"own_yale_user": "Resident_placeholder"},
        ],
        [{"yale_users": ["Resident_placeholder", "Resident_placeholder"]}],
    ],
)
def test_config_rejects_duplicate_own_and_assigned_yale_users(assignments):
    config = copy.deepcopy(CONFIG)
    config[DOMAIN]["users"] = [
        {"ha_user_id": uid, "role": "guests", **assignment}
        for uid, assignment in zip((USER, UNKNOWN), assignments, strict=False)
    ]
    with pytest.raises(vol.Invalid, match="only be assigned to one HA user"):
        CONFIG_SCHEMA(config)


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


async def test_health_single_notify_and_recovery():
    session = SimpleNamespace(state="ok")
    notify = AsyncMock()
    repair = Mock()
    changed = Mock()
    monitor = LoginMonitor(lambda: session.state, notify, repair, changed)
    await monitor.update()
    assert not notify.called
    session.state = "expiring"
    await monitor.update()
    session.state = "failed"
    await monitor.update()
    assert notify.await_count == 1
    assert RELOGIN_COMMAND in notify.call_args.args[0]
    assert "yale_login.py" in RELOGIN_COMMAND
    assert "README" in RELOGIN_COMMAND
    repair.assert_called_with(True)
    session.state = "ok"
    await monitor.update()
    repair.assert_called_with(False)
    session.state = "failed"
    await monitor.update()
    assert notify.await_count == 2


async def test_sensor_and_real_websocket_writer_never_publish_pin(tmp_path, caplog):
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
    hass = HomeAssistant(str(tmp_path))
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


@pytest.mark.parametrize("country", ["US", "DK"])
async def test_setup_missing_session_still_exposes_health_and_cleans_up(
    tmp_path, monkeypatch, country
):
    import aiohttp
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    import custom_components.yale_access as integration

    hass = HomeAssistant(str(tmp_path))
    notifications = []

    async def notify(call):
        notifications.append(call.data)

    hass.services.async_register("notify", "mobile_app_admin_device", notify)
    service = SimpleNamespace(load=AsyncMock(), refresh=AsyncMock())
    captured = {}

    def make_service(client, users, store, display_name, *, protected_users):
        captured["client"] = client
        captured["users"] = users
        captured["protected_users"] = protected_users
        service.refresh.side_effect = client.detail
        return service

    monkeypatch.setattr(integration, "YaleAccessService", make_service)
    monkeypatch.setattr(
        integration,
        "async_create_clientsession",
        lambda hass, **kwargs: aiohttp.ClientSession(**kwargs),
    )
    discovery = AsyncMock()
    monkeypatch.setattr(integration.discovery, "async_load_platform", discovery)
    repair = Mock()
    monkeypatch.setattr(integration.ir, "async_create_issue", repair)
    monkeypatch.setattr(integration.ir, "async_delete_issue", Mock())
    cancel = Mock()
    interval = Mock(return_value=cancel)
    monkeypatch.setattr(integration, "async_track_time_interval", interval)
    config = copy.deepcopy(CONFIG)
    config[DOMAIN]["session_file"] = str(tmp_path / "missing-session.json")
    config[DOMAIN]["country"] = country
    config[DOMAIN]["protected_users"] = ["Admin_placeholder"]
    config[DOMAIN]["users"][0]["yale_users"] = ["Guest_placeholder"]
    config[DOMAIN]["users"][0]["own_yale_user"] = "Resident_placeholder"
    assert await integration.async_setup(hass, CONFIG_SCHEMA(config))
    await hass.async_block_till_done()
    service.load.assert_awaited_once()
    service.refresh.assert_awaited_once()
    assert captured["protected_users"] == ["Admin_placeholder"]
    assert captured["users"].require(USER).yale_users == ("Guest_placeholder",)
    assert captured["users"].owner("Guest_placeholder") == USER
    assert captured["users"].require(USER).own == "Resident_placeholder"
    assert captured["users"].owner("Resident_placeholder") is None
    assert captured["client"].session_file.state == "failed"
    assert captured["client"]._country == country
    assert len(notifications) == 1
    assert RELOGIN_COMMAND in notifications[0]["message"]
    repair.assert_called_once()
    discovery.assert_awaited_once()
    assert set(hass.data["websocket_api"]) == set(MESSAGES)
    assert captured["client"]._session._retry_connection is False
    await interval.call_args.args[1]()
    assert len(notifications) == 1
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    cancel.assert_called_once()
    await captured["client"]._session.close()
