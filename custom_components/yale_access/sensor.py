"""Yale app login health."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN, SIGNAL_LOGIN
from .session import SessionFile


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    if discovery_info is not None:
        async_add_entities([YaleLoginSensor(hass.data[DOMAIN].client.session_file)])


class YaleLoginSensor(SensorEntity):
    _attr_name = "Yale Access Login"
    _attr_unique_id = "yale_access_login"
    _attr_should_poll = False

    def __init__(self, session: SessionFile) -> None:
        self._session = session

    @property
    def native_value(self) -> str:
        return self._session.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"expires": self._session.expires}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_LOGIN, self.async_write_ha_state)
        )
