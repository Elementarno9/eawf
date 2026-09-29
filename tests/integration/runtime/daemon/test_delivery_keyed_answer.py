"""The keyed answer a delivery verb replays a retry from.

``runtime.delivery.adopt_landed`` and ``runtime.delivery.prove_task``
append ledger lines, so each files the answer it returned under the
caller's idempotency key and answers a retry from it. The key lives in
the root's namespace and in the verb's, so one client key sent to two
verbs names two requests; the same key with other parameters is refused;
and an answer that cannot be read refuses rather than running twice.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.daemon.epoch2_recovery import idempotency_receipt_path
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery import file_keyed_answer, keyed_answer
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

AT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
PARAMS: dict[str, Any] = {"urn": world.TASK, "gates": []}
ANSWER: dict[str, Any] = {"task_ref": world.TASK, "passed": True}


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def context(tmp_path: Path) -> Epoch2RootContext:
    """Return the native context of a disposable canary."""
    return world.native_tree(tmp_path / "repo", tmp_path / "runtime")


def _file(context: Epoch2RootContext, *, method: str = "m.prove", key: str = "k") -> None:
    """File the shared answer under *key* for *method*."""
    file_keyed_answer(context, method=method, key=key, params=PARAMS, answer=ANSWER, at=AT)


def test_a_key_that_answered_nothing_replays_nothing(context: Epoch2RootContext) -> None:
    assert keyed_answer(context, method="m.prove", key="k", params=PARAMS) is None


def test_a_filed_answer_replays_for_the_same_parameters(context: Epoch2RootContext) -> None:
    _file(context)

    assert keyed_answer(context, method="m.prove", key="k", params=dict(PARAMS)) == ANSWER


def test_the_same_key_with_other_parameters_is_refused(context: Epoch2RootContext) -> None:
    _file(context)

    with pytest.raises(DaemonValidationError, match="idempotency_conflict"):
        keyed_answer(context, method="m.prove", key="k", params={**PARAMS, "gates": ["G-01"]})


def test_one_key_sent_to_two_verbs_names_two_requests(context: Epoch2RootContext) -> None:
    _file(context, method="m.prove")

    assert keyed_answer(context, method="m.adopt", key="k", params=PARAMS) is None


def test_an_unreadable_answer_refuses_rather_than_running_twice(
    context: Epoch2RootContext,
) -> None:
    _file(context)
    path = idempotency_receipt_path(context, namespaced_key=context.idempotency_key("m.prove:k"))
    path.write_bytes(b"{not json")

    with pytest.raises(DaemonValidationError, match="cannot be read"):
        keyed_answer(context, method="m.prove", key="k", params=PARAMS)
