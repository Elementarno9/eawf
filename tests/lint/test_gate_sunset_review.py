"""The recorded sunset review of the gates a closing phase left behind.

A gate that never fires is retired at phase close; a gate that is kept
has to say why, in the place a reader meets it. Without a recorded
review both outcomes look identical from the tree -- a kept gate and a
forgotten one are the same line of YAML -- so this module is where each
verdict is pinned.

Two verdicts are pinned here.

The telemetry-sync CI step is a KEEP. It guards a silent regression: an
off-by-default projector leaves the suite green and every duration
distribution empty, so the step running at all is the evidence. Its
comment carries the review, and this test refuses a tree where the step
or the note has quietly gone.

The unsettled-reasoning-runtime filter is a RETIRE. It shipped as an
escape hatch over an empty runtime set, so it could never fire, and an
exclusion rung nothing can reach only makes the cost ladder harder to
read. The mechanism is gone from the producer; the record still
publishes its two fields, which read false and zero, because persisted
baseline artifacts carry those keys and the model forbids unknown ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"
_SRC = _REPO_ROOT / "src" / "eawf"
_TURN_COST = _SRC / "observability" / "telemetry" / "turn_cost.py"

#: Name of the kept CI step, exactly as the workflow spells it.
_TELEMETRY_STEP = "Telemetry sync (REL-009)"

#: The retired mechanism's symbol. Its reappearance anywhere in the
#: producer means the empty escape hatch came back.
_RETIRED_SYMBOL = "REASONING_UNSETTLED_RUNTIMES"


def _ci_document() -> dict:
    """Return the parsed CI workflow."""
    parsed = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _test_job_steps() -> list[dict]:
    """Return the steps of the matrix test job the review covers."""
    steps = _ci_document()["jobs"]["test"]["steps"]
    assert isinstance(steps, list)
    return steps


def test_kept_telemetry_step_still_runs_in_ci() -> None:
    """The KEEP verdict is only real while the step is still wired."""
    names = [step.get("name") for step in _test_job_steps()]

    assert _TELEMETRY_STEP in names


def test_kept_telemetry_step_runs_its_fire_proof_test() -> None:
    """The step's value is the assertions in the test it invokes."""
    step = next(item for item in _test_job_steps() if item.get("name") == _TELEMETRY_STEP)

    assert "tests/integration/test_metrics_cli.py" in step["run"]


def test_kept_telemetry_step_carries_the_review_note() -> None:
    """A keep with no recorded reason is indistinguishable from an oversight."""
    source = _CI.read_text(encoding="utf-8")
    step_at = source.index(_TELEMETRY_STEP)
    note_at = source.index("Sunset review: KEEP", step_at)

    assert note_at - step_at < 1_000


def test_retired_runtime_filter_is_gone_from_the_producer() -> None:
    """The RETIRE verdict is only real while the mechanism stays deleted."""
    assert _RETIRED_SYMBOL not in _TURN_COST.read_text(encoding="utf-8")


def test_retired_runtime_filter_is_gone_from_every_production_module() -> None:
    """A filter re-added under another module is the same unfirable rung."""
    carriers = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _SRC.rglob("*.py")
        if _RETIRED_SYMBOL in path.read_text(encoding="utf-8")
    )

    assert carriers == []


def test_retired_filter_leaves_its_record_fields_readable() -> None:
    """Retiring the rung must not break a persisted baseline artifact."""
    source = _TURN_COST.read_text(encoding="utf-8")

    assert "reasoning_summand_unsettled: bool" in source
    assert "reasoning_summand_unsettled_run_count: int" in source
