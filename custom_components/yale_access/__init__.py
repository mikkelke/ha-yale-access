"""Manage Yale lock users through authenticated Home Assistant websockets."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from . import api, mapping
from .client import YaleClient
from .const import (
    DEFAULT_COUNTRY,
    DEFAULT_SESSION_FILE,
    DOMAIN,
    RELOGIN_COMMAND,
    SIGNAL_LOGIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .errors import YaleAccessError
from .logging_filter import install_filter
from .login import LoginMonitor
from .service import MappedUser, UserMap, YaleAccessService
from .session import SessionFile

_LOGGER = logging.getLogger(__name__)


def _ha_user_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise vol.Invalid(
            "expected a Home Assistant user id (32 hexadecimal characters)"
        )
    return value.lower()


def _lock_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
        raise vol.Invalid("expected a Yale lock id")
    return value


def _yale_user_id(value: Any) -> str:
    try:
        return mapping.identifier(value)
    except YaleAccessError:
        raise vol.Invalid("expected a Yale user id") from None


def _notify_service(value: Any) -> str:
    service = cv.service(value)
    if not service.startswith("notify."):
        raise vol.Invalid("expected a notify service")
    return service


def _country(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{2}", value):
        raise vol.Invalid("expected a country code (two uppercase letters)")
    return value


def _unique_users(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = [user["ha_user_id"] for user in users]
    if len(values) != len(set(values)):
        raise vol.Invalid("ha_user_id may only be mapped once")
    yale_ids = [uid for user in users for uid in user["yale_users"]]
    yale_ids.extend(user["own_yale_user"] for user in users if "own_yale_user" in user)
    if len(yale_ids) != len(set(yale_ids)):
        raise vol.Invalid("a Yale user id may only be assigned to one HA user")
    return users


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional("session_file", default=DEFAULT_SESSION_FILE): vol.All(
                    cv.string, vol.Length(min=1)
                ),
                vol.Required("lock_id"): _lock_id,
                vol.Optional("country", default=DEFAULT_COUNTRY): _country,
                vol.Required("notify"): _notify_service,
                vol.Optional("protected_users", default=[]): [_yale_user_id],
                vol.Required("users"): vol.All(
                    cv.ensure_list,
                    vol.Length(min=1),
                    [
                        vol.Schema(
                            {
                                vol.Required("ha_user_id"): _ha_user_id,
                                vol.Required("role"): vol.In(("admin", "guests")),
                                vol.Optional("yale_users", default=[]): [_yale_user_id],
                                vol.Optional("own_yale_user"): _yale_user_id,
                            }
                        )
                    ],
                    _unique_users,
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    if (conf := config.get(DOMAIN)) is None:
        return True

    install_filter()
    users = UserMap(
        MappedUser(
            user["ha_user_id"],
            user["role"],
            tuple(user["yale_users"]),
            user.get("own_yale_user"),
        )
        for user in conf["users"]
    )
    session_file = SessionFile(
        conf["session_file"], executor=hass.async_add_executor_job
    )
    session = async_create_clientsession(hass, trust_env=False)
    session._retry_connection = False
    client = YaleClient(session, session_file, conf["lock_id"], country=conf["country"])

    async def display_name(user_id: str) -> str | None:
        user = await hass.auth.async_get_user(user_id)
        return user.name if user is not None else None

    async def notify(message: str) -> None:
        domain, name = conf["notify"].split(".", 1)
        try:
            await hass.services.async_call(
                domain, name, {"title": "Yale login", "message": message}, blocking=True
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("login notification failed")

    def repair(unhealthy: bool) -> None:
        if unhealthy:
            ir.async_create_issue(
                hass,
                DOMAIN,
                "login",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="login",
                translation_placeholders={"command": RELOGIN_COMMAND},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, "login")

    monitor = LoginMonitor(
        lambda: session_file.state,
        notify,
        repair,
        lambda: async_dispatcher_send(hass, SIGNAL_LOGIN),
    )
    session_file.state_changed = monitor.update
    service = YaleAccessService(
        client,
        users,
        Store(hass, STORAGE_VERSION, STORAGE_KEY),
        display_name=display_name,
        protected_users=conf["protected_users"],
    )
    await service.load()
    hass.data[DOMAIN] = service
    api.async_register_commands(hass)

    async def refresh(_now: Any = None) -> None:
        try:
            await service.refresh()
        except YaleAccessError as err:
            _LOGGER.debug("refresh: %s", err.code)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("refresh failed")

    hass.async_create_task(refresh(), "yale_access first refresh")
    await discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    cancel_refresh = async_track_time_interval(hass, refresh, timedelta(minutes=5))

    hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, lambda _event: cancel_refresh()
    )
    return True
