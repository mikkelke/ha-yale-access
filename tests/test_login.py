"""The app protocol keeps credentials out of errors and logs."""

from __future__ import annotations

import json

import pytest
from aiohttp import ClientError

from custom_components.yale_access.client import APP_API_KEY, USER_AGENT
from custom_components.yale_access.login import LoginError, YaleLogin
from tests.test_client import Response, Session
from tests.test_session import token


async def test_password_login_and_lock_listing_rotation(caplog):
    original, rotated = token(60), token(120)
    http = Session(
        Response(body={"needVerify": False}, headers={"x-access-token": original}),
        Response(
            body={"placeholder-lock": {"LockName": "Entry"}},
            headers={"x-access-token": rotated},
        ),
    )
    login = YaleLogin(http, "DK", "placeholder-install")
    with caplog.at_level("DEBUG"):
        assert await login.signin("email", "placeholder-identifier", "secret-password")
        assert await login.locks() == {"placeholder-lock": "Entry"}
    assert login.token == rotated
    assert http._retry_connection is True
    method, url, options = http.calls[0]
    assert method == "POST"
    assert url.endswith("/v2/session/signin")
    assert options["json"] == {
        "identifierType": "email",
        "identifier": "placeholder-identifier",
        "credential": "secret-password",
        "installID": "placeholder-install",
    }
    assert "x-access-token" not in options["headers"]
    assert http.calls[1][0] == "GET"
    assert http.calls[1][2]["headers"]["x-access-token"] == original
    for _, _, kwargs in http.calls:
        assert kwargs["headers"]["x-country"] == "DK"
        assert kwargs["headers"]["x-api-key"] == APP_API_KEY
        assert kwargs["headers"]["User-Agent"] == USER_AGENT
        assert kwargs["headers"]["x-branding"] == "yale"
        assert kwargs["headers"]["accept-version"] == "0.0.1"
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"].total == 25
    for secret in ("secret-password", original, rotated):
        assert secret not in caplog.text
        assert secret not in repr(login)


@pytest.mark.parametrize("method", ["email", "phone"])
async def test_verification_uses_latest_step_token(method, caplog):
    access_token = token(120)
    http = Session(
        Response(
            body={"needVerify": True},
            headers={"x-step-token": "first-step", "x-access-token": access_token},
        ),
        Response(
            headers={"x-step-token": "second-step", "x-access-token": access_token}
        ),
        Response(headers={"x-access-token": access_token}),
    )
    login = YaleLogin(http, "US", "placeholder-install")
    with caplog.at_level("DEBUG"):
        assert not await login.signin(
            method, "placeholder-identifier", "secret-password"
        )
        assert login.token is None
        await login.verify(" 123456 ")
    assert login.token == access_token
    assert http.calls[1][1].endswith("/v2/validation/" + method)
    assert http.calls[1][2]["json"] == {
        "identifier": "placeholder-identifier",
        "channel": "sms",
    }
    assert http.calls[1][2]["headers"]["x-step-token"] == "first-step"
    assert http.calls[2][1].endswith("/v2/validate/" + method)
    assert http.calls[2][2]["headers"]["x-step-token"] == "second-step"
    assert http.calls[2][2]["json"] == {
        "identifier": "placeholder-identifier",
        "code": "123456",
    }
    assert login._identifier == ""
    assert login._step_token is None
    for secret in (
        "secret-password",
        "123456",
        access_token,
        "first-step",
        "second-step",
    ):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (Response(status=401, body={"secret": "secret-password"}), "invalid_auth"),
        (Response(status=403), "invalid_auth"),
        (Response(status=503), "cannot_connect"),
        (Response(status=429), "cannot_connect"),
        (Response(status=302), "unknown"),
        (ClientError("secret-password"), "cannot_connect"),
        (TimeoutError("secret-password"), "cannot_connect"),
        (Response(body=[]), "unknown"),
        (Response(body={"needIdentifier": True}), "invalid_auth"),
        (Response(body={"needVerify": False}), "unknown"),
        (Response(body={"needVerify": True}), "unknown"),
        (Response(body={}), "unknown"),
        (
            Response(
                body={"needVerify": False}, headers={"x-access-token": "bad-token"}
            ),
            "unknown",
        ),
    ],
)
async def test_login_errors_are_safe(response, error, caplog):
    http = Session(response)
    login = YaleLogin(http, "US", "placeholder-install")
    with caplog.at_level("DEBUG"), pytest.raises(LoginError) as raised:
        await login.signin("email", "placeholder-identifier", "secret-password")
    assert raised.value.code == error
    assert "secret-password" not in str(raised.value)
    assert "secret-password" not in caplog.text
    assert len(http.calls) == 1


@pytest.mark.parametrize("code", ["", "12345", "1234567", "１２３４５６", "abcdef"])
async def test_invalid_code_does_not_send_request(code):
    login = YaleLogin(Session(), "US", "placeholder-install")
    login._step_token = "step"
    with pytest.raises(LoginError) as raised:
        await login.verify(code)
    assert raised.value.code == "invalid_code"


async def test_invalid_code_can_be_retried():
    access_token = token(120)
    http = Session(
        Response(body={"needVerify": True}, headers={"x-step-token": "first-step"}),
        Response(headers={"x-step-token": "second-step"}),
        Response(status=401),
        Response(headers={"x-access-token": access_token}),
    )
    login = YaleLogin(http, "US", "placeholder-install")
    assert not await login.signin("email", "placeholder-identifier", "secret-password")
    with pytest.raises(LoginError) as raised:
        await login.verify("123456")
    assert raised.value.code == "invalid_code"
    await login.verify("654321")
    assert login.token == access_token


@pytest.mark.parametrize("body", [{"../invalid": {}}, {"placeholder-lock": []}])
async def test_invalid_lock_listing(body):
    login = YaleLogin(Session(Response(body=body)), "US", "placeholder-install")
    login.token = token(60)
    with pytest.raises(LoginError) as raised:
        await login.locks()
    assert raised.value.code == "unknown"


async def test_malformed_response_is_sanitized():
    response = Response()

    async def read():
        return b"private malformed response"

    response.read = read
    login = YaleLogin(Session(response), "US", "placeholder-install")
    with pytest.raises(LoginError) as raised:
        await login.signin("email", "placeholder-identifier", "secret-password")
    assert "private" not in str(raised.value)
    assert "secret-password" not in json.dumps(vars(login), default=str)
