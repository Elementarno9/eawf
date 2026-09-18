"""Capability evidence is total over the declared set.

The detector used to resolve a capability with no probe rule to ``OK``
with the detail ``no probe rule``, so the declaration certified itself and
a green conformance run was substantially the matrix agreeing with itself.
These tests pin the repair from both sides: an unruled capability resolves
``UNKNOWN`` and never ``OK``, and no row anywhere reaches ``OK`` without a
probe rule behind it.

Four cases cover the declared set: a covered capability, an uncovered one,
an uninstalled runtime, and the crafted-matrix boundary where the declared
cell is itself ``unknown``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from eawf.runtime.runtimes.capabilities import (
    CAPABILITY_NAMES,
    EXPECTED_CAPABILITY_ROWS,
    RUNTIME_IDS,
    CapabilityMatrix,
    DriftRow,
    ProbeResult,
    detect_drift,
    load_matrix,
)

pytestmark = pytest.mark.unit

#: Capabilities carrying a probe rule for ``claude-code``. Every other
#: declared capability is uncovered and can only resolve ``UNKNOWN``.
CLAUDE_RULED = frozenset({"session_resume", "tool_use", "streaming"})

#: Flags that satisfy the claude-code rules for tool_use and streaming.
CLAUDE_FLAGS = ("--allowedTools", "--output-format")


def _probe(runtime_id: str, *, installed: bool, flags: tuple[str, ...] = ()) -> ProbeResult:
    return ProbeResult(runtime_id=runtime_id, installed=installed, observed_flags=flags)


def _uniform_matrix(tmp_path: Path, cell: str) -> CapabilityMatrix:
    """Return a matrix whose every cell is *cell*, loaded from disk."""
    body = {
        "schema_version": "1.0",
        "runtimes": list(RUNTIME_IDS),
        "capabilities": {
            name: {
                "description": f"row for {name}",
                "claude-code": cell,
                "codex": cell,
                "opencode": cell,
            }
            for name in CAPABILITY_NAMES
        },
    }
    target = tmp_path / "capabilities.yaml"
    target.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return load_matrix(target)


# ---------------------------------------------------------------------------
# An uncovered capability resolves UNKNOWN, never OK
# ---------------------------------------------------------------------------


def test_detect_drift_returns_unknown_for_a_capability_with_no_probe_rule() -> None:
    """A declared capability with no rule is UNKNOWN even when installed."""
    rows = detect_drift("claude-code", _probe("claude-code", installed=True, flags=CLAUDE_FLAGS))
    uncovered = [row for row in rows if row.capability not in CLAUDE_RULED]
    assert uncovered, "the fixture runtime must declare at least one unruled capability"
    for row in uncovered:
        assert row.status == "UNKNOWN"
        assert row.probe_rule == ()
        assert "no probe rule" in row.detail


def test_detect_drift_never_returns_ok_for_an_unruled_capability() -> None:
    """No runtime x probe combination lets an unruled row reach OK."""
    probes = (
        _probe("claude-code", installed=True, flags=CLAUDE_FLAGS),
        _probe("claude-code", installed=True),
        _probe("codex", installed=True, flags=("resume", "mcp", "exec")),
        _probe("opencode", installed=True, flags=("--continue", "--agent", "--format")),
    )
    for probe in probes:
        for row in detect_drift(probe.runtime_id, probe):
            assert not (row.status == "OK" and row.probe_rule == ())


def test_detect_drift_ok_rows_all_carry_their_probe_rule() -> None:
    """Every OK row names the tokens its verdict rested on."""
    rows = detect_drift("claude-code", _probe("claude-code", installed=True, flags=CLAUDE_FLAGS))
    ok_rows = [row for row in rows if row.status == "OK"]
    assert {row.capability for row in ok_rows} == set(CLAUDE_RULED)
    for row in ok_rows:
        assert row.probe_rule != ()


# ---------------------------------------------------------------------------
# A covered capability still resolves on its evidence
# ---------------------------------------------------------------------------


def test_detect_drift_covered_capability_passes_on_positive_evidence() -> None:
    """tool_use is declared supported and the probe shows its flag."""
    rows = detect_drift("claude-code", _probe("claude-code", installed=True, flags=CLAUDE_FLAGS))
    tool_use = next(row for row in rows if row.capability == "tool_use")
    assert tool_use.status == "OK"
    assert "--allowedTools" in tool_use.probe_rule


def test_detect_drift_covered_capability_drifts_on_absent_evidence() -> None:
    """A declared-supported capability with a rule and no flag drifts."""
    rows = detect_drift("claude-code", _probe("claude-code", installed=True, flags=("--nope",)))
    drifted = {row.capability for row in rows if row.status == "DRIFT"}
    assert drifted == {"tool_use", "streaming"}


# ---------------------------------------------------------------------------
# Boundary: uninstalled runtime, and an unknown declared cell
# ---------------------------------------------------------------------------


def test_detect_drift_uninstalled_runtime_yields_no_ok_row() -> None:
    """Every cell of an absent binary is MISSING rather than passing."""
    for runtime_id in RUNTIME_IDS:
        rows = detect_drift(runtime_id, _probe(runtime_id, installed=False))
        assert len(rows) == EXPECTED_CAPABILITY_ROWS
        assert {row.status for row in rows} == {"MISSING"}


def test_detect_drift_unknown_cell_is_unknown_for_its_own_reason(tmp_path: Path) -> None:
    """An ``unknown`` cell reports the cell, not the missing rule."""
    matrix = _uniform_matrix(tmp_path, "unknown")
    rows = detect_drift("codex", _probe("codex", installed=True), matrix=matrix)
    assert {row.status for row in rows} == {"UNKNOWN"}
    for row in rows:
        assert "declared=unknown" in row.detail


def test_detect_drift_supported_everywhere_still_leaves_unruled_rows_unknown(
    tmp_path: Path,
) -> None:
    """Declaring every cell supported cannot buy a pass for an unruled row."""
    matrix = _uniform_matrix(tmp_path, "supported")
    rows = detect_drift(
        "claude-code",
        _probe("claude-code", installed=True, flags=(*CLAUDE_FLAGS, "--continue")),
        matrix=matrix,
    )
    statuses = {row.capability: row.status for row in rows}
    assert {name: status for name, status in statuses.items() if status == "OK"}.keys() == set(
        CLAUDE_RULED
    )
    assert all(statuses[name] == "UNKNOWN" for name in statuses if name not in CLAUDE_RULED)


def test_detect_drift_covers_every_declared_capability_exactly_once() -> None:
    """Evidence is total: one row per declared capability, no more."""
    rows = detect_drift("codex", _probe("codex", installed=True, flags=("resume",)))
    assert tuple(row.capability for row in rows) == CAPABILITY_NAMES


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_detect_drift_rejects_a_probe_for_another_runtime() -> None:
    """The probe and the requested runtime must be the same runtime."""
    with pytest.raises(ValueError, match="probe runtime mismatch"):
        detect_drift("codex", _probe("claude-code", installed=True))


def test_detect_drift_rejects_an_unknown_runtime() -> None:
    """A runtime outside the declared three has no cells to resolve."""
    with pytest.raises(ValueError, match="unknown runtime"):
        detect_drift("aider", _probe("aider", installed=True))


def test_drift_row_probe_rule_defaults_to_empty_and_is_frozen() -> None:
    """A hand-built row carries no rule and cannot be re-statused."""
    row = DriftRow(capability="skills", declared="supported", status="UNKNOWN", detail="d")
    assert row.probe_rule == ()
    with pytest.raises(FrozenInstanceError, match="cannot assign to field 'status'"):
        row.status = "OK"  # type: ignore[misc]
