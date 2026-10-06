"""The store, epoch-2 entity and payload-kind packages re-export lazily.

Each package resolves a re-exported name on first access, so importing one
submodule does not build every model the package re-exports. A re-exported
name is still the very object its submodule defines, and a name the package
does not re-export is refused as before.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from types import ModuleType

import pytest

import eawf.kernel.state.epoch2 as epoch2
import eawf.kernel.store as store
import eawf.kernel.store.kinds as kinds
from eawf.kernel.state.enums import StoreKind

_PACKAGES = (store, epoch2)


@pytest.mark.parametrize("package", _PACKAGES, ids=lambda p: p.__name__)
def test_every_listed_name_is_the_submodules_own_object(package: ModuleType) -> None:
    for name in package.__all__:
        module = importlib.import_module(package._EXPORTS[name])
        assert getattr(package, name) is getattr(module, name)


@pytest.mark.parametrize("package", _PACKAGES, ids=lambda p: p.__name__)
def test_the_export_table_and_the_listed_names_agree(package: ModuleType) -> None:
    assert set(package._EXPORTS) == set(package.__all__)


@pytest.mark.parametrize("package", [*_PACKAGES, kinds], ids=lambda p: p.__name__)
def test_a_name_the_package_does_not_export_is_refused(package: ModuleType) -> None:
    with pytest.raises(AttributeError, match="no_such_name"):
        package.no_such_name  # noqa: B018


def test_from_import_resolves_a_lazy_name() -> None:
    from eawf.kernel.store import LedgerRecord, ledger

    assert LedgerRecord is ledger.LedgerRecord


def test_the_payload_registry_is_built_once() -> None:
    first = kinds.PAYLOAD_MODELS
    assert kinds.PAYLOAD_MODELS is first
    assert StoreKind.MEMORY in first
    assert len(first) >= 1


def _loaded_after(statement: str, candidates: tuple[str, ...]) -> set[str]:
    """Return which *candidates* a fresh interpreter has loaded after *statement*."""
    probe = (
        "import sys\n"
        f"{statement}\n"
        f"print('\\n'.join(name for name in {candidates!r} if name in sys.modules))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    return {line for line in proc.stdout.splitlines() if line}


@pytest.mark.parametrize(
    ("statement", "unloaded"),
    [
        (
            "import eawf.kernel.store.ledger",
            ("eawf.kernel.store.compact", "eawf.kernel.store.kinds.audit"),
        ),
        (
            "import eawf.kernel.store.compaction",
            ("eawf.kernel.state.epoch2.task", "eawf.kernel.state.models"),
        ),
        (
            "import eawf.kernel.state.epoch2.authority",
            ("eawf.kernel.migration.epoch2.cutover", "eawf.kernel.state.epoch2.milestone"),
        ),
        ("import eawf.kernel.store.kinds.memory", ("eawf.kernel.store.kinds.audit",)),
    ],
)
def test_one_submodule_import_leaves_the_rest_unloaded(
    statement: str, unloaded: tuple[str, ...]
) -> None:
    assert _loaded_after(statement, unloaded) == set()


def test_the_probe_sees_a_module_that_does_load() -> None:
    # Reading the registry builds every payload model; proves the probe can fail.
    loaded = _loaded_after(
        "import eawf.kernel.store.kinds as k; k.PAYLOAD_MODELS",
        ("eawf.kernel.store.kinds.audit",),
    )
    assert loaded == {"eawf.kernel.store.kinds.audit"}
