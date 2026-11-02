"""Role-scoped users and serialized Yale credential changes."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import mapping
from .errors import (
    Forbidden,
    InvalidRequest,
    NotFound,
    Pending,
    Protected,
    ReadOnly,
    RequestFailed,
    UpstreamUnavailable,
    YaleAccessError,
)


@dataclass(frozen=True, slots=True)
class MappedUser:
    ha_user_id: str
    role: str
    yale_users: tuple[str, ...] = ()
    own: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"admin", "guests"}:
            raise ValueError("role must be admin or guests")


class UserMap:
    def __init__(self, users: Iterable[MappedUser]) -> None:
        self._by_ha: dict[str, MappedUser] = {}
        self._by_yale: dict[str, str] = {}
        assigned: set[str] = set()
        for user in users:
            if user.ha_user_id in self._by_ha:
                raise ValueError("a Home Assistant user is mapped twice")
            self._by_ha[user.ha_user_id] = user
            ids = user.yale_users + ((user.own,) if user.own is not None else ())
            for uid in ids:
                if uid in assigned:
                    raise ValueError("a Yale user is assigned to multiple HA users")
                assigned.add(uid)
            for uid in user.yale_users:
                self._by_yale[uid] = user.ha_user_id

    def owner(self, yale_user_id: str) -> str | None:
        return self._by_yale.get(yale_user_id)

    def require(self, ha_user_id: object) -> MappedUser:
        user = self._by_ha.get(ha_user_id) if isinstance(ha_user_id, str) else None
        if user is None:
            raise Forbidden
        return user


class YaleAccessService:
    def __init__(
        self,
        client: Any,
        users: UserMap,
        store: Any,
        display_name: Callable[[str], Awaitable[str | None]] | None = None,
        *,
        protected_users: Iterable[str] = (),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        poll_budget: float = 180,
    ) -> None:
        self._client = client
        self.users = users
        self._store = store
        self._display_name = display_name
        self._protected_users = frozenset(protected_users)
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._poll_budget = poll_budget
        self._pin_length = 6
        self._lock = asyncio.Lock()
        self._ownership: dict[str, dict[str, Any]] = {}
        self._rows: dict[str, dict[str, Any]] = {}
        self._imported_pending: dict[str, dict[str, Any]] = {}
        self._ready = False

    @property
    def client(self) -> Any:
        return self._client

    @property
    def pin_length(self) -> int:
        return self._pin_length

    async def load(self) -> None:
        raw = await self._store.async_load()
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise RequestFailed("The Yale ownership store is unreadable")
        for uid, owner in raw.items():
            if not isinstance(owner, dict):
                raise RequestFailed("The Yale ownership store is unreadable")
            try:
                mapping.identifier(uid)
            except YaleAccessError:
                raise RequestFailed("The Yale ownership store is unreadable") from None
            if not all(
                isinstance(owner.get(key), str)
                for key in ("created_by", "created_at", "name")
            ):
                raise RequestFailed("The Yale ownership store is unreadable")
            self._ownership[uid] = {
                key: owner[key] for key in ("created_by", "created_at", "name")
            }
            pending = owner.get("pending")
            if isinstance(pending, dict) and pending.get("operation") in {
                "create",
                "update",
                "end",
            }:
                self._ownership[uid]["pending"] = {
                    key: pending.get(key)
                    for key in ("operation", "slot", "kind", "from", "to")
                }

    def _user(self, user: MappedUser) -> MappedUser:
        actual = self.users.require(getattr(user, "ha_user_id", None))
        if actual != user:
            raise Forbidden
        return actual

    def _allowed(self, user: MappedUser, uid: str) -> bool:
        return (
            user.role == "admin"
            or self._ownership.get(uid, {}).get("created_by") == user.ha_user_id
            or self.users.owner(uid) == user.ha_user_id
        )

    def _target(
        self, user: MappedUser, uid: object, *, allow_own: bool = False
    ) -> dict[str, Any]:
        if (
            not isinstance(uid, str)
            or not (allow_own and uid == user.own or self._allowed(user, uid))
            or uid not in self._rows
        ):
            raise NotFound
        return self._rows[uid]

    async def _pins(self) -> list[mapping.PinRecord]:
        records = mapping.pin_records(await self._client.pins())
        self._pin_length = mapping.pin_length(records)
        return records

    async def _snapshot(
        self,
    ) -> tuple[dict[str, Any], list[mapping.PinRecord], list[mapping.PinRecord]]:
        detail = await self._client.detail()
        mapping.lock_users(detail, self._client.lock_id)
        direct = await self._pins()
        embedded = mapping.pin_records(detail.get("pins"))
        self._pin_length = mapping.pin_length(direct + embedded)
        return detail, direct, embedded

    async def refresh(self) -> None:
        async with self._lock:
            await self._refresh()

    async def _refresh(self, *, full: bool = True) -> None:
        detail, direct, embedded = await self._snapshot()
        lock_users = mapping.lock_users(detail, self._client.lock_id)
        records = direct + embedded
        by_user: dict[str, list[mapping.PinRecord]] = {}
        for record in records:
            by_user.setdefault(record.user_id, []).append(record)
        ids = set(lock_users) | set(by_user)
        for uid in list(self._imported_pending):
            if uid not in by_user and self._imported_pending[uid]["operation"] == "end":
                self._imported_pending.pop(uid)
        last_uses = (
            await self._last_uses(detail.get("HouseID"), ids)
            if full
            else {uid: row.get("last_use") for uid, row in self._rows.items()}
        )
        names: dict[str, str | None] = {}
        for uid in ids:
            known_name = self._rows.get(uid, {}).get("name")
            if known_name == "Unknown user":
                known_name = None
            names[uid] = (
                known_name
                or mapping.profile_name(lock_users.get(uid))
                or self._ownership.get(uid, {}).get("name")
            )
            if full or not names[uid]:
                try:
                    names[uid] = mapping.profile_name(await self._client.profile(uid))
                except NotFound:
                    names[uid] = None
        rows = {}
        changed = False
        for uid in ids:
            user_records = by_user.get(uid, [])
            chosen = (
                min(
                    user_records,
                    key=lambda record: (record.collection != "loaded", record.slot),
                )
                if user_records
                else None
            )
            access = (
                chosen.validity
                if chosen is not None
                else mapping.read_validity(lock_users[uid])
            )
            owner = self._ownership.get(uid, {})
            pending = owner.get("pending") or self._imported_pending.get(uid)
            if pending:
                expected = mapping.validity(pending.get("from"), pending.get("to"))
                if (
                    pending["operation"] != "end"
                    and user_records
                    and all(
                        record.collection == "loaded"
                        and record.state == "loaded"
                        and mapping.same_validity(record.validity, expected)
                        for record in user_records
                    )
                ):
                    if owner.pop("pending", None) is not None:
                        changed = True
                    self._imported_pending.pop(uid, None)
            creator = self.users.owner(uid) or owner.get("created_by")
            display_name = (
                await self._display_name(creator)
                if creator and self._display_name
                else None
            )
            rows[uid] = {
                "user_id": uid,
                "name": names[uid]
                or mapping.profile_name(lock_users.get(uid))
                or owner.get("name")
                or "Unknown user",
                "slot": chosen.slot if chosen is not None else None,
                "kind": access.kind,
                "from": mapping.utc_ms(access.start)
                if access.start is not None
                else None,
                "to": mapping.utc_ms(access.end) if access.end is not None else None,
                "status": mapping.status(user_records, access, self._now()),
                "last_use": last_uses.get(uid),
                "created_by": display_name,
            }
            if owner.get("pending") or uid in self._imported_pending:
                rows[uid]["status"] = "pending"
        for uid, owner in self._ownership.items():
            if uid not in rows and owner.get("pending"):
                if owner["pending"]["operation"] == "end":
                    owner.pop("pending")
                    changed = True
                else:
                    rows[uid] = await self._pending_row(uid, owner["name"])
        if changed:
            await self._store.async_save(self._ownership)
        self._rows = rows
        self._ready = True

    async def _last_uses(self, house_id: object, ids: set[str]) -> dict[str, str]:
        if not isinstance(house_id, str) or not ids:
            return {}
        cutoff = self._now() - timedelta(days=90)
        found: dict[str, datetime] = {}
        cursor = None
        seen: set[str] = set()
        for _ in range(1000):
            payload = await self._client.activities(house_id, end=cursor)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("events"), list
            ):
                raise UpstreamUnavailable("Yale returned unexpected activities")
            events = payload["events"]
            oldest = None
            for event in events:
                if not isinstance(event, dict):
                    continue
                timestamp = event.get("timestamp")
                if type(timestamp) not in (int, float):
                    continue
                try:
                    moment = datetime.fromtimestamp(timestamp / 1000, UTC)
                except (ValueError, OverflowError, OSError):
                    continue
                oldest = min(oldest, moment) if oldest else moment
                user = event.get("user")
                uid = user.get("UserID") if isinstance(user, dict) else None
                if (
                    uid in ids
                    and moment >= cutoff
                    and event.get("deviceID") == self._client.lock_id
                    and event.get("action")
                    in {
                        "pin_unlock",
                        "unlock",
                        "app_unlock",
                        "remote_unlock",
                        "ble_unlock",
                    }
                ):
                    found[uid] = max(found.get(uid, moment), moment)
            if (
                ids <= found.keys()
                or not events
                or oldest is not None
                and oldest < cutoff
            ):
                break
            next_page = payload.get("nextPage")
            if not isinstance(next_page, str):
                break
            values = parse_qs(urlsplit(next_page).query).get("end", [])
            if len(values) != 1 or not values[0] or values[0] in seen:
                break
            cursor = values[0]
            seen.add(cursor)
        return {uid: mapping.utc_ms(moment) for uid, moment in found.items()}

    async def _pending_row(self, uid: str, name: str) -> dict[str, Any]:
        creator = self.users.owner(uid) or self._ownership[uid]["created_by"]
        pending = self._ownership[uid].get("pending", {})
        display_name = await self._display_name(creator) if self._display_name else None
        return {
            "user_id": uid,
            "name": name,
            "slot": pending.get("slot"),
            "kind": pending.get("kind", "always"),
            "from": pending.get("from"),
            "to": pending.get("to"),
            "status": "pending",
            "last_use": self._rows.get(uid, {}).get("last_use"),
            "created_by": display_name,
        }

    @staticmethod
    def _validity(user: MappedUser, start: object, end: object) -> mapping.Validity:
        access = mapping.validity(start, end)
        if access.kind == "always" and user.role != "admin":
            raise InvalidRequest("Guests need a period with a start and an end")
        return access

    async def me(self, user: MappedUser) -> dict[str, Any]:
        self._user(user)
        async with self._lock:
            if not self._ready:
                await self._refresh()
            row = self._rows.get(user.own)
            return {
                "role": user.role,
                "pin_length": self.pin_length,
                "own": dict(row) if row is not None else None,
            }

    async def list_users(self, user: MappedUser) -> dict[str, Any]:
        self._user(user)
        async with self._lock:
            if not self._ready:
                await self._refresh()
            return {
                "users": [
                    dict(row)
                    for uid, row in sorted(
                        self._rows.items(),
                        key=lambda item: (
                            str(item[1].get("name") or "").casefold(),
                            item[0],
                        ),
                    )
                    if uid not in self._protected_users and self._allowed(user, uid)
                ]
            }

    def _safe_sync(
        self, records: list[mapping.PinRecord], uid: str | None = None
    ) -> None:
        if (
            any(
                record.collection in mapping.PENDING and record.user_id != uid
                for record in records
            )
            or any(
                owner.get("pending") and owner_id != uid
                for owner_id, owner in self._ownership.items()
            )
            or any(owner_id != uid for owner_id in self._imported_pending)
        ):
            raise RequestFailed(
                "Another credential change is pending; wait before changing codes"
            )

    async def create(
        self,
        user: MappedUser,
        name: object,
        start: object = None,
        end: object = None,
        pin: object = None,
    ) -> dict[str, Any]:
        self._user(user)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise InvalidRequest("Provide a name of 1 to 100 characters")
        name = name.strip()
        access = self._validity(user, start, end)
        async with self._lock:
            if (
                any(owner.get("pending") for owner in self._ownership.values())
                or self._imported_pending
            ):
                await self._refresh()
                if (
                    any(owner.get("pending") for owner in self._ownership.values())
                    or self._imported_pending
                ):
                    raise RequestFailed(
                        "A previous credential change is awaiting confirmation; check its status before creating another guest"
                    )
            detail, direct, embedded = await self._snapshot()
            records = direct + embedded
            self._safe_sync(records)
            if pin is not None:
                self._validate_pin(pin, records)
            slot = mapping.choose_slot(detail, records)
            baseline = set(mapping.lock_users(detail, self._client.lock_id)) | {
                record.user_id for record in records
            }
            uid = await self._client.create_user(name)
            mapping.identifier(uid)
            if uid in baseline or uid in self._ownership:
                raise Pending(
                    "User creation returned an existing identity. Inspect Yale before creating another guest"
                )
            self._ownership[uid] = {
                "created_by": user.ha_user_id,
                "created_at": mapping.utc_ms(self._now()),
                "name": name,
                "pending": {
                    "operation": "create",
                    "slot": slot,
                    "kind": access.kind,
                    "from": mapping.utc_ms(access.start) if access.start else None,
                    "to": mapping.utc_ms(access.end) if access.end else None,
                },
            }
            self._rows[uid] = await self._pending_row(uid, name)
            try:
                await self._store.async_save(self._ownership)
            except Exception:  # noqa: BLE001
                raise Pending(
                    "The new user is pending because ownership could not be saved. Inspect Yale before creating another guest"
                ) from None
            body: dict[str, str] = {}
            existing: set[str] = set()
            try:
                detail, direct, embedded = await self._snapshot()
                records = direct + embedded
                self._safe_sync(records, uid)
                if any(
                    record.slot == slot or record.user_id == uid for record in records
                ):
                    raise Pending
                digits = mapping.pin_length(records)
                existing = {record.pin for record in records if record.pin is not None}
                if pin is not None:
                    self._validate_pin(pin, records)
                else:
                    for _ in range(1000):
                        pin = str(
                            secrets.randbelow(9 * 10 ** (digits - 1))
                            + 10 ** (digits - 1)
                        )
                        if pin not in existing:
                            break
                    else:
                        raise Pending
                body = {
                    "type": "pin",
                    "slot": str(slot),
                    "state": "load",
                    "action": "intent",
                    "pin": pin,
                    **access.body(),
                }
                await self._client.intent(uid, body)
                await self._client.sync()
                await self._poll_loaded(uid, slot, pin, access, strict_user=True)
                row = self._rows[uid]
                try:
                    await self._refresh(full=False)
                except Exception:  # noqa: BLE001
                    return {"user": dict(row), "pin": pin}
                return {"user": dict(self._rows.get(uid, row)), "pin": pin}
            except RequestFailed:
                raise Pending(
                    "Yale refused a required request. The new user is pending; check account permissions. The one-time PIN was not delivered and cannot be recovered here. Do not create the guest again"
                ) from None
            except YaleAccessError:
                raise Pending from None
            except Exception:  # noqa: BLE001
                raise Pending from None
            finally:
                body.clear()
                existing.clear()

    async def _poll_loaded(
        self,
        uid: str,
        slot: int,
        pin: str,
        access: mapping.Validity,
        *,
        strict_user: bool = False,
    ) -> None:
        async def check() -> bool:
            records = await self._pins()
            matches = [
                record
                for record in records
                if record.slot == slot or strict_user and record.user_id == uid
            ]
            return bool(matches) and all(
                record.collection == "loaded"
                and record.state == "loaded"
                and record.user_id == uid
                and record.slot == slot
                and record.pin == pin
                and mapping.same_validity(record.validity, access)
                for record in matches
            )

        await self._poll(check)

    async def _poll(self, check: Callable[[], Awaitable[bool]]) -> None:
        started = self._clock()
        deadline = started + self._poll_budget
        while (remaining := deadline - self._clock()) > 0:
            try:
                async with asyncio.timeout(remaining):
                    if await check():
                        return
            except TimeoutError:
                break
            except UpstreamUnavailable:
                if (
                    getattr(getattr(self._client, "session_file", None), "state", None)
                    == "failed"
                ):
                    raise Pending from None
            delay = 1 if self._clock() - started < 15 else 3
            await self._sleep(min(delay, max(0, deadline - self._clock())))
        raise Pending(
            "The change is pending. The one-time PIN was not delivered and cannot be shown again here. Check the user list; do not create the guest again"
        )

    @staticmethod
    def _validate_pin(
        pin: object, records: list[mapping.PinRecord], uid: str | None = None
    ) -> str:
        digits = mapping.pin_length(records)
        if (
            not isinstance(pin, str)
            or len(pin) != digits
            or not pin.isascii()
            or not pin.isdigit()
        ):
            raise InvalidRequest(f"The code must be {digits} digits")
        if any(record.pin == pin and record.user_id != uid for record in records):
            raise InvalidRequest("That code is already in use")
        return pin

    async def pin(self, user: MappedUser, user_id: object) -> dict[str, Any]:
        self._user(user)
        if (
            isinstance(user_id, str)
            and user_id != user.own
            and user_id in self._protected_users
        ):
            raise Protected if user.role == "admin" else NotFound
        async with self._lock:
            if not self._ready:
                await self._refresh()
            self._target(user, user_id, allow_own=True)
            targets = [
                record
                for record in await self._pins()
                if record.user_id == user_id
                and record.collection == "loaded"
                and record.state == "loaded"
            ]
            if not targets:
                raise NotFound
            chosen = min(targets, key=lambda record: record.slot)
            if chosen.pin is None:
                raise NotFound
            return {"pin": chosen.pin}

    async def set_validity(
        self,
        user: MappedUser,
        user_id: object,
        start: object = None,
        end: object = None,
    ) -> dict[str, Any]:
        self._user(user)
        if user.own is not None and user_id == user.own:
            raise Protected(
                "You can't change your own access period or end your own access here"
            )
        if isinstance(user_id, str) and user_id in self._protected_users:
            raise Protected if user.role == "admin" else NotFound
        access = self._validity(user, start, end)
        async with self._lock:
            if not self._ready:
                await self._refresh()
            self._target(user, user_id)
            return await self._update(user_id, access=access)

    async def set_pin(
        self, user: MappedUser, user_id: object, pin: object
    ) -> dict[str, Any]:
        self._user(user)
        if (
            isinstance(user_id, str)
            and user_id != user.own
            and user_id in self._protected_users
        ):
            raise Protected if user.role == "admin" else NotFound
        async with self._lock:
            if not self._ready:
                await self._refresh()
            self._target(user, user_id, allow_own=True)
            return await self._update(user_id, pin=pin)

    async def _update(
        self,
        user_id: str,
        *,
        access: mapping.Validity | None = None,
        pin: object = None,
    ) -> dict[str, Any]:
        _detail, direct, embedded = await self._snapshot()
        records = direct + embedded
        self._safe_sync(records, user_id)
        targets = [record for record in direct if record.user_id == user_id]
        if not targets or any(
            record.validity.kind == "recurring" for record in targets
        ):
            raise ReadOnly("This access schedule is read-only")
        by_slot = {record.slot: record for record in targets}
        if any(
            record.slot in by_slot and record.user_id != user_id for record in records
        ):
            raise RequestFailed("A PIN slot belongs to another user")
        if access is None:
            pin = self._validate_pin(pin, records, user_id)
            first = by_slot[min(by_slot)].validity
            if any(
                record.validity.kind == "temporary"
                and (
                    record.validity.start is None
                    or record.validity.end is None
                    or record.validity.end <= record.validity.start
                )
                or not mapping.same_validity(record.validity, first)
                for record in by_slot.values()
            ):
                raise ReadOnly("This access schedule is read-only")
        elif any(record.pin is None for record in by_slot.values()):
            raise RequestFailed("The existing PIN is unavailable for this change")
        chosen = by_slot[min(by_slot)]
        expected = access or chosen.validity
        pending = {
            "operation": "update",
            "slot": chosen.slot,
            "kind": expected.kind,
            "from": mapping.utc_ms(expected.start) if expected.start else None,
            "to": mapping.utc_ms(expected.end) if expected.end else None,
        }
        if user_id in self._ownership:
            self._ownership[user_id]["pending"] = pending
            await self._store.async_save(self._ownership)
        else:
            self._imported_pending[user_id] = pending
        self._rows[user_id]["status"] = "pending"
        for record in by_slot.values():
            body = {
                "type": "pin",
                "slot": str(record.slot),
                "state": "update",
                "action": "intent",
                "pin": record.pin if access is not None else pin,
                **(access or record.validity).body(),
            }
            try:
                await self._client.intent(user_id, body)
            finally:
                body.clear()
        await self._client.sync()
        for record in by_slot.values():
            await self._poll_loaded(
                user_id,
                record.slot,
                record.pin if access is not None else pin,
                access or record.validity,
            )
        await self._refresh(full=False)
        return {"user": dict(self._rows[user_id])}

    async def end(self, user: MappedUser, user_id: object) -> dict[str, Any]:
        self._user(user)
        if user.own is not None and user_id == user.own:
            raise Protected(
                "You can't change your own access period or end your own access here"
            )
        if isinstance(user_id, str) and user_id in self._protected_users:
            raise Protected if user.role == "admin" else NotFound
        async with self._lock:
            if not self._ready:
                await self._refresh()
            self._target(user, user_id)
            _detail, direct, embedded = await self._snapshot()
            records = direct + embedded
            self._safe_sync(records, user_id)
            targets = [record for record in records if record.user_id == user_id]
            slots = {record.slot for record in targets}
            pending = self._ownership.get(user_id, {}).get(
                "pending"
            ) or self._imported_pending.get(user_id, {})
            reserved_slot = pending.get("slot")
            if reserved_slot is not None:
                if type(reserved_slot) is not int or not 0 <= reserved_slot <= 10000:
                    raise RequestFailed("The pending credential slot is invalid")
                slots.add(reserved_slot)
            if any(
                record.slot in slots and record.user_id != user_id for record in records
            ):
                raise RequestFailed("A PIN slot belongs to another user")
            ending = {
                "operation": "end",
                "slot": min(slots) if slots else None,
            }
            if user_id in self._ownership:
                self._ownership[user_id]["pending"] = ending
                await self._store.async_save(self._ownership)
            else:
                self._imported_pending[user_id] = ending
            self._rows[user_id]["status"] = "pending"
            for slot in sorted(slots):
                await self._client.intent(
                    user_id,
                    {
                        "type": "pin",
                        "slot": str(slot),
                        "state": "delete",
                        "action": "intent",
                    },
                )
            if slots:
                await self._client.sync()

                async def absent() -> bool:
                    _, direct_now, embedded_now = await self._snapshot()
                    return not any(
                        record.user_id == user_id
                        for record in direct_now + embedded_now
                    )

                await self._poll(absent)
            await self._refresh(full=False)
            if user_id in self._ownership and user_id in self._rows:
                raise Pending
            return {"ok": True}
