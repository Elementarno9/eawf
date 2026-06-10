"""Tests for the EAWF023 artifact-placement lint + ``ArtifactPathStr``.

Covers the placement rule (canonical kind sub-directory + mandatory
``YYYY-MM-DD-`` date stem) across boundary and error paths, that the real
git-tracked artifact tree passes clean (the grandfather baseline), and
that the same contract holds at the model boundary via
:data:`eawf.kernel.spec.common.ArtifactPathStr` and
:func:`eawf.kernel.spec.common.artifact_path_str`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.spec.common import ArtifactPathStr, artifact_path_str
from eawf.platform.lint.eawf023_artifact_placement import (
    GRANDFATHERED_ARTIFACTS,
    check_artifact_path,
    check_artifact_paths,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


# --- placement lint: error paths -------------------------------------------


def test_check_artifact_path_misplaced_subdir_flagged() -> None:
    path = ".ea/artifacts/notakind/2026-06-11-thing.md"
    violation = check_artifact_path(path)
    assert violation is not None
    assert violation.code == "EAWF023"
    assert violation.path == path
    assert "not a canonical artifact kind" in violation.reason


def test_check_artifact_path_missing_date_stem_flagged() -> None:
    path = ".ea/artifacts/audits/p30-i14-closeout.md"
    violation = check_artifact_path(path)
    assert violation is not None
    assert violation.code == "EAWF023"
    assert "YYYY-MM-DD- date stem" in violation.reason


def test_check_artifact_path_directly_in_root_flagged() -> None:
    path = ".ea/artifacts/2026-06-11-loose.md"
    violation = check_artifact_path(path)
    assert violation is not None
    assert "kind sub-directory" in violation.reason


def test_check_artifact_path_bad_date_shape_flagged() -> None:
    # A near-miss stem (single-digit month) must not satisfy the date prefix.
    path = ".ea/artifacts/research/2026-6-11-thing.md"
    violation = check_artifact_path(path)
    assert violation is not None
    assert "date stem" in violation.reason


# --- placement lint: pass paths --------------------------------------------


def test_check_artifact_path_canonical_passes() -> None:
    assert check_artifact_path(".ea/artifacts/audits/2026-06-11-p30-i14-closeout.md") is None


def test_check_artifact_path_nested_subdir_passes() -> None:
    # research/long-term/ is a valid nested sub-tree under the research kind.
    nested = ".ea/artifacts/research/long-term/2026-05-16-c01-foundations.md"
    assert check_artifact_path(nested) is None


def test_check_artifact_path_out_of_surface_skipped() -> None:
    # Not under .ea/artifacts/ -> outside the rule's surface, never flagged.
    assert check_artifact_path(".ea/local/research/2026-06-11-draft.md") is None
    assert check_artifact_path("src/eawf/foo.py") is None


def test_check_artifact_path_non_markdown_skipped() -> None:
    assert check_artifact_path(".ea/artifacts/notakind/2026-06-11-thing.txt") is None


def test_check_artifact_path_grandfathered_passes() -> None:
    # A legacy date-stem-less file on the baseline is exempt.
    legacy = ".ea/artifacts/audits/A09-P08-ship-gate.md"
    assert legacy in GRANDFATHERED_ARTIFACTS
    assert check_artifact_path(legacy) is None
    # ... but with an empty grandfather set it IS flagged.
    assert check_artifact_path(legacy, grandfather=frozenset()) is not None


# --- placement lint: list aggregation + clean tree -------------------------


def test_check_artifact_paths_empty() -> None:
    assert check_artifact_paths([]) == []


def test_check_artifact_paths_collects_in_order() -> None:
    paths = [
        ".ea/artifacts/audits/2026-06-11-good.md",
        ".ea/artifacts/badkind/2026-06-11-x.md",
        ".ea/artifacts/audits/no-date.md",
    ]
    violations = check_artifact_paths(paths)
    assert [v.path for v in violations] == [
        ".ea/artifacts/badkind/2026-06-11-x.md",
        ".ea/artifacts/audits/no-date.md",
    ]


def test_check_artifact_paths_clean_over_real_tree() -> None:
    proc = subprocess.run(
        ["git", "ls-files", ".ea/artifacts/**/*.md", ".ea/artifacts/*.md"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    tracked = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert tracked, "expected a non-empty tracked artifact tree"
    violations = check_artifact_paths(tracked)
    assert violations == [], f"clean tree should pass; got {[v.path for v in violations]}"


# --- model boundary: ArtifactPathStr ---------------------------------------


class _Generic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: ArtifactPathStr


def test_artifact_path_str_generic_accepts_canonical() -> None:
    model = _Generic(path=".ea/artifacts/audits/2026-06-11-closeout.md")
    assert model.path.endswith("2026-06-11-closeout.md")


def test_artifact_path_str_generic_accepts_nested() -> None:
    model = _Generic(path=".ea/artifacts/research/long-term/2026-05-16-c01-foundations.md")
    assert "long-term" in model.path


def test_artifact_path_str_generic_rejects_missing_date_stem() -> None:
    with pytest.raises(ValidationError) as exc:
        _Generic(path=".ea/artifacts/audits/closeout.md")
    assert "path" in str(exc.value)


def test_artifact_path_str_generic_rejects_bad_subdir() -> None:
    with pytest.raises(ValidationError):
        _Generic(path=".ea/artifacts/notakind/2026-06-11-x.md")


# --- model boundary: per-kind artifact_path_str factory --------------------


def test_artifact_path_str_factory_pins_to_kind() -> None:
    class _Audit(BaseModel):
        model_config = ConfigDict(extra="forbid")

        path: artifact_path_str("audit")  # type: ignore[valid-type]

    ok = _Audit(path=".ea/artifacts/audits/2026-06-11-closeout.md")
    assert ok.path.endswith(".md")
    # A research path is the wrong sub-directory for the audit-pinned field.
    with pytest.raises(ValidationError):
        _Audit(path=".ea/artifacts/research/2026-06-11-thing.md")


def test_artifact_path_str_factory_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown artifact kind"):
        artifact_path_str("notakind")
