"""Tests in :mod:`tests.workflow.verify` exercise the CLI end-to-end.

The :func:`test_close_and_pin_calls_compute_after_close_wave` test
drives ``eawf wave close`` via :class:`typer.testing.CliRunner`
against a tmp_path repo. Per the V1 daemonless carve-out (see
``tests/integration/conftest.py``), short-lived in-process CLI
invocations run daemonless — the daemon path here would attempt to
talk to the operator's real daemon and fail with
``daemon_required``.

Force ``EAWF_DAEMONLESS=1`` for every test in this package so the
in-process WAL-backed fallback runs uniformly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _daemonless_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the V1 daemonless carve-out for every verify test."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
