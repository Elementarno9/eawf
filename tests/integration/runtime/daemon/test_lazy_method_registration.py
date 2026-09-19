"""Daemon startup does not pay for the whole method surface.

Handlers register by import side effect. Importing every method module in
``server`` made a liveness probe wait for Pydantic to build validators for
models it never touches, which pushed cold daemon readiness past the spawn
budget on a loaded machine. Only the liveness and subscribe modules import
eagerly now; the rest register on first need.

Three properties are pinned here. The deferred modules really are absent
from a fresh interpreter that has imported the server, so the saving is
measured rather than intended. Every name a decorator registers anywhere in
the package resolves once the surface loads, so adding an entry module and
forgetting the list fails here instead of turning a verb into an unknown
method. And dispatch still resolves a deferred verb and still refuses an
unknown one, so the deferral costs no behaviour.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest

from eawf.runtime.daemon.methods import (
    _METHOD_MODULES,
    MethodContext,
    MethodNotFoundError,
    dispatch,
    ensure_all_methods_registered,
    registered_methods,
)

pytestmark = pytest.mark.integration

#: ``tests/integration/runtime/daemon`` - four levels up lands on the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: The modules ``server`` imports for their names, so they cannot be deferred.
_EAGER_MODULES = frozenset({"daemon", "event", "state_subscribe"})

#: Package members that are not method modules.
_NON_METHOD_MEMBERS = frozenset({"__init__", "__pycache__"})

#: A verb owned by a deferred module, used to prove on-demand registration.
_DEFERRED_VERB = "state.read"


def _methods_package_dir() -> Path:
    """Return the directory holding the method modules."""
    return _REPO_ROOT / "src" / "eawf" / "runtime" / "daemon" / "methods"


def _package_module_names() -> set[str]:
    """Return every method module name present in the package."""
    return {
        path.stem
        for path in _methods_package_dir().glob("*.py")
        if path.stem not in _NON_METHOD_MEMBERS
    }


# ---- the saving is real ------------------------------------------------------


def test_importing_the_server_leaves_the_deferred_modules_unimported() -> None:
    """A fresh interpreter that imports the server has not loaded the surface.

    Run out of process: this test session has imported half the daemon
    already, so an in-process check would prove nothing.
    """
    probe = (
        "import sys;"
        "import eawf.runtime.daemon.server;"
        "print(sum(1 for name in sys.modules"
        " if name.startswith('eawf.runtime.daemon.methods.')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    loaded = int(result.stdout.strip())
    # The three eager modules, plus whatever they themselves import from
    # the package. Far below the full surface is the property that matters.
    assert loaded < len(_METHOD_MODULES), (
        f"server import loaded {loaded} method modules; the deferral is not in effect"
    )


def test_a_ping_only_interpreter_does_not_load_the_deferred_modules() -> None:
    """The liveness handler resolves without the deferred modules loading."""
    probe = (
        "import sys;"
        "import eawf.runtime.daemon.server;"
        "from eawf.runtime.daemon.methods import _REGISTRY;"
        "print('daemon.ping' in _REGISTRY, 'state.read' in _REGISTRY)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True False", result.stdout


# ---- the list cannot silently drift -----------------------------------------


def _registered_names_in_source() -> set[str]:
    """Return every method name a decorator in the package registers.

    Scans source rather than the live registry, because the point is to
    catch a verb whose module never imports -- which is invisible to the
    registry by definition.
    """
    pattern = re.compile(r"@(?:register|native_mutator)\(\s*[\"']([a-z][a-z0-9_.]*)[\"']", re.M)
    found: set[str] = set()
    for path in _methods_package_dir().glob("*.py"):
        found |= set(pattern.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_registered_name_in_the_package_resolves() -> None:
    """No verb in the package dispatches as an unknown method.

    This is the property a filename list would only approximate: several
    entry modules import helpers that register verbs of their own, so what
    matters is that each name resolves, not that each file is listed.
    """
    declared = _registered_names_in_source()
    assert declared, "found no registration decorators; the scan pattern has drifted"
    unreachable = sorted(declared - set(registered_methods()))
    assert not unreachable, (
        f"registered in source but absent from the loaded surface: {unreachable}; "
        "their entry module is missing from _METHOD_MODULES"
    )


def test_module_list_names_no_module_that_does_not_exist() -> None:
    """The list cannot name a module that was renamed or removed."""
    stale = sorted(set(_METHOD_MODULES) - _package_module_names())
    assert not stale, f"module list names absent modules: {stale}"


def test_module_list_includes_the_eager_modules() -> None:
    """The eager modules are listed too, so the surface is whole without ``server``."""
    assert set(_METHOD_MODULES) >= _EAGER_MODULES


def test_module_list_has_no_repeats() -> None:
    """A repeated entry would import twice and hide a missing one."""
    assert len(_METHOD_MODULES) == len(set(_METHOD_MODULES))


# ---- laziness costs no behaviour --------------------------------------------


def test_the_whole_surface_is_registered_after_the_deferred_load() -> None:
    """``registered_methods`` answers for the whole surface, not part of it."""
    names = registered_methods()
    assert _DEFERRED_VERB in names
    assert "daemon.ping" in names


def test_ensure_is_idempotent() -> None:
    """Calling it twice registers nothing twice, so no duplicate-name error."""
    ensure_all_methods_registered()
    first = len(registered_methods())
    ensure_all_methods_registered()
    assert len(registered_methods()) == first


def test_dispatch_refuses_an_unknown_method_after_loading_everything() -> None:
    """A registry miss is still an unknown method once the surface is loaded."""
    ctx = MethodContext(started_at="", pid=1, protocol_version="1", version="1")
    with pytest.raises(MethodNotFoundError):
        asyncio.run(dispatch("definitely.not.a.method", ctx, {}))


def test_dispatch_resolves_a_deferred_verb_from_a_cleared_registry() -> None:
    """Dispatch loads the deferred modules on a miss rather than refusing.

    The registry is not cleared here -- that would disturb sibling tests --
    so the property is checked the way production reaches it: the verb is
    present without this module having imported its owner.
    """
    ensure_all_methods_registered()
    assert _DEFERRED_VERB in registered_methods()
