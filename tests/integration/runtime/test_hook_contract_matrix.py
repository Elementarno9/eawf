"""Every hook of the packaged plugin keeps the contract in every context.

The plugin is packaged as it ships and its one wrapper is run the way the
host runs it, through the manifest command, by the same matrix ``eawf
doctor hooks`` runs: six contexts (managed root, managed subfolder, managed
linked worktree, unmanaged git, no git, runner missing) by the ten
subscribed events by the normal, empty-stdin and malformed-JSON payloads,
plus the daemon-down and stale-version payloads in the managed contexts.
A cell passes on exit status 0, stdout empty or one JSON document of the
event's shape, empty stderr outside a managed repository and no daemon
socket but the one a managed session start may start.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from eawf.observability.doctor.checks_hooks import run_matrix
from eawf.observability.doctor.hook_matrix import (
    CONTEXTS,
    MANAGED_CONTEXTS,
    matrix_events,
    payloads_for,
)
from eawf.runtime.runtimes.claude.plugin_package import package_plugin

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed"),
]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The plugin as ``package_plugin`` writes it."""
    root = tmp_path_factory.mktemp("plugin") / "eawf"
    package_plugin(root, include_marketplace=False, include_readme=False, extra_tools={})
    return root


def _expected_cells() -> int:
    return sum(
        len(payloads_for(event, managed=context in MANAGED_CONTEXTS))
        for context in CONTEXTS
        for event in matrix_events()
    )


def test_matrix_cells(bundle: Path) -> None:
    cells, failures = run_matrix(bundle)

    assert [failure.line() for failure in failures] == []
    assert cells == _expected_cells() == 282


def test_every_context_gets_every_payload_kind() -> None:
    for context in CONTEXTS:
        managed = context in MANAGED_CONTEXTS
        for event in matrix_events():
            labels = {payload.label for payload in payloads_for(event, managed=managed)}
            assert {"empty", "malformed"} <= labels
            assert ({"daemon-down", "stale-daemon"} <= labels) is managed
