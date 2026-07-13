# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Aiohomematic-compatible exception aliases."""

from __future__ import annotations

from aiohomematic.exceptions import BaseHomematicException

from openccu_loom_client.exceptions import LoomAuthError, LoomTransportError, LoomValidationError

# ``BaseHomematicException`` is re-exported *verbatim* from aiohomematic rather
# than aliased to ``BaseLoomException``. Since ``BaseLoomException`` now derives
# from it (see :mod:`openccu_loom_client.exceptions`), the upstream class is the
# strict superset: it catches loom failures **and** any genuine aiohomematic
# error leaking out of the aiohomematic code the compat layer reuses (e.g.
# ``validate_paramset``). ``homematicip_local`` imports it from the real
# aiohomematic anyway; this alias only serves callers reaching for the shim
# namespace.
#
# The specific aliases below stay bound to the *loom* classes on purpose: they
# are ``except`` targets for failures this client raises, and the same-named
# upstream classes are unrelated branches of the tree (a loom auth failure is
# not an ``aiohomematic.exceptions.AuthFailure``).
AuthFailure = LoomAuthError
NoConnectionException = LoomTransportError
ValidationException = LoomValidationError


__all__ = [
    "AuthFailure",
    "BaseHomematicException",
    "NoConnectionException",
    "ValidationException",
]
