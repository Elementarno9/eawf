"""Each module on a former import cycle imports on its own.

The cycle only shows when a module is the first of the chain a fresh
interpreter loads; inside a pytest process some other test has usually
imported the chain already, so each import runs in its own subprocess.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

CHAIN_MODULES: tuple[str, ...] = (
    "eawf.kernel.store",
    "eawf.kernel.store.commit_census",
    "eawf.runtime.integration.commit_policy",
    "eawf.kernel.runtime.candidate",
    "eawf.kernel.runtime.semantic",
    "eawf.surfaces.cli.commands.lifecycle",
    "eawf.surfaces.cli.commands.domain",
    "eawf.surfaces.cli.commands.domain_delivery",
    "eawf.surfaces.cli.commands.domain_integration",
    "eawf.surfaces.cli.commands.domain_legacy",
)


@pytest.mark.parametrize("module", CHAIN_MODULES)
def test_module_chain_repro_import_cycle(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_provenance_trailer_key_single_definition() -> None:
    from eawf.kernel.store import commit_policy as kernel_policy
    from eawf.runtime.integration import commit_policy as runtime_policy

    assert runtime_policy.PROVENANCE_TRAILER_KEY is kernel_policy.PROVENANCE_TRAILER_KEY
