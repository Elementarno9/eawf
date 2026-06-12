"""Shared fixtures for the ``tui`` golden-snapshot suite.

The golden snapshots assert a screen's *content*, not the ambient daemon
transport state. The app top-docks a daemon-degraded banner ("daemon socket
unavailable; polling state.json | socket=<path> ...") when the binder's probe
finds no live daemon -- a condition that is environment-dependent (a CI runner
has no daemon, a dev box may), set ASYNCHRONOUSLY via ``call_after_refresh``,
so its presence races the capture and embeds the runtime socket path. Both make
the goldens flaky across machines (and would leak the path). The autouse
fixture below no-ops the banner sync so every captured frame is banner-free and
byte-stable regardless of daemon availability; no snapshot test asserts the
degraded banner (the dedicated banner behaviour lives in non-golden tests).
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.app import EaApp


@pytest.fixture(autouse=True)
def _suppress_daemon_degraded_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the daemon-degraded banner unmounted for deterministic captures."""
    monkeypatch.setattr(EaApp, "_sync_degraded_banner", lambda _self: None)
