"""Tests for ``eawf.surfaces.render.statusline`` — segment construction."""

from __future__ import annotations

from typing import Final

from eawf.surfaces.render.statusline import (
    SegmentSource,
    StatuslineSegment,
    sourced_segment,
)

_SOURCE: Final = SegmentSource(producer="test", provenance="test#field")
_SCOPE: Final = sourced_segment("scope", "scope", "P04", _SOURCE)


def test_segment_carries_its_truth_field() -> None:
    assert isinstance(_SCOPE, StatuslineSegment)
    assert _SCOPE.truth.value == "P04"
    assert _SCOPE.truth.producer == "test"
