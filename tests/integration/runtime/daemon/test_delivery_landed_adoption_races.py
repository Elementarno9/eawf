"""An adoption re-checks the Batch head it extends, and a keyed retry replays.

``runtime.delivery.adopt_landed`` reads the repository with no lock held,
so another generation can be selected between the read and the append.
The head is read again under the lock the new lines are appended under,
and an adoption prepared on a head that has since moved is refused rather
than filed as a second generation of one ordinal. The idempotency key the
verb takes is honoured: a retry under it answers what the first call
answered, and the same key naming other parameters is refused.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.ledger import append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import delivery_landed
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path
from tests.integration.runtime.daemon.test_delivery_landed_loop import _adopt, landed_canary
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _generations(path: Path) -> list[dict[str, Any]]:
    """Return every generation line of the Batch ledger, oldest first."""
    return [
        dict(item.payload)
        for item in read_ledger_records(ledger_path(path, Epoch2Collection.BATCH))
        if item.record_key.startswith("ING-")
    ]


def test_r08_adoption_refuses_a_head_selected_while_the_repository_was_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generation selected mid-read leaves the adoption refused, not duplicated."""
    canary, base, head = landed_canary(tmp_path)
    path = document_path(canary)
    unlocked_read = delivery_landed._read_repository

    def racing_read(*args: Any, **kwargs: Any) -> Any:
        read = unlocked_read(*args, **kwargs)
        append_ledger_record(
            ledger_path(path, Epoch2Collection.BATCH),
            world.generation_line(world.delivering_generation()),
        )
        return read

    monkeypatch.setattr(delivery_landed, "_read_repository", racing_read)

    with pytest.raises(methods.DaemonValidationError, match="adoption_head_moved"):
        _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    ordinals = [item["generation"] for item in _generations(path)]
    assert ordinals == [2]


def test_r08_adoption_on_an_unmoved_head_still_selects_its_generation(tmp_path: Path) -> None:
    """The positive control: nothing moved, so the recheck lets the append through."""
    canary, base, head = landed_canary(tmp_path)

    answer = _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    assert answer["generation"] == 2
    assert [item["id"] for item in _generations(document_path(canary))] == ["ING-000002"]


def test_r03_adoption_retried_under_its_key_replays_the_first_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry under the same key answers from the filed answer, reading nothing again."""
    canary, base, head = landed_canary(tmp_path)
    first = _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a keyed retry must not read the repository again")

    monkeypatch.setattr(delivery_landed, "_read_repository", unreachable)
    again = _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    assert again == {**first, "replayed": True}
    assert len(_generations(document_path(canary))) == 1


def test_r03_adoption_key_naming_other_parameters_is_refused(tmp_path: Path) -> None:
    """One key cannot name two different adoptions."""
    canary, base, head = landed_canary(tmp_path)
    _adopt(canary, tmp_path, base_commit=base, head_sha=head)

    with pytest.raises(methods.DaemonValidationError, match="idempotency_conflict"):
        _adopt(canary, tmp_path, base_commit=base, head_sha=head, affected_criterion_ids=["CR-01"])
