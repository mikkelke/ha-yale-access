"""HTTP requests use the proven app routes without leaking bodies."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import ClientError

from custom_components.yale_access.client import APP_API_KEY, USER_AGENT, YaleClient
from custom_components.yale_access.errors import (
    Pending,
    RequestFailed,
    UpstreamUnavailable,
)
from custom_components.yale_access.session import SessionFile
from tests.test_session import token, write_session

LOCK_ID = "placeholder-lock"
USER_ID = "placeholder-user"
HOUSE_ID = "placeholder-house"


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self.body = body if body is not None else {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        return json.dumps(self.body).encode()


class Session:
    _retry_connection = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def client(tmp_path, *responses, country="US"):
    path = tmp_path / "session.json"
    write_session(path, token(60))
    session_file = SessionFile(str(path))
    http = Session(*responses)
    return YaleClient(http, session_file, LOCK_ID, country=country), http, session_file


@pytest.mark.asyncio
@pytest.mark.parametrize("country", ["US", "DK"])
async def test_routes_headers_and_bodies(tmp_path, country):
    api, http, _ = client(
        tmp_path,
        *(Response() for _ in range(6)),
        Response(body={"id": USER_ID}),
        country=country,
    )
    await api.detail()
    await api.pins()
    await api.profile(USER_ID)
    await api.activities(HOUSE_ID, "placeholder+cursor")
    credential = {"type": "pin", "slot": "1", "state": "load", "action": "intent"}
    await api.intent(USER_ID, credential)
    await api.sync()
    assert await api.create_user("Guest placeholder") == USER_ID
    assert http._retry_connection is False
    for _, _, options in http.calls:
        headers = options["headers"]
        assert headers["x-api-key"] == APP_API_KEY
        assert headers["x-branding"] == "yale"
        assert headers["x-country"] == country
        assert headers["User-Agent"] == USER_AGENT
        assert not headers["x-access-token"].startswith("Bearer ")
        assert options["allow_redirects"] is False
        assert options["timeout"].total == 25
    assert http.calls[2][2]["headers"]["accept-version"] == "2.0.0"
    assert http.calls[3][2]["headers"]["accept-version"] == "4.0.0"
    assert parse_qs(urlsplit(str(http.calls[3][1])).query) == {
        "limit": ["100"],
        "end": ["placeholder+cursor"],
    }
    assert str(http.calls[4][1]).endswith(
        f"/locks/{LOCK_ID}/users/{USER_ID}/credentials"
    )
    assert http.calls[4][2]["json"] == credential
    assert str(http.calls[5][1]).endswith(
        "/credentials/sync?notificationType=websocket"
    )
    assert http.calls[5][2]["json"] == {"hideActivityLog": False}
    assert http.calls[6][2]["json"] == {
        "lockID": LOCK_ID,
        "credentialType": "pin",
        "firstName": "Guest",
        "lastName": "placeholder",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "first_name", "last_name"),
    [("Guest", "Guest", "."), ("Guest van Placeholder", "Guest", "van Placeholder")],
)
async def test_creation_sends_nonempty_first_and_last_names(
    tmp_path, name, first_name, last_name
):
    api, http, _ = client(tmp_path, Response(body={"id": USER_ID}))
    assert await api.create_user(name) == USER_ID
    assert http.calls[0][2]["json"]["firstName"] == first_name
    assert http.calls[0][2]["json"]["lastName"] == last_name


@pytest.mark.asyncio
async def test_every_response_rotates_session_before_handling_status(tmp_path):
    rotated = token(120)
    api, _, session = client(
        tmp_path, Response(status=403, headers={"x-access-token": rotated})
    )
    with pytest.raises(RequestFailed):
        await api.intent(USER_ID, {})
    assert session.token == rotated
    assert session.state == "ok"
    assert (
        json.loads((tmp_path / "session.json").read_text())["access_token"] == rotated
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_read_auth_failure_marks_login_failed(tmp_path, status):
    api, http, session = client(tmp_path, Response(status=status))
    with pytest.raises(RequestFailed):
        await api.pins()
    assert session.state == "failed"
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_write_403_is_permission_problem(tmp_path):
    api, _, session = client(tmp_path, Response(status=403))
    with pytest.raises(RequestFailed, match="permission"):
        await api.sync()
    assert session.state == "ok"


@pytest.mark.asyncio
async def test_write_401_marks_failed(tmp_path):
    api, _, session = client(tmp_path, Response(status=401))
    with pytest.raises(RequestFailed):
        await api.sync()
    assert session.state == "failed"


@pytest.mark.asyncio
async def test_transport_failure_does_not_retry_or_leak(tmp_path, caplog):
    synthetic_pin = str(10**5 + 47)
    api, http, _ = client(tmp_path, ClientError(synthetic_pin))
    with caplog.at_level("DEBUG"), pytest.raises(Pending) as error:
        await api.intent(USER_ID, {"pin": synthetic_pin})
    assert len(http.calls) == 1
    assert synthetic_pin not in str(error.value)
    assert synthetic_pin not in caplog.text
    assert error.value.__suppress_context__


@pytest.mark.asyncio
async def test_http_error_body_is_never_exposed(tmp_path, caplog):
    synthetic_pin = str(10**5 + 59)
    api, _, _ = client(tmp_path, Response(status=500, body={"message": synthetic_pin}))
    with caplog.at_level("DEBUG"), pytest.raises(UpstreamUnavailable) as error:
        await api.pins()
    assert synthetic_pin not in str(error.value)
    assert synthetic_pin not in caplog.text


@pytest.mark.asyncio
async def test_ambiguous_created_identity_is_pending(tmp_path):
    api, _, _ = client(
        tmp_path, Response(body={"id": USER_ID, "userID": "another-placeholder"})
    )
    with pytest.raises(Pending, match="Inspect the Yale app"):
        await api.create_user("Guest placeholder")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_server_write_failure_remains_pending_without_retry(tmp_path, status):
    api, http, session = client(tmp_path, Response(status=status))
    with pytest.raises(Pending, match="outcome is unresolved"):
        await api.sync()
    assert len(http.calls) == 1
    assert session.state == "ok"


@pytest.mark.asyncio
async def test_invalid_creation_identity_field_is_not_ignored(tmp_path):
    api, _, _ = client(tmp_path, Response(body={"id": USER_ID, "UserID": 1}))
    with pytest.raises(Pending):
        await api.create_user("Guest placeholder")
