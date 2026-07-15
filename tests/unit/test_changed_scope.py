"""Unit tests for ``tools/changed_scope.py`` -- the stateless changed-scope
pytest selector.

Pins the three falsifiable behaviours the wave promises: a non-``.py`` change
forces the full golden tier; a ``src/eawf/<pkg>/<module>.py`` change maps to
its mirror test file + package directory; and the selector holds no persisted
state (same input -> same output, no side effects). Boundary + error paths are
covered alongside.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "changed_scope.py"
_TOOL_DIR = _TOOL_PATH.parent


def _load_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("changed_scope", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["changed_scope"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


# --- non-.py change forces the full golden tier ---------------------------


def test_non_py_change_forces_full_golden_tier(mod):
    result = mod.select_scope(["src/eawf/_data/templates/agents_md.j2"])
    assert result.full_golden is True
    for tier in mod.GOLDEN_TIER:
        assert tier in result.paths


def test_changed_yaml_forces_golden_tier(mod):
    result = mod.select_scope([".github/workflows/ci.yaml"])
    assert result.full_golden is True
    assert set(mod.GOLDEN_TIER).issubset(set(result.paths))


def test_golden_fixture_change_forces_golden_tier(mod):
    # A committed golden byte-file (non-.py) invalidates the whole tier.
    result = mod.select_scope(["tests/golden/agents_md/rendered.md"])
    assert result.full_golden is True
    assert set(mod.GOLDEN_TIER).issubset(set(result.paths))


# --- src module maps to mirror test file + package directory ---------------


def test_src_module_maps_to_mirror_test_file_and_package_dir(mod):
    result = mod.select_scope(["src/eawf/kernel/state/models.py"])
    assert result.full_golden is False
    assert "tests/kernel/state/test_models.py" in result.paths  # mirror test file
    assert "tests/kernel/state" in result.paths  # package directory's tests


def test_src_top_level_module_maps_to_mirror_file_only(mod):
    # A module directly under src/eawf/ has no useful package dir (it would be
    # the whole tests/ tree), so only the mirror test file is selected.
    result = mod.select_scope(["src/eawf/_version.py"])
    assert result.paths == ("tests/test__version.py",)


def test_src_package_init_selects_package_dir_only(mod):
    result = mod.select_scope(["src/eawf/kernel/__init__.py"])
    assert result.paths == ("tests/kernel",)


def test_changed_test_file_selects_itself(mod):
    result = mod.select_scope(["tests/unit/test_changed_scope.py"])
    assert result.paths == ("tests/unit/test_changed_scope.py",)


def test_other_py_contributes_nothing(mod):
    # A tools/ script or the eawf root __init__ has no mirror mapping.
    result = mod.select_scope(["tools/changed_scope.py", "src/eawf/__init__.py"])
    assert result.paths == ()
    assert result.full_golden is False


def test_union_across_mixed_change_set(mod):
    result = mod.select_scope(
        [
            "docs/x.md",  # non-.py -> golden tier
            "src/eawf/kernel/state/models.py",  # src module -> mirror + pkg dir
        ]
    )
    assert result.full_golden is True
    expected = set(mod.GOLDEN_TIER) | {
        "tests/kernel/state",
        "tests/kernel/state/test_models.py",
    }
    assert set(result.paths) == expected


# --- statelessness: same input -> same output, no side effects -------------


def test_selector_is_stateless_same_input_same_output(mod):
    changed = ["src/eawf/kernel/state/models.py", "docs/x.md"]
    first = mod.select_scope(changed)
    second = mod.select_scope(changed)
    assert first == second


def test_selector_does_not_mutate_input(mod):
    changed = ["src/eawf/kernel/state/models.py", "docs/x.md"]
    snapshot = list(changed)
    mod.select_scope(changed)
    assert changed == snapshot  # no side effect on the caller's list


def test_selector_output_order_independent(mod):
    a = mod.select_scope(["src/eawf/kernel/state/models.py", "src/eawf/runtime/spawn.py"])
    b = mod.select_scope(["src/eawf/runtime/spawn.py", "src/eawf/kernel/state/models.py"])
    assert a.paths == b.paths  # sorted union is order-independent


def test_selector_accepts_a_generator(mod):
    # A one-shot generator proves the selector reads its input exactly once and
    # keeps no persisted handle to it.
    gen = (p for p in ["src/eawf/kernel/state/models.py"])
    result = mod.select_scope(gen)
    assert "tests/kernel/state/test_models.py" in result.paths


# --- boundary + error paths ------------------------------------------------


def test_empty_change_set_selects_nothing(mod):
    result = mod.select_scope([])
    assert result.full_golden is False
    assert result.paths == ()


def test_blank_and_whitespace_entries_are_skipped(mod):
    result = mod.select_scope(["", "   "])
    assert result.paths == ()
    assert result.full_golden is False


def test_windows_separators_are_normalized(mod):
    result = mod.select_scope(["src\\eawf\\kernel\\state\\models.py"])
    assert "tests/kernel/state/test_models.py" in result.paths


def test_bare_string_input_raises_type_error(mod):
    with pytest.raises(TypeError, match="not a bare string"):
        mod.select_scope("src/eawf/kernel/state/models.py")


def test_non_string_element_raises_type_error(mod):
    with pytest.raises(TypeError, match="must be str"):
        mod.select_scope(["src/eawf/kernel/state/models.py", 42])
