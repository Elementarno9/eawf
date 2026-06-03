"""Unit tests for ``tools/evidence_provenance_gate.py``.

Covers the deterministic evidence-provenance gate: a pass when the
evidence is pinned to the expected commit AND its bytes equal the
committed golden, and the typed failure for each independent violation
(byte drift, forged provenance, missing stamp, missing files). The gate
module is loaded via :mod:`importlib` because ``tools/`` is excluded from
the package and so is not importable by name.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from eawf.surfaces.tui.snapshot.asciinema import write_cast

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "evidence_provenance_gate.py"
_TOOL_DIR = _GATE_PATH.parent


def _load_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("evidence_provenance_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["evidence_provenance_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


_COMMIT = "abc1234"
_FRAMES = [(0.0, "frame-zero"), (0.05, "frame-one")]


def _write_golden(tmp_path: Path, *, commit: str = _COMMIT) -> Path:
    """Write a provenance-stamped golden cast and return its path."""
    golden = tmp_path / "golden.cast"
    write_cast(_FRAMES, golden, source_commit=commit, fixture_id="repo-active")
    return golden


# --------------------------------------------------------------------------
# pass path
# --------------------------------------------------------------------------


def test_gate_passes_when_provenance_and_bytes_match(tmp_path: Path, mod) -> None:
    golden = _write_golden(tmp_path)
    # Regenerated evidence is byte-identical to the golden and carries the
    # same source commit.
    evidence = tmp_path / "evidence.cast"
    write_cast(_FRAMES, evidence, source_commit=_COMMIT, fixture_id="repo-active")

    result = mod.check_evidence_provenance(evidence, golden, _COMMIT)
    assert result.passed is True
    assert result.failure is None


# --------------------------------------------------------------------------
# failure paths — byte drift and forged provenance
# --------------------------------------------------------------------------


def test_gate_fails_on_byte_drift(tmp_path: Path, mod) -> None:
    golden = _write_golden(tmp_path)
    # A stale / hand-edited frame: provenance still matches but the bytes
    # differ from the committed golden.
    evidence = tmp_path / "evidence.cast"
    write_cast(
        [(0.0, "frame-zero"), (0.05, "TAMPERED")],
        evidence,
        source_commit=_COMMIT,
        fixture_id="repo-active",
    )

    result = mod.check_evidence_provenance(evidence, golden, _COMMIT)
    assert result.passed is False
    assert result.failure is mod.GateFailure.BYTE_DRIFT
    assert "drift" in result.message
    assert "evidence.cast" in result.message


def test_gate_fails_on_forged_provenance(tmp_path: Path, mod) -> None:
    golden = _write_golden(tmp_path)
    # Bytes would match the golden's frames, but the embedded commit is a
    # different (forged / stale) SHA than expected.
    evidence = tmp_path / "evidence.cast"
    write_cast(_FRAMES, evidence, source_commit="0000000", fixture_id="repo-active")

    result = mod.check_evidence_provenance(evidence, golden, _COMMIT)
    assert result.passed is False
    assert result.failure is mod.GateFailure.PROVENANCE_MISMATCH
    assert "0000000" in result.message
    assert _COMMIT in result.message


def test_gate_fails_when_evidence_has_no_provenance_stamp(tmp_path: Path, mod) -> None:
    golden = _write_golden(tmp_path)
    evidence = tmp_path / "evidence.cast"
    write_cast(_FRAMES, evidence)  # no provenance

    result = mod.check_evidence_provenance(evidence, golden, _COMMIT)
    assert result.passed is False
    assert result.failure is mod.GateFailure.MISSING_PROVENANCE


def test_gate_fails_when_evidence_missing(tmp_path: Path, mod) -> None:
    golden = _write_golden(tmp_path)
    result = mod.check_evidence_provenance(tmp_path / "absent.cast", golden, _COMMIT)
    assert result.passed is False
    assert result.failure is mod.GateFailure.MISSING_EVIDENCE


def test_gate_fails_when_golden_missing(tmp_path: Path, mod) -> None:
    evidence = tmp_path / "evidence.cast"
    write_cast(_FRAMES, evidence, source_commit=_COMMIT)
    result = mod.check_evidence_provenance(evidence, tmp_path / "absent.cast", _COMMIT)
    assert result.passed is False
    assert result.failure is mod.GateFailure.MISSING_GOLDEN


# --------------------------------------------------------------------------
# precedence — provenance is checked before byte drift
# --------------------------------------------------------------------------


def test_gate_reports_provenance_before_drift(tmp_path: Path, mod) -> None:
    # Both contracts are violated (wrong commit AND wrong bytes); the gate
    # names the more fundamental provenance failure first.
    golden = _write_golden(tmp_path)
    evidence = tmp_path / "evidence.cast"
    write_cast(
        [(0.0, "frame-zero"), (0.05, "TAMPERED")],
        evidence,
        source_commit="0000000",
        fixture_id="repo-active",
    )

    result = mod.check_evidence_provenance(evidence, golden, _COMMIT)
    assert result.failure is mod.GateFailure.PROVENANCE_MISMATCH


# --------------------------------------------------------------------------
# CLI wrapper
# --------------------------------------------------------------------------


def test_cli_returns_zero_on_pass(tmp_path: Path, mod) -> None:
    golden = _write_golden(tmp_path)
    evidence = tmp_path / "evidence.cast"
    write_cast(_FRAMES, evidence, source_commit=_COMMIT, fixture_id="repo-active")

    code = mod.main(["evidence_provenance_gate.py", str(evidence), str(golden), _COMMIT])
    assert code == 0


def test_cli_returns_one_on_failure(tmp_path: Path, mod) -> None:
    golden = _write_golden(tmp_path)
    evidence = tmp_path / "evidence.cast"
    write_cast(_FRAMES, evidence, source_commit="0000000")

    code = mod.main(["evidence_provenance_gate.py", str(evidence), str(golden), _COMMIT])
    assert code == 1


def test_cli_returns_two_on_usage_error(mod) -> None:
    code = mod.main(["evidence_provenance_gate.py", "only-one-arg"])
    assert code == 2
