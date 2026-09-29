"""SURF-117: the statusline reads the host rate-limit block and keeps no idle helper.

Each window of the host's ``rate_limits`` block renders as its own bar with its used
percentage and reset time, and a window the block omits or states without a usable
fraction is left out rather than drawn as zero. Every public render helper of the
statusline has a production caller, and the orchestrator's per-session line cache is
the only cache the statusline keeps.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import rate_window
from eawf.surfaces.render import statusline

_SRC = Path(statusline.__file__).resolve().parents[2]


def _payload(**windows: Any) -> dict[str, Any]:
    return {"rate_limits": windows}


def test_surf_117_each_host_window_renders_its_fraction_and_reset() -> None:
    segment = rate_window.build(
        _payload(
            five_hour={"used_percentage": 25, "resets_at": 1_790_000_000},
            seven_day={"utilization": 0.5, "resets_at": "2026-09-30T08:00:00+00:00"},
        ),
        None,
    )
    assert segment.module == "rate_window"
    five, seven = segment.text.removeprefix("rate:").split(" · ")
    assert five.startswith("five_hour ") and " 25% ↻" in five
    assert seven.endswith("50% ↻08:00Z")
    assert segment.truth.producer == "claude-code.statusline-payload"


def test_surf_117_one_unusable_window_never_hides_the_others() -> None:
    segment = rate_window.build(
        _payload(five_hour={"resets_at": 1}, seven_day={"used_percentage": 10}), None
    )
    assert segment.text.startswith("rate:seven_day ")
    assert "five_hour" not in segment.text


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "no-rate-limits"),
        ({"rate_limits": {}}, "no-rate-limits"),
        ({"rate_limits": "full"}, "no-rate-limits"),
        (_payload(w={"used_percentage": 101}), "no-usable-window"),
        (_payload(w={"utilization": -0.1}), "no-usable-window"),
        (_payload(w={"used_percentage": True}), "no-usable-window"),
        (_payload(w=[1, 2]), "no-usable-window"),
    ],
)
def test_surf_117_an_absent_or_unusable_block_is_named_never_zero(
    payload: dict[str, Any], reason: str
) -> None:
    segment = rate_window.build(payload, None)
    assert segment.text == f"rate:n/a({reason})"
    assert segment.truth.value is None


def test_surf_117_an_unparseable_reset_keeps_the_fraction() -> None:
    segment = rate_window.build(_payload(w={"used_percentage": 0, "resets_at": "soon"}), None)
    assert segment.text.endswith(" 0%")


def test_surf_117_the_orchestrator_renders_the_rate_window_segment() -> None:
    assert rate_window in orchestrator._MODULE_ORDER


def _referenced_names(root: Path, *, skip: Path) -> set[str]:
    names: set[str] = set()
    for path in root.rglob("*.py"):
        if path == skip:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == statusline.__name__:
                names.update(alias.name for alias in node.names)
    return names


def test_surf_117_every_public_render_helper_has_a_production_caller() -> None:
    functions = {
        name for name in statusline.__all__ if inspect.isfunction(getattr(statusline, name))
    }
    used = _referenced_names(_SRC, skip=Path(statusline.__file__).resolve())
    internal = Path(statusline.__file__).read_text(encoding="utf-8")
    idle = sorted(name for name in functions if name not in used and internal.count(f"{name}(") < 2)
    assert idle == []
    for retired in ("render_rows", "context_usage_segment", "terminal_supports_color"):
        assert not hasattr(statusline, retired)


def test_surf_117_the_line_cache_is_the_only_statusline_cache() -> None:
    package = Path(rate_window.__file__).parent
    for path in [*package.glob("*.py"), Path(statusline.__file__)]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        decorators = {
            ast.unparse(decorator)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            for decorator in node.decorator_list
        }
        assert not {d for d in decorators if "cache" in d}, path.name
    assert orchestrator.cache_path_for("s").parent == orchestrator._cache_root()
