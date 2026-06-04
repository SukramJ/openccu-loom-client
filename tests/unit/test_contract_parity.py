# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Cross-implementation parity against ``aiohomematic-contract``.

The contract package is the algorithm-of-record for the HA routing keys
and categorization enums that ``aiohomematic`` and this client must
produce identically (or HA loses entity identity on cutover). aiohomematic
imports the reference implementations directly; this client rebuilds them
(it adds the ``central_id`` from the daemon snapshot, uses the
daemon-generated ``openccu_loom_types`` enums, etc.). These tests pin the
client's output to the contract's golden fixtures so the two cannot drift.

See ``aiohomematic/docs/contract-gaps.md``.
"""

from __future__ import annotations

import json

import pytest
from aiohomematic_contract import (
    canonical_unique_id,
    generate_channel_unique_id,
    generate_unique_id,
    golden_fixture_path,
    hub_slug,
    load_golden_cases,
    serial_suffix,
)
from aiohomematic_contract.unique_id import PROGRAM_ADDRESS, SYSVAR_ADDRESS
from openccu_loom_types.enums import DataPointCategory, DataPointType

from openccu_loom_client.compat.aiohomematic.model.custom import custom_unique_id
from openccu_loom_client.compat.aiohomematic.model.hub import (
    program_unique_id,
    sysvar_unique_id,
)
from openccu_loom_client.events.types import data_point_event_key


def _enum_golden(name: str) -> dict[str, dict[str, object]]:
    return json.loads(golden_fixture_path("category").read_text(encoding="utf-8"))[name]


class TestEnumParity:
    """P2: the daemon-generated enum copies must match the contract values.

    The client uses ``openccu_loom_types.enums`` (PascalCase members,
    generated from the daemon's ``enums.json``); the contract ships the
    same enum with SCREAMING_CASE members. Member *names* differ on
    purpose, so parity is checked on the string *values* HA filters on.
    """

    def test_data_point_category_values_match_contract(self) -> None:
        contract = set(_enum_golden("DataPointCategory").values())
        client = {e.value for e in DataPointCategory}
        assert client == contract

    def test_data_point_type_values_match_contract(self) -> None:
        contract = set(_enum_golden("DataPointType").values())
        client = {e.value for e in DataPointType}
        assert client == contract


def _device_cases() -> list[dict]:
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
        "serial, expected",
        [
            ("3014F711A0001234", "11a0001234"),  # last 10, lower-cased
            ("ABC", "abc"),  # shorter than 10 → whole
            ("", ""),  # empty in → empty out
        ],
    )
    def test_serial_suffix(self, serial: str, expected: str) -> None:
        assert serial_suffix(serial) == expected


class TestUniqueIdWrappers:
    """The client's key helpers must reproduce the contract's canonical key.

    The canonical key is ``loom_`` + the routing key, with the CCU serial
    suffix in the central-id slot. The golden ``central_id`` field stands
    in for that slot here — the algorithm is identical, only the slot
    value differs (entry-id era → serial era).
    """

    @pytest.mark.parametrize("case", _device_cases())
    def test_generic_data_point_key(self, case: dict) -> None:
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
    def test_sysvar_key_matches_contract(self, name: str) -> None:
        assert sysvar_unique_id(serial_suffix="11a0001234", name=name) == canonical_unique_id(
            serial_suffix="11a0001234", address=SYSVAR_ADDRESS, parameter=hub_slug(name)
        )

    @pytest.mark.parametrize("name", ["All off", "Anwesenheit Simulation"])
    def test_program_key_matches_contract(self, name: str) -> None:
        assert program_unique_id(serial_suffix="11a0001234", name=name) == canonical_unique_id(
            serial_suffix="11a0001234", address=PROGRAM_ADDRESS, parameter=hub_slug(name)
        )


class TestContractReferenceGoldens:
    """Sanity: the imported reference impls still satisfy their fixtures.

    (The contract package owns these too, but running them here fails the
    client build loudly if an incompatible contract version is installed.)
    """

    @pytest.mark.parametrize("case", load_golden_cases("unique_id"))
    def test_unique_id_reference(self, case: dict) -> None:
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
    def test_canonical_is_loom_prefixed_routing_key(self, case: dict) -> None:
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
    def test_channel_unique_id_reference(self, case: dict) -> None:
        assert (
            generate_channel_unique_id(central_id=case["central_id"], address=case["address"])
            == case["expected"]
        )

    @pytest.mark.parametrize("case", load_golden_cases("hub_slug"))
    def test_hub_slug_reference(self, case: dict) -> None:
        assert hub_slug(case["name"]) == case["slug"]
