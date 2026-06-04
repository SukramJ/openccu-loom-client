# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Authentication + API-token provisioning REST operations.

Covers the ``/auth``-rooted endpoints: session login/logout, the
current-identity probe, and the two API-token surfaces (the legacy
``/auth/tokens`` and the v2 fingerprint-based ``/auth/tokens/v2``).

The OIDC browser-redirect endpoints (``/auth/oidc/start`` and
``/auth/oidc/callback``) are intentionally absent — they are
interactive browser flows, not machine-to-machine calls.
"""

from __future__ import annotations

from openccu_loom_types.rest import (
    CreateTokenResponse,
    Identity,
    TokenCreate,
    TokenCreated,
    TokenListEntry,
    TokenSummary,
    UserListEntry,
)

from openccu_loom_client.operations._base import _OperationsBase


class AuthOperations(_OperationsBase):
    """Session auth + API-token provisioning."""

    # ---- session ----

    async def login(self, *, username: str, password: str) -> Identity:
        """Exchange credentials for a session cookie.

        Wire: ``POST /auth/login``. Not retried.
        """
        payload = await self._transport.request(
            "POST",
            "/auth/login",
            json_body={"username": username, "password": password},
            allow_retry=False,
        )
        return Identity.model_validate(payload)

    async def logout(self) -> None:
        """Revoke the current session. Wire: ``POST /auth/logout``."""
        await self._transport.request("POST", "/auth/logout", allow_retry=False)

    async def me(self) -> Identity:
        """Current authenticated identity. Wire: ``GET /auth/me``."""
        payload = await self._transport.request("GET", "/auth/me")
        return Identity.model_validate(payload)

    async def list_users(self) -> list[UserListEntry]:
        """List configured Basic-auth users. Wire: ``GET /auth/users``."""
        payload = await self._transport.request("GET", "/auth/users")
        return [UserListEntry.model_validate(u) for u in (payload or [])]

    # ---- API tokens (legacy) ----

    async def list_tokens(self) -> list[TokenListEntry]:
        """List API tokens. Wire: ``GET /auth/tokens``."""
        payload = await self._transport.request("GET", "/auth/tokens")
        return [TokenListEntry.model_validate(tok) for tok in (payload or [])]

    async def create_token(self, *, subject: str, role: str) -> CreateTokenResponse:
        """Issue a new bearer token (admin).

        Wire: ``POST /auth/tokens``. The plaintext token is returned
        once, here, and never again — store it immediately.
        """
        payload = await self._transport.request(
            "POST",
            "/auth/tokens",
            json_body={"subject": subject, "role": role},
            allow_retry=False,
        )
        return CreateTokenResponse.model_validate(payload)

    async def delete_token(self, *, token_id: str) -> None:
        """Revoke an API token by id. Wire: ``DELETE /auth/tokens/{id}``."""
        await self._transport.request("DELETE", f"/auth/tokens/{token_id}")

    # ---- API tokens (v2, fingerprint-based) ----

    async def list_tokens_v2(self) -> list[TokenSummary]:
        """List API tokens with fingerprint + metadata.

        Wire: ``GET /auth/tokens/v2``.
        """
        payload = await self._transport.request("GET", "/auth/tokens/v2")
        return [TokenSummary.model_validate(tok) for tok in (payload or [])]

    async def create_token_v2(self, *, token: TokenCreate) -> TokenCreated:
        """Issue a new bearer token (v2). Wire: ``POST /auth/tokens/v2``."""
        payload = await self._transport.request(
            "POST",
            "/auth/tokens/v2",
            json_body=token.model_dump(mode="json", exclude_none=True),
            allow_retry=False,
        )
        return TokenCreated.model_validate(payload)

    async def delete_token_v2(self, *, fingerprint: str) -> None:
        """Revoke an API token by fingerprint.

        Wire: ``DELETE /auth/tokens/v2/{fingerprint}``.
        """
        await self._transport.request("DELETE", f"/auth/tokens/v2/{fingerprint}")
