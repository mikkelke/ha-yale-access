#!/usr/bin/env python3
"""Interactively save a Yale Home app session without printing credentials."""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

BASE_URL = "https://api.aaecosystem.com"
APP_API_KEY = "2fcb39a8-40b1-4f4e-bc58-1aeecc0670e0"
USER_AGENT = "Yale Home/2026.11.0 261013398 (Android; SDK 35; Home Assistant)"


class LoginError(Exception):
    pass


def _country(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{2}", value):
        raise argparse.ArgumentTypeError("country must be two uppercase letters")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
        raise LoginError("Yale returned an unexpected identifier")
    return value


def _expiry(token: str) -> float:
    try:
        encoded = token.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        exp = payload["exp"]
        if type(exp) not in (int, float) or exp <= 0:
            raise ValueError
        datetime.fromtimestamp(exp, UTC)
        return float(exp)
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, OverflowError):
        raise LoginError("Yale returned an invalid session") from None


def _install_id(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        value = data["install_id"]
        if isinstance(value, str) and value:
            return value
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        pass
    return str(uuid.uuid4())


def save_session(path: Path, access_token: str, install_id: str) -> None:
    data = {
        "access_token": access_token,
        "install_id": install_id,
        "saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "exp": _expiry(access_token),
    }
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".yale-session-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise LoginError("The session file could not be saved") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


async def _request(
    session: aiohttp.ClientSession,
    path: str,
    body: dict[str, Any] | None = None,
    step_token: str | None = None,
    *,
    country: str = "US",
    access_token: str | None = None,
    version: str = "0.0.1",
    save_token: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], str | None, str | None]:
    headers = {
        "x-api-key": APP_API_KEY,
        "x-branding": "yale",
        "x-country": _country(country),
        "accept-version": version,
        "User-Agent": USER_AGENT,
        "content-type": "application/json",
    }
    if step_token:
        headers["x-step-token"] = step_token
    if access_token:
        headers["x-access-token"] = access_token
    request = session.get if access_token else session.post
    try:
        async with request(
            BASE_URL + path,
            headers=headers,
            json=body,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=25),
        ) as response:
            rotated = response.headers.get("x-access-token")
            if rotated and save_token is not None:
                save_token(rotated)
            if not 200 <= response.status < 300:
                if access_token:
                    raise LoginError(
                        "Yale rejected the listing request; try logging in again"
                    )
                raise LoginError("Yale rejected the login step")
            raw = await response.read()
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise LoginError("Yale returned an unexpected response")
            return (
                payload,
                response.headers.get("x-step-token"),
                rotated,
            )
    except (
        aiohttp.ClientError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        UnicodeError,
    ):
        raise LoginError("The Yale request could not be completed") from None


async def login(
    session: aiohttp.ClientSession,
    identifier_type: str,
    identifier: str,
    password: str,
    install_id: str,
    get_code: Callable[[], str],
    country: str = "US",
) -> str:
    if identifier_type not in {"email", "phone"}:
        raise LoginError("Choose email or phone login")
    result, step_token, access_token = await _request(
        session,
        "/v2/session/signin",
        {
            "identifierType": identifier_type,
            "identifier": identifier,
            "credential": password,
            "installID": install_id,
        },
        country=country,
    )
    password = ""
    if result.get("needIdentifier"):
        raise LoginError(
            "Complete account setup in the Yale Home app before signing in"
        )
    if result.get("needVerify") is False:
        if not access_token:
            raise LoginError("Yale did not return a session")
        _expiry(access_token)
        return access_token
    if result.get("needVerify") is not True or not step_token:
        raise LoginError("Yale returned an unsupported login response")
    _, step_token, _ = await _request(
        session,
        "/v2/validation/" + identifier_type,
        {"identifier": identifier, "channel": "sms"},
        step_token,
        country=country,
    )
    if not step_token:
        raise LoginError("Yale did not return a verification session")
    code = get_code().strip()
    if len(code) != 6 or any(character not in "0123456789" for character in code):
        raise LoginError("Enter the six-digit verification code")
    try:
        _, _, access_token = await _request(
            session,
            "/v2/validate/" + identifier_type,
            {"identifier": identifier, "code": code},
            step_token,
            country=country,
        )
    finally:
        code = ""
    if not access_token:
        raise LoginError("Yale did not return a session")
    _expiry(access_token)
    return access_token


def _text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def list_locks(
    session: aiohttp.ClientSession, path: Path, country: str = "US"
) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        access_token, install_id = data["access_token"], data["install_id"]
        if (
            not isinstance(access_token, str)
            or not isinstance(install_id, str)
            or not install_id
        ):
            raise ValueError
        _expiry(access_token)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, LoginError):
        raise LoginError("The session file could not be read; log in again") from None

    def rotate(value: str) -> None:
        nonlocal access_token
        save_session(path, value, install_id)
        access_token = value

    async def get(resource: str, version: str = "0.0.1") -> dict[str, Any]:
        payload, _, _ = await _request(
            session,
            resource,
            country=country,
            access_token=access_token,
            version=version,
            save_token=rotate,
        )
        return payload

    locks = await get("/users/locks/mine")
    if not locks:
        print("No locks are available to this account.")
        return
    houses: dict[str, str] = {}
    for lock_id, summary in locks.items():
        lock_id = _identifier(lock_id)
        if not isinstance(summary, dict):
            raise LoginError("Yale returned an unexpected lock listing")
        detail = await get("/locks/" + lock_id)
        if detail.get("LockID") != lock_id or not isinstance(detail.get("users"), dict):
            raise LoginError("Yale returned unexpected lock details")
        lock_name = _text(detail, "LockName", "lockName") or _text(
            summary, "LockName", "lockName"
        )
        house_name = _text(detail, "HouseName", "houseName") or _text(
            summary, "HouseName", "houseName"
        )
        if not house_name:
            house_id = _identifier(detail.get("HouseID", summary.get("HouseID")))
            if house_id not in houses:
                house = await get("/houses/" + house_id)
                houses[house_id] = _text(house, "HouseName", "houseName")
            house_name = houses[house_id]
        print(
            f"Lock ID: {lock_id}; name: {json.dumps(lock_name)}; house: {json.dumps(house_name)}"
        )
        if not detail["users"]:
            print("  No users returned for this lock.")
        for user_id, member in detail["users"].items():
            user_id = _identifier(user_id)
            if not isinstance(member, dict):
                raise LoginError("Yale returned an unexpected lock user")
            profile = member
            if not _text(member, "FirstName", "firstName"):
                profile = await get("/users/" + user_id, "2.0.0")
            first = _text(profile, "FirstName", "firstName")
            last = _text(profile, "LastName", "lastName")
            user_type = _text(member, "UserType", "userType") or _text(
                profile, "UserType", "userType"
            )
            print(
                f"  Yale user ID: {user_id}; first name: {json.dumps(first)}; last name: {json.dumps(last)}; type: {json.dumps(user_type)}"
            )
        listed = set(detail["users"])
        pins = await get("/locks/" + lock_id + "/pins")
        for entries in pins.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                user_id = entry.get("userID")
                if not isinstance(user_id, str) or user_id in listed:
                    continue
                listed.add(_identifier(user_id))
                first = _text(entry, "firstName", "FirstName")
                last = _text(entry, "lastName", "LastName")
                print(
                    f'  Yale user ID: {user_id}; first name: {json.dumps(first)}; last name: {json.dumps(last)}; type: "pin only"; slot: {entry.get("slot")}'
                )


async def _run(path: Path, country: str = "US", list_only: bool = False) -> None:
    async with aiohttp.ClientSession(trust_env=False) as session:
        if hasattr(session, "_retry_connection"):
            session._retry_connection = False
        if not list_only:
            identifier = getpass.getpass(
                "Yale email or phone number (hidden): "
            ).strip()
            if not identifier:
                raise LoginError("An account identifier is required")
            identifier_type = "email" if "@" in identifier else "phone"
            password = getpass.getpass("Yale password: ")
            if not password:
                raise LoginError("A password is required")
            install_id = _install_id(path)
            access_token = await login(
                session,
                identifier_type,
                identifier,
                password,
                install_id,
                lambda: getpass.getpass("Yale verification code: "),
                country,
            )
            save_session(path, access_token, install_id)
            print("Yale session saved.")
        await list_locks(session, path, country)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-file", default="/config/.yale_app_session.json")
    parser.add_argument("--country", type=_country, default="US")
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List locks and users using the saved session",
    )
    args = parser.parse_args()
    if not args.list_only and not sys.stdin.isatty():
        print("Run this helper in an interactive terminal.", file=sys.stderr)
        return 1
    try:
        asyncio.run(_run(Path(args.session_file), args.country, args.list_only))
    except LoginError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Login cancelled.", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - Exception details may contain credentials.
        print("Login could not be completed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
