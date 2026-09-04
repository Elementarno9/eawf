"""Pins ``vcs.conventions.subject_style`` to its ``trailer`` default.

The trailer commit form -- a bare ``<type>: <summary>`` subject carrying an
``Eawf-Wave: P##-I##-W##`` body trailer -- is the written form. Three
independently-editable sources declare that default and must agree, or a
repository silently writes the deprecated bracket-prefix form:

- :data:`eawf.kernel.config.defaults.BUILT_IN_DEFAULTS` -- the built-in
  layer the loader merges from.
- :class:`eawf.kernel.config.schema.VcsConventionsConfig` -- the validating
  model a hand-written ``.ea/config.yaml`` is checked against.
- the ``vcs.conventions.subject_style`` row in
  :data:`eawf.kernel.config.registry.leaf_catalog.LEAF_KEY_REGISTRY` -- the
  operator-facing catalog the config surfaces read.

``tools/commit_prefix_lint.py`` carries a hand-mirrored fourth copy (the
commit-msg hook runs under system Python and cannot import the package), so
its constant is pinned against the built-in layer here too.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.layered import merge_config
from eawf.kernel.config.registry.leaf_catalog import leaf_key_lookup
from eawf.kernel.config.schema import VcsConventionsConfig

_SUBJECT_STYLE_KEY = "vcs.conventions.subject_style"
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_commit_prefix_lint() -> Any:
    """Import ``tools/commit_prefix_lint.py`` by path (it ships outside the package)."""
    lint_path = _REPO_ROOT / "tools" / "commit_prefix_lint.py"
    tool_dir = str(lint_path.parent)
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    spec = importlib.util.spec_from_file_location("commit_prefix_lint", lint_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = module
    spec.loader.exec_module(module)
    return module


def test_layered_config_without_a_vcs_override_resolves_trailer(tmp_path: Path) -> None:
    """A repo whose ``.ea/config.yaml`` declares no ``vcs`` block reads ``trailer``."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "config.yaml").write_text(
        "schema_version: '1.0'\nplanning:\n  max_parallel_waves: 4\n",
        encoding="utf-8",
    )

    merged, sources = merge_config(repo=repo, env={}, cli_overrides={})

    assert merged["vcs"]["conventions"]["subject_style"] == "trailer"
    assert sources[_SUBJECT_STYLE_KEY] == "built-in"


def test_layered_config_with_no_layers_at_all_resolves_trailer() -> None:
    """Boundary: the empty layer stack falls through to the built-in default."""
    merged, sources = merge_config(workspace=None, repo=None, env={}, cli_overrides={})

    assert merged["vcs"]["conventions"]["subject_style"] == "trailer"
    assert sources[_SUBJECT_STYLE_KEY] == "built-in"


def test_layered_config_repo_override_still_wins(tmp_path: Path) -> None:
    """A repo layer that does declare ``bracket`` still overrides the default."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "config.yaml").write_text(
        "vcs:\n  conventions:\n    subject_style: bracket\n",
        encoding="utf-8",
    )

    merged, sources = merge_config(repo=repo, env={}, cli_overrides={})

    assert merged["vcs"]["conventions"]["subject_style"] == "bracket"
    assert sources[_SUBJECT_STYLE_KEY] == "repo"


def test_built_in_defaults_declare_trailer() -> None:
    """The built-in layer's literal value is ``trailer``."""
    assert BUILT_IN_DEFAULTS["vcs"]["conventions"]["subject_style"] == "trailer"


def test_conventions_model_defaults_to_trailer() -> None:
    """An empty ``vcs.conventions`` block validates to the trailer form."""
    conventions = VcsConventionsConfig()

    assert conventions.subject_style == "trailer"
    assert conventions.wave_trailer == "Eawf-Wave"


def test_conventions_model_rejects_an_unknown_subject_style() -> None:
    """Error path: the style is a closed Literal, not free text."""
    with pytest.raises(ValidationError, match="subject_style"):
        VcsConventionsConfig.model_validate({"subject_style": "prefix"})


def test_conventions_model_rejects_an_extra_conventions_key() -> None:
    """Error path: ``extra="forbid"`` rejects a typo'd sibling key."""
    with pytest.raises(ValidationError, match="extra"):
        VcsConventionsConfig.model_validate({"subject_style": "trailer", "subject_stile": "x"})


def test_leaf_catalog_row_declares_the_trailer_default() -> None:
    """The operator-facing catalog row agrees with the built-in layer."""
    leaf = leaf_key_lookup(_SUBJECT_STYLE_KEY)

    assert leaf.default == "trailer"
    assert leaf.type == "literal"
    assert leaf.choices == ("bracket", "trailer")
    assert leaf.domain == "vcs"
    assert leaf.default in leaf.choices


def test_leaf_catalog_row_is_writable_by_the_repo_layer() -> None:
    """A repo can pin the style back to ``bracket`` through the catalog."""
    leaf = leaf_key_lookup(_SUBJECT_STYLE_KEY)

    assert "repo" in leaf.writable_layers


def test_leaf_catalog_lookup_rejects_an_unknown_sibling_key() -> None:
    """Error path: an off-by-one key spelling raises rather than defaulting."""
    with pytest.raises(ValueError, match="unknown config key"):
        leaf_key_lookup("vcs.conventions.subject_styles")


def test_commit_prefix_lint_default_mirrors_the_built_in_layer() -> None:
    """The hook's hand-mirrored constant tracks the built-in layer."""
    lint = _load_commit_prefix_lint()

    assert BUILT_IN_DEFAULTS["vcs"]["conventions"]["subject_style"] == lint._SUBJECT_STYLE_DEFAULT


def test_commit_prefix_lint_resolves_trailer_for_a_repo_without_a_config(
    tmp_path: Path,
) -> None:
    """Boundary: a repo root with no ``.ea/config.yaml`` at all reads ``trailer``."""
    lint = _load_commit_prefix_lint()
    repo = tmp_path / "bare-repo"
    (repo / ".ea").mkdir(parents=True)

    assert lint._configured_subject_style(repo) == "trailer"
