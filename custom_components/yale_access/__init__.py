"""Manage Yale lock users through authenticated Home Assistant websockets."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from . import api
from .client import YaleClient
from .const import DOMAIN, SIGNAL_LOGIN, STORAGE_KEY, STORAGE_VERSION
from .errors import YaleAccessError
from .health import LoginMonitor
from .logging_filter import install_filter
from .service import MappedUser, UserMap, YaleAccessService
from .session import SessionStore

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class YaleRuntime:
    service: YaleAccessService
    tasks: set[asyncio.Task] = field(default_factory=set)
    stopped: bool = False


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    install_filter()
    if "yale_access/me" not in hass.data.get("websocket_api", {}):
        api.async_register_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    users = UserMap(
        MappedUser(
            user["ha_user_id"],
            user["role"],
            tuple(user.get("yale_users", [])),
            user.get("own_yale_user"),
        )
        for user in entry.options.get("users", [])
    )
    session_store = SessionStore(hass)
    session = async_create_clientsession(hass, trust_env=False)
    session._retry_connection = False
    entry.async_on_unload(session.detach)
    client = YaleClient(
        session, session_store, entry.data["lock_id"], country=entry.data["country"]
    )

    async def display_name(user_id: str) -> str | None:
        user = await hass.auth.async_get_user(user_id)
        return user.name if user is not None else None

    async def notify(message: str) -> None:
        if not (target := entry.options.get("notify")):
            return
        domain, name = target.split(".", 1)
        try:
            await hass.services.async_call(
                domain, name, {"title": "Yale login", "message": message}, blocking=True
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("login notification failed")

    monitor = LoginMonitor(
        lambda: session_store.state,
        notify,
        lambda: entry.async_start_reauth(hass),
        lambda: async_dispatcher_send(hass, SIGNAL_LOGIN),
    )
    session_store.state_changed = monitor.update
    service = YaleAccessService(
        client,
        users,
        Store(hass, STORAGE_VERSION, STORAGE_KEY),
        display_name=display_name,
        protected_users=entry.options.get("protected_users", []),
    )
    await service.load()
    await session_store.load()
    runtime = YaleRuntime(service)
    entry.runtime_data = runtime
    hass.data[DOMAIN] = service

    @callback
    def clear_runtime() -> None:
        runtime.stopped = True
        session_store.state_changed = None
        if hass.data.get(DOMAIN) is service:
            hass.data.pop(DOMAIN)

    entry.async_on_unload(clear_runtime)

    async def refresh() -> None:
        try:
            await service.refresh()
        except YaleAccessError as err:
            _LOGGER.debug("refresh: %s", err.code)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("refresh failed")

    @callback
    def schedule_refresh(_now: Any = None) -> None:
        if runtime.stopped:
            return
        task = entry.async_create_task(hass, refresh(), "yale_access refresh")
        if not task.done():
            runtime.tasks.add(task)
            task.add_done_callback(runtime.tasks.discard)

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    entry.async_on_unload(
        async_track_time_interval(hass, schedule_refresh, timedelta(minutes=5))
    )
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    schedule_refresh()
    return True


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, _PLATFORMS):
        return False
    runtime: YaleRuntime = entry.runtime_data
    runtime.stopped = True
    for task in runtime.tasks:
        task.cancel()
    if runtime.tasks:
        await asyncio.gather(*runtime.tasks, return_exceptions=True)
    runtime.service.client.session_store.state_changed = None
    if hass.data.get(DOMAIN) is runtime.service:
        hass.data.pop(DOMAIN)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await SessionStore(hass).remove()
