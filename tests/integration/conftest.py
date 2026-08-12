"""Integration-test wiring.

After P24-W10 the built-in ``daemon.proxy_enabled`` default flipped to
``True`` — every mutating CLI verb routes through the daemon by
default. Integration tests (which exercise the CLI via Typer's
``CliRunner`` against a tmp_path repo) historically ran daemonless
and would now fail with ``daemon_required`` envelopes against the
operator's real daemon (or a missing one).

The V1 carve-out documented in the authority map names three
contexts that legitimately run daemonless: **CI environments**,
**read-only one-shot CLI calls**, and **recovery shell**. The CLI
integration suite is functionally the first of these — short-lived
in-process invocations with no live daemon. We honour that by
forcing ``EAWF_DAEMONLESS=1`` at the start of every integration
test; suites that need to exercise the proxy path directly (the
W10 ``tests/cli/test_*_proxy.py`` modules) set the env explicitly
inside their own fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TIER_DIR = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every test collected under ``tests/integration/`` with the ``integration`` marker.

    The marker is derived from the directory, not a per-file
    ``@pytest.mark.integration`` decorator. Pytest calls each conftest's
    copy of this hook with the full collected set, so it marks only the
    items whose path is beneath this tier directory -- tier membership is
    a property of WHERE a test lives, with no per-file edit.
    """
    for item in items:
        if _TIER_DIR in item.path.parents:
            item.add_marker("integration")


@pytest.fixture(autouse=True)
def _daemonless_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the V1 daemonless carve-out for every integration test.

    Tests that explicitly exercise the daemon-proxy path
    (``tests/cli/test_*_proxy.py``) live in the ``tests/cli/`` tree;
    integration tests stay daemonless until a dedicated daemon-up
    fixture is wired in a later wave.
    """
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")


@pytest.fixture(autouse=True)
def _isolate_user_plugin_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the user-scope plugin detectors find nothing by default.

    These tests render plugin trees into tmp workspaces, but the conflict
    gates probe the *developer's* real home. Until the claude detector was
    repaired it always returned None here, so the suite passed by accident;
    with it working, every install / sync / doctor test failed on a machine
    that happens to have the plugin installed.

    A test that wants a conflict patches the same target itself, and its
    fixture runs after this one.
    """
    for target in (
        "eawf.surfaces.cli.commands.plugin.detect_marketplace_install",
        "eawf.surfaces.cli.commands.plugin.codex_detect_user_install",
        "eawf.surfaces.cli.commands.plugin.opencode_detect_user_install",
    ):
        monkeypatch.setattr(target, lambda: None)
