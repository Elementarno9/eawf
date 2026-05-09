"""Tests for canonical Eä exit codes.

The exit-code module defines the integer constants used by every CLI handler
and the ``CliError`` taxonomy in :mod:`eawf.cli.errors`. The values are part
of the public contract per ``ea-proposal.md`` §7 and the v0.1 plan §5.
"""

from __future__ import annotations

import pytest

from eawf.cli import exit_codes


def test_canonical_codes() -> None:
    assert exit_codes.OK == 0
    assert exit_codes.GENERIC_ERROR == 1
    assert exit_codes.NOT_FOUND == 2
    assert exit_codes.INVALID_INPUT == 3
    assert exit_codes.VALIDATION_FAILED == 4
    assert exit_codes.LOCK_CONFLICT == 5
    assert exit_codes.INSTRUMENT_MISSING == 6
    assert exit_codes.USER_DECLINED == 7
    assert exit_codes.INTEGRITY_VIOLATION == 8
    assert exit_codes.HOOK_BLOCKED == 9


def test_name_for_round_trips() -> None:
    for name in (
        "OK",
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
        code = getattr(exit_codes, name)
        assert exit_codes.name_for(code) == name


def test_name_for_unknown_code_raises() -> None:
    with pytest.raises(KeyError):
        exit_codes.name_for(999)
