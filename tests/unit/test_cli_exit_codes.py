"""Tests for canonical Eä exit codes.

The exit-code module defines the integer constants used by every CLI
handler and the :class:`eawf.surfaces.cli.errors.CliError` taxonomy. The 0..5
surface is the public contract per C05 § 5.3.

The legacy 0..9 alias block was dropped in P28-I02-W21 once every
downstream callsite migrated to the canonical names — see the
exit-codes-aliases-dropped audit row in ``state.json``.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.cli import exit_codes


def test_canonical_codes() -> None:
    """The new 0..5 surface per C05 § 5.3."""
    assert exit_codes.OK == 0
    assert exit_codes.USER_ERROR == 1
    assert exit_codes.VALIDATION_ERROR == 2
    assert exit_codes.STATE_CONFLICT == 3
    assert exit_codes.DAEMON_UNREACHABLE == 4
    assert exit_codes.INTERNAL_ERROR == 5


def test_legacy_aliases_are_dropped() -> None:
    """Post-W21: the legacy 0..9 alias block no longer exists on the module."""
    for legacy_name in (
        "GENERIC_ERROR",
        "NOT_FOUND",
        "INVALID_INPUT",
        "VALIDATION_FAILED",
        "LOCK_CONFLICT",
        "INSTRUMENT_MISSING",
        "USER_DECLINED",
        "INTEGRITY_VIOLATION",
        "HOOK_BLOCKED",
    ):
        assert not hasattr(exit_codes, legacy_name), (
            f"legacy alias {legacy_name!r} must be removed after P28-I02-W21"
        )


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
