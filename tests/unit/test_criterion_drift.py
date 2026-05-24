"""Unit tests for :mod:`eawf.workflow.lifecycle.criterion_drift` (P23-I02-W01).

Coverage:

- :func:`extract_path_globs` parses path-shaped tokens from a list of
  success_criteria strings.
- :func:`unresolved_globs` filters globs that resolve to zero files on
  disk (wildcard via :meth:`pathlib.Path.glob`; plain paths via
  :meth:`pathlib.Path.exists`).
- :func:`check_wave_criteria_drift` is the wave-facing convenience that
  returns unresolved globs for a wave's ``success_criteria``.
- The P23 audit followups F2 path globs (``tests/unit/test_state_urn*.py``
  and ``tests/unit/test_lifecycle*.py``) are the worked example — both
  resolve to zero files; the detector MUST flag them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import EffortBucket, WaveStatus
from eawf.kernel.state.models import Wave
from eawf.workflow.lifecycle.criterion_drift import (
    check_wave_criteria_drift,
    extract_path_globs,
    unresolved_globs,
)


def _make_wave(
    success_criteria: list[str],
    *,
    wave_id: str = "P99-I01-W01",
) -> Wave:
    return Wave(
        id=wave_id,
        iter_id="P99-I01",
        title="test wave",
        status=WaveStatus.CLOSED,
        file_scopes=["src/foo.py"],
        success_criteria=success_criteria,
        effort_bucket=EffortBucket.M,
        opened_at=datetime(2026, 5, 18, tzinfo=UTC),
        closed_at=datetime(2026, 5, 18, tzinfo=UTC),
    )


def test_extract_path_globs_finds_tests_and_src_prefixes() -> None:
    criteria = [
        "uv run pytest tests/unit/test_urn.py -q green",
        "uv run mypy src/eawf/state/urn.py green",
    ]
    assert extract_path_globs(criteria) == [
        "tests/unit/test_urn.py",
        "src/eawf/state/urn.py",
    ]


def test_extract_path_globs_deduplicates_preserving_order() -> None:
    criteria = [
        "first: tests/unit/test_a.py green",
        "second: tests/unit/test_a.py twice",
        "third: tests/unit/test_b.py",
    ]
    assert extract_path_globs(criteria) == [
        "tests/unit/test_a.py",
        "tests/unit/test_b.py",
    ]


def test_extract_path_globs_handles_wildcards() -> None:
    criteria = ["uv run pytest tests/unit/test_state_urn*.py -q green"]
    assert extract_path_globs(criteria) == ["tests/unit/test_state_urn*.py"]


def test_extract_path_globs_returns_empty_when_no_path_tokens() -> None:
    criteria = [
        "URN_KINDS extended from 10 to 26 kinds",
        "brief cited",
    ]
    assert extract_path_globs(criteria) == []


def test_extract_path_globs_stops_at_punctuation() -> None:
    """Tokens MUST stop at whitespace/comma/parenthesis/quote."""
    criteria = ['failing on src/foo.py, and tests/bar.py "edge"']
    assert extract_path_globs(criteria) == ["src/foo.py", "tests/bar.py"]


def test_unresolved_globs_flags_missing_wildcard(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_real.py").write_text("")
    assert unresolved_globs(
        tmp_path,
        ["tests/unit/test_real.py", "tests/unit/test_missing*.py"],
    ) == ["tests/unit/test_missing*.py"]


def test_unresolved_globs_resolves_existing_wildcard(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_urn.py").write_text("")
    (tmp_path / "tests" / "unit" / "test_kinds.py").write_text("")
    assert unresolved_globs(tmp_path, ["tests/unit/test_*.py"]) == []


def test_unresolved_globs_flags_missing_plain_path(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("")
    assert unresolved_globs(tmp_path, ["src/real.py", "src/missing.py"]) == [
        "src/missing.py",
    ]


def test_unresolved_globs_empty_input_returns_empty(tmp_path: Path) -> None:
    assert unresolved_globs(tmp_path, []) == []


def test_check_wave_criteria_drift_empty_criteria_returns_empty(
    tmp_path: Path,
) -> None:
    wave = _make_wave([])
    assert check_wave_criteria_drift(wave, tmp_path) == []


def test_check_wave_criteria_drift_no_path_tokens_returns_empty(
    tmp_path: Path,
) -> None:
    wave = _make_wave(["URN_KINDS extended", "brief cited"])
    assert check_wave_criteria_drift(wave, tmp_path) == []


def test_check_wave_criteria_drift_flags_audit_f2_worked_example(
    tmp_path: Path,
) -> None:
    """P23 audit F2: the two unresolved globs MUST be flagged.

    Worked example from A29-P23-ship-gate.md F2.
    """
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_urn.py").write_text("")
    (tmp_path / "tests" / "unit" / "test_lifecycle_spec.py").write_text("")
    wave = _make_wave(
        [
            "uv run pytest tests/unit/test_state_urn*.py -q green",
            "uv run pytest tests/unit/test_lifecycle*.py -q green",
            "uv run pytest tests/unit/test_urn.py -q green",
        ]
    )
    unresolved = check_wave_criteria_drift(wave, tmp_path)
    assert "tests/unit/test_state_urn*.py" in unresolved
    assert "tests/unit/test_lifecycle*.py" not in unresolved  # matches test_lifecycle_spec.py
    assert "tests/unit/test_urn.py" not in unresolved


def test_check_wave_criteria_drift_real_repo_resolution(
    tmp_path: Path,
) -> None:
    """Mix of resolved + unresolved globs returns only the unresolved subset."""
    (tmp_path / "src" / "eawf" / "state").mkdir(parents=True)
    (tmp_path / "src" / "eawf" / "state" / "urn.py").write_text("")
    wave = _make_wave(
        [
            "uv run mypy src/eawf/state/urn.py green",
            "uv run mypy src/eawf/state/missing.py green",
        ]
    )
    assert check_wave_criteria_drift(wave, tmp_path) == [
        "src/eawf/state/missing.py",
    ]


@pytest.mark.parametrize(
    ("criterion", "expected"),
    [
        ("plain prose with no paths", []),
        ("src/eawf/foo.py", ["src/eawf/foo.py"]),
        ("tests/unit/test_a.py and src/bar.py", ["tests/unit/test_a.py", "src/bar.py"]),
        (
            "build/eawf-plugin/skills/research/SKILL.md",
            ["build/eawf-plugin/skills/research/SKILL.md"],
        ),
        ("docs/architecture/state-model.md", ["docs/architecture/state-model.md"]),
        ("scripts/release.sh runs", ["scripts/release.sh"]),
    ],
)
def test_extract_path_globs_prefix_matrix(criterion: str, expected: list[str]) -> None:
    assert extract_path_globs([criterion]) == expected
