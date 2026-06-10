"""Unit tests for the legacy-to-typed backfill CLI shim (P30-I10-W01).

Covers the standalone ``tools/eawf021_measurable_criterion.py`` shim that drives
:func:`eawf.kernel.spec.common.backfill_legacy_criteria` over a SAMPLE fixture
and asserts the active-criteria legacy count drops to ZERO:

- the tool exits ``0`` on a well-formed sample whose strings all convert;
- a missing-file / non-object / empty-list sample exits ``1`` with a stderr
  reason (the malformed-input error path);
- a sample with an empty ``file_scopes`` exits ``1`` (conversion raises).

The shim is loaded via :mod:`importlib` because ``tools/`` is excluded from the
package and so is not importable by name -- mirroring the existing
``tests/unit/test_sigil_totality_gate.py`` loader.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import orjson

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "eawf021_measurable_criterion.py"


def _load_tool() -> ModuleType:
    """Import the backfill shim by file path (``tools/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("eawf021_measurable_criterion", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eawf021_measurable_criterion"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_sample(tmp_path: Path, payload: object) -> Path:
    """Write *payload* as JSON to a temp sample file and return its path."""
    sample = tmp_path / "sample.json"
    sample.write_bytes(orjson.dumps(payload))
    return sample


def test_run_backfill_exits_zero_and_drains_legacy(tmp_path: Path) -> None:
    tool = _load_tool()
    sample = _write_sample(
        tmp_path,
        {
            "file_scopes": ["src/eawf/kernel/spec/common.py"],
            "criteria": [
                "validates the wave-body schema in pytest tests/unit/spec/test_x.py",
                "the converter exists and drains the legacy count to zero",
            ],
        },
    )
    assert tool.run_backfill(sample) == 0


def test_main_usage_error_on_wrong_arity() -> None:
    tool = _load_tool()
    assert tool.main([]) == 1
    assert tool.main(["a.json", "b.json"]) == 1


def test_run_backfill_missing_sample_exits_one(tmp_path: Path) -> None:
    tool = _load_tool()
    assert tool.run_backfill(tmp_path / "does-not-exist.json") == 1


def test_run_backfill_non_object_sample_exits_one(tmp_path: Path) -> None:
    tool = _load_tool()
    sample = _write_sample(tmp_path, ["not", "an", "object"])
    assert tool.run_backfill(sample) == 1


def test_run_backfill_empty_criteria_exits_one(tmp_path: Path) -> None:
    tool = _load_tool()
    sample = _write_sample(
        tmp_path, {"file_scopes": ["src/eawf/kernel/spec/common.py"], "criteria": []}
    )
    assert tool.run_backfill(sample) == 1


def test_run_backfill_empty_file_scopes_exits_one(tmp_path: Path) -> None:
    tool = _load_tool()
    sample = _write_sample(
        tmp_path, {"file_scopes": [], "criteria": ["validates the schema"]}
    )
    assert tool.run_backfill(sample) == 1


def test_run_backfill_malformed_json_exits_one(tmp_path: Path) -> None:
    tool = _load_tool()
    sample = tmp_path / "bad.json"
    sample.write_text("{ not valid json", encoding="utf-8")
    assert tool.run_backfill(sample) == 1
