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

from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

import pytest

from eawf.surfaces.cli import _version_display
from eawf.surfaces.cli._version_display import is_editable_install


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
