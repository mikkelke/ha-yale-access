"""People, protection and notification options through Home Assistant."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.yale_access.const import DOMAIN

YALE_A = "placeholder-yale-a"
YALE_B = "placeholder-yale-b"


@pytest.fixture
async def people(hass):
    first = await hass.auth.async_create_user("First person")
    second = await hass.auth.async_create_user("Second person")
    inactive = await hass.auth.async_create_user("Inactive person")
    await hass.auth.async_deactivate_user(inactive)
    await hass.auth.async_create_system_user("System person")
    return first, second


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "country": "US",
            "lock_id": "placeholder-lock",
            "install_id": "placeholder-install",
        },
        options={"users": [], "protected_users": [], "notify": None},
    )
    entry.add_to_hass(hass)
    hass.data[DOMAIN] = SimpleNamespace(
        async_user_choices=AsyncMock(
            return_value={
                YALE_A: "Resident (keypad slot 1)",
                YALE_B: "Guest (app user)",
            }
        )
    )
    return entry


async def start(hass, entry, section):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["person", "protected", "notify"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": section}
    )


async def person(hass, entry, ha_user_id):
    result = await start(hass, entry, "person")
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"ha_user_id": ha_user_id}
    )


async def test_person_add_edit_remove(hass, entry, people):
    first, _ = people
    result = await person(hass, entry, first.id)
    assert result["step_id"] == "person_details"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"role": "admin", "own_yale_user": YALE_A, "yale_users": [YALE_B]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["users"] == [
        {
            "ha_user_id": first.id,
            "role": "admin",
            "own_yale_user": YALE_A,
            "yale_users": [YALE_B],
        }
    ]
    result = await person(hass, entry, first.id)
    assert result["data_schema"]({"role": "guests"})["yale_users"] == [YALE_B]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"role": "guests", "yale_users": []}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["users"] == [
        {"ha_user_id": first.id, "role": "guests", "yale_users": []}
    ]
    result = await person(hass, entry, first.id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"role": "none"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["users"] == []


async def test_person_choices_exclude_inactive_and_system(hass, entry, people):
    result = await start(hass, entry, "person")
    selector = next(iter(result["data_schema"].schema.values()))
    assert selector.config["options"] == [
        {"value": user.id, "label": user.name} for user in people
    ]


@pytest.mark.parametrize(
    "previous,current",
    [
        ("own_yale_user", "own_yale_user"),
        ("own_yale_user", "yale_users"),
        ("yale_users", "own_yale_user"),
        ("yale_users", "yale_users"),
    ],
)
async def test_duplicate_yale_assignment(hass, entry, people, previous, current):
    first, second = people
    mapping = {"ha_user_id": first.id, "role": "admin", "yale_users": []}
    mapping[previous] = YALE_A if previous == "own_yale_user" else [YALE_A]
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "users": [mapping]}
    )
    result = await person(hass, entry, second.id)
    submitted = {
        "role": "guests",
        current: YALE_A if current == "own_yale_user" else [YALE_A],
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], submitted
    )
    assert result["step_id"] == "person_details"
    assert result["errors"] == {"base": "yale_user_taken"}
    assert entry.options["users"] == [mapping]


async def test_protected_choices_include_protected_users(hass, entry):
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "protected_users": [YALE_A]}
    )
    result = await start(hass, entry, "protected")
    selector = next(iter(result["data_schema"].schema.values()))
    assert {item["value"] for item in selector.config["options"]} == {YALE_A, YALE_B}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"protected_users": [YALE_B]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["protected_users"] == [YALE_B]
    result = await start(hass, entry, "protected")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"protected_users": []}
    )
    assert entry.options["protected_users"] == []


async def test_notify_service_and_clear(hass, entry):
    hass.services.async_register("notify", "placeholder_device", AsyncMock())
    result = await start(hass, entry, "notify")
    selector = next(iter(result["data_schema"].schema.values()))
    assert selector.config["options"] == [
        {"value": "notify.placeholder_device", "label": "notify.placeholder_device"}
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"notify": "notify.placeholder_device"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["notify"] == "notify.placeholder_device"
    result = await start(hass, entry, "notify")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert entry.options["notify"] is None


async def test_options_not_loaded(hass, entry):
    hass.data.pop(DOMAIN)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"


async def test_options_refresh_error_can_be_retried(hass, entry):
    from custom_components.yale_access.errors import UpstreamUnavailable

    hass.data[DOMAIN].async_user_choices.side_effect = [
        UpstreamUnavailable(),
        {YALE_A: "Resident (keypad slot 1)"},
    ]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU


async def test_same_person_own_and_assigned_duplicate(hass, entry, people):
    result = await person(hass, entry, people[0].id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"role": "admin", "own_yale_user": YALE_A, "yale_users": [YALE_A]},
    )
    assert result["errors"] == {"base": "yale_user_taken"}
