"""Unit tests for :mod:`eawf.mcp.catalog`.

The v0.1 catalog ships empty (plan §9 line 461). These tests pin
the contract so v0.1.1 can populate ``KNOWN_MCPS`` without surface
churn:

- ``KNOWN_MCPS`` is an empty tuple.
- :class:`McpCatalogEntry` is the documented dataclass with seven
  fields.
- Importing the module is side-effect-free (no warnings emitted).
"""

from __future__ import annotations

import dataclasses
import importlib
import warnings

import pytest

from eawf.mcp.catalog import KNOWN_MCPS, McpCatalogEntry
from eawf.state.enums import McpRisk

pytestmark = pytest.mark.unit


def test_known_mcps_is_empty_tuple_in_v01() -> None:
    """v0.1 ships intentionally empty per the spec rationale."""
    assert KNOWN_MCPS == ()
    assert isinstance(KNOWN_MCPS, tuple)


def test_mcp_catalog_entry_is_dataclass() -> None:
    assert dataclasses.is_dataclass(McpCatalogEntry)


def test_mcp_catalog_entry_has_seven_documented_fields() -> None:
    """Field set is the v0.1.1 contract; pin it."""
    field_names = {f.name for f in dataclasses.fields(McpCatalogEntry)}
    assert field_names == {
        "id",
        "command",
        "default_args",
        "default_env_refs",
        "risk",
        "write_capable",
        "description",
    }


def test_mcp_catalog_entry_defaults_match_spec() -> None:
    """Empty defaults are required so callers can pass only ``id``/``command``."""
    entry = McpCatalogEntry(id="probe", command="/usr/bin/probe")
    assert entry.default_args == ()
    assert entry.default_env_refs == ()
    assert entry.risk is McpRisk.READ
    assert entry.write_capable is False
    assert entry.description == ""


def test_mcp_catalog_module_import_is_side_effect_free() -> None:
    """Re-importing the module must not emit warnings or print."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        # ``importlib.reload`` re-executes the module body.
        import eawf.mcp.catalog as catalog_mod

        importlib.reload(catalog_mod)
