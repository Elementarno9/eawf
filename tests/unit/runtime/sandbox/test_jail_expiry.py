"""Sunset guard for the self-built Linux jail and its REL-034 fixes.

The bubblewrap backend is temporary: the runtime cutover retires the
self-built jail in favour of the runtimes' own sandboxing, and when it
lands the two REL-034 fixes -- the per-platform ``TMPDIR`` pin and the
conditional cred-dir masks -- must leave with it rather than linger as
orphaned special-casing nobody can explain.

This module is the tripwire that makes that happen. While the Linux jail
backend exists these tests pass and cost nothing; the moment the cutover
deletes it they raise ``AssertionError`` with the exact follow-up list, so
the sunset is a deliberate act instead of an oversight.

They also pin the fixes' blast radius: each is reachable through ONE
production module (the tmpfs masks through the jail, the ``TMPDIR`` pin
through the env scrub that the jail shares its temp path with). A third
consumer would make the jail's retirement a cross-cutting migration, which
is exactly what a soon-to-be-deleted backend must not become.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SRC = _REPO_ROOT / "src" / "eawf"
_SANDBOX = _SRC / "runtime" / "sandbox"
_JAIL = _SANDBOX / "jail.py"
_ENV_SCRUB = _SANDBOX / "env_scrub.py"

#: What the cutover has to remove alongside the backend, named in every
#: failure message so the red test reads as a checklist rather than a
#: puzzle.
_SUNSET_TODO = (
    "The self-built jail is gone: delete the per-platform TMPDIR pin "
    "(pinned_tmpdir in env_scrub.py), the conditional cred-dir masks "
    "(_build_linux_argv in jail.py), the linux-jail CI job in "
    ".github/workflows/ci.yaml with its lint gate in "
    "tests/lint/test_phase_ci_gates.py, and this expiry module."
)


def _production_modules_containing(needle: str) -> set[Path]:
    """Return every ``src/eawf`` module whose source contains *needle*.

    Args:
        needle: The literal source fragment to look for.

    Returns:
        The set of matching module paths, relative to the repo root, so a
        failure message names files rather than absolute machine paths.
    """
    return {
        path.relative_to(_REPO_ROOT)
        for path in _SRC.rglob("*.py")
        if needle in path.read_text(encoding="utf-8")
    }


def test_linux_jail_backend_still_exists() -> None:
    """The Linux jail backend is still here, so its fixes are still load-bearing."""
    assert _JAIL.exists(), _SUNSET_TODO
    assert "_build_linux_argv" in _JAIL.read_text(encoding="utf-8"), _SUNSET_TODO


def test_tmpfs_masks_are_reachable_only_through_the_linux_jail() -> None:
    """Exactly one production module emits a tmpfs mask: the jail backend."""
    emitters = _production_modules_containing("--tmpfs")
    assert emitters == {_JAIL.relative_to(_REPO_ROOT)}, (
        f"the tmpfs-mask fix leaked out of the jail backend into {emitters}; "
        f"it must expire with the jail. {_SUNSET_TODO}"
    )


def test_the_tmpdir_pin_is_reachable_only_through_the_jail_and_its_env_scrub() -> None:
    """The TMPDIR pin has exactly two consumers: the env scrub and the jail.

    The pin exists BECAUSE the jail confines writes; a third consumer would
    outlive the jail and turn the cutover into a migration.
    """
    consumers = _production_modules_containing("pinned_tmpdir(")
    assert consumers == {
        _ENV_SCRUB.relative_to(_REPO_ROOT),
        _JAIL.relative_to(_REPO_ROOT),
    }, f"the TMPDIR pin gained a consumer outside the jail seam: {consumers}. {_SUNSET_TODO}"


def test_the_expiry_guard_reds_on_a_missing_backend(tmp_path: Path) -> None:
    """The guard fires on the real event it exists to catch: a deleted backend.

    Runs the same predicate against a tree where the backend is absent, so
    the tripwire is proven to trip rather than assumed to.
    """
    absent = tmp_path / "jail.py"
    with pytest.raises(AssertionError, match="self-built jail is gone"):
        assert absent.exists(), _SUNSET_TODO
