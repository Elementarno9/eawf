"""REL-027: the ratified epoch-2 state-tree Decision stays on the record.

P31-I01-W13 settled the epoch-2 target tree before the dev2 importer is
written, so that the importer validates against a decided shape instead of
establishing one as a side effect. The ruling is only useful while it stays
queryable and keeps naming both of its halves: the target tree shape, and
the boundary saying where an epoch-1 row that epoch-2 cannot model natively
is allowed to live. This suite pins the ``eawf decision list`` surface, the
stored rationale carrying the boundary, and the brief that argues for them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[4]
_STATE_PATH = _REPO_ROOT / ".ea" / "state.json"
_BRIEF_DIR = _REPO_ROOT / ".ea" / "artifacts" / "research"

#: The target shape, as the decision summary must still say it. Matched on
#: content rather than on the id ``D42`` so a renumbering does not silently
#: retire the gate, and a summary that drops either term stops answering
#: "which tree, and where do the legacy rows go".
_SHAPE_TERMS = ("compact native tree", "legacy store")

#: The two boundary clauses. A legacy *reference* is a string on a native
#: envelope (``legacy_refs``); a legacy *record* is an envelope in the legacy
#: ledger. Losing either term from the rationale loses the boundary.
_BOUNDARY_TERMS = ("legacy_refs", "legacy record")

runner = CliRunner()


def _load_state() -> dict[str, Any]:
    return json.loads(_STATE_PATH.read_text(encoding="utf-8"))


def _state_tree_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the rows whose summary names the epoch-2 state-tree shape."""
    return [row for row in rows if all(term in row["summary"].lower() for term in _SHAPE_TERMS)]


@pytest.fixture
def listed_decisions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Return ``eawf decision list --json`` rows for the live repo state."""
    monkeypatch.setenv("EA_STATE", str(_STATE_PATH))
    result = runner.invoke(app, ["--json", "decision", "list"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    return list(payload["decisions"])


def test_decision_list_returns_active_state_tree_decision(
    listed_decisions: list[dict[str, Any]],
) -> None:
    """CR-01: exactly one ACTIVE decision names the epoch-2 target tree."""
    matches = _state_tree_rows(listed_decisions)
    assert len(matches) == 1, f"expected one state-tree decision, got {[m['id'] for m in matches]}"
    decision = matches[0]
    assert decision["status"] == "active"
    assert decision["scope_id"] == "EAWF"


def test_decision_rationale_names_the_legacy_store_boundary(
    listed_decisions: list[dict[str, Any]],
) -> None:
    """CR-01: the ruling states where a legacy row lives, not just the shape."""
    decision_id = _state_tree_rows(listed_decisions)[0]["id"]
    rationale = (_load_state()["decisions"][decision_id]["rationale"] or "").lower()
    missing = [term for term in _BOUNDARY_TERMS if term not in rationale]
    assert not missing, f"{decision_id} rationale no longer names the boundary: {missing}"


def test_decision_alternatives_record_the_merged_tree_rival(
    listed_decisions: list[dict[str, Any]],
) -> None:
    """CR-01: the rejected single-merged-tree shape stays on the record.

    A decision that lost its alternatives reads as the only option anyone
    considered, which is exactly what makes a later reversal unreviewable.
    """
    decision_id = _state_tree_rows(listed_decisions)[0]["id"]
    alternatives = _load_state()["decisions"][decision_id]["alternatives"]
    assert any("merged tree" in alternative.lower() for alternative in alternatives), alternatives


def test_state_tree_rows_rejects_a_half_named_shape() -> None:
    """The matcher discriminates: naming the tree without the store is not a match.

    Negative control for the three assertions above. Without it, a matcher
    that accepted any decision mentioning a tree would keep this suite green
    through exactly the regression it exists to catch.
    """
    half_named = [
        {"id": "D00", "summary": "Adopt the compact native tree", "status": "active"},
        {"id": "D01", "summary": "Keep a separate legacy store somewhere", "status": "active"},
    ]
    assert _state_tree_rows(half_named) == []


def test_promoted_brief_argues_for_the_decided_shape(
    listed_decisions: list[dict[str, Any]],
) -> None:
    """CR-01: a research brief on the decided shape backs the ruling.

    Binds the state row to committed prose, so a decision cannot survive as
    a bare title once the reasoning behind it is gone.
    """
    assert _state_tree_rows(listed_decisions), "no ACTIVE state-tree decision to back"
    titled = [
        path
        for path in sorted(_BRIEF_DIR.glob("*.md"))
        if all(
            term in path.read_text(encoding="utf-8").splitlines()[0].lower()
            for term in _SHAPE_TERMS
        )
    ]
    assert len(titled) == 1, f"expected one state-tree brief, got {[p.name for p in titled]}"
    text = titled[0].read_text(encoding="utf-8").lower()
    missing = [term for term in _BOUNDARY_TERMS if term not in text]
    assert not missing, f"{titled[0].name} no longer names the boundary: {missing}"
