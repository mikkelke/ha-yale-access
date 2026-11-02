"""The standalone helper follows password and verification flows privately."""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError

from tests.test_client import Response
from tests.test_session import token

SPEC = importlib.util.spec_from_file_location(
    "yale_login", Path(__file__).resolve().parents[1] / "tools/yale_login.py"
)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self.post(url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_password_only_session():
    access_token = token(120)
    session = Session(
        Response(body={"needVerify": False}, headers={"x-access-token": access_token})
    )
    result = await helper.login(
        session,
        "email",
        "placeholder-identifier",
        "placeholder-password",
        "placeholder-install",
        lambda: pytest.fail("Unexpected verification"),
    )
    assert result == access_token
    url, options = session.calls[0]
    assert url.endswith("/v2/session/signin")
    assert options["json"] == {
        "identifierType": "email",
        "identifier": "placeholder-identifier",
        "credential": "placeholder-password",
        "installID": "placeholder-install",
    }
    assert options["allow_redirects"] is False
    assert options["headers"]["x-country"] == "US"
    assert "x-access-token" not in options["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier_type", ["email", "phone"])
async def test_verification_uses_latest_step_token(identifier_type):
    access_token = token(120)
    session = Session(
        Response(
            body={"needVerify": True},
            headers={"x-step-token": "placeholder-first-step"},
        ),
        Response(headers={"x-step-token": "placeholder-next-step"}),
        Response(headers={"x-access-token": access_token}),
    )
    code = str(10**5 + 73)
    assert (
        await helper.login(
            session,
            identifier_type,
            "placeholder-identifier",
            "placeholder-password",
            "placeholder-install",
            lambda: code,
            country="DK",
        )
        == access_token
    )
    assert session.calls[1][0].endswith("/v2/validation/" + identifier_type)
    assert session.calls[1][1]["json"] == {
        "identifier": "placeholder-identifier",
        "channel": "sms",
    }
    assert session.calls[1][1]["headers"]["x-step-token"] == "placeholder-first-step"
    assert session.calls[2][0].endswith("/v2/validate/" + identifier_type)
    assert session.calls[2][1]["headers"]["x-step-token"] == "placeholder-next-step"
    assert session.calls[2][1]["json"] == {
        "identifier": "placeholder-identifier",
        "code": code,
    }
    assert "credential" not in session.calls[2][1]["json"]
    assert all(options["headers"]["x-country"] == "DK" for _, options in session.calls)


def test_session_save_is_private_and_reuses_install_id(tmp_path):
    path = tmp_path / "session.json"
    access_token = token(120)
    helper.save_session(path, access_token, "placeholder-install")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert helper._install_id(path) == "placeholder-install"
    assert json.loads(path.read_text())["access_token"] == access_token
    assert list(tmp_path.iterdir()) == [path]


def test_session_save_without_posix_file_apis(tmp_path, monkeypatch):
    portable_os = SimpleNamespace(**vars(helper.os))
    portable_os.name = "nt"
    del portable_os.fchmod
    portable_os.open = lambda *args: pytest.fail("Unexpected directory fsync")
    monkeypatch.setattr(helper, "os", portable_os)
    path = tmp_path / "session.json"
    access_token = token(120)
    helper.save_session(path, access_token, "placeholder-install")
    assert json.loads(path.read_text())["access_token"] == access_token


@pytest.mark.asyncio
async def test_rejection_never_exposes_response_body(caplog):
    synthetic_pin = str(10**5 + 83)
    session = Session(Response(status=403, body={"error": synthetic_pin}))
    with caplog.at_level("DEBUG"), pytest.raises(helper.LoginError) as error:
        await helper.login(
            session,
            "email",
            "placeholder-identifier",
            "placeholder-password",
            "placeholder-install",
            lambda: synthetic_pin,
        )
    assert synthetic_pin not in str(error.value)
    assert synthetic_pin not in caplog.text
    assert len(session.calls) == 1


def lock_response(lock_id, users):
    return Response(
        body={
            "LockID": lock_id,
            "LockName": "Entry",
            "HouseName": "Example house",
            "users": users,
            "pins": {"loaded": [{"pin": "654321"}]},
            "access_token": "private-body-token",
        }
    )


@pytest.mark.asyncio
async def test_listing_multiple_locks_profiles_country_and_rotation(
    tmp_path, capsys, caplog
):
    path = tmp_path / "session.json"
    original, rotated = token(60), token(120)
    helper.save_session(path, original, "placeholder-install")
    session = Session(
        Response(
            body={
                "placeholder-lock-one": {
                    "LockName": "Entry",
                    "HouseName": "Example house",
                },
                "placeholder-lock-two": {
                    "LockName": "Side door",
                    "HouseName": "Example house",
                },
            },
            headers={"x-access-token": rotated},
        ),
        lock_response(
            "placeholder-lock-one",
            {
                "placeholder-owner": {
                    "FirstName": "Example",
                    "LastName": "Owner",
                    "UserType": "superuser",
                    "pin": "654321",
                },
                "placeholder-guest": {"UserType": "user"},
            },
        ),
        Response(
            body={
                "FirstName": "Example",
                "LastName": "Guest",
                "pin": "654321",
                "access_token": "private-body-token",
            }
        ),
        Response(
            body={
                "loaded": [
                    {"userID": "placeholder-owner", "slot": 1, "pin": "654321"},
                    {
                        "userID": "placeholder-pin-user",
                        "firstName": "Keypad",
                        "lastName": "Only",
                        "slot": 4,
                        "pin": "654321",
                    },
                ],
                "created": [],
            }
        ),
        lock_response("placeholder-lock-two", {}),
        Response(body={"loaded": []}),
    )
    with caplog.at_level("DEBUG"):
        await helper.list_locks(session, path, "DK")
    output = capsys.readouterr().out
    for expected in (
        "placeholder-lock-one",
        "placeholder-lock-two",
        "Entry",
        "Example house",
        "placeholder-owner",
        "placeholder-guest",
        "Owner",
        "Guest",
        "superuser",
        "user",
        "placeholder-pin-user",
        "Keypad",
        '"pin only"; slot: 4',
    ):
        assert expected in output
    assert output.count("placeholder-owner") == 1
    for secret in (original, rotated, "654321", "private-body-token"):
        assert secret not in output
        assert secret not in caplog.text
    assert [url.removeprefix(helper.BASE_URL) for url, _ in session.calls] == [
        "/users/locks/mine",
        "/locks/placeholder-lock-one",
        "/users/placeholder-guest",
        "/locks/placeholder-lock-one/pins",
        "/locks/placeholder-lock-two",
        "/locks/placeholder-lock-two/pins",
    ]
    assert session.calls[0][1]["headers"]["x-access-token"] == original
    for _, options in session.calls[1:]:
        assert options["headers"]["x-access-token"] == rotated
        assert options["headers"]["x-country"] == "DK"
        assert options["allow_redirects"] is False
    assert session.calls[2][1]["headers"]["accept-version"] == "2.0.0"
    assert json.loads(path.read_text())["access_token"] == rotated
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_list_only_has_no_login_prompts(tmp_path, monkeypatch, capsys):
    path = tmp_path / "session.json"
    helper.save_session(path, token(120), "placeholder-install")
    session = Session(Response(body={}))
    monkeypatch.setattr(helper.aiohttp, "ClientSession", lambda **kwargs: session)
    monkeypatch.setattr(
        helper.getpass, "getpass", lambda *args: pytest.fail("Unexpected login prompt")
    )
    await helper._run(path, list_only=True)
    assert session.calls[0][0].endswith("/users/locks/mine")
    assert session.calls[0][1]["headers"]["x-country"] == "US"
    assert "No locks" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_login_saves_before_listing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "session.json"
    access_token = token(120)
    session = Session(
        Response(body={"needVerify": False}, headers={"x-access-token": access_token}),
        Response(
            body={
                "placeholder-lock": {"LockName": "Entry", "HouseName": "Example house"}
            }
        ),
        lock_response(
            "placeholder-lock",
            {
                "placeholder-owner": {
                    "FirstName": "Example",
                    "LastName": "Owner",
                    "UserType": "superuser",
                }
            },
        ),
        Response(body={"loaded": []}),
    )
    prompts = iter(["placeholder-identifier", "placeholder-password"])
    monkeypatch.setattr(helper.getpass, "getpass", lambda *args: next(prompts))
    monkeypatch.setattr(helper.aiohttp, "ClientSession", lambda **kwargs: session)
    await helper._run(path, "DK")
    output = capsys.readouterr().out
    assert "Yale session saved." in output
    assert "placeholder-owner" in output
    assert access_token not in output
    assert json.loads(path.read_text())["access_token"] == access_token
    assert all(options["headers"]["x-country"] == "DK" for _, options in session.calls)


@pytest.mark.parametrize(
    "country", ["dk", "Us", "USA", "U", " US", "US\n", "ÅB", "ＵＳ", ""]
)
def test_country_rejected_before_http(country):
    with pytest.raises(helper.argparse.ArgumentTypeError):
        helper._country(country)


def test_list_only_cli_works_without_tty(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(helper, "_run", run)
    monkeypatch.setattr(
        helper.sys,
        "argv",
        [
            "yale_login.py",
            "--list-only",
            "--session-file",
            "session.json",
            "--country",
            "DK",
        ],
    )
    monkeypatch.setattr(helper.sys.stdin, "isatty", lambda: False)
    assert helper.main() == 0
    run.assert_awaited_once_with(Path("session.json"), "DK", True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    ["not json", "[]", '{"access_token":"invalid","install_id":"placeholder-install"}'],
)
async def test_list_only_invalid_session_is_private(tmp_path, body):
    path = tmp_path / "session.json"
    path.write_text(body)
    session = Session()
    with pytest.raises(helper.LoginError, match="session file could not be read"):
        await helper.list_locks(session, path)
    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        Response(status=403, body={"pin": "654321"}),
        ClientError("654321"),
        Response(body={"https://untrusted.invalid/secret": {}}),
        Response(body={"placeholder-lock": []}),
    ],
)
async def test_listing_failures_are_sanitized(tmp_path, response, capsys, caplog):
    path = tmp_path / "session.json"
    helper.save_session(path, token(120), "placeholder-install")
    session = Session(response)
    with caplog.at_level("DEBUG"), pytest.raises(helper.LoginError) as error:
        await helper.list_locks(session, path)
    assert "654321" not in str(error.value)
    assert "654321" not in caplog.text
    assert capsys.readouterr().out == ""
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_listing_error_still_saves_rotated_token(tmp_path):
    path = tmp_path / "session.json"
    rotated = token(120)
    helper.save_session(path, token(60), "placeholder-install")
    session = Session(Response(status=403, headers={"x-access-token": rotated}))
    with pytest.raises(helper.LoginError):
        await helper.list_locks(session, path)
    assert json.loads(path.read_text())["access_token"] == rotated


@pytest.mark.asyncio
async def test_listing_fetches_missing_house_names_once(tmp_path, capsys):
    path = tmp_path / "session.json"
    helper.save_session(path, token(120), "placeholder-install")
    session = Session(
        Response(
            body={
                "placeholder-lock-one": {
                    "LockName": "Entry",
                    "HouseID": "placeholder-house",
                },
                "placeholder-lock-two": {
                    "LockName": "Side door",
                    "HouseID": "placeholder-house",
                },
            }
        ),
        Response(
            body={
                "LockID": "placeholder-lock-one",
                "HouseID": "placeholder-house",
                "users": {},
            }
        ),
        Response(body={"HouseName": "Example house"}),
        Response(body={"loaded": []}),
        Response(
            body={
                "LockID": "placeholder-lock-two",
                "HouseID": "placeholder-house",
                "users": {},
            }
        ),
        Response(body={"loaded": []}),
    )
    await helper.list_locks(session, path)
    assert capsys.readouterr().out.count('house: "Example house"') == 2
    assert [url for url, _ in session.calls if "/houses/" in url] == [
        helper.BASE_URL + "/houses/placeholder-house"
    ]
