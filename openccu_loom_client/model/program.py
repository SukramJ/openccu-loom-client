# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Program domain model — wraps ProgramSummary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openccu_loom_types.rest import ProgramSummary

if TYPE_CHECKING:
    from openccu_loom_client.model.channel import Channel
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

    @property
    def execute_available(self) -> bool:
        """
        Return whether running this program would do anything.

        A CCU program is two controls: an activity flag deciding whether
        it reacts at all, and an execution that runs it once. The CCU
        refuses the execution while the flag is off — so a consumer
        offering "run now" should render that control unavailable rather
        than firing a call the CCU will reject.

        The daemon answers this (``execute_available``, api 3.12.0)
        instead of leaving every consumer to re-derive it from
        :attr:`active`: it is CCU semantics, not presentation. Fails
        **open** — an older daemon omits the field and a CCU that has not
        reported the flag yet leaves it unset, and in both cases a
        control must not be greyed out on missing information.
        """
        return self._summary.execute_available is not False

    # ---- device attachment ----

    @property
    def channel_address(self) -> str | None:
        """
        Return the canonical ``"ADDR:idx"`` channel this program is linked to.

        The daemon resolves the link from a device identifier matched in
        the program name. ``None`` means the program belongs to no device —
        HA consumers then attach the entity to the central hub device
        instead of a physical device.
        """
        return self._summary.channel or None

    @property
    def device_address(self) -> str | None:
        """
        Return the device part of :attr:`channel_address` (before ``":"``).

        HA consumers use it to group the entity under the owning physical
        device; ``None`` together with :attr:`channel_address` (the entity
        belongs on the central hub device).
        """
        return self._summary.device_address or None

    @property
    def channel(self) -> Channel | None:
        """
        Return the linked :class:`Channel` from the store, or ``None``.

        ``None`` when the program is not linked to a device (hub-level
        entity) or the linked channel is not loaded in the store.
        """
        if (channel_address := self.channel_address) is None:
            return None
        return self._store.get_channel_by_address(channel_address=channel_address)

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
