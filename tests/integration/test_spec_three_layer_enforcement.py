"""Three-layer enforcement integration tests (P25-W05 success criterion 1).

Each layer must catch the RC-1 stale-paths failure class on its own
(per the C03 brief §1). This module verifies the two layers W05 ships
end-to-end:

- **Layer 1 — Pydantic + loader.** A WaveSpec that names a missing
  test path passes ``model_validate`` (paths are not checked at the
  Pydantic level) but fails when the loader function
  :func:`eawf.spec.validators.validate_wave_spec_at_load` is invoked
  against a temp project root.
- **Layer 1 (UI heuristic).** A UI-scope WaveSpec without mockup +
  without waiver fails at ``model_validate`` time — the heuristic
  catches the failure even before the loader runs.
- **Layer 2 — Pre-commit hook.** :mod:`tools.pre_commit_spec_paths`
  greps committed spec markdown for ``tests/`` paths and fails when
  any cited file is missing on disk.

The third layer (audit DSL ``verify-implements`` from W02) is **not**
exercised here — it is not on this branch's ancestry. The W02 wave
ships the kind separately and its own tests verify the audit path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.spec.common import VerdictCitation
from eawf.spec.validators import SpecValidationError, validate_wave_spec_at_load
from eawf.spec.wave import WaveBehavior, WaveMockup, WaveSpec
from eawf.state.enums import AgentSessionRole, EffortBucket

# Path to the hook script (resolved from the test's repo so the script
# call works no matter where pytest runs from).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK_SCRIPT = _REPO_ROOT / "tools" / "pre_commit_spec_paths.py"


def _verdict_citation(
    brief: str = ".ea/artifacts/research/2026-05-16-c03-spec-infrastructure.md",
) -> VerdictCitation:
    return VerdictCitation(verdict_id="V12", brief=brief)


def _wave_spec(
    *,
    file_scopes: list[str] | None = None,
    tests: list[str] | None = None,
    mockup: WaveMockup | None = None,
    mockup_waiver_reason: str | None = None,
) -> WaveSpec:
    return WaveSpec.model_validate(
        {
            "id": "P25-I01-W05",
            "iter_id": "P25-I01",
            "phase_id": "P25",
            "title": "C03 validators",
            "agent_role": AgentSessionRole.EXECUTOR,
            "effort_bucket": EffortBucket.M,
            "file_scopes": file_scopes or ["src/eawf/spec/validators.py"],
            "implements": [_verdict_citation()],
            "behaviors": [
                WaveBehavior(
                    id="B1",
                    text="loader validators reject missing test paths",
                )
            ],
            "failure_modes": ["false negative: stale path slips through pre-commit"],
            "tests": tests or [],
            "mockup": mockup,
            "mockup_waiver_reason": mockup_waiver_reason,
        }
    )


# ---- Layer 1 — Pydantic (model_validator) ----------------------------------


def test_layer1_pydantic_ui_scope_without_mockup_fails_at_model_validate() -> None:
    # No tmp_path / disk lookup — the UI heuristic catches this at
    # ``model_validate`` time so the failure surfaces before any loader
    # function is called.
    with pytest.raises(ValidationError) as excinfo:
        _wave_spec(file_scopes=["src/eawf/tui_v2/header.py"])
    assert "ui-scope wave requires mockup reference" in str(excinfo.value)


def test_layer1_pydantic_ui_scope_with_mockup_passes() -> None:
    spec = _wave_spec(
        file_scopes=["src/eawf/tui_v2/header.py"],
        mockup=WaveMockup(ascii="+---+\n|.|\n+---+"),
    )
    assert spec.mockup is not None


# ---- Layer 1 — Loader (validate_wave_spec_at_load) -------------------------


def test_layer1_loader_passes_when_test_paths_exist(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "unit" / "test_real.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("# test\n", encoding="utf-8")
    brief = tmp_path / ".ea" / "artifacts" / "research" / "2026-05-16-c03-spec-infrastructure.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("# brief\n", encoding="utf-8")
    spec = _wave_spec(tests=["tests/unit/test_real.py"])
    validate_wave_spec_at_load(spec, tmp_path)


def test_layer1_loader_rejects_missing_test_path(tmp_path: Path) -> None:
    # Brief path exists; only the test path is stale.
    brief = tmp_path / ".ea" / "artifacts" / "research" / "2026-05-16-c03-spec-infrastructure.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("# brief\n", encoding="utf-8")
    spec = _wave_spec(tests=["tests/unit/test_missing.py"])
    with pytest.raises(SpecValidationError) as excinfo:
        validate_wave_spec_at_load(spec, tmp_path)
    assert "tests/unit/test_missing.py" in str(excinfo.value)


def test_layer1_loader_rejects_missing_brief_path(tmp_path: Path) -> None:
    # Test path exists; brief path is stale.
    test_file = tmp_path / "tests" / "unit" / "test_real.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("# test\n", encoding="utf-8")
    spec = _wave_spec(tests=["tests/unit/test_real.py"])
    with pytest.raises(SpecValidationError) as excinfo:
        validate_wave_spec_at_load(spec, tmp_path)
    assert "2026-05-16-c03-spec-infrastructure.md" in str(excinfo.value)


# ---- Layer 2 — Pre-commit hook ---------------------------------------------


def _run_hook(staged_paths: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the pre-commit script from ``cwd`` with ``staged_paths`` as argv."""
    return subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT), *staged_paths],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _seed_spec_markdown(spec_path: Path, body: str) -> None:
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(body, encoding="utf-8")


def test_layer2_hook_passes_when_no_spec_files_staged(tmp_path: Path) -> None:
    # Staged paths outside .ea/specs/ are ignored.
    proc = _run_hook(["src/eawf/spec/wave.py", "tests/unit/test_x.py"], tmp_path)
    assert proc.returncode == 0


def test_layer2_hook_passes_when_cited_test_files_exist(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "unit" / "test_real.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("# test\n", encoding="utf-8")
    spec_path = tmp_path / ".ea" / "specs" / "P25" / "P25-I01-W05.md"
    _seed_spec_markdown(
        spec_path,
        "# WaveSpec P25-I01-W05\n\n## Tests\n\n- tests/unit/test_real.py\n",
    )
    proc = _run_hook([".ea/specs/P25/P25-I01-W05.md"], tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_layer2_hook_rejects_when_cited_test_file_missing(tmp_path: Path) -> None:
    spec_path = tmp_path / ".ea" / "specs" / "P25" / "P25-I01-W05.md"
    _seed_spec_markdown(
        spec_path,
        "# WaveSpec P25-I01-W05\n\n## Tests\n\n- tests/unit/test_missing.py\n",
    )
    proc = _run_hook([".ea/specs/P25/P25-I01-W05.md"], tmp_path)
    assert proc.returncode == 1
    assert "tests/unit/test_missing.py" in proc.stderr
    assert "P25-I01-W05.md" in proc.stderr


def test_layer2_hook_rejects_multiple_missing_paths(tmp_path: Path) -> None:
    # Both seeded existing + missing — only the missing one should be
    # reported.
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_real.py").write_text("# test\n", encoding="utf-8")
    spec_path = tmp_path / ".ea" / "specs" / "P25" / "P25-I01-W05.md"
    _seed_spec_markdown(
        spec_path,
        "# WaveSpec\n\n## Tests\n\n- tests/unit/test_real.py\n- tests/unit/test_gone.py\n"
        "- `tests/integration/test_z.py`\n",
    )
    proc = _run_hook([".ea/specs/P25/P25-I01-W05.md"], tmp_path)
    assert proc.returncode == 1
    assert "tests/unit/test_gone.py" in proc.stderr
    assert "tests/integration/test_z.py" in proc.stderr
    assert "tests/unit/test_real.py" not in proc.stderr


def test_layer2_hook_handles_markdown_link_citation(tmp_path: Path) -> None:
    # Tests paths inside ``[label](tests/...)`` markdown links are
    # detected by the regex.
    spec_path = tmp_path / ".ea" / "specs" / "P25" / "P25-I01-W05.md"
    _seed_spec_markdown(
        spec_path,
        "# WaveSpec\n\n## Tests\n\nSee [the test](tests/unit/test_missing.py) for the assertion.\n",
    )
    proc = _run_hook([".ea/specs/P25/P25-I01-W05.md"], tmp_path)
    assert proc.returncode == 1
    assert "tests/unit/test_missing.py" in proc.stderr


def test_layer2_hook_dedupes_same_path_cited_twice(tmp_path: Path) -> None:
    spec_path = tmp_path / ".ea" / "specs" / "P25" / "P25-I01-W05.md"
    _seed_spec_markdown(
        spec_path,
        "# WaveSpec\n\n## Tests\n\n- tests/unit/test_missing.py\n- tests/unit/test_missing.py\n",
    )
    proc = _run_hook([".ea/specs/P25/P25-I01-W05.md"], tmp_path)
    assert proc.returncode == 1
    # Path is listed once in the diagnostic (de-duplicated).
    assert proc.stderr.count("tests/unit/test_missing.py") == 1


def test_layer2_hook_ignores_non_spec_paths(tmp_path: Path) -> None:
    # Non-spec markdown is not scanned even when it cites missing tests.
    doc_path = tmp_path / "docs" / "x.md"
    _seed_spec_markdown(doc_path, "Cites tests/unit/test_missing.py\n")
    proc = _run_hook(["docs/x.md"], tmp_path)
    assert proc.returncode == 0


def test_layer2_hook_ignores_deleted_spec_path(tmp_path: Path) -> None:
    # When pre-commit passes a path that no longer exists in the working
    # tree (e.g. a rename in flight), the hook silently skips it.
    proc = _run_hook([".ea/specs/P25/P25-I01-W99-deleted.md"], tmp_path)
    assert proc.returncode == 0


def test_layer2_hook_processes_multiple_spec_files(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_real.py").write_text("# test\n", encoding="utf-8")
    spec_a = tmp_path / ".ea" / "specs" / "P25" / "P25-I01-W01.md"
    spec_b = tmp_path / ".ea" / "specs" / "P25" / "P25-I01-W02.md"
    _seed_spec_markdown(spec_a, "## Tests\n\nCites tests/unit/test_real.py\n")
    _seed_spec_markdown(spec_b, "## Tests\n\nCites tests/unit/test_missing.py\n")
    proc = _run_hook(
        [".ea/specs/P25/P25-I01-W01.md", ".ea/specs/P25/P25-I01-W02.md"],
        tmp_path,
    )
    assert proc.returncode == 1
    assert "P25-I01-W02.md" in proc.stderr
    assert "P25-I01-W01.md" not in proc.stderr
