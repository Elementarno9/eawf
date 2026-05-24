"""Unit tests for the C07a capability matrix (W13).

Pins:

* ``capabilities.yaml`` ships exactly 8 capability rows x 3 runtimes
  (C07a §G9 + W13 success criterion 1).
* :func:`eawf.runtime.runtimes.capabilities.load_matrix` rejects malformed
  inputs: unknown runtime, wrong row count, unknown cell value, missing
  schema_version.
* :func:`eawf.runtime.runtimes.capabilities.detect_drift` emits the four
  drift-status verdicts (``OK`` / ``DRIFT`` / ``MISSING`` / ``UNKNOWN``)
  on the expected probe inputs.
* :func:`eawf.runtime.runtimes.selector.runtime_supports` boolean view aligns
  with the adapter class attribute values declared by the three
  W10 adapters (no parallel hard-coded table per W13 criterion 3).
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
import yaml

from eawf.runtime.runtimes.capabilities import (
    CAPABILITY_CELLS,
    CAPABILITY_NAMES,
    EXPECTED_CAPABILITY_ROWS,
    EXPECTED_RUNTIMES,
    MATRIX_PATH,
    RUNTIME_IDS,
    CapabilityCell,
    CapabilityMatrix,
    DriftRow,
    ProbeResult,
    detect_drift,
    get_matrix,
    get_runtime_capabilities,
    load_matrix,
    render_drift_table,
)
from eawf.runtime.runtimes.selector import SUPPORTED_CELLS, runtime_supports, select_adapter

# ---------------------------------------------------------------------------
# Matrix shape — 8 x 3 invariants (W13 success criterion 1)
# ---------------------------------------------------------------------------


def test_packaged_yaml_loads_clean() -> None:
    """The shipped ``capabilities.yaml`` validates against the typed loader."""
    matrix = load_matrix()
    assert isinstance(matrix, CapabilityMatrix)


def test_matrix_has_exactly_8_capability_rows() -> None:
    """W13 success criterion 1 — exactly 8 capability rows."""
    matrix = get_matrix()
    assert len(matrix.capability_names()) == EXPECTED_CAPABILITY_ROWS == 8


def test_matrix_has_exactly_3_runtimes() -> None:
    """W13 success criterion 1 — exactly 3 runtimes."""
    matrix = get_matrix()
    assert len(matrix.runtime_ids()) == EXPECTED_RUNTIMES == 3
    assert matrix.runtime_ids() == ("claude-code", "codex", "opencode")


def test_matrix_total_cell_count_is_24() -> None:
    """8 x 3 grid yields 24 cells."""
    matrix = get_matrix()
    cell_count = sum(
        1
        for _ in (
            (cap, runtime_id)
            for cap in matrix.capability_names()
            for runtime_id in matrix.runtime_ids()
        )
    )
    assert cell_count == 24


def test_capability_names_match_canonical_order() -> None:
    """Capability ordering pinned to the canonical tuple."""
    matrix = get_matrix()
    assert matrix.capability_names() == CAPABILITY_NAMES


def test_capability_cells_are_closed_4_tuple() -> None:
    """Cell-value Literal carries exactly the 4 documented states."""
    args = set(get_args(CapabilityCell))
    assert args == {"supported", "unsupported", "partial", "unknown"}
    assert set(CAPABILITY_CELLS) == args


def test_every_cell_is_a_valid_literal() -> None:
    """Every loaded cell matches the closed :data:`CapabilityCell`."""
    matrix = get_matrix()
    valid = set(get_args(CapabilityCell))
    for cap in matrix.capability_names():
        for runtime_id in matrix.runtime_ids():
            cell = matrix.cell(cap, runtime_id)
            assert cell in valid, f"{cap}/{runtime_id} cell {cell!r} not in {valid!r}"


# ---------------------------------------------------------------------------
# Schema-rejection paths (error rule per AGENTS §test discipline)
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path: Path, body: dict[str, object]) -> Path:
    target = tmp_path / "capabilities.yaml"
    # ``sort_keys=False`` preserves insertion order so capability-name
    # ordering tests stay deterministic against the canonical tuple.
    target.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return target


def _valid_body() -> dict[str, object]:
    """Construct a minimal valid matrix body for mutation tests."""
    rows: dict[str, dict[str, str]] = {}
    for cap in CAPABILITY_NAMES:
        rows[cap] = {
            "description": f"row for {cap}",
            "claude-code": "supported",
            "codex": "supported",
            "opencode": "supported",
        }
    return {
        "schema_version": "1.0",
        "runtimes": list(RUNTIME_IDS),
        "capabilities": rows,
    }


def test_load_matrix_rejects_missing_file(tmp_path: Path) -> None:
    """Loader raises on absent target path."""
    with pytest.raises(FileNotFoundError, match="capability matrix not found"):
        load_matrix(tmp_path / "nope.yaml")


def test_load_matrix_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    """Loader rejects a YAML list at the top level."""
    target = tmp_path / "capabilities.yaml"
    target.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level must be a mapping"):
        load_matrix(target)


def test_load_matrix_rejects_missing_capabilities_block(tmp_path: Path) -> None:
    """Loader rejects body without ``capabilities`` mapping."""
    target = _write_yaml(
        tmp_path,
        {"schema_version": "1.0", "runtimes": list(RUNTIME_IDS)},
    )
    with pytest.raises(ValueError, match="missing ``capabilities`` block"):
        load_matrix(target)


def test_load_matrix_rejects_wrong_row_count(tmp_path: Path) -> None:
    """Loader rejects matrices with != 8 capability rows."""
    body = _valid_body()
    # Drop one row to land at 7.
    body["capabilities"] = {  # type: ignore[index]
        cap: row
        for cap, row in body["capabilities"].items()
        if cap != "skills"  # type: ignore[union-attr]
    }
    target = _write_yaml(tmp_path, body)
    with pytest.raises(ValueError, match="capability row count 7 != 8"):
        load_matrix(target)


def test_load_matrix_rejects_unknown_runtime(tmp_path: Path) -> None:
    """Loader rejects matrices declaring runtimes outside the 3-tuple."""
    body = _valid_body()
    body["runtimes"] = ["claude-code", "codex", "aider"]
    target = _write_yaml(tmp_path, body)
    with pytest.raises(ValueError, match="runtime list mismatch"):
        load_matrix(target)


def test_load_matrix_rejects_unknown_cell_value(tmp_path: Path) -> None:
    """Loader rejects cells with values outside the closed Literal."""
    body = _valid_body()
    body["capabilities"]["skills"]["claude-code"] = "maybe"  # type: ignore[index]
    target = _write_yaml(tmp_path, body)
    # Pydantic raises ValidationError; loader propagates as ValueError-equivalent.
    with pytest.raises(Exception, match=r"(?i)maybe|literal|validation"):
        load_matrix(target)


def test_load_matrix_rejects_capability_reorder(tmp_path: Path) -> None:
    """Loader rejects when row order does not match the canonical tuple."""
    body = _valid_body()
    # Reverse the dict order; reorder names so length still matches but
    # order differs from CAPABILITY_NAMES.
    body["capabilities"] = dict(reversed(list(body["capabilities"].items())))  # type: ignore[arg-type]
    target = _write_yaml(tmp_path, body)
    with pytest.raises(ValueError, match="capability ordering mismatch"):
        load_matrix(target)


def test_load_matrix_rejects_extra_top_level_key(tmp_path: Path) -> None:
    """``extra='forbid'`` rejects unknown top-level keys."""
    body = _valid_body()
    body["extra_field"] = True
    target = _write_yaml(tmp_path, body)
    with pytest.raises(Exception, match=r"(?i)extra|forbidden|extra_forbidden"):
        load_matrix(target)


# ---------------------------------------------------------------------------
# Per-runtime helpers (W13 success criterion 3 — adapter consumers)
# ---------------------------------------------------------------------------


def test_get_runtime_capabilities_returns_all_8(claude_id: str = "claude-code") -> None:
    """Each runtime view carries all 8 capability rows."""
    caps = get_runtime_capabilities(claude_id)
    assert len(caps) == EXPECTED_CAPABILITY_ROWS
    assert set(caps.keys()) == set(CAPABILITY_NAMES)


def test_get_runtime_capabilities_rejects_unknown_runtime() -> None:
    """Unknown runtime id raises ``ValueError``."""
    with pytest.raises(ValueError, match="unknown runtime"):
        get_runtime_capabilities("aider")


def test_runtime_supports_maps_supported_and_partial_to_true() -> None:
    """``supported`` and ``partial`` map to boolean ``True``."""
    assert {"supported", "partial"} == SUPPORTED_CELLS


def test_runtime_supports_claude_session_resume() -> None:
    """Claude declares ``supported`` for session_resume."""
    assert runtime_supports("claude-code", "session_resume") is True


def test_runtime_supports_opencode_session_resume_is_false() -> None:
    """OpenCode session_resume is ``unsupported`` in v0.3 (matches adapter)."""
    assert runtime_supports("opencode", "session_resume") is False


def test_runtime_supports_codex_cache_control_is_false() -> None:
    """Codex cache_control is ``unsupported`` (matches adapter)."""
    assert runtime_supports("codex", "cache_control") is False


def test_runtime_supports_rejects_unknown_capability() -> None:
    """Unknown capability row raises ``KeyError``."""
    with pytest.raises(KeyError, match="unknown capability"):
        runtime_supports("claude-code", "no_such_row")


# ---------------------------------------------------------------------------
# Adapter-side derivation: matrix is the single source of truth
# (W13 success criterion 3)
# ---------------------------------------------------------------------------


def test_claude_adapter_capabilities_derive_from_matrix() -> None:
    """ClaudeAdapter bool flags equal the YAML-backed view."""
    from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter

    adapter = ClaudeAdapter()
    assert adapter.accepts_continue is runtime_supports("claude-code", "session_resume")
    assert adapter.supports_cache_control is runtime_supports("claude-code", "cache_control")


def test_codex_adapter_capabilities_derive_from_matrix() -> None:
    """CodexAdapter bool flags equal the YAML-backed view."""
    from eawf.runtime.runtimes.codex.adapter import CodexAdapter

    adapter = CodexAdapter()
    assert adapter.accepts_continue is runtime_supports("codex", "session_resume")
    assert adapter.supports_cache_control is runtime_supports("codex", "cache_control")


def test_opencode_adapter_capabilities_derive_from_matrix() -> None:
    """OpenCodeAdapter bool flags equal the YAML-backed view."""
    from eawf.runtime.runtimes.opencode.adapter import OpenCodeAdapter

    adapter = OpenCodeAdapter()
    assert adapter.accepts_continue is runtime_supports("opencode", "session_resume")
    assert adapter.supports_cache_control is runtime_supports("opencode", "cache_control")


def test_select_adapter_returns_canonical_three() -> None:
    """:func:`select_adapter` returns instances of the canonical adapters."""
    for runtime_id in RUNTIME_IDS:
        adapter = select_adapter(runtime_id)
        assert adapter.id == runtime_id


def test_select_adapter_rejects_unknown_runtime() -> None:
    """:func:`select_adapter` raises on unknown runtime id."""
    with pytest.raises(ValueError, match="unknown runtime"):
        select_adapter("aider")


# ---------------------------------------------------------------------------
# Drift detector (W13 success criterion 2)
# ---------------------------------------------------------------------------


def _probe(runtime_id: str, *, installed: bool, flags: tuple[str, ...] = ()) -> ProbeResult:
    return ProbeResult(runtime_id=runtime_id, installed=installed, observed_flags=flags)


def test_detect_drift_missing_when_runtime_not_installed() -> None:
    """All cells become ``MISSING`` when the binary is absent."""
    rows = detect_drift("claude-code", _probe("claude-code", installed=False))
    assert len(rows) == EXPECTED_CAPABILITY_ROWS
    for row in rows:
        assert row.status == "MISSING"
        assert "not installed" in row.detail


def test_detect_drift_ok_when_evidence_present() -> None:
    """OK row when probe flags match the ``supported`` declaration."""
    # ``--continue`` + ``--allowedTools`` + ``--output-format`` cover the three
    # capabilities with probe rules; the remaining rows have no probe rule and
    # default to OK against the declared cell.
    probe = _probe(
        "claude-code",
        installed=True,
        flags=("--continue", "--allowedTools", "--output-format"),
    )
    rows = detect_drift("claude-code", probe)
    assert {row.status for row in rows} <= {"OK"}


def test_detect_drift_flags_drift_on_missing_evidence() -> None:
    """Declared ``supported`` + no probe evidence + has rule → ``DRIFT``."""
    probe = _probe("claude-code", installed=True, flags=("--unrelated",))
    rows = detect_drift("claude-code", probe)
    drift_rows = [r for r in rows if r.status == "DRIFT"]
    drift_caps = {r.capability for r in drift_rows}
    # session_resume + tool_use + streaming all have probe rules; none of the
    # observed flags satisfy any rule.
    assert drift_caps == {"session_resume", "tool_use", "streaming"}


def test_detect_drift_flags_drift_on_unexpected_evidence() -> None:
    """Declared ``unsupported`` + probe flags present → ``DRIFT`` (cell flipped)."""
    # OpenCode session_resume is declared ``unsupported``; if probe finds
    # ``--continue`` / ``--session`` the detector flags drift so operator
    # audits the v0.3 conservative gate.
    probe = _probe("opencode", installed=True, flags=("--continue", "--session"))
    rows = detect_drift("opencode", probe)
    flagged = [r for r in rows if r.capability == "session_resume"]
    assert len(flagged) == 1
    assert flagged[0].status == "DRIFT"
    assert "declared='unsupported'" in flagged[0].detail


def test_detect_drift_unknown_when_cell_is_unknown(
    tmp_path: Path,
) -> None:
    """``unknown`` cell + installed probe → ``UNKNOWN`` status row."""
    body = _valid_body()
    body["capabilities"]["skills"]["claude-code"] = "unknown"  # type: ignore[index]
    target = _write_yaml(tmp_path, body)
    matrix = load_matrix(target)
    rows = detect_drift(
        "claude-code",
        _probe("claude-code", installed=True),
        matrix=matrix,
    )
    unknown_rows = [r for r in rows if r.status == "UNKNOWN"]
    assert len(unknown_rows) == 1
    assert unknown_rows[0].capability == "skills"


def test_detect_drift_rejects_runtime_id_mismatch() -> None:
    """``probe.runtime_id`` and the ``runtime_id`` argument must agree."""
    with pytest.raises(ValueError, match="probe runtime mismatch"):
        detect_drift("claude-code", _probe("codex", installed=True))


def test_detect_drift_rejects_unknown_runtime() -> None:
    """Unknown runtime id raises ``ValueError``."""
    with pytest.raises(ValueError, match="unknown runtime"):
        detect_drift("aider", _probe("aider", installed=True))


def test_detect_drift_returns_eight_rows() -> None:
    """Drift output is always one row per declared capability (8)."""
    rows = detect_drift("codex", _probe("codex", installed=False))
    assert len(rows) == EXPECTED_CAPABILITY_ROWS
    assert {row.capability for row in rows} == set(CAPABILITY_NAMES)


def test_drift_row_is_frozen_dataclass() -> None:
    """``DriftRow`` is immutable so callers cannot mutate the result."""
    row = DriftRow(capability="skills", declared="supported", status="OK", detail="ok")
    with pytest.raises(Exception, match=r"(?i)cannot|frozen"):
        row.status = "DRIFT"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_render_drift_table_emits_one_line_per_row() -> None:
    """Plain-text rendering has one line per drift row + header rows."""
    rows = detect_drift("claude-code", _probe("claude-code", installed=False))
    text = render_drift_table("claude-code", rows)
    # 1 runtime line + 1 blank + 1 header line + 1 separator + 8 body rows.
    assert text.count("\n") == 4 + EXPECTED_CAPABILITY_ROWS - 1
    assert "runtime=claude-code" in text
    assert "MISSING" in text


# ---------------------------------------------------------------------------
# MATRIX_PATH points at the packaged YAML file
# ---------------------------------------------------------------------------


def test_matrix_path_points_to_existing_file() -> None:
    """The packaged YAML is reachable through :data:`MATRIX_PATH`."""
    assert MATRIX_PATH.exists()
    assert MATRIX_PATH.suffix == ".yaml"
    assert MATRIX_PATH.name == "capabilities.yaml"
