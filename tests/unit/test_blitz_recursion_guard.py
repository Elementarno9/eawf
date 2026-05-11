"""Unit tests for the ``/blitz`` recursion guard (P14-W11 / D22)."""

from __future__ import annotations

import pytest

from eawf.skills.blitz import (
    BlitzRecursionExhausted,
    bump_depth,
    current_depth,
    depth_cap,
    reset_depth,
    should_auto_invoke,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EAWF_BLITZ_DEPTH", raising=False)
    monkeypatch.delenv("EAWF_BLITZ_DEPTH_COUNTER", raising=False)
    yield
    reset_depth()


def test_default_depth_cap_is_eight() -> None:
    assert depth_cap() == 8


def test_depth_cap_honours_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAWF_BLITZ_DEPTH", "3")
    assert depth_cap() == 3


def test_depth_cap_falls_back_on_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAWF_BLITZ_DEPTH", "not-a-number")
    assert depth_cap() == 8


def test_bump_depth_increments_counter() -> None:
    assert current_depth() == 0
    assert bump_depth() == 1
    assert bump_depth() == 2
    assert current_depth() == 2


def test_bump_depth_raises_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAWF_BLITZ_DEPTH", "2")
    bump_depth()
    bump_depth()
    with pytest.raises(BlitzRecursionExhausted):
        bump_depth()


def test_reset_clears_counter() -> None:
    bump_depth()
    reset_depth()
    assert current_depth() == 0


def test_should_auto_invoke_only_when_more_than_one_unknown() -> None:
    assert should_auto_invoke(residual_unknowns=2) is True
    assert should_auto_invoke(residual_unknowns=1) is False
    assert should_auto_invoke(residual_unknowns=0) is False
