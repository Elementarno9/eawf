"""Regression: ``daemon.proxy_enabled`` default flipped to ``True`` (P24-W10).

The flip from ``False`` to ``True`` is the C02 §7.2 sub-phase c
handoff — once it lands, every CLI mutation that routes through
``_save_value_to_layer`` / ``_persist_registry`` / W09's
``state_mutate`` proxy goes through the daemon by default. The
in-process arm remains as the V1 carve-out fallback (CI / one-shot /
recovery shell) but is no longer the implicit default.

This module pins the on-disk default so any later phase that needs to
revert (incident response, sub-phase rollback) does so by an explicit
``state``-tagged commit and not by silent flag drift.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_daemon_proxy_enabled_default_is_true() -> None:
    """Sanity check: the built-in defaults set ``daemon.proxy_enabled = True``."""
    from eawf.config.defaults import BUILT_IN_DEFAULTS, built_in_defaults

    snapshot = built_in_defaults()
    assert snapshot["daemon"]["proxy_enabled"] is True
    # The public read-only view agrees.
    assert BUILT_IN_DEFAULTS["daemon"]["proxy_enabled"] is True


def test_daemon_section_carries_idle_timeout_and_session_ttl() -> None:
    """Adjacent daemon keys still carry their expected defaults.

    Acts as a canary: an accidental rewrite of the daemon block that
    drops ``idle_timeout_seconds`` or ``session_handle_ttl_seconds``
    will fail here rather than at runtime when the watchdog or session
    sweeper boots without its config.
    """
    from eawf.config.defaults import built_in_defaults

    snapshot = built_in_defaults()
    daemon = snapshot["daemon"]
    assert daemon["idle_timeout_seconds"] == 300
    assert daemon["session_handle_ttl_seconds"] == 86400
