"""Validate Yale data and select the fields sent to websocket clients."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .errors import InvalidRequest, RequestFailed, UpstreamUnavailable

COLLECTIONS = frozenset(
    {"created", "loaded", "disabled", "disabling", "enabling", "deleting", "updating"}
)
PENDING = COLLECTIONS - {"loaded", "disabled"}
STATES = COLLECTIONS | {"load", "delete", "update", "enable", "disable"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{8,128}")


def identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise UpstreamUnavailable("Yale returned an unexpected user identifier")
    return value


def utc_ms(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def date_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(UTC) if result.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True, slots=True)
class Validity:
    kind: str
    start: datetime | None = None
    end: datetime | None = None

    def body(self) -> dict[str, str]:
        result = {"accessType": self.kind}
        if self.start is not None and self.end is not None:
            result["accessTimes"] = (
                f"DTSTART={utc_ms(self.start)};DTEND={utc_ms(self.end)}"
            )
        return result


def validity(start: object = None, end: object = None) -> Validity:
    if start is None and end is None:
        return Validity("always")
    first, last = date_time(start), date_time(end)
    if first is None or last is None or last <= first:
        raise InvalidRequest("Provide from and to with offsets, with to after from")
    return Validity("temporary", first, last)


def read_validity(raw: dict[str, Any]) -> Validity:
    kind = raw.get("accessType", "always")
    if kind not in {"always", "temporary", "recurring"}:
        raise UpstreamUnavailable("Yale returned an unexpected access type")
    if kind != "temporary":
        return Validity(kind)
    start = date_time(raw.get("accessStartTime"))
    end = date_time(raw.get("accessEndTime"))
    text = raw.get("accessTimes")
    if isinstance(text, str):
        parts = dict(part.split("=", 1) for part in text.split(";") if "=" in part)
        start = start or date_time(parts.get("DTSTART"))
        end = end or date_time(parts.get("DTEND"))
    return Validity(kind, start, end)


@dataclass(frozen=True, slots=True)
class PinRecord:
    collection: str
    slot: int
    user_id: str
    state: str
    validity: Validity
    pin: str | None = field(repr=False, compare=False)


def pin_records(body: object) -> list[PinRecord]:
    if not isinstance(body, dict) or set(body) != COLLECTIONS:
        raise UpstreamUnavailable("Yale returned unexpected PIN collections")
    result = []
    for collection, entries in body.items():
        if not isinstance(entries, list):
            raise UpstreamUnavailable("Yale returned unexpected PIN collections")
        for entry in entries:
            if not isinstance(entry, dict):
                raise UpstreamUnavailable("Yale returned an unexpected PIN record")
            slot = entry.get("slot")
            state = entry.get("state")
            if (
                isinstance(slot, bool)
                or not re.fullmatch(r"[0-9]+", str(slot))
                or state not in STATES
            ):
                raise UpstreamUnavailable("Yale returned an unexpected PIN record")
            pin = entry.get("pin")
            result.append(
                PinRecord(
                    collection,
                    int(slot),
                    identifier(entry.get("userID")),
                    state,
                    read_validity(entry),
                    pin if isinstance(pin, str) else None,
                )
            )
    return result


def lock_users(detail: object, lock_id: str) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(detail, dict)
        or detail.get("LockID") != lock_id
        or not isinstance(detail.get("users"), dict)
    ):
        raise UpstreamUnavailable("Yale returned unexpected lock details")
    users = detail["users"]
    for uid, user in users.items():
        identifier(uid)
        if not isinstance(user, dict):
            raise UpstreamUnavailable("Yale returned an unexpected lock user")
    return users


def supported_slots(detail: dict[str, Any]) -> tuple[int, int] | None:
    ranges = set()

    def walk(value: object, context: str = "") -> None:
        if not isinstance(value, dict):
            return
        normalized = {re.sub(r"[^a-z]", "", k.lower()): v for k, v in value.items()}
        pairs = [("minpinslot", "maxpinslot"), ("minentrycodeslot", "maxentrycodeslot")]
        if any(
            word in context for word in ("pin", "credential", "entrycode", "keypad")
        ):
            pairs += [("minslot", "maxslot"), ("minimumslot", "maximumslot")]
        for low_key, high_key in pairs:
            if low_key in normalized and high_key in normalized:
                low, high = normalized[low_key], normalized[high_key]
                if (
                    type(low) is not int
                    or type(high) is not int
                    or not 0 <= low <= high <= 10000
                ):
                    raise UpstreamUnavailable("Yale returned invalid slot bounds")
                ranges.add((low, high))
        for key, child in value.items():
            if key not in {"pins", "users"}:
                walk(child, context + re.sub(r"[^a-z]", "", key.lower()))

    walk(detail)
    if len(ranges) > 1:
        raise UpstreamUnavailable("Yale returned conflicting slot bounds")
    return next(iter(ranges)) if ranges else None


def choose_slot(detail: dict[str, Any], records: list[PinRecord]) -> int:
    if detail.get("supportsEntryCodes") is False:
        raise RequestFailed("The lock does not support entry codes")
    occupied = {record.slot for record in records}
    low, high = supported_slots(detail) or (1, max(occupied, default=0) + 1)
    for slot in range(low, high + 1):
        if slot not in occupied:
            return slot
    raise RequestFailed("No free PIN slot is available")


def pin_length(records: list[PinRecord]) -> int:
    lengths = Counter(
        sum(character.isdigit() for character in record.pin)
        for record in records
        if record.pin is not None
    )
    lengths.pop(0, None)
    return lengths.most_common(1)[0][0] if lengths else 6


def profile_name(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    values = [
        raw.get("FirstName", raw.get("firstName")),
        raw.get("LastName", raw.get("lastName")),
    ]
    name = " ".join(
        value.strip() for value in values if isinstance(value, str) and value.strip()
    )
    return name or None


def status(records: list[PinRecord], access: Validity, now: datetime) -> str:
    if (
        access.kind == "temporary"
        and access.end is not None
        and now >= access.end
        and all(record.collection in {"loaded", "disabled"} for record in records)
    ):
        return "expired"
    if any(
        record.collection in PENDING or record.state != "loaded" for record in records
    ):
        if any(record.collection == "disabled" for record in records):
            return "failed"
        return "pending"
    if access.kind == "temporary":
        if access.start is None or access.end is None:
            return "failed"
        if now >= access.end:
            return "expired"
        if now < access.start:
            return "scheduled"
    return "active"


def same_validity(left: Validity, right: Validity) -> bool:
    return left.kind == right.kind and (
        left.kind != "temporary"
        or (
            left.start is not None
            and left.end is not None
            and right.start is not None
            and right.end is not None
            and utc_ms(left.start) == utc_ms(right.start)
            and utc_ms(left.end) == utc_ms(right.end)
        )
    )
