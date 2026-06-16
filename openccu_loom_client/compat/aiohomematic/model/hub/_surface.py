# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Shared entity surface for hub data points.

The :class:`_HubEntitySurface` mixin carries the category / registration
/ enabled_default plumbing every hub data point (sysvar, program and the
hub singletons) exposes to the HA component. It lives in its own module
so both ``hub/__init__.py`` (sysvar/program twins) and
``hub/singletons.py`` (alarm/service/inbox/metrics/connectivity/update/
install-mode singletons) can mix it in without import cycles.
"""

from __future__ import annotations

from typing import ClassVar

from openccu_loom_types.enums import DataPointCategory


class _HubEntitySurface:
    """Shared entity surface for hub data points."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubSensor

    @classmethod
    def default_category(cls) -> DataPointCategory:
        """Return the HA data-point category for this class."""
        return cls._category

    @property
    def category(self) -> DataPointCategory:
        """Return the HA data-point category of this instance."""
        return self._category

    @property
    def is_registered(self) -> bool:
        """Return whether this hub entity has been registered with HA."""
        return getattr(self, "_registered", False)

    def register(self) -> None:
        """Mark this hub entity as registered with HA."""
        self._registered = True

    def unregister(self) -> None:
        """Mark this hub entity as no longer registered with HA."""
        self._registered = False

    @property
    def enabled_default(self) -> bool:
        """
        Return whether the entity is enabled by default.

        Mirrors aiohomematic: hub entities default to disabled unless a
        configured description marker matched (the resolver sets the
        flag at build time).
        """
        return getattr(self, "_enabled_default", False)

    def set_enabled_default(self, *, enabled: bool) -> None:
        """Record the marker-resolved enabled_default flag."""
        self._enabled_default = enabled

    @property
    def state_uncertain(self) -> bool:
        """Return whether the current state is considered uncertain."""
        return False


__all__ = ["_HubEntitySurface"]
