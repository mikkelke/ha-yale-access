from __future__ import annotations

import asyncio
import copy
import json
import secrets
from datetime import UTC, datetime

import pytest

from custom_components.yale_access import mapping
from custom_components.yale_access.errors import (
    Forbidden,
    InvalidRequest,
    NotFound,
    Pending,
    Protected,
    ReadOnly,
    RequestFailed,
    UpstreamUnavailable,
)
from custom_components.yale_access.service import MappedUser, UserMap, YaleAccessService

ADMIN = MappedUser("a" * 32, "admin")
GUEST_A = MappedUser("b" * 32, "guests")
GUEST_B = MappedUser("c" * 32, "guests")
UID_A = "placeholder_guest_a"
UID_B = "placeholder_guest_b"
UID_IMPORTED = "placeholder_imported"
NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)


def empty_pins():
    return {collection: [] for collection in mapping.COLLECTIONS}


def record(uid, slot, collection="loaded", **extra):
    return {
        "userID": uid,
        "slot": slot,
        "state": collection,
        "accessType": "always",
        "pin": str(secrets.randbelow(900000) + 100000),
        "unverified": True,
        **extra,
    }


class Store:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data)
        self.saves = []

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.data = copy.deepcopy(data)
        self.saves.append(copy.deepcopy(data))


class Clock:
    def __init__(self):
        self.time = 0

    def __call__(self):
        return self.time

    async def sleep(self, seconds):
        self.time += seconds
        await asyncio.sleep(0)


class Client:
    lock_id = "placeholder_lock"

    def __init__(self):
        self.body = empty_pins()
        self.embedded = None
        self.members = {}
        self.calls = []
        self.accept_sync = True
        self.write_error = None
        self.profile_error = None
        self.created = "placeholder_created"
        self.pages = {}

    async def detail(self):
        self.calls.append(("detail",))
        return {
            "LockID": self.lock_id,
            "HouseID": "placeholder_house",
            "users": copy.deepcopy(self.members),
            "pins": copy.deepcopy(
                self.embedded if self.embedded is not None else self.body
            ),
        }

    async def pins(self):
        self.calls.append(("pins",))
        return copy.deepcopy(self.body)

    async def profile(self, uid):
        self.calls.append(("profile", uid))
        if self.profile_error:
            raise self.profile_error
        return {"FirstName": "Guest", "LastName": "Placeholder"}

    async def activities(self, house_id, end=None):
        self.calls.append(("activities", end))
        return copy.deepcopy(self.pages.get(end, {"events": [], "nextPage": None}))

    async def create_user(self, name):
        self.calls.append(("create", name))
        self.members[self.created] = {"FirstName": name, "UserType": "Guest"}
        return self.created

    async def intent(self, uid, body):
        self.calls.append(("intent", uid, copy.deepcopy(body)))
        if self.write_error:
            raise self.write_error
        state = body["state"]
        if state == "load":
            self.body["created"].append(
                record(
                    uid,
                    int(body["slot"]),
                    "created",
                    **{
                        key: body[key]
                        for key in ("pin", "accessType", "accessTimes")
                        if key in body
                    },
                )
            )
        elif state == "update":
            for values in self.body.values():
                for entry in values:
                    if entry["userID"] == uid and entry["slot"] == int(body["slot"]):
                        entry.update(
                            {
                                key: body[key]
                                for key in ("pin", "accessType", "accessTimes")
                                if key in body
                            }
                        )
                        if body["accessType"] == "always":
                            entry.pop("accessTimes", None)
                            entry.pop("accessStartTime", None)
                            entry.pop("accessEndTime", None)
        elif state == "delete":
            pin_only = all(
                entry.get("unverified")
                for values in self.body.values()
                for entry in values
                if entry["userID"] == uid
            )
            for collection in list(self.body):
                self.body[collection] = [
                    entry
                    for entry in self.body[collection]
                    if not (
                        entry["userID"] == uid and entry["slot"] == int(body["slot"])
                    )
                ]
            if pin_only and not any(
                entry["userID"] == uid
                for values in self.body.values()
                for entry in values
            ):
                self.members.pop(uid, None)

    async def sync(self):
        self.calls.append(("sync",))
        if self.accept_sync:
            for collection in list(self.body):
                if collection in mapping.PENDING:
                    for entry in self.body[collection]:
                        entry["state"] = "loaded"
                        self.body["loaded"].append(entry)
                    self.body[collection] = []

    async def remove_user(self, uid):
        self.calls.append(("remove", uid))
        self.members.pop(uid, None)


def ownership(uid, creator):
    return {
        uid: {
            "created_by": creator.ha_user_id,
            "created_at": mapping.utc_ms(NOW),
            "name": "Guest Placeholder",
        }
    }


async def make_service(client=None, data=None, *, protected_users=(), users=None):
    client = client or Client()
    store = Store(data)
    clock = Clock()

    async def display_name(uid):
        return "Creator Placeholder" if uid == GUEST_A.ha_user_id else None

    service = YaleAccessService(
        client,
        UserMap(users if users is not None else [ADMIN, GUEST_A, GUEST_B]),
        store,
        display_name,
        protected_users=protected_users,
        clock=clock,
        sleep=clock.sleep,
        now=lambda: NOW,
        poll_budget=30,
    )
    await service.load()
    return service, client, store


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [ADMIN, GUEST_A, GUEST_B])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("command", ["set_validity", "end", "pin", "set_pin"])
async def test_protected_users_refused_before_any_yale_request(user, ready, command):
    client = Client()
    client.body["loaded"] = [record(UID_A, 1)]
    service, client, store = await make_service(
        client, ownership(UID_A, GUEST_A), protected_users=[UID_A]
    )
    if ready:
        await service.refresh()
    client.calls.clear()
    expected = Protected if user is ADMIN else NotFound
    with pytest.raises(expected):
        await getattr(service, command)(
            user, UID_A, *(["654321"] if command == "set_pin" else [])
        )
    assert client.calls == []
    assert store.saves == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "guests"])
@pytest.mark.parametrize("found", [False, True])
@pytest.mark.parametrize("protected", [False, True])
async def test_me_returns_cached_own_row_and_only_refreshes_once(
    role, found, protected
):
    user = MappedUser(ADMIN.ha_user_id, role, own=UID_IMPORTED)
    client = Client()
    client.body["loaded"] = [record(UID_A, 1, pin="123456")]
    if found:
        client.body["loaded"].append(record(UID_IMPORTED, 2, pin="654321"))
    service, client, store = await make_service(
        client, users=[user], protected_users=[UID_IMPORTED] if protected else []
    )
    result = await service.me(user)
    assert result["role"] == role
    assert result["pin_length"] == 6
    assert sum(call[0] == "activities" for call in client.calls) == 1
    if found:
        assert result["own"] == {
            "user_id": UID_IMPORTED,
            "name": "Guest Placeholder",
            "slot": 2,
            "kind": "always",
            "from": None,
            "to": None,
            "status": "active",
            "last_use": None,
            "created_by": None,
        }
        result["own"]["name"] = "Changed outside the cache"
    else:
        assert result["own"] is None
    client.calls.clear()
    cached = await service.me(user)
    assert cached["own"] is None or cached["own"]["name"] == "Guest Placeholder"
    assert client.calls == []
    assert store.saves == []
    assert "123456" not in json.dumps(cached)
    assert "654321" not in json.dumps(cached)


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [ADMIN, GUEST_A])
async def test_me_returns_null_without_own_mapping(user):
    service, _, _ = await make_service()
    assert await service.me(user) == {
        "role": user.role,
        "pin_length": 6,
        "own": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "guests"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("protected", [False, True])
async def test_own_pin_can_be_read_and_changed(role, ready, protected, caplog):
    user = MappedUser(ADMIN.ha_user_id, role, own=UID_IMPORTED)
    client = Client()
    client.body["loaded"] = [record(UID_IMPORTED, 1, pin="123456")]
    service, client, store = await make_service(
        client, users=[user], protected_users=[UID_IMPORTED] if protected else []
    )
    if ready:
        await service.refresh()
    with caplog.at_level("DEBUG"):
        assert await service.pin(user, UID_IMPORTED) == {"pin": "123456"}
        client.calls.clear()
        result = await service.set_pin(user, UID_IMPORTED, "654321")
        assert result["user"]["user_id"] == UID_IMPORTED
        assert result["user"]["kind"] == "always"
        assert not any(call[0] == "activities" for call in client.calls)
        assert await service.pin(user, UID_IMPORTED) == {"pin": "654321"}
    assert store.saves == []
    assert "123456" not in json.dumps(result)
    assert "654321" not in json.dumps(result)
    assert "123456" not in caplog.text
    assert "654321" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [ADMIN, GUEST_B])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("command", ["pin", "set_pin", "set_validity", "end"])
async def test_protected_own_user_remains_protected_from_others(caller, ready, command):
    resident = MappedUser(GUEST_A.ha_user_id, "guests", own=UID_IMPORTED)
    client = Client()
    client.body["loaded"] = [record(UID_IMPORTED, 1)]
    service, client, store = await make_service(
        client, users=[ADMIN, resident, GUEST_B], protected_users=[UID_IMPORTED]
    )
    if ready:
        await service.refresh()
    client.calls.clear()
    with pytest.raises(Protected if caller is ADMIN else NotFound):
        await getattr(service, command)(
            caller, UID_IMPORTED, *(["654321"] if command == "set_pin" else [])
        )
    assert client.calls == []
    assert store.saves == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "guests"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("command", ["set_validity", "end"])
async def test_own_access_period_and_end_refused_before_any_yale_request(
    role, ready, protected, command
):
    user = MappedUser(ADMIN.ha_user_id, role, own=UID_IMPORTED)
    client = Client()
    client.body["loaded"] = [record(UID_IMPORTED, 1)]
    service, client, store = await make_service(
        client, users=[user], protected_users=[UID_IMPORTED] if protected else []
    )
    if ready:
        await service.refresh()
    client.calls.clear()
    with pytest.raises(Protected) as error:
        await getattr(service, command)(user, UID_IMPORTED)
    assert error.value.message == (
        "You can't change your own access period or end your own access here"
    )
    assert client.calls == []
    assert store.saves == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pin", "set_pin", "set_validity", "end"])
@pytest.mark.parametrize("caller", [ADMIN, GUEST_B])
async def test_other_residents_own_user_follows_normal_scope_rules(command, caller):
    resident = MappedUser(GUEST_A.ha_user_id, "guests", own=UID_IMPORTED)
    client = Client()
    client.body["loaded"] = [record(UID_IMPORTED, 1)]
    service, client, _ = await make_service(client, users=[ADMIN, resident, GUEST_B])
    await service.refresh()
    client.calls.clear()
    args = ["654321"] if command == "set_pin" else []
    if command == "set_validity":
        args = ["2026-10-30T12:00:00+01:00", "2026-10-31T12:00:00+01:00"]
    if caller is ADMIN:
        await getattr(service, command)(caller, UID_IMPORTED, *args)
        assert client.calls
    else:
        with pytest.raises(NotFound):
            await getattr(service, command)(caller, UID_IMPORTED, *args)
        assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "guests"])
@pytest.mark.parametrize("protected", [False, True])
async def test_own_mapping_does_not_change_list_scope(role, protected):
    original = MappedUser(GUEST_A.ha_user_id, role, (UID_A,))
    resident = MappedUser(GUEST_A.ha_user_id, role, (UID_A,), own=UID_IMPORTED)
    client = Client()
    client.body["loaded"] = [record(UID_A, 1), record(UID_IMPORTED, 2)]
    options = {"protected_users": [UID_IMPORTED] if protected else []}
    service, _, _ = await make_service(client, users=[original], **options)
    expected = await service.list_users(original)
    service, _, _ = await make_service(client, users=[resident], **options)
    assert await service.list_users(resident) == expected
    assert service.users.owner(UID_IMPORTED) is None
    assert {row["user_id"] for row in expected["users"]} == (
        {UID_A, UID_IMPORTED} if role == "admin" and not protected else {UID_A}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("user", [ADMIN, GUEST_A, GUEST_B])
async def test_lists_hide_protected_users_including_pending(protected, user):
    client = Client()
    client.body["loaded"] = [record(UID_A, 1), record(UID_B, 2)]
    data = {
        **ownership(UID_A, user),
        **ownership(UID_B, user),
        **ownership(UID_IMPORTED, user),
    }
    data[UID_IMPORTED]["pending"] = {"operation": "create", "kind": "always"}
    service, _, _ = await make_service(
        client, data, protected_users=[UID_A, UID_IMPORTED] if protected else []
    )
    rows = {row["user_id"]: row for row in (await service.list_users(user))["users"]}
    assert set(rows) == ({UID_B} if protected else {UID_A, UID_B, UID_IMPORTED})
    assert all("protected" not in row for row in rows.values())
    if not protected:
        assert rows[UID_IMPORTED]["status"] == "pending"


@pytest.mark.asyncio
async def test_assigned_imported_guest_is_visible_and_manageable_only_by_owner():
    assigned = MappedUser(GUEST_A.ha_user_id, "guests", (UID_IMPORTED,))
    client = Client()
    client.body["loaded"] = [record(UID_A, 1), record(UID_IMPORTED, 2)]
    service, client, store = await make_service(
        client, ownership(UID_A, GUEST_A), users=[ADMIN, assigned, GUEST_B]
    )
    rows = (await service.list_users(assigned))["users"]
    assert {row["user_id"] for row in rows} == {UID_A, UID_IMPORTED}
    assert (await service.list_users(GUEST_B))["users"] == []
    admin_rows = {
        row["user_id"]: row for row in (await service.list_users(ADMIN))["users"]
    }
    assert admin_rows[UID_IMPORTED]["created_by"] == "Creator Placeholder"
    assert store.saves == []
    before = len(client.calls)
    with pytest.raises(NotFound):
        await service.set_validity(
            GUEST_B,
            UID_IMPORTED,
            "2026-10-30T12:00:00+01:00",
            "2026-10-31T12:00:00+01:00",
        )
    with pytest.raises(NotFound):
        await service.end(GUEST_B, UID_IMPORTED)
    assert len(client.calls) == before
    result = await service.set_validity(
        assigned,
        UID_IMPORTED,
        "2026-10-30T12:00:00+01:00",
        "2026-10-31T12:00:00+01:00",
    )
    assert result["user"]["kind"] == "temporary"
    assert result["user"]["created_by"] == "Creator Placeholder"
    assert await service.end(assigned, UID_IMPORTED) == {"ok": True}
    assert {
        row["user_id"] for row in (await service.list_users(assigned))["users"]
    } == {UID_A}
    assert UID_IMPORTED not in store.data
    assert store.saves == []


@pytest.mark.asyncio
async def test_assignment_adds_access_without_changing_stored_creator():
    assigned = MappedUser(GUEST_A.ha_user_id, "guests", (UID_IMPORTED,))
    data = ownership(UID_IMPORTED, GUEST_B)
    data[UID_IMPORTED]["pending"] = {"operation": "create", "kind": "always"}
    service, _, store = await make_service(data=data, users=[ADMIN, assigned, GUEST_B])
    for user in (ADMIN, assigned, GUEST_B):
        rows = (await service.list_users(user))["users"]
        assert len(rows) == 1
        assert rows[0]["user_id"] == UID_IMPORTED
        assert rows[0]["created_by"] == "Creator Placeholder"
        assert rows[0]["status"] == "pending"
    assert store.data == data
    assert store.saves == []


def test_user_map_rejects_assignment_to_multiple_ha_users():
    with pytest.raises(ValueError, match="assigned to multiple HA users"):
        UserMap(
            [
                MappedUser(GUEST_A.ha_user_id, "guests", (UID_IMPORTED,)),
                MappedUser(GUEST_B.ha_user_id, "guests", (UID_IMPORTED,)),
            ]
        )


@pytest.mark.parametrize(
    "users",
    [
        [
            MappedUser(ADMIN.ha_user_id, "admin", own=UID_IMPORTED),
            MappedUser(GUEST_A.ha_user_id, "guests", own=UID_IMPORTED),
        ],
        [MappedUser(ADMIN.ha_user_id, "admin", (UID_IMPORTED,), own=UID_IMPORTED)],
        [
            MappedUser(ADMIN.ha_user_id, "admin", own=UID_IMPORTED),
            MappedUser(GUEST_A.ha_user_id, "guests", (UID_IMPORTED,)),
        ],
        [
            MappedUser(ADMIN.ha_user_id, "admin", (UID_IMPORTED,)),
            MappedUser(GUEST_A.ha_user_id, "guests", own=UID_IMPORTED),
        ],
        [MappedUser(ADMIN.ha_user_id, "admin", (UID_IMPORTED, UID_IMPORTED))],
    ],
)
def test_user_map_rejects_duplicate_own_and_assigned_yale_users(users):
    with pytest.raises(ValueError, match="assigned to multiple HA users"):
        UserMap(users)


@pytest.mark.asyncio
async def test_roles_filter_before_serialization_and_cross_owner_targets_are_missing():
    client = Client()
    client.body["loaded"] = [
        record(UID_A, 1),
        record(UID_B, 2),
        record(UID_IMPORTED, 3),
    ]
    service, client, _ = await make_service(
        client, {**ownership(UID_A, GUEST_A), **ownership(UID_B, GUEST_B)}
    )
    await service.refresh()
    assert len((await service.list_users(ADMIN))["users"]) == 3
    assert [row["user_id"] for row in (await service.list_users(GUEST_A))["users"]] == [
        UID_A
    ]
    assert (await service.list_users(GUEST_A))["users"][0][
        "created_by"
    ] == "Creator Placeholder"
    for target in (UID_B, UID_IMPORTED, "placeholder_missing"):
        before = len(client.calls)
        with pytest.raises(NotFound):
            await service.set_validity(
                GUEST_A,
                target,
                "2026-10-30T12:00:00+01:00",
                "2026-10-31T12:00:00+01:00",
            )
        with pytest.raises(NotFound):
            await service.end(GUEST_A, target)
        assert len(client.calls) == before


@pytest.mark.asyncio
async def test_unknown_users_and_forged_role_are_denied_for_every_service_command():
    service, client, _ = await make_service()
    for unknown in (
        MappedUser("d" * 32, "admin"),
        MappedUser(GUEST_A.ha_user_id, "admin"),
        MappedUser(GUEST_A.ha_user_id, "guests", own=UID_A),
    ):
        for call in (
            lambda unknown=unknown: service.me(unknown),
            lambda unknown=unknown: service.list_users(unknown),
            lambda unknown=unknown: service.create(unknown, "Guest Placeholder"),
            lambda unknown=unknown: service.set_validity(unknown, UID_A),
            lambda unknown=unknown: service.end(unknown, UID_A),
            lambda unknown=unknown: service.pin(unknown, UID_A),
            lambda unknown=unknown: service.set_pin(unknown, UID_A, "654321"),
        ):
            with pytest.raises(Forbidden):
                await call()
    assert client.calls == []


@pytest.mark.asyncio
async def test_create_returns_pin_once_and_persists_immutable_owner_before_load(caplog):
    service, client, store = await make_service()
    result = await service.create(
        GUEST_A,
        "Guest Placeholder",
        "2026-10-30T12:00:00+01:00",
        "2026-10-31T12:00:00+01:00",
    )
    uid = result["user"]["user_id"]
    assert result["pin"].isdigit()
    assert result["user"]["status"] == "scheduled"
    assert store.data[uid]["created_by"] == GUEST_A.ha_user_id
    assert "pending" not in store.data[uid]
    assert "pending" in store.saves[0][uid]
    assert result["pin"] not in json.dumps(store.saves)
    assert result["pin"] not in json.dumps(await service.list_users(ADMIN))
    assert result["pin"] not in caplog.text
    assert len([call for call in client.calls if call[0] == "create"]) == 1
    assert (await service.list_users(GUEST_B))["users"] == []


@pytest.mark.asyncio
async def test_guests_role_cannot_create_or_set_always_but_admin_can():
    service, client, _ = await make_service()
    with pytest.raises(InvalidRequest):
        await service.create(GUEST_A, "Guest Placeholder")
    assert not [call for call in client.calls if call[0] == "create"]
    result = await service.create(
        GUEST_A,
        "Guest Placeholder",
        "2026-10-30T12:00:00+01:00",
        "2026-10-31T12:00:00+01:00",
    )
    with pytest.raises(InvalidRequest):
        await service.set_validity(GUEST_A, result["user"]["user_id"])
    updated = await service.set_validity(ADMIN, result["user"]["user_id"])
    assert updated["user"]["kind"] == "always"
    client.created = "placeholder_created_admin"
    assert (await service.create(ADMIN, "Admin Placeholder"))["user"][
        "kind"
    ] == "always"


@pytest.mark.asyncio
async def test_users_are_listed_by_name():
    data = {**ownership(UID_A, GUEST_A), **ownership(UID_B, GUEST_B)}
    for uid, name in ((UID_A, "Zebra"), (UID_B, "alpha")):
        data[uid]["name"] = name
        data[uid]["pending"] = {"operation": "create", "kind": "always"}
    service, _, _ = await make_service(data=data)
    names = [row["name"] for row in (await service.list_users(ADMIN))["users"]]
    assert names == ["alpha", "Zebra"]


@pytest.mark.asyncio
async def test_create_timeout_preserves_pending_and_reconciles_after_restart():
    service, client, store = await make_service()
    client.accept_sync = False
    with pytest.raises(Pending, match="one-time PIN"):
        await service.create(
            GUEST_A,
            "Guest Placeholder",
            "2026-10-29T12:00:00+01:00",
            "2026-10-31T12:00:00+01:00",
        )
    assert (await service.list_users(GUEST_A))["users"][0]["status"] == "pending"
    restarted, _, _ = await make_service(client, store.data)
    await restarted.refresh()
    assert (await restarted.list_users(GUEST_A))["users"][0]["status"] == "pending"
    client.accept_sync = True
    await client.sync()
    await restarted.refresh()
    assert (await restarted.list_users(GUEST_A))["users"][0]["status"] == "active"
    assert len([call for call in client.calls if call[0] == "create"]) == 1


@pytest.mark.asyncio
async def test_create_with_known_name_does_not_fetch_profile():
    service, client, store = await make_service()
    client.profile_error = UpstreamUnavailable()
    result = await service.create(
        GUEST_A,
        "Guest Placeholder",
        "2026-10-30T12:00:00+01:00",
        "2026-10-31T12:00:00+01:00",
    )
    assert result["pin"] == client.body["loaded"][0]["pin"]
    assert result["user"] == service._rows[client.created]
    assert result["user"]["name"] == "Guest Placeholder"
    assert result["user"]["status"] == "scheduled"
    assert "pending" not in store.data[client.created]
    assert not any(call[0] == "profile" for call in client.calls)
    assert result["pin"] not in json.dumps(store.data)


@pytest.mark.asyncio
async def test_create_reconciliation_store_failure_returns_confirmed_pin():
    service, client, store = await make_service()
    save = store.async_save

    async def fail_reconciliation(data):
        if store.saves:
            raise OSError("placeholder failure")
        await save(data)

    store.async_save = fail_reconciliation
    result = await service.create(
        GUEST_A,
        "Guest Placeholder",
        "2026-10-30T12:00:00+01:00",
        "2026-10-31T12:00:00+01:00",
    )
    assert result["pin"] == client.body["loaded"][0]["pin"]
    assert result["user"]["name"] == "Guest Placeholder"
    assert result["user"]["status"] == "pending"
    assert "pending" in store.data[client.created]
    assert result["pin"] not in json.dumps(store.data)


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_users", [[], [UID_IMPORTED]])
async def test_slot_selection_unions_embedded_pending_and_direct_reservations(
    protected_users,
):
    service, client, _ = await make_service(protected_users=protected_users)
    client.body["loaded"] = [record(UID_IMPORTED, 1)]
    client.embedded = empty_pins()
    client.embedded["disabled"] = [record("placeholder_embedded", 2, "disabled")]
    listed = {row["user_id"] for row in (await service.list_users(ADMIN))["users"]}
    assert (UID_IMPORTED in listed) is (not protected_users)
    result = await service.create(
        GUEST_A,
        "Guest Placeholder",
        "2026-10-30T12:00:00+01:00",
        "2026-10-31T12:00:00+01:00",
    )
    assert result["user"]["slot"] == 3
    client.embedded["created"] = [record("placeholder_pending", 4, "created")]
    before = len([call for call in client.calls if call[0] == "create"])
    with pytest.raises(RequestFailed):
        await service.create(
            GUEST_B,
            "Guest Placeholder",
            "2026-10-30T12:00:00+01:00",
            "2026-10-31T12:00:00+01:00",
        )
    assert len([call for call in client.calls if call[0] == "create"]) == before


@pytest.mark.asyncio
async def test_wrong_loaded_validity_does_not_finish_poll():
    service, client, _ = await make_service()
    pin = record(UID_A, 1)["pin"]
    client.body["loaded"] = [record(UID_A, 1, pin=pin)]
    with pytest.raises(Pending):
        await service._poll_loaded(
            UID_A,
            1,
            pin,
            mapping.validity("2026-10-30T00:00:00Z", "2026-10-31T00:00:00Z"),
        )


@pytest.mark.asyncio
async def test_set_validity_uses_update_intent_and_never_returns_existing_pin():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1)]
    service, client, store = await make_service(client, ownership(UID_A, GUEST_A))
    await service.refresh()
    pin = client.body["loaded"][0]["pin"]
    result = await service.set_validity(
        GUEST_A, UID_A, "2026-10-30T12:00:00+01:00", "2026-10-31T12:00:00+01:00"
    )
    intent = next(call[2] for call in client.calls if call[0] == "intent")
    assert intent["state"] == "update"
    assert intent["pin"] == pin
    assert "DTSTART=2026-10-30T11:00:00.000Z" in intent["accessTimes"]
    assert pin not in json.dumps(result)
    assert pin not in json.dumps(store.data)


@pytest.mark.asyncio
async def test_recurring_is_read_only_for_validity_but_can_be_ended():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1, accessType="recurring")]
    service, client, _ = await make_service(client, ownership(UID_A, GUEST_A))
    await service.refresh()
    with pytest.raises(ReadOnly):
        await service.set_validity(
            GUEST_A, UID_A, "2026-10-30T12:00:00+01:00", "2026-10-31T12:00:00+01:00"
        )
    assert await service.end(GUEST_A, UID_A) == {"ok": True}


@pytest.mark.asyncio
async def test_end_pin_only_does_not_delete_membership_and_tombstone_stays_absent():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1)]
    client.members[UID_A] = {"FirstName": "Guest"}
    service, client, store = await make_service(client, ownership(UID_A, GUEST_A))
    await service.refresh()
    assert await service.end(GUEST_A, UID_A) == {"ok": True}
    assert not any(call[0] == "remove" for call in client.calls)
    assert store.data[UID_A]["created_by"] == GUEST_A.ha_user_id
    restarted, _, _ = await make_service(client, store.data)
    await restarted.refresh()
    assert (await restarted.list_users(ADMIN))["users"] == []


@pytest.mark.asyncio
async def test_end_app_only_user_preserves_membership_without_writes():
    client = Client()
    client.members[UID_IMPORTED] = {"FirstName": "Guest", "UserType": "Guest"}
    service, client, _ = await make_service(client)
    await service.refresh()
    assert await service.end(ADMIN, UID_IMPORTED) == {"ok": True}
    assert UID_IMPORTED in client.members
    assert not any(call[0] in {"intent", "sync", "remove"} for call in client.calls)
    assert service._imported_pending == {}


@pytest.mark.asyncio
async def test_activities_paginate_end_and_filter_lock_action_and_90_day_boundary():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1), record(UID_B, 2)]
    timestamp = int(NOW.timestamp() * 1000)

    def event(uid, stamp, **extra):
        return {
            "user": {"UserID": uid},
            "timestamp": stamp,
            "deviceID": client.lock_id,
            "action": "pin_unlock",
            **extra,
        }

    client.pages = {
        None: {
            "events": [
                event(UID_A, timestamp),
                event(UID_B, timestamp, action="lock"),
                event(UID_B, timestamp, deviceID="placeholder_other_lock"),
            ],
            "nextPage": "https://api.aaecosystem.com/houses/placeholder_house/activities?end=cursor_placeholder&limit=100",
        },
        "cursor_placeholder": {
            "events": [event(UID_B, timestamp - 1000)],
            "nextPage": "https://api.aaecosystem.com/path?end=unused",
        },
    }
    service, client, _ = await make_service(client)
    await service.refresh()
    rows = (await service.list_users(ADMIN))["users"]
    assert all(row["last_use"] is not None for row in rows)
    assert [(call[1]) for call in client.calls if call[0] == "activities"] == [
        None,
        "cursor_placeholder",
    ]


def test_user_map_rejects_duplicate_and_unknown_ids():
    users = UserMap([ADMIN, GUEST_A])
    assert users.require(ADMIN.ha_user_id) == ADMIN
    for uid in (None, [], "d" * 32):
        with pytest.raises(Forbidden):
            users.require(uid)
    with pytest.raises(ValueError):
        UserMap([ADMIN, ADMIN])


@pytest.mark.asyncio
async def test_local_pending_slot_blocks_new_create_and_foreign_sync_after_restart():
    data = {**ownership(UID_A, GUEST_A), **ownership(UID_B, GUEST_B)}
    data[UID_A]["pending"] = {
        "operation": "create",
        "slot": 1,
        "kind": "always",
        "from": None,
        "to": None,
    }
    client = Client()
    client.body["loaded"] = [record(UID_B, 2)]
    service, client, _ = await make_service(client, data)
    await service.refresh()
    with pytest.raises(RequestFailed):
        await service.create(
            GUEST_B,
            "Guest Placeholder",
            "2026-10-30T12:00:00+01:00",
            "2026-10-31T12:00:00+01:00",
        )
    with pytest.raises(RequestFailed):
        await service.set_validity(
            GUEST_B, UID_B, "2026-10-30T12:00:00+01:00", "2026-10-31T12:00:00+01:00"
        )
    with pytest.raises(RequestFailed):
        await service.end(GUEST_B, UID_B)
    assert not any(call[0] in {"create", "intent", "sync"} for call in client.calls)


@pytest.mark.asyncio
async def test_imported_validity_change_reconciles_without_claiming_ownership():
    client = Client()
    client.body["loaded"] = [record(UID_IMPORTED, 1)]
    service, client, store = await make_service(client)
    await service.refresh()
    result = await service.set_validity(ADMIN, UID_IMPORTED)
    assert result["user"]["status"] == "active"
    assert UID_IMPORTED not in (store.data or {})
    assert service._imported_pending == {}


@pytest.mark.asyncio
async def test_multiple_pin_slots_can_be_updated_and_ended():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1), record(UID_A, 2)]
    service, client, _ = await make_service(client, ownership(UID_A, GUEST_A))
    await service.refresh()
    result = await service.set_validity(
        GUEST_A, UID_A, "2026-10-29T12:00:00+01:00", "2026-10-31T12:00:00+01:00"
    )
    assert result["user"]["status"] == "active"
    assert await service.end(GUEST_A, UID_A) == {"ok": True}
    intents = [call[2] for call in client.calls if call[0] == "intent"]
    assert {(body["state"], body["slot"]) for body in intents} == {
        ("update", "1"),
        ("update", "2"),
        ("delete", "1"),
        ("delete", "2"),
    }


@pytest.mark.asyncio
async def test_foreign_user_same_slot_blocks_target_update_and_end():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1)]
    client.embedded = empty_pins()
    client.embedded["loaded"] = [record(UID_B, 1)]
    service, client, _ = await make_service(client, ownership(UID_A, GUEST_A))
    await service.refresh()
    with pytest.raises(RequestFailed):
        await service.set_validity(
            GUEST_A, UID_A, "2026-10-30T12:00:00+01:00", "2026-10-31T12:00:00+01:00"
        )
    with pytest.raises(RequestFailed):
        await service.end(GUEST_A, UID_A)
    assert not any(call[0] in {"intent", "sync"} for call in client.calls)


@pytest.mark.asyncio
async def test_end_app_user_with_pin_deletes_only_pin_and_preserves_membership():
    client = Client()
    client.members[UID_IMPORTED] = {"FirstName": "Guest", "UserType": "Guest"}
    client.body["loaded"] = [record(UID_IMPORTED, 1, unverified=False)]
    service, client, store = await make_service(client)
    await service.refresh()
    assert await service.end(ADMIN, UID_IMPORTED) == {"ok": True}
    assert UID_IMPORTED in client.members
    assert not any(client.body.values())
    assert not any(call[0] == "remove" for call in client.calls)
    intent_index = next(
        index for index, call in enumerate(client.calls) if call[0] == "intent"
    )
    assert client.calls[intent_index][2] == {
        "type": "pin",
        "slot": "1",
        "state": "delete",
        "action": "intent",
    }
    sync_index = client.calls.index(("sync",))
    assert intent_index < sync_index
    assert any(call[0] == "pins" for call in client.calls[sync_index + 1 :])
    assert (await service.list_users(ADMIN))["users"][0]["status"] != "pending"
    assert service._imported_pending == {}
    assert UID_IMPORTED not in (store.data or {})


@pytest.mark.asyncio
async def test_end_imported_user_waits_for_pin_absence_and_reconciles_with_member():
    client = Client()
    client.members[UID_IMPORTED] = {"FirstName": "Guest", "UserType": "Guest"}
    client.body["loaded"] = [record(UID_IMPORTED, 1, unverified=False)]
    client.embedded = copy.deepcopy(client.body)
    service, client, store = await make_service(client)
    await service.refresh()
    with pytest.raises(Pending):
        await service.end(ADMIN, UID_IMPORTED)
    assert (await service.list_users(ADMIN))["users"][0]["status"] == "pending"
    client.embedded = empty_pins()
    await service.refresh()
    assert service._imported_pending == {}
    assert (await service.list_users(ADMIN))["users"][0]["status"] != "pending"
    assert UID_IMPORTED in client.members
    assert not any(call[0] == "remove" for call in client.calls)
    assert UID_IMPORTED not in (store.data or {})


@pytest.mark.asyncio
async def test_end_waits_for_absence_in_embedded_collections_too():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1)]
    client.embedded = copy.deepcopy(client.body)
    service, client, store = await make_service(client, ownership(UID_A, GUEST_A))
    await service.refresh()
    with pytest.raises(Pending):
        await service.end(GUEST_A, UID_A)
    assert store.data[UID_A]["pending"]["operation"] == "end"
    client.embedded = empty_pins()
    restarted, _, _ = await make_service(client, store.data)
    await restarted.refresh()
    assert (await restarted.list_users(GUEST_A))["users"] == []


@pytest.mark.asyncio
async def test_activity_search_stops_at_90_days_without_using_older_unlock():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1)]
    old = int(NOW.timestamp() * 1000) - 91 * 86400 * 1000
    client.pages = {
        None: {
            "events": [
                {
                    "user": {"UserID": UID_A},
                    "timestamp": old,
                    "deviceID": client.lock_id,
                    "action": "pin_unlock",
                }
            ],
            "nextPage": "https://api.aaecosystem.com/path?end=unused",
        },
    }
    service, client, _ = await make_service(client)
    await service.refresh()
    assert (await service.list_users(ADMIN))["users"][0]["last_use"] is None
    assert [call for call in client.calls if call[0] == "activities"] == [
        ("activities", None)
    ]


@pytest.mark.asyncio
async def test_end_pending_create_revokes_reserved_slot_before_pin_becomes_visible():
    data = ownership(UID_A, GUEST_A)
    data[UID_A]["pending"] = {
        "operation": "create",
        "slot": 3,
        "kind": "always",
        "from": None,
        "to": None,
    }
    client = Client()
    client.members[UID_A] = {"FirstName": "Guest", "UserType": "Guest"}
    service, client, store = await make_service(client, data)
    await service.refresh()
    assert await service.end(GUEST_A, UID_A) == {"ok": True}
    intents = [call for call in client.calls if call[0] == "intent"]
    assert intents == [
        (
            "intent",
            UID_A,
            {"type": "pin", "slot": "3", "state": "delete", "action": "intent"},
        )
    ]
    assert ("sync",) in client.calls
    assert not any(call[0] == "remove" for call in client.calls)
    assert "pending" not in store.data[UID_A]


@pytest.mark.asyncio
async def test_end_pending_reserved_slot_conflict_is_refused_without_writes():
    data = ownership(UID_A, GUEST_A)
    data[UID_A]["pending"] = {
        "operation": "create",
        "slot": 3,
        "kind": "always",
        "from": None,
        "to": None,
    }
    client = Client()
    client.body["loaded"] = [record(UID_B, 3)]
    service, client, _ = await make_service(client, data)
    await service.refresh()
    with pytest.raises(RequestFailed, match="another user"):
        await service.end(GUEST_A, UID_A)
    assert not any(call[0] in {"intent", "sync", "remove"} for call in client.calls)


@pytest.mark.asyncio
async def test_end_does_not_report_success_while_final_refresh_still_has_pending_user():
    client = Client()
    client.members[UID_A] = {"FirstName": "Guest", "UserType": "Guest"}
    client.body["loaded"] = [record(UID_A, 1)]
    original_intent = client.intent

    async def intent(uid, body):
        await original_intent(uid, body)
        client.members[uid] = {"FirstName": "Guest", "UserType": "Guest"}

    client.intent = intent
    service, client, store = await make_service(client, ownership(UID_A, GUEST_A))
    await service.refresh()
    with pytest.raises(Pending):
        await service.end(GUEST_A, UID_A)
    assert store.data[UID_A]["pending"]["operation"] == "end"
    assert (await service.list_users(GUEST_A))["users"][0]["status"] == "pending"
    assert not any(call[0] == "remove" for call in client.calls)


@pytest.mark.asyncio
async def test_poll_checks_every_second_then_every_three_seconds_within_budget():
    clock = Clock()
    sleeps = []
    checks = []

    async def sleep(seconds):
        sleeps.append(seconds)
        await clock.sleep(seconds)

    async def check():
        checks.append(clock())
        return False

    service = YaleAccessService(
        Client(), UserMap([ADMIN]), Store(), clock=clock, sleep=sleep
    )
    with pytest.raises(Pending):
        await service._poll(check)
    assert checks == list(range(16)) + list(range(18, 180, 3))
    assert sleeps == [1] * 15 + [3] * 55
    assert clock() == 180


@pytest.mark.asyncio
async def test_poll_clamps_sleep_and_uses_elapsed_time_for_slow_checks():
    clock = Clock()
    checks = []

    async def check():
        checks.append(clock())
        clock.time += 2
        return False

    service = YaleAccessService(
        Client(),
        UserMap([ADMIN]),
        Store(),
        clock=clock,
        sleep=clock.sleep,
        poll_budget=19,
    )
    with pytest.raises(Pending):
        await service._poll(check)
    assert checks == [0, 3, 6, 9, 12, 15]
    assert clock() == 19


@pytest.mark.asyncio
async def test_poll_returns_as_soon_as_lock_confirms():
    clock = Clock()
    service = YaleAccessService(
        Client(), UserMap([ADMIN]), Store(), clock=clock, sleep=clock.sleep
    )

    async def check():
        return clock() >= 2

    await service._poll(check)
    assert clock() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["create", "set_validity", "end", "set_pin"])
async def test_writes_skip_activity_and_known_profiles_and_preserve_last_use(command):
    client = Client()
    client.members[UID_A] = {"FirstName": "Existing", "UserType": "Guest"}
    client.body["loaded"] = [record(UID_A, 1, pin="123456", unverified=False)]
    client.pages[None] = {
        "events": [
            {
                "user": {"UserID": UID_A},
                "timestamp": NOW.timestamp() * 1000,
                "deviceID": client.lock_id,
                "action": "pin_unlock",
            }
        ]
    }
    service, client, _ = await make_service(
        client, ownership(UID_A, GUEST_A) if command != "end" else None
    )
    await service.refresh()
    assert any(call[0] == "activities" for call in client.calls)
    client.calls.clear()
    if command == "create":
        result = await service.create(ADMIN, "New Guest", pin="654321")
        assert result["user"]["last_use"] is None
    elif command == "set_validity":
        await service.set_validity(ADMIN, UID_A)
    elif command == "set_pin":
        await service.set_pin(ADMIN, UID_A, "654321")
    else:
        await service.end(ADMIN, UID_A)
    assert not any(call[0] in {"activities", "profile"} for call in client.calls)
    rows = {row["user_id"]: row for row in (await service.list_users(ADMIN))["users"]}
    assert rows[UID_A]["last_use"] == mapping.utc_ms(NOW)
    client.calls.clear()
    await service.refresh()
    assert any(call[0] == "activities" for call in client.calls)


@pytest.mark.asyncio
async def test_write_fetches_profile_only_for_new_unknown_users():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1)]
    service, client, _ = await make_service(client, ownership(UID_A, GUEST_A))
    await service.refresh()
    client.body["loaded"].append(record(UID_IMPORTED, 2))
    client.calls.clear()
    await service.set_validity(ADMIN, UID_A)
    assert [call for call in client.calls if call[0] == "profile"] == [
        ("profile", UID_IMPORTED)
    ]
    assert not any(call[0] == "activities" for call in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [ADMIN, GUEST_A])
async def test_pin_reads_fresh_loaded_record_with_lowest_slot(user, caplog):
    client = Client()
    client.body["loaded"] = [
        record(UID_A, 4, pin="444444"),
        record(UID_A, 2, pin="222222"),
    ]
    service, client, store = await make_service(client, ownership(UID_A, GUEST_A))
    await service.refresh()
    client.body["loaded"][1]["pin"] = "654321"
    client.calls.clear()
    with caplog.at_level("DEBUG"):
        assert await service.pin(user, UID_A) == {"pin": "654321"}
    assert client.calls == [("pins",)]
    assert "654321" not in json.dumps(store.saves)
    assert "654321" not in json.dumps(await service.list_users(user))
    assert "654321" not in caplog.text


@pytest.mark.asyncio
async def test_assigned_guest_can_read_and_change_pin_and_other_guest_cannot():
    assigned = MappedUser(GUEST_A.ha_user_id, "guests", (UID_IMPORTED,))
    client = Client()
    client.body["loaded"] = [record(UID_IMPORTED, 1, pin="123456")]
    service, client, store = await make_service(
        client, users=[ADMIN, assigned, GUEST_B]
    )
    await service.refresh()
    assert await service.pin(assigned, UID_IMPORTED) == {"pin": "123456"}
    client.calls.clear()
    with pytest.raises(NotFound):
        await service.pin(GUEST_B, UID_IMPORTED)
    with pytest.raises(NotFound):
        await service.set_pin(GUEST_B, UID_IMPORTED, "654321")
    assert client.calls == []
    result = await service.set_pin(assigned, UID_IMPORTED, "654321")
    assert result["user"]["status"] == "active"
    assert await service.pin(assigned, UID_IMPORTED) == {"pin": "654321"}
    assert service._imported_pending == {}
    assert store.saves == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collection,state,pin",
    [
        ("created", "created", "123456"),
        ("loaded", "update", "123456"),
        ("loaded", "loaded", None),
    ],
)
async def test_pin_requires_loaded_pin(collection, state, pin):
    client = Client()
    client.body[collection] = [record(UID_A, 1, collection, state=state, pin=pin)]
    service, _, _ = await make_service(client, ownership(UID_A, GUEST_A))
    with pytest.raises(NotFound):
        await service.pin(GUEST_A, UID_A)


@pytest.mark.asyncio
async def test_create_accepts_custom_pin_including_leading_zeroes(caplog):
    service, client, store = await make_service()
    with caplog.at_level("DEBUG"):
        result = await service.create(ADMIN, "Custom Guest", pin="012345")
    assert result["pin"] == "012345"
    intent = next(call[2] for call in client.calls if call[0] == "intent")
    assert intent["pin"] == "012345"
    assert "012345" not in json.dumps(store.saves)
    assert "012345" not in json.dumps(result["user"])
    assert "012345" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["create", "set_pin"])
@pytest.mark.parametrize(
    "pin",
    [
        "",
        "12345",
        "1234567",
        "abcdef",
        "12345 ",
        "12345\n",
        "１２３４５６",
        123456,
        True,
    ],
)
async def test_custom_pin_format_rejected_before_writes(command, pin):
    client = Client()
    client.body["loaded"] = [record(UID_A, 1, pin="123456")]
    service, client, store = await make_service(client, ownership(UID_A, GUEST_A))
    with pytest.raises(InvalidRequest, match="^The code must be 6 digits$"):
        if command == "create":
            await service.create(ADMIN, "Custom Guest", pin=pin)
        else:
            await service.set_pin(ADMIN, UID_A, pin)
    assert not any(call[0] in {"create", "intent", "sync"} for call in client.calls)
    assert store.saves == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["create", "set_pin"])
@pytest.mark.parametrize("embedded", [False, True])
async def test_duplicate_pin_rejected_before_writes_including_protected_records(
    command, embedded
):
    client = Client()
    client.body["loaded"] = [record(UID_A, 1, pin="123456")]
    source = client.body
    if embedded:
        client.embedded = empty_pins()
        source = client.embedded
    source["disabled"] = [record(UID_B, 2, "disabled", pin="654321")]
    service, client, store = await make_service(
        client, ownership(UID_A, GUEST_A), protected_users=[UID_B]
    )
    with pytest.raises(InvalidRequest, match="^That code is already in use$"):
        if command == "create":
            await service.create(ADMIN, "Custom Guest", pin="654321")
        else:
            await service.set_pin(ADMIN, UID_A, "654321")
    assert not any(call[0] in {"create", "intent", "sync"} for call in client.calls)
    assert store.saves == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["always", "temporary"])
async def test_set_pin_preserves_validity_and_pending_ownership(kind, caplog):
    client = Client()
    access = (
        mapping.validity()
        if kind == "always"
        else mapping.validity("2026-10-30T12:00:00+01:00", "2026-10-31T12:00:00+01:00")
    )
    client.body["loaded"] = [record(UID_A, 1, pin="123456", **access.body())]
    service, client, store = await make_service(client, ownership(UID_A, GUEST_A))
    with caplog.at_level("DEBUG"):
        result = await service.set_pin(GUEST_A, UID_A, "654321")
    intent = next(call[2] for call in client.calls if call[0] == "intent")
    assert intent == {
        "type": "pin",
        "slot": "1",
        "state": "update",
        "action": "intent",
        "pin": "654321",
        **access.body(),
    }
    assert client.calls.index(("sync",)) > next(
        i for i, call in enumerate(client.calls) if call[0] == "intent"
    )
    assert result["user"]["kind"] == kind
    assert result["user"]["status"] == ("active" if kind == "always" else "scheduled")
    assert store.saves[0][UID_A]["pending"]["operation"] == "update"
    assert "pending" not in store.data[UID_A]
    assert "654321" not in json.dumps(store.saves)
    assert "654321" not in json.dumps(result)
    assert "654321" not in caplog.text


@pytest.mark.asyncio
async def test_set_pin_refuses_slots_with_different_validities():
    client = Client()
    access = mapping.validity("2026-10-30T00:00:00Z", "2026-10-31T00:00:00Z")
    client.body["loaded"] = [
        record(UID_A, 1, pin="123456"),
        record(UID_A, 2, pin="234567", **access.body()),
    ]
    service, client, store = await make_service(client, ownership(UID_A, GUEST_A))
    with pytest.raises(ReadOnly):
        await service.set_pin(ADMIN, UID_A, "654321")
    assert not any(call[0] in {"intent", "sync"} for call in client.calls)
    assert store.saves == []


@pytest.mark.asyncio
async def test_set_pin_recurring_is_read_only_before_any_write():
    client = Client()
    client.body["loaded"] = [record(UID_A, 1, accessType="recurring")]
    service, client, store = await make_service(client, ownership(UID_A, GUEST_A))
    with pytest.raises(ReadOnly):
        await service.set_pin(ADMIN, UID_A, "654321")
    assert not any(call[0] in {"create", "intent", "sync"} for call in client.calls)
    assert store.saves == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, True])
async def test_set_pin_waits_for_new_code_with_correct_pending_bookkeeping(owned):
    client = Client()
    client.body["loaded"] = [record(UID_A, 1, pin="123456")]
    service, client, store = await make_service(
        client, ownership(UID_A, GUEST_A) if owned else None
    )
    await service.refresh()
    original_sleep = service._sleep

    async def intent(uid, body):
        client.calls.append(("intent", uid, copy.deepcopy(body)))

    async def sleep(seconds):
        pending = (
            store.data[UID_A]["pending"] if owned else service._imported_pending[UID_A]
        )
        assert pending["operation"] == "update"
        assert "654321" not in json.dumps(pending)
        await original_sleep(seconds)
        if service._clock() >= 2:
            client.body["loaded"][0]["pin"] = "654321"

    client.intent = intent
    service._sleep = sleep
    result = await service.set_pin(ADMIN, UID_A, "654321")
    assert service._clock() == 2
    assert result["user"]["status"] == "active"
    assert not service._imported_pending
    if owned:
        assert "pending" not in store.data[UID_A]
    else:
        assert store.saves == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["create", "set_pin"])
async def test_custom_pin_uses_length_from_current_records(command):
    client = Client()
    client.body["loaded"] = [record(UID_A, 1, pin="1234")]
    service, client, _ = await make_service(client, ownership(UID_A, GUEST_A))
    with pytest.raises(InvalidRequest, match="^The code must be 4 digits$"):
        if command == "create":
            await service.create(ADMIN, "Custom Guest", pin="654321")
        else:
            await service.set_pin(ADMIN, UID_A, "654321")
    assert not any(call[0] in {"create", "intent", "sync"} for call in client.calls)
    if command == "create":
        assert (await service.create(ADMIN, "Custom Guest", pin="6543"))[
            "pin"
        ] == "6543"
    else:
        await service.set_pin(ADMIN, UID_A, "6543")
        assert await service.pin(ADMIN, UID_A) == {"pin": "6543"}


async def test_configuration_choices_include_protected_keypad_and_app_users():
    client = Client()
    client.members[UID_IMPORTED] = {"FirstName": "App", "UserType": "Guest"}
    client.body["loaded"] = [record(UID_A, 3)]
    service, client, _ = await make_service(client, protected_users=[UID_A])
    choices = await service.async_user_choices()
    assert choices == {
        UID_IMPORTED: "Guest Placeholder (app user)",
        UID_A: "Guest Placeholder (keypad slot 3)",
    }
    assert UID_A not in {
        row["user_id"] for row in (await service.list_users(ADMIN))["users"]
    }
    client.calls.clear()
    assert await service.async_user_choices() == choices
    assert client.calls == []
