# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Cross-implementation parity against aiohomematic's routing-key algorithm.

aiohomematic's ``generate_unique_id`` is the algorithm-of-record for the
HA routing keys this client must produce identically (or HA loses entity
identity on cutover). ``openccu_loom_client.canonical`` calls it directly
and adds the loom-specific wrappers (``loom_`` namespace, CCU serial
suffix). These tests pin the client's output to the golden fixtures
vendored under ``tests/fixtures/`` (formerly shipped by the retired
``aiohomematic-contract`` package; the daemon's Go ``internal/routingkey``
mirrors the same cases) so the implementations cannot drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiohomematic.const import PROGRAM_ADDRESS, SYSVAR_ADDRESS
import pytest

from openccu_loom_client.canonical import (
    canonical_unique_id,
    generate_channel_unique_id,
    generate_unique_id,
    hub_slug,
    serial_suffix,
)
from openccu_loom_client.compat.aiohomematic.model.custom import custom_unique_id
from openccu_loom_client.compat.aiohomematic.model.hub import program_unique_id, sysvar_unique_id
from openccu_loom_client.events.types import data_point_event_key
from openccu_loom_client.wire.enums import DataPointCategory, DataPointType

_FIXTURES: Path = Path(__file__).parent.parent / "fixtures"


def _golden_fixture(name: str) -> dict[str, Any]:
    """Load a vendored ``{name}_golden.json`` fixture as a dict."""
    return json.loads((_FIXTURES / f"{name}_golden.json").read_text(encoding="utf-8"))


def load_golden_cases(name: str) -> list[dict[str, Any]]:
    """Return the ``cases`` list from the vendored ``{name}_golden.json``."""
    return list(_golden_fixture(name)["cases"])


def _enum_golden(name: str) -> dict[str, Any]:
    return _golden_fixture("category")[name]


class TestEnumParity:
    """
    P2: the daemon-generated enum copies must match the golden values.

    The client uses ``openccu_loom_client.wire.enums`` (PascalCase members,
    generated from the daemon's ``enums.json``); aiohomematic ships the
    same enum with SCREAMING_CASE members. Member *names* differ on
    purpose, so parity is checked on the string *values* HA filters on.
    """

    def test_data_point_category_values_match_golden(self) -> None:
        golden = set(_enum_golden("DataPointCategory").values())
        client = {e.value for e in DataPointCategory}
        assert client == golden

    def test_data_point_type_values_match_golden(self) -> None:
        golden = set(_enum_golden("DataPointType").values())
        client = {e.value for e in DataPointType}
        assert client == golden


def _device_cases() -> list[dict[str, Any]]:
    """Golden unique_id cases addressed as ``device:channel`` (no prefix)."""
    out = []
    for case in load_golden_cases("unique_id"):
        addr = case["address"]
        if case["prefix"] is None and addr.count(":") == 1:
            out.append(case)
    return out


class TestSerialSuffix:
    """The CCU serial fills the central-id slot of canonical keys."""

    @pytest.mark.parametrize(
        ("serial", "expected"),
        [
            ("3014F711A0001234", "11a0001234"),  # last 10, lower-cased
            ("ABC", "abc"),  # shorter than 10 → whole
            ("", ""),  # empty in → empty out
        ],
    )
    def test_serial_suffix(self, serial: str, expected: str) -> None:
        assert serial_suffix(serial=serial) == expected


class TestUniqueIdWrappers:
    """
    The client's key helpers must reproduce the canonical key.

    The canonical key is ``loom_`` + the routing key, with the CCU serial
    suffix in the central-id slot. The golden ``central_id`` field stands
    in for that slot here — the algorithm is identical, only the slot
    value differs (entry-id era → serial era).
    """

    @pytest.mark.parametrize("case", _device_cases())
    def test_generic_data_point_key(self, case: dict[str, Any]) -> None:
        device_address, channel = case["address"].split(":")
        expected = f"loom_{case['expected']}"
        if case["parameter"] is None:
            # A device:channel case with no parameter is the custom-DP
            # form (keyed on the primary channel address, no parameter).
            assert (
                custom_unique_id(
                    serial_suffix=case["central_id"],
                    device_address=device_address,
                    channel_no=int(channel),
                )
                == expected
            )
        else:
            assert (
                data_point_event_key(
                    serial_suffix=case["central_id"],
                    device_address=device_address,
                    channel=channel,
                    parameter=case["parameter"],
                )
                == expected
            )

    @pytest.mark.parametrize("name", ["My Var", "Außen Temperatur", "alarm", "Wert mit-Strich"])
    def test_sysvar_key_matches_canonical(self, name: str) -> None:
        assert sysvar_unique_id(serial_suffix="11a0001234", name=name) == canonical_unique_id(
            serial_suffix="11a0001234", address=SYSVAR_ADDRESS, parameter=hub_slug(name=name)
        )

    @pytest.mark.parametrize("name", ["All off", "Anwesenheit Simulation"])
    def test_program_key_matches_canonical(self, name: str) -> None:
        assert program_unique_id(serial_suffix="11a0001234", name=name) == canonical_unique_id(
            serial_suffix="11a0001234", address=PROGRAM_ADDRESS, parameter=hub_slug(name=name)
        )


class TestRoutingKeyGoldens:
    """
    The aiohomematic-backed helpers still satisfy the golden fixtures.

    aiohomematic owns the algorithm (and runs the same fixture in
    ``tests/test_unique_id_golden.py``); running the cases here fails the
    client build loudly if an incompatible aiohomematic is installed.
    """

    @pytest.mark.parametrize("case", load_golden_cases("unique_id"))
    def test_unique_id_reference(self, case: dict[str, Any]) -> None:
        assert (
            generate_unique_id(
                central_id=case["central_id"],
                address=case["address"],
                parameter=case["parameter"],
                prefix=case["prefix"],
            )
            == case["expected"]
        )

    @pytest.mark.parametrize("case", load_golden_cases("unique_id"))
    def test_canonical_is_loom_prefixed_routing_key(self, case: dict[str, Any]) -> None:
        # The canonical key is exactly ``loom_`` + the routing key.
        assert (
            canonical_unique_id(
                serial_suffix=case["central_id"],
                address=case["address"],
                parameter=case["parameter"],
                prefix=case["prefix"],
            )
            == f"loom_{case['expected']}"
        )

    @pytest.mark.parametrize("case", load_golden_cases("channel_unique_id"))
    def test_channel_unique_id_reference(self, case: dict[str, Any]) -> None:
        assert generate_channel_unique_id(central_id=case["central_id"], address=case["address"]) == case["expected"]

    @pytest.mark.parametrize("case", load_golden_cases("hub_slug"))
    def test_hub_slug_reference(self, case: dict[str, Any]) -> None:
        assert hub_slug(name=case["name"]) == case["slug"]
