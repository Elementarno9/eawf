"""Tests for canonical Eä exit codes.

The exit-code module defines the integer constants used by every CLI
handler and the :class:`eawf.cli.errors.CliError` taxonomy. The 0..5
surface is the public contract per C05 § 5.3.

Legacy 0..9 names remain importable as deprecation aliases mapped onto
the new five buckets per the § 5.3 bucket table. The numeric values
under those legacy names *changed* with the C05 cutover (BREAKING) —
downstream waves migrate each callsite to the new names; until then
this module asserts the alias bindings stay correct.
"""

from __future__ import annotations

import pytest

from eawf.cli import exit_codes


def test_canonical_codes() -> None:
    """The new 0..5 surface per C05 § 5.3."""
    assert exit_codes.OK == 0
    assert exit_codes.USER_ERROR == 1
    assert exit_codes.VALIDATION_ERROR == 2
    assert exit_codes.STATE_CONFLICT == 3
    assert exit_codes.DAEMON_UNREACHABLE == 4
    assert exit_codes.INTERNAL_ERROR == 5


def test_legacy_aliases_map_per_bucket_table() -> None:
    """Legacy 0..9 names alias onto the new buckets per § 5.3 table."""
    # Legacy → INTERNAL_ERROR (5)
    assert exit_codes.GENERIC_ERROR == exit_codes.INTERNAL_ERROR
    # Legacy → USER_ERROR (1)
    assert exit_codes.NOT_FOUND == exit_codes.USER_ERROR
    assert exit_codes.INVALID_INPUT == exit_codes.USER_ERROR
    assert exit_codes.INSTRUMENT_MISSING == exit_codes.USER_ERROR
    assert exit_codes.USER_DECLINED == exit_codes.USER_ERROR
    # Legacy → VALIDATION_ERROR (2)
    assert exit_codes.VALIDATION_FAILED == exit_codes.VALIDATION_ERROR
    # Legacy → STATE_CONFLICT (3)
    assert exit_codes.LOCK_CONFLICT == exit_codes.STATE_CONFLICT
    assert exit_codes.INTEGRITY_VIOLATION == exit_codes.STATE_CONFLICT
    assert exit_codes.HOOK_BLOCKED == exit_codes.STATE_CONFLICT


def test_name_for_round_trips() -> None:
    """``name_for`` round-trips for the canonical 0..5 names only."""
    for name in (
        "OK",
        "USER_ERROR",
        "VALIDATION_ERROR",
        "STATE_CONFLICT",
        "DAEMON_UNREACHABLE",
        "INTERNAL_ERROR",
    ):
        code = getattr(exit_codes, name)
        assert exit_codes.name_for(code) == name


def test_name_for_unknown_code_raises() -> None:
    """Out-of-range codes raise ``KeyError`` per the docstring."""
    with pytest.raises(KeyError):
        exit_codes.name_for(999)
