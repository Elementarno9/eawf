"""A transport fallback stamps ``daemon-fallback`` on the close event.

Pure-function coverage of the ``transport_fallback`` parameter on
:func:`resolve_close_mechanism` / :func:`close_event_extras`.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.cli._mutation import close_event_extras, resolve_close_mechanism

pytestmark = pytest.mark.integration


def test_resolve_close_mechanism_transport_fallback_wins() -> None:
    """``transport_fallback=True`` yields ``"daemon-fallback"`` regardless of gate/waiver."""
    assert (
        resolve_close_mechanism(gate_bearing=False, waived=False, transport_fallback=True)
        == "daemon-fallback"
    )
    assert (
        resolve_close_mechanism(gate_bearing=True, waived=True, transport_fallback=True)
        == "daemon-fallback"
    )


def test_resolve_close_mechanism_default_is_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: the default (no transport fallback, no daemonless env) is ``"daemon"``."""
    # The integration tier forces the daemonless carve-out for every test; this
    # one asserts the behaviour when that env is ABSENT, so it clears it first.
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    assert resolve_close_mechanism(gate_bearing=True, waived=False) == "daemon"


def test_close_event_extras_transport_fallback_preserves_base() -> None:
    """``close_event_extras`` folds ``daemon-fallback`` in while preserving base extras."""
    extras = close_event_extras(
        {"readiness_warnings_count": 3}, gate_bearing=False, waived=False, transport_fallback=True
    )
    assert extras["close_mechanism"] == "daemon-fallback"
    assert extras["readiness_warnings_count"] == 3
