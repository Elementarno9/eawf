"""A canary's runtime directory lives inside its own tree and leaves with it.

``provision_canary`` allocates the daemon runtime directory the canary is
served from. It must land under the root it was handed, never in the
system temp directory, so that nothing a canary allocates can outlive the
tree: a failed provision removes it at once, and a teardown removes it
together with the tree. ``tempfile.tempdir`` is redirected to a scratch
directory in every test so an allocation that ignored the root would be
visible there instead of leaking into the real temp directory.
"""

from __future__ import annotations

import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.platform.install import canary as canary_module
from eawf.platform.install.canary import (
    RUNTIME_DIR_PREFIX,
    RUNTIME_PARENT_LOCATOR,
    CanaryProvision,
    CanaryProvisionError,
    canary_ref,
    discard_canary,
    provision_canary,
)

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect bare temp allocations so a leak shows up in this directory."""
    path = tmp_path / "scratch"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


def _provision(repo_root: Path, code: str = "RTD") -> CanaryProvision:
    return provision_canary(repo_root=repo_root, ref=canary_ref(code), provisioned_at=AT)


def _runtime_dirs(parent: Path) -> list[Path]:
    return sorted(parent.glob(f"{RUNTIME_DIR_PREFIX}*"))


def test_provision_canary_allocates_the_runtime_dir_under_the_given_root(
    tmp_path: Path, scratch: Path
) -> None:
    provision = _provision(tmp_path / "repo")

    root = (tmp_path / "repo").resolve()
    assert provision.runtime_dir.parent == root / ".ea" / RUNTIME_PARENT_LOCATOR
    assert provision.runtime_dir.is_relative_to(root)
    assert provision.runtime_dir.name.startswith(RUNTIME_DIR_PREFIX)
    assert provision.runtime_dir.is_dir()
    assert list(provision.runtime_dir.iterdir()) == []
    assert _runtime_dirs(scratch) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_provision_canary_runtime_dir_is_owner_only(tmp_path: Path, scratch: Path) -> None:
    provision = _provision(tmp_path / "repo")

    assert stat.S_IMODE(provision.runtime_dir.stat().st_mode) == 0o700


def test_provision_canary_gives_each_canary_its_own_runtime_dir(
    tmp_path: Path, scratch: Path
) -> None:
    """Boundary: two canaries under one code still never share a directory."""
    first = _provision(tmp_path / "one")
    second = _provision(tmp_path / "two")

    assert first.runtime_dir != second.runtime_dir
    assert first.runtime_dir.is_relative_to(first.root)
    assert second.runtime_dir.is_relative_to(second.root)


def test_discard_canary_removes_the_runtime_dir_with_the_tree(
    tmp_path: Path, scratch: Path
) -> None:
    provision = _provision(tmp_path / "repo")

    teardown = discard_canary(provision, removed_registry_codes=())

    assert teardown.runtime_dir_removed is True
    assert not provision.runtime_dir.exists()
    assert not provision.root.exists()
    assert _runtime_dirs(scratch) == []


@pytest.mark.parametrize("failure", [OSError("disk full"), KeyboardInterrupt()])
def test_provision_canary_removes_the_runtime_dir_when_the_record_write_fails(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    """Error path: an interrupted provision leaves no runtime dir behind."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(canary_module, "write_json_record", refuse)

    with pytest.raises(type(failure)):
        _provision(tmp_path / "repo")

    parent = (tmp_path / "repo").resolve() / ".ea" / RUNTIME_PARENT_LOCATOR
    assert _runtime_dirs(parent) == []
    assert _runtime_dirs(scratch) == []


def test_provision_canary_refusal_allocates_no_second_runtime_dir(
    tmp_path: Path, scratch: Path
) -> None:
    """Error path: re-provisioning a declared tree is refused before allocating."""
    provision = _provision(tmp_path / "repo")

    with pytest.raises(CanaryProvisionError, match="already declares"):
        _provision(tmp_path / "repo")

    assert _runtime_dirs(provision.runtime_dir.parent) == [provision.runtime_dir]
