# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
Name the person an integration acts for in the daemon's change log.

An integration holds one credential for all its users. The daemon records
that credential as the principal of every change and, beside it, whatever
the request names in ``X-On-Behalf-Of`` (``on_behalf_of`` on an
``AuditEntry``). It accepts the header only from an API token or a box API
token, and answers **400** for a value that is empty, longer than 128 bytes,
not UTF-8, or carries a control (``Cc``) or format (``Cf``) character
(openccu-loom ``internal/north/rest/middleware/audit_actor.go``,
``invalidOnBehalfOf``). A display name taken from a user profile can break
every one of those rules, and a 400 would fail the user's save rather than
the label — so :func:`on_behalf_of_label` reduces any name to a value the
daemon stores, instead of each caller re-deriving the rule.

Send it per request, never on the session: it names who asked for this one
call.

    headers = {}
    if label := on_behalf_of_label(name=user.name, user_id=f"ha:{user.id}"):
        headers[ON_BEHALF_OF_HEADER] = label
    await transport.request(method="PUT", path=path, json_body=body, headers=headers)
"""

from __future__ import annotations

from typing import Final
import unicodedata

ON_BEHALF_OF_HEADER: Final = "X-On-Behalf-Of"

# The daemon's bound (``onBehalfOfMaxBytes``): a display label, stored on
# every row.
ON_BEHALF_OF_MAX_BYTES: Final = 128

# Categories the daemon refuses (``Cc``, ``Cf``) plus lone surrogates
# (``Cs``), which a Python ``str`` can hold but UTF-8 cannot encode.
_REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs"})


def on_behalf_of_label(*, name: str, user_id: str | None = None) -> str | None:
    """
    Build an ``X-On-Behalf-Of`` value the daemon accepts.

    The result is ``"<name> (<user_id>)"``, or whichever of the two is
    non-empty. Control, format and surrogate characters are dropped, and
    the result fits 128 UTF-8 bytes. The name is shortened before the id,
    because the id is what tells two users of the same name apart; a cut
    never splits a character. Returns ``None`` when nothing is left to
    name, so the caller sends no header rather than an empty one, which
    the daemon refuses.
    """
    clean_name = _clean(text=name)
    clean_id = _clean(text=user_id or "")
    if not clean_id:
        return _fit(text=clean_name, max_bytes=ON_BEHALF_OF_MAX_BYTES) or None
    suffix = f" ({clean_id})"
    budget = ON_BEHALF_OF_MAX_BYTES - len(suffix.encode())
    if not clean_name or budget <= 0:
        return _fit(text=clean_id, max_bytes=ON_BEHALF_OF_MAX_BYTES) or None
    if not (short_name := _fit(text=clean_name, max_bytes=budget)):
        return _fit(text=clean_id, max_bytes=ON_BEHALF_OF_MAX_BYTES) or None
    return short_name + suffix


def _clean(*, text: str) -> str:
    """Drop every character the daemon refuses and trim the ends."""
    return "".join(c for c in text if unicodedata.category(c) not in _REFUSED_CATEGORIES).strip()


def _fit(*, text: str, max_bytes: int) -> str:
    """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes on a character boundary."""
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode(errors="ignore").rstrip()
