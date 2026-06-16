# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Program domain model — wraps ProgramSummary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openccu_loom_types.rest import ProgramSummary

if TYPE_CHECKING:
    from openccu_loom_client.store import LoomStore


class Program:
    """
    Store-aware wrapper around one CCU program.

    Programs are hub-level scripts exposed by the daemon (the
    CCU side calls them ``Programs``). They can be triggered
    server-side or by external clients; we observe execution via
    the ``hub.program_executed`` broadcast.
    """

    __slots__ = ("_store", "_summary")

    def __init__(self, *, summary: ProgramSummary, store: LoomStore) -> None:
        """Bind this wrapper to its wire summary and owning store."""
        self._summary = summary
        self._store = store

    @property
    def summary(self) -> ProgramSummary:
        """Return the backing wire summary."""
        return self._summary

    @property
    def id(self) -> str:
        """Return the program's CCU identifier."""
        return self._summary.id

    @property
    def name(self) -> str:
        """Return the program's display name."""
        return self._summary.name

    @property
    def description(self) -> str | None:
        """Return the program's description, or ``None`` if unset."""
        return self._summary.description

    @property
    def active(self) -> bool | None:
        """Return whether the program is enabled, or ``None`` if unknown."""
        return self._summary.active

    async def execute(self) -> None:
        """
        Trigger this program on the CCU.

        Wire: ``POST /programs/{id}/execute`` via
        :meth:`LoomStore.execute_program`.
        """
        await self._store.execute_program(program_id=self.id)

    def _replace_summary(self, *, summary: ProgramSummary) -> None:
        self._summary = summary

    def __repr__(self) -> str:
        """Return a debug representation with id and name."""
        return f"Program(id={self.id!r}, name={self.name!r})"
