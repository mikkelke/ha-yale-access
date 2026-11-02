"""Login, lock selection and reauthentication through Home Assistant."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.yale_access.const import DOMAIN
from custom_components.yale_access.login import LoginError

INPUT = {
    "country": "DK",
    "method": "email",
    "identifier": "placeholder@example.invalid",
    "password": "placeholder-password",
}
LOCK = "placeholder-lock"


@pytest.fixture
def login():
    login = SimpleNamespace(
        token="placeholder-token",
        signin=AsyncMock(return_value=True),
        locks=AsyncMock(return_value={LOCK: "Front door"}),
    )

    async def verify(code):
        login.token = "placeholder-token"

    login.verify = AsyncMock(side_effect=verify)
    return login


@pytest.fixture
def login_factory(login):
    with (
        patch(
            "custom_components.yale_access.config_flow.YaleLogin", return_value=login
        ) as factory,
        patch("custom_components.yale_access.config_flow.SessionStore") as store,
        patch("custom_components.yale_access.async_setup_entry", return_value=True),
    ):
        store.return_value.rotate = AsyncMock()
        yield factory, store.return_value.rotate


async def start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_password_login_and_entry(hass, login, login_factory):
    factory, save = login_factory
    hass.config.country = "DK"
    result = await start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["data_schema"]({**INPUT, "country": "DK"})["country"] == "DK"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Front door"
    assert set(result["data"]) == {"country", "lock_id", "install_id"}
    assert result["data"]["lock_id"] == LOCK
    assert str(UUID(result["data"]["install_id"])) == result["data"]["install_id"]
    assert result["options"] == {"users": [], "protected_users": [], "notify": None}
    assert result["result"].unique_id == LOCK
    assert factory.call_args.args[1:] == ("DK", result["data"]["install_id"])
    login.signin.assert_awaited_once_with(
        "email", INPUT["identifier"], INPUT["password"]
    )
    save.assert_awaited_once_with(login.token)


async def test_verification_login(hass, login, login_factory):
    login.signin.return_value = False
    login.token = None
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["step_id"] == "verify"
    login_factory[1].assert_not_awaited()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    login.verify.assert_awaited_once_with("123456")


async def test_multiple_locks(hass, login, login_factory):
    login.locks.return_value = {LOCK: "Front door", "placeholder-second": "Side door"}
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["step_id"] == "lock"
    login_factory[1].assert_not_awaited()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"lock_id": "placeholder-second"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Side door"
    assert result["data"]["lock_id"] == "placeholder-second"


@pytest.mark.parametrize(
    "locks,reason", [({}, "no_locks"), ({LOCK: "Front door"}, "already_configured")]
)
async def test_abort(hass, login, login_factory, locks, reason):
    result = await start(hass)
    if reason == "already_configured":
        MockConfigEntry(domain=DOMAIN, unique_id=LOCK).add_to_hass(hass)
    login.locks.return_value = locks
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    login_factory[1].assert_not_awaited()


@pytest.mark.parametrize("code", ["invalid_auth", "cannot_connect", "unknown"])
async def test_login_errors(hass, login, login_factory, code, caplog):
    login.signin.side_effect = LoginError(code)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": code}
    assert INPUT["password"] not in caplog.text
    assert login.token not in caplog.text


async def test_invalid_verification_code_can_be_retried(hass, login, login_factory):
    login.signin.return_value = False
    login.token = None
    login.verify.side_effect = [LoginError("invalid_code"), None]
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "000000"}
    )
    assert result["step_id"] == "verify"
    assert result["errors"] == {"base": "invalid_code"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("verification", [False, True])
@pytest.mark.parametrize("wrong_account", [False, True])
async def test_reauth(hass, login, login_factory, verification, wrong_account):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=LOCK,
        data={"country": "DK", "install_id": "placeholder-install", "lock_id": LOCK},
        options={"users": [], "protected_users": [], "notify": None},
    )
    entry.add_to_hass(hass)
    login.signin.return_value = not verification
    if verification:
        login.token = None
    if wrong_account:
        login.locks.return_value = {"placeholder-other": "Other door"}
    with patch.object(
        hass.config_entries, "async_reload", return_value=True
    ) as reload_entry:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=dict(entry.data),
        )
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {key: value for key, value in INPUT.items() if key != "country"},
        )
        if verification:
            assert result["step_id"] == "verify"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"code": "123456"}
            )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == (
            "wrong_account" if wrong_account else "reauth_successful"
        )
        assert login_factory[0].call_args.args[1:] == ("DK", "placeholder-install")
        if wrong_account:
            login_factory[1].assert_not_awaited()
            reload_entry.assert_not_awaited()
        else:
            login_factory[1].assert_awaited_once_with(login.token)
            reload_entry.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize("country,expected", [("DK", "DK"), (None, "US")])
async def test_country_default_and_serializable_form(
    hass, login_factory, country, expected
):
    import probatio
    from homeassistant.helpers import config_validation as cv

    hass.config.country = country
    result = await start(hass)
    fields = probatio.to_field_list(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    assert (
        next(field for field in fields if field["name"] == "country")["default"]
        == expected
    )
    submitted = {key: value for key, value in INPUT.items() if key != "country"}
    assert result["data_schema"](submitted)["country"] == expected
    for invalid in ("dk", "Dk", "USA", "D", "US\n", "ÜS"):
        with pytest.raises(vol.Invalid):
            result["data_schema"]({**submitted, "country": invalid})


async def test_unexpected_login_error_is_sanitized(hass, login, login_factory, caplog):
    login.signin.side_effect = RuntimeError(INPUT["password"] + login.token)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    assert result["errors"] == {"base": "unknown"}
    assert INPUT["password"] not in caplog.text
    assert login.token not in caplog.text


async def test_lock_fetch_failure_after_verification_can_retry(
    hass, login, login_factory
):
    login.signin.return_value = False
    login.token = None

    async def verified(code):
        login.token = "placeholder-token"

    login.verify.side_effect = verified
    login.locks.side_effect = [LoginError("cannot_connect"), {LOCK: "Front door"}]
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["step_id"] == "verify"
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    login.verify.assert_awaited_once_with("123456")
