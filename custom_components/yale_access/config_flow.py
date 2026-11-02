"""Home Assistant login and access configuration."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DEFAULT_COUNTRY, DOMAIN
from .errors import YaleAccessError
from .login import LoginError, YaleLogin
from .session import SessionStore

DEFAULT_OPTIONS = {"users": [], "protected_users": [], "notify": None}


def _select(choices: Mapping[str, str], *, multiple: bool = False) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                {"value": value, "label": label} for value, label in choices.items()
            ],
            multiple=multiple,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _credentials(country: str | None = None) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if country is not None:
        fields[vol.Required("country", default=country)] = vol.All(
            str, vol.Match(r"[A-Z]{2}\Z")
        )
    fields.update(
        {
            vol.Required("method", default="email"): SelectSelector(
                SelectSelectorConfig(
                    options=["email", "phone"], translation_key="method"
                )
            ),
            vol.Required("identifier"): TextSelector(),
            vol.Required("password"): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )
    return vol.Schema(fields)


class YaleAccessConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._country = DEFAULT_COUNTRY
        self._install_id = str(uuid4())
        self._login: YaleLogin | None = None
        self._locks: dict[str, str] = {}
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> YaleAccessOptionsFlow:
        return YaleAccessOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_credentials("user", user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_entry = self._get_reauth_entry()
        self._country = self._reauth_entry.data["country"]
        self._install_id = self._reauth_entry.data["install_id"]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_credentials("reauth_confirm", user_input)

    async def _async_credentials(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._country = user_input.get("country", self._country)
            self._login = YaleLogin(
                async_get_clientsession(self.hass), self._country, self._install_id
            )
            try:
                if await self._login.signin(
                    user_input["method"],
                    user_input["identifier"],
                    user_input["password"],
                ):
                    return await self._async_logged_in()
                return await self.async_step_verify()
            except LoginError as err:
                errors["base"] = err.code
            except AbortFlow:
                raise
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
        default_country = self.hass.config.country or DEFAULT_COUNTRY
        return self.async_show_form(
            step_id=step_id,
            data_schema=_credentials(
                self._country if user_input is not None else default_country
            )
            if step_id == "user"
            else _credentials(),
            errors=errors,
        )

    async def async_step_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                assert self._login is not None
                if self._login.token is None:
                    await self._login.verify(user_input["code"])
                return await self._async_logged_in()
            except LoginError as err:
                errors["base"] = err.code
            except AbortFlow:
                raise
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
        return self.async_show_form(
            step_id="verify",
            data_schema=vol.Schema({vol.Required("code"): TextSelector()}),
            errors=errors,
        )

    async def _async_logged_in(self) -> ConfigFlowResult:
        assert self._login is not None
        self._locks = await self._login.locks()
        if self._reauth_entry is not None:
            if self._reauth_entry.data["lock_id"] not in self._locks:
                return self.async_abort(reason="wrong_account")
            await SessionStore(self.hass).rotate(self._login.token)
            return self.async_update_reload_and_abort(self._reauth_entry)
        if not self._locks:
            return self.async_abort(reason="no_locks")
        if len(self._locks) == 1:
            return await self._async_create(next(iter(self._locks)))
        return await self.async_step_lock()

    async def async_step_lock(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return await self._async_create(user_input["lock_id"])
            except AbortFlow:
                raise
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
        return self.async_show_form(
            step_id="lock",
            data_schema=vol.Schema({vol.Required("lock_id"): _select(self._locks)}),
            errors=errors,
        )

    async def _async_create(self, lock_id: str) -> ConfigFlowResult:
        title = self._locks[lock_id]
        await self.async_set_unique_id(lock_id)
        self._abort_if_unique_id_configured()
        assert self._login is not None
        await SessionStore(self.hass).rotate(self._login.token)
        return self.async_create_entry(
            title=title,
            data={
                "country": self._country,
                "lock_id": lock_id,
                "install_id": self._install_id,
            },
            options=deepcopy(DEFAULT_OPTIONS),
        )


class YaleAccessOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._choices: dict[str, str] = {}
        self._ha_user_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        service = self.hass.data.get(DOMAIN)
        if service is None:
            return self.async_abort(reason="not_loaded")
        self._options = deepcopy({**DEFAULT_OPTIONS, **self.config_entry.options})
        try:
            self._choices = await service.async_user_choices()
        except YaleAccessError:
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({}),
                errors={"base": "cannot_connect"},
            )
        except Exception:  # noqa: BLE001
            return self.async_show_form(
                step_id="init", data_schema=vol.Schema({}), errors={"base": "unknown"}
            )
        return self.async_show_menu(
            step_id="init", menu_options=["person", "protected", "notify"]
        )

    async def async_step_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if DOMAIN not in self.hass.data:
            return self.async_abort(reason="not_loaded")
        users = {
            user.id: user.name or user.id
            for user in await self.hass.auth.async_get_users()
            if not user.system_generated and user.is_active
        }
        if user_input is not None:
            self._ha_user_id = user_input["ha_user_id"]
            return await self.async_step_person_details()
        return self.async_show_form(
            step_id="person",
            data_schema=vol.Schema({vol.Required("ha_user_id"): _select(users)}),
        )

    async def async_step_person_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if DOMAIN not in self.hass.data:
            return self.async_abort(reason="not_loaded")
        others = [
            user
            for user in self._options["users"]
            if user["ha_user_id"] != self._ha_user_id
        ]
        existing = next(
            (
                user
                for user in self._options["users"]
                if user["ha_user_id"] == self._ha_user_id
            ),
            {"role": "none", "yale_users": []},
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input["role"] == "none":
                self._options["users"] = others
                return self.async_create_entry(title="", data=self._options)
            assigned = list(user_input.get("yale_users", []))
            own = user_input.get("own_yale_user")
            if own:
                assigned.append(own)
            taken = {
                uid
                for user in others
                for uid in [*user.get("yale_users", []), user.get("own_yale_user")]
                if uid
            }
            if len(assigned) != len(set(assigned)) or taken.intersection(assigned):
                errors["base"] = "yale_user_taken"
            else:
                person = {
                    "ha_user_id": self._ha_user_id,
                    "role": user_input["role"],
                    "yale_users": user_input.get("yale_users", []),
                }
                if own:
                    person["own_yale_user"] = own
                self._options["users"] = [*others, person]
                return self.async_create_entry(title="", data=self._options)
            existing = user_input
        return self.async_show_form(
            step_id="person_details",
            data_schema=vol.Schema(
                {
                    vol.Required("role", default=existing["role"]): SelectSelector(
                        SelectSelectorConfig(
                            options=["none", "admin", "guests"], translation_key="role"
                        )
                    ),
                    vol.Optional(
                        "own_yale_user",
                        description={"suggested_value": existing.get("own_yale_user")},
                    ): _select(self._choices),
                    vol.Optional(
                        "yale_users", default=existing.get("yale_users", [])
                    ): _select(self._choices, multiple=True),
                }
            ),
            errors=errors,
        )

    async def async_step_protected(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if DOMAIN not in self.hass.data:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            self._options["protected_users"] = user_input.get("protected_users", [])
            return self.async_create_entry(title="", data=self._options)
        return self.async_show_form(
            step_id="protected",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "protected_users", default=self._options["protected_users"]
                    ): _select(self._choices, multiple=True)
                }
            ),
        )

    async def async_step_notify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if DOMAIN not in self.hass.data:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            self._options["notify"] = user_input.get("notify")
            return self.async_create_entry(title="", data=self._options)
        choices = {
            f"notify.{name}": f"notify.{name}"
            for name in self.hass.services.async_services().get("notify", {})
        }
        return self.async_show_form(
            step_id="notify",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "notify",
                        description={"suggested_value": self._options["notify"]},
                    ): _select(choices)
                }
            ),
        )
