"""Measure the largest supported state's apply against its recorded budget.

The blocking rehearsal asserts what the apply published; this test asserts
how long it took, against the maintenance window the live corpus pin records
in ``apply_timeout_budget_s``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration.kernel.migration._live_corpus import PIN_FILENAME, LiveCorpusPin
from tests.integration.kernel.migration._rehearsal import LIVE_CORPUS_ROOT, REPO_ROOT, rehearse
from tests.integration.kernel.migration._rehearsal_set import LARGEST_FIXTURE, stage_corpus

_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1",
    reason="EAWF_SKIP_PERF=1 - perf gate skipped",
)


@pytest.fixture(autouse=True)
def _no_inherited_state_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an inherited ``EA_STATE`` from redirecting the rehearsal's tree."""
    monkeypatch.delenv("EA_STATE", raising=False)


@_skip_perf
def test_largest_supported_state_applies_inside_its_declared_budget(tmp_path: Path) -> None:
    pin = LiveCorpusPin.load(LIVE_CORPUS_ROOT / PIN_FILENAME)
    corpus = stage_corpus(fixture=LARGEST_FIXTURE, root=tmp_path, repo_root=REPO_ROOT)

    record = rehearse(fixture=LARGEST_FIXTURE, corpus=corpus, root=tmp_path)

    assert record.apply is not None
    assert record.apply.applied is True
    assert record.apply.wall_clock_s <= pin.apply_timeout_budget_s, (
        f"the apply took {record.apply.wall_clock_s}s, over the "
        f"{pin.apply_timeout_budget_s}s budget the pin records"
    )
