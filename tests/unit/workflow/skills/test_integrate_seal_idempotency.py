"""``/integrate seal`` names a repeated seal the way it named the first one.

The report-bind verb is the daemon's record of one seal request, and a
request the transport retries must reach it under the same name, or a
dropped answer turns one seal into two requests. The key is therefore
derived from the Run and the candidate the seal is about rather than
minted per invocation.

The daemon here is a stand-in that keeps one ledger row per request name
and answers a name it already holds with its standing binding, which is
the behaviour a derived key exists to reach. A minted key gives the second
invocation a fresh name, so the stand-in writes a second row and the
suite reds.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from eawf.workflow.skills.engine import SkillContext
from eawf.workflow.skills.integrate import CANDIDATE_REPORT_BIND_METHOD, IntegrateSkill

RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
OTHER_RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
CANDIDATE: Final = f"CND-{'a' * 32}"
OTHER_CANDIDATE: Final = f"CND-{'b' * 32}"
TREE: Final = f"sha256:{'1' * 64}"


class KeyedLedger:
    """A report-bind verb that records one row per request name.

    Attributes:
        rows: The binding rows written, keyed by the request name.
        keys: Every request name presented, in order.
    """

    def __init__(self) -> None:
        """Start holding no binding."""
        self.rows: dict[str, dict[str, Any]] = {}
        self.keys: list[str] = []

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Answer one call, replaying the standing binding of a known name."""
        assert method == CANDIDATE_REPORT_BIND_METHOD
        key = str(params["idempotency_key"])
        self.keys.append(key)
        standing = self.rows.get(key)
        if standing is not None:
            return {**standing, "replayed": True}
        row = {"candidate_ref": params["candidate_ref"], "sealed": True, "replayed": False}
        self.rows[key] = row
        return row


def seal(ledger: KeyedLedger, **overrides: Any) -> dict[str, Any]:
    """Run ``/integrate seal`` against *ledger* and return its typed body."""
    args: dict[str, Any] = {
        "action": "seal",
        "subject_ref": CANDIDATE,
        "run": RUN_URN,
        "resulting_tree_digest": TREE,
        "expected_revision": 2,
        **overrides,
    }
    result = IntegrateSkill(caller=ledger).action(
        SkillContext(scope="scope", session="session", args=args)
    )
    assert isinstance(result.body, dict)
    return result.body


def test_a_repeated_seal_returns_the_standing_binding_with_no_second_row() -> None:
    ledger = KeyedLedger()

    first = seal(ledger)
    second = seal(ledger)

    assert first["outcome"] == second["outcome"] == "sealed"
    assert len(ledger.rows) == 1
    assert ledger.keys[0] == ledger.keys[1]


def test_a_single_seal_writes_exactly_one_row() -> None:
    ledger = KeyedLedger()

    seal(ledger)

    assert len(ledger.rows) == 1
    assert ledger.keys[0].startswith("seal-")
    assert len(ledger.keys[0]) <= 128


@pytest.mark.parametrize(
    ("run", "candidate"),
    [(OTHER_RUN_URN, CANDIDATE), (RUN_URN, OTHER_CANDIDATE)],
    ids=["other-run", "other-candidate"],
)
def test_another_run_or_candidate_is_another_seal(run: str, candidate: str) -> None:
    ledger = KeyedLedger()

    seal(ledger)
    seal(ledger, run=run, subject_ref=candidate)

    assert len(ledger.rows) == 2
    assert ledger.keys[0] != ledger.keys[1]


def test_a_presented_key_is_sent_as_presented() -> None:
    ledger = KeyedLedger()

    seal(ledger, idempotency_key="operator-key")

    assert ledger.keys == ["operator-key"]


def test_a_seal_missing_its_run_stops_before_any_key_is_sent() -> None:
    ledger = KeyedLedger()

    body = seal(ledger, run=None)

    assert body["refusal_code"] == "candidate_report_unbound"
    assert ledger.keys == []
