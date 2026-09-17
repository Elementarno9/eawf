"""Pins the ``vcs.integration_commit_unit`` and ``vcs.task_reference`` leaves.

Each leaf is declared in four places that must agree: the built-in layer
the loader merges from, the strict ``vcs`` model a merged config is
validated against, the leaf catalog the daemon checks writes against, and
the curated registry the config menu edits. The loader path under test is
the one production readers take: :func:`merge_config` followed by
:class:`VcsConfig` validation of the merged ``vcs`` block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.layered import merge_config
from eawf.kernel.config.registry import coerce_and_validate, leaf_key_lookup, registry_lookup
from eawf.runtime.vcs.coauthor import VcsConfig
from eawf.surfaces.cli.errors import UserError

_UNIT_KEY = "vcs.integration_commit_unit"
_REFERENCE_KEY = "vcs.task_reference"


def _repo_with_vcs(tmp_path: Path, body: str) -> Path:
    """Return a repo root whose ``.ea/config.yaml`` holds *body* under ``vcs``."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "config.yaml").write_text(f"vcs:\n{body}", encoding="utf-8")
    return repo


def _load_vcs(repo: Path | None) -> VcsConfig:
    """Merge the layered config for *repo* and validate its ``vcs`` block."""
    merged, _ = merge_config(repo=repo, env={}, cli_overrides={})
    return VcsConfig.model_validate(merged["vcs"])


def _minimal_vcs(**leaves: Any) -> dict[str, Any]:
    """Return the required ``vcs`` scalars plus *leaves*."""
    base = {
        key: value
        for key, value in BUILT_IN_DEFAULTS["vcs"].items()
        if key not in {"integration_commit_unit", "task_reference", "conventions", "coauthor"}
    }
    return {**base, **leaves}


# ---- defaults -----------------------------------------------------------------


def test_merge_config_defaults_integration_commit_unit_to_batch() -> None:
    merged, sources = merge_config(workspace=None, repo=None, env={}, cli_overrides={})

    assert merged["vcs"]["integration_commit_unit"] == "batch"
    assert sources[_UNIT_KEY] == "built-in"


def test_merge_config_defaults_task_reference_to_trailer() -> None:
    merged, sources = merge_config(workspace=None, repo=None, env={}, cli_overrides={})

    assert merged["vcs"]["task_reference"] == "trailer"
    assert sources[_REFERENCE_KEY] == "built-in"


def test_vcs_config_loads_both_defaults_from_a_repo_without_the_leaves(tmp_path: Path) -> None:
    repo = _repo_with_vcs(tmp_path, "  auto_commit: never\n")

    vcs = _load_vcs(repo)

    assert vcs.integration_commit_unit == "batch"
    assert vcs.task_reference == "trailer"


def test_vcs_config_defaults_both_leaves_when_the_block_omits_them() -> None:
    """Boundary: a ``vcs`` block written before the leaves existed still loads."""
    vcs = VcsConfig.model_validate(_minimal_vcs())

    assert vcs.integration_commit_unit == "batch"
    assert vcs.task_reference == "trailer"


# ---- accepted values ----------------------------------------------------------


def test_vcs_config_accepts_task_integration_commit_unit(tmp_path: Path) -> None:
    repo = _repo_with_vcs(tmp_path, "  integration_commit_unit: task\n")

    merged, sources = merge_config(repo=repo, env={}, cli_overrides={})
    vcs = VcsConfig.model_validate(merged["vcs"])

    assert vcs.integration_commit_unit == "task"
    assert sources[_UNIT_KEY] == "repo"


@pytest.mark.parametrize("value", ["trailer", "subject", "none"])
def test_vcs_config_accepts_each_declared_task_reference(tmp_path: Path, value: str) -> None:
    repo = _repo_with_vcs(tmp_path, f"  task_reference: '{value}'\n")

    assert _load_vcs(repo).task_reference == value


# ---- rejected values ----------------------------------------------------------


@pytest.mark.parametrize("value", ["squash", "BATCH", "", "milestone"])
def test_vcs_config_rejects_an_undeclared_integration_commit_unit(
    tmp_path: Path, value: str
) -> None:
    repo = _repo_with_vcs(tmp_path, f"  integration_commit_unit: '{value}'\n")

    with pytest.raises(ValidationError, match="integration_commit_unit"):
        _load_vcs(repo)


@pytest.mark.parametrize("value", ["footer", "Trailer", "", "both"])
def test_vcs_config_rejects_an_undeclared_task_reference(tmp_path: Path, value: str) -> None:
    repo = _repo_with_vcs(tmp_path, f"  task_reference: '{value}'\n")

    with pytest.raises(ValidationError, match="task_reference"):
        _load_vcs(repo)


def test_vcs_config_rejects_a_boolean_integration_commit_unit(tmp_path: Path) -> None:
    """Error path: a YAML ``true`` is not a unit name."""
    repo = _repo_with_vcs(tmp_path, "  integration_commit_unit: true\n")

    with pytest.raises(ValidationError, match="integration_commit_unit"):
        _load_vcs(repo)


def test_vcs_config_rejects_a_null_task_reference() -> None:
    """Error path: an explicit null does not fall back to the default."""
    with pytest.raises(ValidationError, match="task_reference"):
        VcsConfig.model_validate(_minimal_vcs(task_reference=None))


def test_vcs_config_rejects_a_misspelled_leaf() -> None:
    """Error path: ``extra="forbid"`` refuses a near-miss key."""
    with pytest.raises(ValidationError, match="integration_commit_units"):
        VcsConfig.model_validate(_minimal_vcs(integration_commit_units="task"))


# ---- catalog and registry agreement -------------------------------------------


@pytest.mark.parametrize(
    ("key", "default", "choices"),
    [
        (_UNIT_KEY, "batch", ("batch", "task")),
        (_REFERENCE_KEY, "trailer", ("trailer", "subject", "none")),
    ],
)
def test_leaf_catalog_declares_the_leaf_as_a_closed_literal(
    key: str, default: str, choices: tuple[str, ...]
) -> None:
    leaf = leaf_key_lookup(key)

    assert leaf.type == "literal"
    assert leaf.domain == "vcs"
    assert leaf.default == default
    assert leaf.choices == choices
    assert "repo" in leaf.writable_layers
    assert not leaf.reserved


@pytest.mark.parametrize(
    ("key", "leaf_name"),
    [(_UNIT_KEY, "integration_commit_unit"), (_REFERENCE_KEY, "task_reference")],
)
def test_registry_row_mirrors_the_leaf_catalog_and_built_in_layer(key: str, leaf_name: str) -> None:
    entry = registry_lookup(key)
    leaf = leaf_key_lookup(key)

    assert entry is not None
    assert entry.type == "choice"
    assert entry.choices == leaf.choices
    assert entry.default == leaf.default == BUILT_IN_DEFAULTS["vcs"][leaf_name]


@pytest.mark.parametrize(("key", "value"), [(_UNIT_KEY, "squash"), (_REFERENCE_KEY, "footer")])
def test_registry_coercion_rejects_an_undeclared_value(key: str, value: str) -> None:
    entry = registry_lookup(key)
    assert entry is not None

    with pytest.raises(UserError, match="not in choices"):
        coerce_and_validate(entry, value)


def test_leaf_catalog_lookup_rejects_an_unknown_sibling_key() -> None:
    """Error path: an off-by-one spelling is unknown, not defaulted."""
    with pytest.raises(ValueError, match="unknown config key"):
        leaf_key_lookup("vcs.task_references")
