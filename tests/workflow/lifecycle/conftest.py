"""Lifecycle tests run daemonless so the WAL-backed in-process fallback kicks in.

Mirrors :mod:`tests.workflow.verify.conftest` — short-lived CLI
invocations in this package would otherwise attempt to talk to the
operator's real daemon and fail with ``daemon_required``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _daemonless_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the V1 daemonless carve-out for every lifecycle test."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
