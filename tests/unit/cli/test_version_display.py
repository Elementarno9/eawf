"""Unit tests for the version-display install-type probe.

The wave-verifiable contract for :func:`is_editable_install`:

- **CR-1 (editable flag true)** -- when the distribution's
  ``direct_url.json`` carries ``dir_info.editable: true`` the probe
  returns ``True`` without consulting the ``.git`` fallback.
- **CR-2 (editable flag false)** -- a non-editable wheel
  ``direct_url.json`` (``editable: false``) returns ``False`` when no
  ``.git`` marker is reachable.
- **CR-3 (PackageNotFoundError -> .git fallback)** -- when the
  distribution is not installed the probe falls back to ``.git``
  presence at (or above) the package import root.

Both seams are stubbed: ``Distribution.from_name`` is monkeypatched to a
stub whose ``read_text`` returns the chosen ``direct_url.json`` body (or
raises), and ``_package_import_root`` is monkeypatched onto a controlled
temp directory so the ``.git`` fallback is exercised against a planted
marker rather than the real repo.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

from eawf.surfaces.cli import _version_display
from eawf.surfaces.cli._version_display import (
    compose_display_version,
    is_editable_install,
)

_EDITABLE_DIRTY_RE = re.compile(r"^0\.6\.0\+dev\.g[0-9a-f]{12}\.dirty$")


class _StubDistribution:
    """Minimal :class:`importlib.metadata.Distribution` stand-in.

    Returns *body* from :meth:`read_text` regardless of the requested
    resource, mirroring the single-resource lookup the probe performs.
    """

    def __init__(self, body: str | None) -> None:
        self._body = body

    def read_text(self, _filename: str) -> str | None:
        return self._body


def _patch_distribution(monkeypatch: pytest.MonkeyPatch, *, body: str | None) -> None:
    """Route ``Distribution.from_name`` to a stub returning *body*."""

    def _from_name(name: str) -> _StubDistribution:
        return _StubDistribution(body)

    monkeypatch.setattr(_version_display.Distribution, "from_name", staticmethod(_from_name))


def _patch_missing_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ``Distribution.from_name`` to raise ``PackageNotFoundError``."""

    def _from_name(name: str) -> Any:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(_version_display.Distribution, "from_name", staticmethod(_from_name))


def _patch_import_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point the ``.git`` fallback search root at *root*."""
    monkeypatch.setattr(_version_display, "_package_import_root", lambda: root)


def test_is_editable_install_direct_url_editable_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``dir_info.editable: true`` returns True without the .git fallback."""
    _patch_distribution(
        monkeypatch,
        body='{"url": "file:///src/eawf", "dir_info": {"editable": true}}',
    )
    # No .git marker anywhere -- proves the direct_url path wins outright.
    _patch_import_root(monkeypatch, tmp_path)

    assert is_editable_install() is True


def test_is_editable_install_direct_url_editable_false_no_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-editable wheel direct_url.json with no .git returns False."""
    _patch_distribution(
        monkeypatch,
        body='{"url": "file:///wheel.whl", "dir_info": {"editable": false}}',
    )
    _patch_import_root(monkeypatch, tmp_path)

    assert is_editable_install() is False


def test_is_editable_install_package_not_found_no_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PackageNotFoundError with no reachable .git returns False."""
    _patch_missing_distribution(monkeypatch)
    _patch_import_root(monkeypatch, tmp_path)

    assert is_editable_install() is False


def test_is_editable_install_package_not_found_git_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PackageNotFoundError falls back to a planted .git marker -> True."""
    (tmp_path / ".git").mkdir()
    _patch_missing_distribution(monkeypatch)
    _patch_import_root(monkeypatch, tmp_path)

    assert is_editable_install() is True


def test_is_editable_install_git_fallback_walks_ancestors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The .git fallback finds a marker in an ancestor of the import root."""
    (tmp_path / ".git").mkdir()
    package_root = tmp_path / "src" / "eawf"
    package_root.mkdir(parents=True)
    _patch_missing_distribution(monkeypatch)
    _patch_import_root(monkeypatch, package_root)

    assert is_editable_install() is True


def test_is_editable_install_git_fallback_worktree_file_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worktree stores .git as a file -- presence still counts -> True."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    _patch_missing_distribution(monkeypatch)
    _patch_import_root(monkeypatch, tmp_path)

    assert is_editable_install() is True


def test_is_editable_install_direct_url_absent_git_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent direct_url.json (read_text -> None) falls back to .git -> True."""
    (tmp_path / ".git").mkdir()
    _patch_distribution(monkeypatch, body=None)
    _patch_import_root(monkeypatch, tmp_path)

    assert is_editable_install() is True


def test_is_editable_install_missing_dir_info_key_git_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """direct_url.json lacking dir_info.editable falls back to .git presence."""
    _patch_distribution(
        monkeypatch,
        body='{"url": "file:///somewhere", "vcs_info": {"vcs": "git"}}',
    )
    (tmp_path / ".git").mkdir()
    _patch_import_root(monkeypatch, tmp_path)

    assert is_editable_install() is True


def test_is_editable_install_unparseable_direct_url_git_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unparseable direct_url.json falls back to .git presence -> True."""
    _patch_distribution(monkeypatch, body="{not json")
    (tmp_path / ".git").mkdir()
    _patch_import_root(monkeypatch, tmp_path)

    assert is_editable_install() is True


def test_is_editable_install_dir_info_not_a_mapping_git_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-mapping dir_info is ignored; the .git fallback decides."""
    _patch_distribution(monkeypatch, body='{"dir_info": "editable"}')
    _patch_import_root(monkeypatch, tmp_path)

    assert is_editable_install() is False


# --- compose_display_version (the dev local-segment composer) ---------------


def _patch_editable(monkeypatch: pytest.MonkeyPatch, *, editable: bool) -> None:
    """Pin :func:`is_editable_install` so the composer takes a known branch."""
    monkeypatch.setattr(_version_display, "is_editable_install", lambda: editable)


def _patch_git(monkeypatch: pytest.MonkeyPatch, responses: dict[str, str | None]) -> None:
    """Route ``_git_output`` to *responses* keyed by the first git arg.

    The composer issues ``rev-parse`` (for the short SHA) then ``status``
    (for the dirty probe); the stub returns the body mapped to the leading
    argument so a test can pin each independently.
    """

    def _fake_git(args: list[str]) -> str | None:
        return responses.get(args[0])

    monkeypatch.setattr(_version_display, "_git_output", _fake_git)


def test_compose_display_version_editable_dirty_matches_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editable + dirty tree -> base+dev.g<12hex>.dirty, PEP 440-parseable."""
    _patch_editable(monkeypatch, editable=True)
    _patch_git(
        monkeypatch,
        {"rev-parse": "0123456789ab", "status": " M src/eawf/foo.py"},
    )

    composed = compose_display_version(base="0.6.0")

    assert _EDITABLE_DIRTY_RE.match(composed)
    assert composed == "0.6.0+dev.g0123456789ab.dirty"
    parsed = Version(composed)
    assert parsed.base_version == "0.6.0"
    assert parsed.local == "dev.g0123456789ab.dirty"


def test_compose_display_version_editable_clean_omits_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editable + clean tree -> base+dev.g<sha>, no .dirty suffix."""
    _patch_editable(monkeypatch, editable=True)
    _patch_git(monkeypatch, {"rev-parse": "abcdef012345", "status": ""})

    composed = compose_display_version(base="0.6.0")

    assert composed == "0.6.0+dev.gabcdef012345"
    assert ".dirty" not in composed
    assert Version(composed).local == "dev.gabcdef012345"


def test_compose_display_version_wheel_path_returns_clean_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-editable (wheel) path returns the bare base unchanged."""
    _patch_editable(monkeypatch, editable=False)
    # Wheel path must never shell out to git.
    _patch_git(monkeypatch, {"rev-parse": "deadbeefcafe", "status": " M x"})

    assert compose_display_version(base="0.6.0") == "0.6.0"
    assert Version(compose_display_version(base="0.6.0")) == Version("0.6.0")


def test_compose_display_version_default_base_is_package_version() -> None:
    """The default base argument is the stored ``__version__`` (0.6.5)."""
    from eawf import __version__

    assert __version__ == "0.6.5"


def test_compose_display_version_missing_sha_falls_back_to_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rev-parse (None short SHA) degrades to the bare base."""
    _patch_editable(monkeypatch, editable=True)
    _patch_git(monkeypatch, {"rev-parse": None, "status": " M x"})

    assert compose_display_version(base="0.6.0") == "0.6.0"


def test_compose_display_version_empty_sha_falls_back_to_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-string short SHA (detached/broken) degrades to the base."""
    _patch_editable(monkeypatch, editable=True)
    _patch_git(monkeypatch, {"rev-parse": "", "status": " M x"})

    assert compose_display_version(base="0.6.0") == "0.6.0"


def test_git_output_returns_none_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-zero git exit is swallowed and reported as None (fail-soft)."""

    class _Result:
        returncode = 1
        stdout = "garbage"

    monkeypatch.setattr(_version_display, "_package_import_root", lambda: tmp_path)
    monkeypatch.setattr(_version_display.subprocess, "run", lambda *a, **k: _Result())

    assert _version_display._git_output(["rev-parse", "--short=12", "HEAD"]) is None


def test_git_output_returns_none_on_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing git executable (OSError) is swallowed -> None."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(_version_display, "_package_import_root", lambda: tmp_path)
    monkeypatch.setattr(_version_display.subprocess, "run", _boom)

    assert _version_display._git_output(["status", "--porcelain"]) is None


def test_git_output_strips_stdout_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A zero-exit git call returns stripped stdout."""

    class _Result:
        returncode = 0
        stdout = "  0123456789ab\n"

    monkeypatch.setattr(_version_display, "_package_import_root", lambda: tmp_path)
    monkeypatch.setattr(_version_display.subprocess, "run", lambda *a, **k: _Result())

    assert _version_display._git_output(["rev-parse", "--short=12", "HEAD"]) == "0123456789ab"
