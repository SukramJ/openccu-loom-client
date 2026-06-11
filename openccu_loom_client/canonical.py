# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Loom-namespaced canonical routing keys.

The cross-backend routing key is aiohomematic's
:func:`aiohomematic.model.support.generate_unique_id` — the
algorithm-of-record every backend must reproduce bit-identically (a
different key ⇒ events route to the wrong or no HA entity). This module
calls it directly and adds the two loom-specific wrappers:

- a constant ``loom_`` namespace prefix segregates loom entities from
  other integrations' entities in a shared registry (notably on the
  MQTT plane); and
- the central-id slot carries the **CCU serial suffix** (last 10 chars,
  lower-cased) for the address classes whose addresses repeat across
  CCUs (hub roots, ``INT000*``, virtual remotes). Normal device
  addresses (e.g. ``VCU1234567``) are globally unique and carry no
  prefix.

aiohomematic's functions take a ``config_provider`` (they read
``config.central_id`` off the live central); the loom client carries a
plain ``central_id`` string instead, so the thin adapters here wrap it
in a minimal stub provider.

This module is the Python side of the daemon's Go
``internal/routingkey`` (``SerialSuffix`` / ``CanonicalUniqueID``); both
run the same routing-key algorithm underneath, so the two produce
bit-identical output. See
``docs/external-clients/ha-unique-id-migration.md`` in the daemon repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

from aiohomematic.interfaces import ConfigProviderProtocol
from aiohomematic.model.support import (
    generate_channel_unique_id as _aio_generate_channel_unique_id,
    generate_unique_id as _aio_generate_unique_id,
)
from slugify import slugify

__all__ = [
    "LOOM_NAMESPACE",
    "SERIAL_SUFFIX_LEN",
    "canonical_unique_id",
    "generate_channel_unique_id",
    "generate_unique_id",
    "hub_slug",
    "serial_suffix",
]

# Constant prefix applied to every external unique_id.
LOOM_NAMESPACE: Final = "loom"

# How many trailing characters of the CCU serial form the per-CCU
# discriminator. Ten mirrors the legacy ``entry_id[-10:]`` width.
SERIAL_SUFFIX_LEN: Final = 10


@dataclass(frozen=True, kw_only=True, slots=True)
class _CentralIdConfig:
    """Minimal config exposing only the ``central_id`` the routing key reads."""

    central_id: str


@dataclass(frozen=True, kw_only=True, slots=True)
class _CentralIdProvider:
    """Minimal stand-in for a full ``CentralUnit`` config provider."""

    config: _CentralIdConfig


def _provider(central_id: str) -> ConfigProviderProtocol:
    """Wrap a plain central id into the provider shape aiohomematic expects."""
    return cast(
        "ConfigProviderProtocol",
        _CentralIdProvider(config=_CentralIdConfig(central_id=central_id)),
    )


def generate_unique_id(
    *,
    central_id: str,
    address: str,
    parameter: str | None = None,
    prefix: str | None = None,
) -> str:
    """Build the routing key via aiohomematic's reference algorithm."""
    return _aio_generate_unique_id(
        config_provider=_provider(central_id),
        address=address,
        parameter=parameter,
        prefix=prefix,
    )


def generate_channel_unique_id(*, central_id: str, address: str) -> str:
    """Build the channel-level routing key via aiohomematic's reference algorithm."""
    return _aio_generate_channel_unique_id(config_provider=_provider(central_id), address=address)


def serial_suffix(serial: str) -> str:
    """
    Return the per-CCU discriminator from the CCU serial.

    This is the last :data:`SERIAL_SUFFIX_LEN` characters of the CCU serial,
    lower-cased. Serials shorter than that are returned whole; empty in, empty out.
    This feeds the central-id slot of :func:`canonical_unique_id` for
    hub / internal / virtual-remote addresses.
    """
    serial = serial.lower()
    if len(serial) <= SERIAL_SUFFIX_LEN:
        return serial
    return serial[-SERIAL_SUFFIX_LEN:]


def canonical_unique_id(
    *,
    serial_suffix: str,
    address: str,
    parameter: str | None = None,
    prefix: str | None = None,
) -> str:
    """
    Build the external, loom-namespaced unique_id ``loom_<routing-key>``.

    ``serial_suffix`` goes in the central-id slot (see :func:`serial_suffix`);
    devices come out unprefixed within the routing key
    (``loom_vcu1234567_1_state``), while hub / internal / virtual-remote
    addresses carry the serial suffix
    (``loom_<serial10>_sysvar_<hub-slug>``).

    For hub data points pass the pseudo-address (``"sysvar"`` /
    ``"program"`` / ``"install_mode"``) and the :func:`hub_slug`-ed name
    as ``parameter``.
    """
    return f"{LOOM_NAMESPACE}_" + generate_unique_id(
        central_id=serial_suffix,
        address=address,
        parameter=parameter,
        prefix=prefix,
    )


def hub_slug(name: str) -> str:
    """
    Slugify a hub data-point name exactly as aiohomematic does.

    aiohomematic builds hub data-point unique_ids with **python-slugify
    default settings** (dash separator, Unicode transliteration,
    lowercased) — e.g. ``"Außen Temperatur"`` → ``"aussen-temperatur"``.
    A naive ``replace().lower()`` cleaner diverges on any non-ASCII name
    and silently orphans the HA entity on cutover.
    """
    return slugify(name)
