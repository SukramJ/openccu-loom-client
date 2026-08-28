# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Tests for LoomConfig and the AuthMethod implementations."""

from __future__ import annotations

import pytest

from openccu_loom_client import BasicAuth, BearerAuth, LoomConfig, SessionAuth


class TestLoomConfig:
    # Cleartext and TLS default to the same port: the daemon serves one
    # listener (ADR 0044), so TLS is a property of it rather than a second
    # port. Both tests are kept so a future split fails loudly here.
    def test_default_port_for_tls(self) -> None:
        cfg = LoomConfig(host="x", auth=BearerAuth(token="t"))
        assert cfg.port == 8119
        assert cfg.http_base_url == "https://x:8119/api/v1"
        assert cfg.ws_url == "wss://x:8119/api/v1/events"

    def test_default_port_for_cleartext(self) -> None:
        cfg = LoomConfig(host="x", tls=False, auth=BearerAuth(token="t"))
        assert cfg.port == 8119
        assert cfg.http_base_url == "http://x:8119/api/v1"
        assert cfg.ws_url == "ws://x:8119/api/v1/events"

    def test_explicit_port_overrides_default(self) -> None:
        cfg = LoomConfig(host="x", port=9000, auth=BearerAuth(token="t"))
        assert cfg.port == 9000

    def test_create_central_url_omits_api_path(self) -> None:
        # Mirrors aiohomematic: scheme + host + port, no base_path.
        cfg = LoomConfig(host="x", auth=BearerAuth(token="t"))
        assert cfg.create_central_url() == "https://x:8119"

    def test_create_central_url_cleartext(self) -> None:
        cfg = LoomConfig(host="x", port=9000, tls=False, auth=BearerAuth(token="t"))
        assert cfg.create_central_url() == "http://x:9000"


class TestAuthMethods:
    def test_basic_auth_header(self) -> None:
        auth = BasicAuth(username="admin", password="secret")
        headers: dict[str, str] = {}
        auth.apply_to_headers(headers=headers)
        # base64("admin:secret") == "YWRtaW46c2VjcmV0"
        assert headers["Authorization"] == "Basic YWRtaW46c2VjcmV0"
        # Identity hint never includes the password.
        assert "secret" not in auth.identity_hint
        assert auth.identity_hint == "basic:admin"

    def test_bearer_auth_header_and_identity_hint(self) -> None:
        auth = BearerAuth(token="abcdef123456", label="ha")
        headers: dict[str, str] = {}
        auth.apply_to_headers(headers=headers)
        assert headers["Authorization"] == "Bearer abcdef123456"
        # Only the last 6 chars leak into logs.
        assert auth.identity_hint == "bearer:ha:…123456"
        assert "abcdef" not in auth.identity_hint

    def test_bearer_auth_short_token_masks_with_stars(self) -> None:
        auth = BearerAuth(token="abc")
        # Less than 6 chars → fully masked.
        assert "abc" not in auth.identity_hint

    def test_session_auth_cookie_header(self) -> None:
        auth = SessionAuth(cookie_value="sess123")
        headers: dict[str, str] = {}
        auth.apply_to_headers(headers=headers)
        assert headers["Cookie"] == "openccu_loom_session=sess123"

    def test_session_auth_preserves_existing_cookie(self) -> None:
        auth = SessionAuth(cookie_value="sess123")
        headers = {"Cookie": "csrf=xyz"}
        auth.apply_to_headers(headers=headers)
        assert "csrf=xyz" in headers["Cookie"]
        assert "openccu_loom_session=sess123" in headers["Cookie"]


@pytest.mark.parametrize(
    ("auth", "expected_prefix"),
    [
        (BasicAuth(username="u", password="p"), "Basic "),
        (BearerAuth(token="t"), "Bearer "),
    ],
)
def test_all_auths_produce_authorization_header(
    auth: BasicAuth | BearerAuth,
    expected_prefix: str,
) -> None:
    headers: dict[str, str] = {}
    auth.apply_to_headers(headers=headers)
    assert headers["Authorization"].startswith(expected_prefix)


class TestSecretRedaction:
    """The auto-generated dataclass repr must never render a secret (F1)."""

    def test_basic_auth_repr_hides_password(self) -> None:
        auth = BasicAuth(username="admin", password="s3cr3t-passw0rd")
        assert "s3cr3t-passw0rd" not in repr(auth)
        assert "s3cr3t-passw0rd" not in str(auth)
        assert repr(auth) == "BasicAuth(basic:admin)"

    def test_bearer_auth_repr_hides_token(self) -> None:
        auth = BearerAuth(token="abcdef-TOP-SECRET-123456", label="ha")
        assert "TOP-SECRET" not in repr(auth)
        assert "abcdef" not in repr(auth)
        # Only the six-char suffix hint survives.
        assert repr(auth) == "BearerAuth(bearer:ha:…123456)"

    def test_session_auth_repr_hides_cookie_value(self) -> None:
        auth = SessionAuth(cookie_value="cookieSECRETvalue")
        assert "cookieSECRETvalue" not in repr(auth)
        assert repr(auth) == "SessionAuth(session:openccu_loom_session)"

    @pytest.mark.parametrize(
        "auth",
        [
            BasicAuth(username="admin", password="s3cr3t-passw0rd"),
            BearerAuth(token="abcdef-TOP-SECRET-123456"),
            SessionAuth(cookie_value="cookieSECRETvalue"),
        ],
    )
    def test_config_repr_does_not_leak_auth_secret(self, auth: BasicAuth | BearerAuth | SessionAuth) -> None:
        # repr(config) recurses into the auth field; it must stay redacted.
        cfg = LoomConfig(host="ccu", auth=auth)
        for secret in ("s3cr3t-passw0rd", "TOP-SECRET", "cookieSECRETvalue"):
            assert secret not in repr(cfg)
