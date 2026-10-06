"""Tests for the ``vcs.checkpoint_requires_commit`` cadence resolver."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.runtime.vcs.checkpoint import requires_checkpoint_commit, resolve_checkpoint_cadence
from eawf.runtime.vcs.coauthor import VcsConfig


def test_resolve_checkpoint_cadence_rejects_non_bool_leaf(tmp_path: Path) -> None:
    """A non-bool ``vcs.checkpoint_requires_commit`` leaf fails loudly."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    (ea_dir / "config.yaml").write_text(
        "vcs:\n  checkpoint_requires_commit: maybe\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError):
        resolve_checkpoint_cadence(tmp_path)


def test_resolve_checkpoint_cadence_without_root_is_inert() -> None:
    """No root resolves to ``False`` -- the cadence has nothing to verify against."""
    assert resolve_checkpoint_cadence(None) is False


def test_requires_checkpoint_commit_reads_the_typed_flag() -> None:
    """The pure predicate mirrors the validated ``VcsConfig`` field."""
    payload = dict(BUILT_IN_DEFAULTS["vcs"])
    payload["checkpoint_requires_commit"] = False

    assert requires_checkpoint_commit(VcsConfig.model_validate(payload)) is False
    payload["checkpoint_requires_commit"] = True
    assert requires_checkpoint_commit(VcsConfig.model_validate(payload)) is True
