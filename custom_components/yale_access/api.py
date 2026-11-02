"""Websocket commands whose identity comes only from connection.user.id."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .errors import YaleAccessError
from .service import MappedUser, YaleAccessService

_LOGGER = logging.getLogger(__name__)
_VALIDITY = {vol.Optional("from"): str, vol.Optional("to"): str}
_TARGET = {vol.Required("user_id"): vol.All(str, vol.Length(min=8, max=128))}


async def _run(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    call: Callable[[YaleAccessService, MappedUser], Awaitable[dict[str, Any]]],
) -> None:
    service: YaleAccessService = hass.data[DOMAIN]
    try:
        user = service.users.require(getattr(connection.user, "id", None))
        result = await call(service, user)
    except YaleAccessError as err:
        _LOGGER.debug("%s refused: %s", msg["type"], err.code)
        connection.send_error(msg["id"], err.code, err.message)
        return
    except Exception:  # noqa: BLE001
        _LOGGER.warning("websocket command failed")
        connection.send_error(
            msg["id"], "request_failed", "Yale could not handle the request"
        )
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "yale_access/me"})
@websocket_api.async_response
async def ws_me(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _run(hass, connection, msg, lambda service, user: service.me(user))


@websocket_api.websocket_command({vol.Required("type"): "yale_access/users"})
@websocket_api.async_response
async def ws_users(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _run(hass, connection, msg, lambda service, user: service.list_users(user))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "yale_access/create",
        vol.Required("name"): vol.All(str, vol.Length(min=1, max=100)),
        vol.Optional("pin"): str,
        **_VALIDITY,
    }
)
@websocket_api.async_response
async def ws_create(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _run(
        hass,
        connection,
        msg,
        lambda service, user: service.create(
            user, msg["name"], msg.get("from"), msg.get("to"), pin=msg.get("pin")
        ),
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "yale_access/set_validity", **_TARGET, **_VALIDITY}
)
@websocket_api.async_response
async def ws_set_validity(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _run(
        hass,
        connection,
        msg,
        lambda service, user: service.set_validity(
            user, msg["user_id"], msg.get("from"), msg.get("to")
        ),
    )


@websocket_api.websocket_command({vol.Required("type"): "yale_access/pin", **_TARGET})
@websocket_api.async_response
async def ws_pin(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _run(
        hass, connection, msg, lambda service, user: service.pin(user, msg["user_id"])
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "yale_access/set_pin",
        **_TARGET,
        vol.Required("pin"): str,
    }
)
@websocket_api.async_response
async def ws_set_pin(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _run(
        hass,
        connection,
        msg,
        lambda service, user: service.set_pin(user, msg["user_id"], msg["pin"]),
    )


@websocket_api.websocket_command({vol.Required("type"): "yale_access/end", **_TARGET})
@websocket_api.async_response
async def ws_end(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    await _run(
        hass, connection, msg, lambda service, user: service.end(user, msg["user_id"])
    )


def async_register_commands(hass: HomeAssistant) -> None:
    for handler in (
        ws_me,
        ws_users,
        ws_create,
        ws_set_validity,
        ws_pin,
        ws_set_pin,
        ws_end,
    ):
        websocket_api.async_register_command(hass, handler)
