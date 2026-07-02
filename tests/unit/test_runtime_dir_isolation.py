"""Guard: the suite's daemon runtime dir is isolated from live ``~/.eawfd``.

Proves the ``runtime_dir_isolation`` autouse fixture (``tests/conftest.py``,
P30-I23-W14) holds three invariants for every worker process:

* ``EAWF_RUNTIME_DIR`` resolves to a per-worker tmp dir under ``$TMPDIR``,
  never the operator's live ``~/.eawfd``;
* the resolved socket path fits the 104-byte macOS AF_UNIX ``sun_path``
  cap, so a real daemon bind under it would succeed; and
* the live ``~/.eawfd`` directory signature is unchanged between the
  fixture's setup (the "before" snapshot) and test time (the "after"),
  so the suite never spawned or rebound a daemon in the live runtime dir.

Gate G-01 runs this file alone, so its session is exactly this file: the
before/after comparison then spans the whole gated run.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from eawf.runtime.daemon.runtime_dir import runtime_dir, socket_path
from tests.conftest import RuntimeDirIsolation, home_runtime_dir_signature

# macOS caps the AF_UNIX sun_path at 104 bytes (Linux allows 108); use the
# tighter bound so a socket that binds on macOS binds everywhere.
_AF_UNIX_SUN_PATH_CAP: int = 104


def test_env_var_matches_isolated_dir(runtime_dir_isolation: RuntimeDirIsolation) -> None:
    """``runtime_dir()`` resolves to the fixture's per-worker tmp dir."""
    assert runtime_dir() == runtime_dir_isolation.runtime_dir


def test_runtime_dir_redirected_off_home(runtime_dir_isolation: RuntimeDirIsolation) -> None:
    """The resolved runtime dir is neither the live ``~/.eawfd`` nor under it."""
    resolved = runtime_dir()
    home_default = Path.home() / ".eawfd"
    assert resolved != home_default
    assert home_default not in resolved.parents


def test_runtime_dir_inside_tmp_tree(runtime_dir_isolation: RuntimeDirIsolation) -> None:
    """The resolved runtime dir lives inside the pytest ``$TMPDIR`` tree.

    ``resolve()`` both sides so the macOS ``/var`` -> ``/private/var``
    symlink does not spuriously fail the containment check.
    """
    tmp_root = Path(tempfile.gettempdir()).resolve()
    assert tmp_root in runtime_dir().resolve().parents


def test_socket_path_fits_afunix_cap(runtime_dir_isolation: RuntimeDirIsolation) -> None:
    """The bind address fits the AF_UNIX ``sun_path`` cap the daemon binds at."""
    sock = socket_path()
    assert len(str(sock).encode()) < _AF_UNIX_SUN_PATH_CAP


def test_home_runtime_dir_untouched(runtime_dir_isolation: RuntimeDirIsolation) -> None:
    """The live ``~/.eawfd`` signature is unchanged before vs after the run."""
    before = runtime_dir_isolation.home_signature_before
    after = home_runtime_dir_signature()
    assert after == before
