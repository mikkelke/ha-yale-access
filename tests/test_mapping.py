from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.yale_access import mapping
from custom_components.yale_access.errors import (
    InvalidRequest,
    RequestFailed,
    UpstreamUnavailable,
)


def empty_pins():
    return {collection: [] for collection in mapping.COLLECTIONS}


def test_validity_encodes_copenhagen_dst_as_utc_milliseconds():
    result = mapping.validity("2026-10-25T02:15:00+02:00", "2026-10-25T02:45:00+01:00")
    assert result.body() == {
        "accessType": "temporary",
        "accessTimes": "DTSTART=2026-10-25T00:15:00.000Z;DTEND=2026-10-25T01:45:00.000Z",
    }
    assert mapping.validity().body() == {"accessType": "always"}


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-01T12:00:00", "2026-01-02T12:00:00Z"),
        ("2026-01-01T12:00:00Z", None),
        (None, "2026-01-01T12:00:00Z"),
        ("2026-01-02T12:00:00Z", "2026-01-01T12:00:00Z"),
        ("bad", "bad"),
    ],
)
def test_invalid_validity_is_rejected(start, end):
    with pytest.raises(InvalidRequest):
        mapping.validity(start, end)


def test_every_collection_reserves_slots_and_unknown_collections_fail_closed():
    body = empty_pins()
    for slot, collection in enumerate(mapping.COLLECTIONS, 1):
        body[collection].append(
            {
                "slot": slot,
                "userID": f"placeholder_{slot}",
                "state": collection,
                "accessType": "always",
            }
        )
    records = mapping.pin_records(body)
    assert mapping.choose_slot({}, records) == len(mapping.COLLECTIONS) + 1
    body["unknown"] = []
    with pytest.raises(UpstreamUnavailable):
        mapping.pin_records(body)


def test_explicit_slot_range_and_conflicting_bounds():
    assert mapping.choose_slot({"keypad": {"minSlot": 3, "maxSlot": 6}}, []) == 3
    with pytest.raises(UpstreamUnavailable):
        mapping.choose_slot(
            {"minPinSlot": 1, "maxPinSlot": 5, "keypad": {"minSlot": 2, "maxSlot": 6}},
            [],
        )
    with pytest.raises(RequestFailed):
        mapping.choose_slot({"supportsEntryCodes": False}, [])


def test_temporary_status_preserves_expired_users():
    access = mapping.validity("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
    assert mapping.status([], access, datetime(2025, 1, 1, tzinfo=UTC)) == "scheduled"
    assert mapping.status([], access, datetime(2026, 1, 1, 12, tzinfo=UTC)) == "active"
    assert mapping.status([], access, datetime(2026, 1, 3, tzinfo=UTC)) == "expired"


def test_profile_only_returns_name():
    assert (
        mapping.profile_name(
            {"FirstName": "Guest", "LastName": "Placeholder", "Email": "<email>"}
        )
        == "Guest Placeholder"
    )


def test_expired_disabled_temporary_code_stays_expired():
    body = empty_pins()
    body["disabled"] = [
        {
            "userID": "placeholder_expired",
            "slot": 1,
            "state": "disabled",
            "accessType": "temporary",
            "accessTimes": "DTSTART=2026-01-01T00:00:00.000Z;DTEND=2026-01-02T00:00:00.000Z",
        }
    ]
    records = mapping.pin_records(body)
    assert (
        mapping.status(records, records[0].validity, datetime(2026, 10, 1, tzinfo=UTC))
        == "expired"
    )
