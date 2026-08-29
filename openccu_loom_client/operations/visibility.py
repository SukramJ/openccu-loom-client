# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Parameter-visibility (un-ignore) REST operations (``/visibility``).

The daemon hides expert/internal parameters by default; un-ignore
patterns surface specific ones per central.
"""

from __future__ import annotations

from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.wire.rest import (
    UnIgnoreCandidateList,
    UnIgnoreListResponse,
    UnIgnoreUpdateRequest,
    UnIgnoreUpdateResponse,
)


class VisibilityOperations(_OperationsBase):
    """Manage un-ignore patterns for otherwise-hidden parameters."""

    async def get_unignore(self) -> UnIgnoreListResponse:
        """
        List active un-ignore patterns per central.

        Wire: ``GET /visibility/unignore``.
        """
        payload = await self._transport.request(method="GET", path="/visibility/unignore")
        return UnIgnoreListResponse.model_validate(payload)

    async def put_unignore(self, *, request: UnIgnoreUpdateRequest) -> UnIgnoreUpdateResponse:
        """
        Replace the un-ignore pattern list (admin).

        Wire: ``PUT /visibility/unignore``.
        """
        payload = await self._transport.request(
            method="PUT",
            path="/visibility/unignore",
            json_body=self._to_json_body(request),
            allow_retry=True,
        )
        return UnIgnoreUpdateResponse.model_validate(payload)

    async def get_unignore_candidates(self) -> UnIgnoreCandidateList:
        """
        List hidden parameters that could be un-ignored.

        Wire: ``GET /visibility/unignore/candidates``.
        """
        payload = await self._transport.request(method="GET", path="/visibility/unignore/candidates")
        return UnIgnoreCandidateList.model_validate(payload)
