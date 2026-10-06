"""The configuration reference names every live leaf and every ``EAWF_`` control variable.

The leaf list comes from the leaf catalog and the variable list from the same AST
census that keeps control variables out of the config merge, so a leaf or variable
added to the code without a row in ``docs/reference/config.md`` reds here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY
from eawf.kernel.config.registry.leaf_keys import LeafKey
from tests.lint.test_env_control_var_census import (
    _DOUBLE_UNDERSCORE_OVERRIDE_PATTERN,
    _control_var_literals,
    _scanned_files,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_DOC = _REPO_ROOT / "docs" / "reference" / "config.md"


def _documented_names(doc: str) -> frozenset[str]:
    """Return every inline-code span in *doc*, the form each leaf and variable is named in."""
    return frozenset(re.findall(r"`([^`\n]+)`", doc))


def _undescribed_live_leaves(registry: dict[str, LeafKey]) -> list[str]:
    """Return the non-reserved leaves of *registry* whose description is blank, sorted."""
    return sorted(
        key
        for key, entry in registry.items()
        if not entry.reserved and not entry.description.strip()
    )


def _control_variables(root: Path) -> frozenset[str]:
    """Return every ``EAWF_`` control variable the census finds under *root*.

    A reserved prefix stem (``EAWF_DAEMON_LOG_``) names a family, not a variable, and
    the ``EAWF_<SECTION>__<KEY>`` shape is a config override covered by the leaf rows.
    """
    literals: set[str] = set()
    for path in _scanned_files(root):
        literals |= _control_var_literals(path.read_text(encoding="utf-8"), filename=str(path))
    return frozenset(
        name
        for name in literals
        if not name.endswith("_") and not _DOUBLE_UNDERSCORE_OVERRIDE_PATTERN.match(name)
    )


# --- helpers: boundary + error paths ----------------------------------------


def test_documented_names_of_empty_doc_is_empty() -> None:
    assert _documented_names("") == frozenset()


def test_documented_names_reads_a_single_span() -> None:
    assert _documented_names("set `ui.theme` here") == frozenset({"ui.theme"})


def test_documented_names_ignores_an_unclosed_span() -> None:
    assert _documented_names("`ui.theme\nnext line`") == frozenset()


def test_documented_names_ignores_a_bare_mention() -> None:
    """A key outside backticks is prose, not a documented row."""
    assert _documented_names("ui.theme and EAWF_HOME") == frozenset()


def test_documented_names_rejects_a_non_string() -> None:
    with pytest.raises(TypeError):
        _documented_names(None)  # type: ignore[arg-type]


def test_undescribed_live_leaves_of_empty_registry_is_empty() -> None:
    assert _undescribed_live_leaves({}) == []


def test_undescribed_live_leaves_flags_a_blank_description() -> None:
    entry = LeafKey(key="ui.example", domain="ui", type="bool", description="   ")
    assert _undescribed_live_leaves({entry.key: entry}) == ["ui.example"]


def test_undescribed_live_leaves_skips_a_reserved_leaf() -> None:
    entry = LeafKey(
        key="ui.example", domain="ui", type="bool", consumer_kind="reserved", reserved=True
    )
    assert _undescribed_live_leaves({entry.key: entry}) == []


def test_undescribed_live_leaves_raises_on_a_non_leaf_value() -> None:
    with pytest.raises(AttributeError):
        _undescribed_live_leaves({"ui.example": "not a leaf"})  # type: ignore[dict-item]


def test_control_variables_of_empty_tree_is_empty(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    assert _control_variables(tmp_path) == frozenset()


def test_control_variables_drops_prefix_stems_and_override_shapes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "knobs.py").write_text(
        'A = "EAWF_SEEDED_KNOB"\nB = "EAWF_DAEMON_LOG_"\nC = "EAWF_UI__THEME"\n',
        encoding="utf-8",
    )
    assert _control_variables(tmp_path) == frozenset({"EAWF_SEEDED_KNOB"})


def test_control_variables_raises_on_unparseable_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "broken.py").write_text("def (:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        _control_variables(tmp_path)


# --- the real catalog, census and doc agree ---------------------------------


def test_every_live_leaf_has_a_description() -> None:
    assert _undescribed_live_leaves(dict(LEAF_KEY_REGISTRY)) == []


def test_every_catalog_leaf_is_named_in_the_reference() -> None:
    documented = _documented_names(_CONFIG_DOC.read_text(encoding="utf-8"))
    missing = sorted(set(LEAF_KEY_REGISTRY) - documented)
    assert missing == [], f"docs/reference/config.md omits config leaves: {missing}"


def test_every_control_variable_is_named_in_the_reference() -> None:
    variables = _control_variables(_REPO_ROOT)
    assert {"EAWF_DAEMON_IDLE_TIMEOUT", "EAWF_COAUTHOR_MODE"} <= variables
    documented = _documented_names(_CONFIG_DOC.read_text(encoding="utf-8"))
    missing = sorted(variables - documented)
    assert missing == [], f"docs/reference/config.md omits environment variables: {missing}"


def test_a_seeded_undocumented_variable_reds_the_check(tmp_path: Path) -> None:
    """The comparison is not vacuous: a fresh variable absent from the doc is reported."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "knob.py").write_text(
        'import os\nos.environ.get("EAWF_SEEDED_KNOB")\n', encoding="utf-8"
    )
    documented = _documented_names(_CONFIG_DOC.read_text(encoding="utf-8"))
    assert sorted(_control_variables(tmp_path) - documented) == ["EAWF_SEEDED_KNOB"]
