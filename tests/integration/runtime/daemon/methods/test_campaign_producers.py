"""The native Campaign writers produce the plan steps, artifact revisions and findings.

Driven through the real ``runtime.campaign.*`` writers and the ``projection.campaign.*``
reads against a provisioned canary whose document holds one live Track. Row ids name the
packet requirement each test proves: PLAN-050 (every artifact revision a Campaign or a step
lists resolves to a record carrying what its card names), UI-056 (the Campaign view carries
its steps and artifacts, a blocked step is derived and names its blocker), UI-070 (the
artifact card is a typed read model whose lines are the revision's text, and a binary one
carries none) and DOM-034 (a Campaign promotion writes a held ``CFN-####`` finding).
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.campaign import ArtifactCardView, CampaignView
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError
from eawf.runtime.daemon.methods.campaign import (
    CAMPAIGN_ARTIFACT_METHOD,
    CAMPAIGN_ARTIFACT_RECORD_METHOD,
    CAMPAIGN_FINDING_PROMOTE_METHOD,
    CAMPAIGN_PLAN_APPROVE_METHOD,
    CAMPAIGN_STEP_UPDATE_METHOD,
    CAMPAIGN_VIEW_METHOD,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK: Final = f"{SLOT}/track/TRK-RUNTIME"
CAMPAIGN: Final = f"{SLOT}/campaign/CAM-0001"
RUN_A: Final = f"{SLOT}/run/RUN-00000010"
RUN_B: Final = f"{SLOT}/run/RUN-00000011"
QUESTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"
CONTRADICTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0004"
REPORT: Final = "# Replay order\n\n- 40 events compared\n- 0 inversions\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtimes under tmp, and the operator's home out of reach."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one live Track to approve a Campaign under."""
    provisioned = provision(tmp_path / "cam", code="CAM")
    seed(provisioned, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    return provisioned


def call(canary: CanaryProvision, tmp_path: Path, method: str, **params: Any) -> dict[str, Any]:
    """Dispatch one verb through the real handler against *canary*."""
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def _axes(hours: int) -> dict[str, Any]:
    return {"axes": [{"axis_kind": "wall_time", "limit": hours, "unit": "h"}]}


def _step(ordinal: int, *, depends_on: tuple[int, ...] = (), **extra: Any) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "title": f"Survey replay order source {ordinal}",
        "method": "survey",
        "question_ref": QUESTION,
        "depends_on": list(depends_on),
        "bound": _axes(2),
        **extra,
    }


def approve(canary: CanaryProvision, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """Approve a three-step plan: 2 waits on 1, 3 waits on a contradiction."""
    params: dict[str, Any] = {
        "actor": "OP-0001",
        "track_ref": TRACK,
        "title": "Establish whether replay preserves event order",
        "evidence_budget": _axes(6),
        "plan_steps": [
            _step(1),
            _step(2, depends_on=(1,)),
            _step(3, blocking_contradiction_refs=[CONTRADICTION]),
        ],
        **overrides,
    }
    return call(canary, tmp_path, CAMPAIGN_PLAN_APPROVE_METHOD, **params)


def update(canary: CanaryProvision, tmp_path: Path, **params: Any) -> dict[str, Any]:
    """Update one step against the Campaign's current revision."""
    revision = read_document(document_path(canary))["campaign"]["CAM-0001"]["revision"]
    return call(
        canary,
        tmp_path,
        CAMPAIGN_STEP_UPDATE_METHOD,
        actor="AG-0001",
        urn=CAMPAIGN,
        expected_revision=revision,
        **params,
    )


def record(canary: CanaryProvision, tmp_path: Path, name: str, **params: Any) -> dict[str, Any]:
    """Record the repository file *name* as an artifact of the Campaign."""
    return call(
        canary,
        tmp_path,
        CAMPAIGN_ARTIFACT_RECORD_METHOD,
        actor="AG-0001",
        urn=CAMPAIGN,
        file_name=name,
        **params,
    )


def view(canary: CanaryProvision, tmp_path: Path) -> CampaignView:
    """Read the Campaign back through its read verb."""
    answer = call(canary, tmp_path, CAMPAIGN_VIEW_METHOD, campaign_key="CAM-0001")
    return CampaignView.model_validate(answer)


def write(canary: CanaryProvision, name: str, content: bytes) -> None:
    path = canary.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def ledger(canary: CanaryProvision, collection: Epoch2Collection) -> list[Any]:
    return list(read_ledger_records(ledger_path(document_path(canary), collection)))


# ---------- UI-056: the plan and its steps ----------


def test_ui_056_plan_approval_writes_the_campaign_row_with_every_step_pending(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    answer = approve(canary, tmp_path)
    assert answer["committed"] is True
    row = read_document(document_path(canary))["campaign"]["CAM-0001"]
    assert [step["state"] for step in row["plan_steps"]] == ["pending"] * 3
    assert approve(canary, tmp_path)["committed"] is False
    projected = call(canary, tmp_path, "projection.campaign.read")
    assert [r["key"] for r in projected["rows"]] == ["CAM-0001"]


def test_ui_056_the_campaign_view_derives_blocked_and_names_the_blocker(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    approve(canary, tmp_path)
    before = view(canary, tmp_path)
    assert [s.state for s in before.steps] == ["pending", "blocked", "blocked"]
    assert before.steps[1].waits_on == ("step 1",)
    assert before.steps[2].waits_on == ("CLM-0004",)
    assert before.plan_line == "0 of 3 steps done · 0 running · 2 blocked by step 1, CLM-0004"

    update(canary, tmp_path, ordinal=1, to_state="running", run_ref=RUN_A, spent={"wall_time": 1})
    running = view(canary, tmp_path).steps[0]
    assert (running.state, str(running.runner_ref)) == ("running", RUN_A)
    assert running.step.bound.axes[0].spent == 1
    update(canary, tmp_path, ordinal=1, to_state="done", outcome="Order holds across restarts")
    update(canary, tmp_path, ordinal=3, cleared_contradiction_refs=[CONTRADICTION])
    after = view(canary, tmp_path)
    assert [s.state for s in after.steps] == ["done", "pending", "pending"]
    assert after.plan_line == "1 of 3 steps done · 0 running · 0 blocked"
    row = read_document(document_path(canary))["campaign"]["CAM-0001"]
    assert row["evidence_budget"]["axes"][0]["spent"] == 1


def test_ui_056_a_blocked_step_cannot_start_and_a_spend_never_goes_down(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    approve(canary, tmp_path)
    with pytest.raises(TransactionRefusedError, match="waits on step 1"):
        update(canary, tmp_path, ordinal=2, to_state="running", run_ref=RUN_B)
    update(canary, tmp_path, ordinal=1, to_state="running", run_ref=RUN_A, spent={"wall_time": 1})
    with pytest.raises(TransactionRefusedError, match="never goes down"):
        update(canary, tmp_path, ordinal=1, spent={"wall_time": 0})
    with pytest.raises(TransactionRefusedError, match="cannot move"):
        update(canary, tmp_path, ordinal=1, to_state="running", run_ref=RUN_B)
    with pytest.raises(TransactionRefusedError, match="does not hold"):
        update(canary, tmp_path, ordinal=1, to_state="done")


def test_ui_056_a_step_bound_above_the_campaign_is_refused_at_approval(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    with pytest.raises(TransactionRefusedError, match="above the Campaign"):
        approve(canary, tmp_path, evidence_budget=_axes(1))
    assert "campaign" not in read_document(document_path(canary))


# ---------- PLAN-050 and UI-070: artifact revisions and their cards ----------


def test_plan_050_a_recorded_artifact_resolves_from_the_campaign_and_its_step(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    approve(canary, tmp_path)
    update(canary, tmp_path, ordinal=1, to_state="running", run_ref=RUN_A)
    write(canary, "reports/replay-order.md", REPORT.encode())
    stored = record(
        canary,
        tmp_path,
        "reports/replay-order.md",
        media_kind="markdown",
        run_ref=RUN_A,
        step_ordinal=1,
    )
    revision = stored["record"]["revision"]
    assert revision["digest"] == f"sha256:{hashlib.sha256(REPORT.encode()).hexdigest()}"
    assert (revision["size_bytes"], revision["written_by"]["step_ordinal"]) == (len(REPORT), 1)
    ref = f"{revision['artifact_ref']}#r1"
    row = read_document(document_path(canary))["campaign"]["CAM-0001"]
    assert row["artifact_revision_refs"] == [ref]
    assert row["plan_steps"][0]["produced"] == [ref]
    assert view(canary, tmp_path).artifacts[0].artifact_ref == ref

    again = record(
        canary,
        tmp_path,
        "reports/replay-order.md",
        media_kind="markdown",
        run_ref=RUN_A,
        step_ordinal=1,
    )
    assert again["committed"] is False
    write(canary, "reports/replay-order.md", (REPORT + "- 2 restarts\n").encode())
    record(
        canary,
        tmp_path,
        "reports/replay-order.md",
        media_kind="markdown",
        run_ref=RUN_A,
        step_ordinal=1,
    )
    keys = [line.record_key for line in ledger(canary, Epoch2Collection.ARTIFACT)]
    assert keys == ["ART-0001#r1", "ART-0001#r2"]
    assert [card.artifact_ref.rsplit("#", 1)[1] for card in view(canary, tmp_path).artifacts] == [
        "r1",
        "r2",
    ]


def test_plan_050_an_artifact_from_a_run_the_step_never_ran_is_refused(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    approve(canary, tmp_path)
    write(canary, "notes.txt", b"plain\n")
    with pytest.raises(TransactionRefusedError, match="never run"):
        record(canary, tmp_path, "notes.txt", media_kind="plain", run_ref=RUN_A, step_ordinal=1)
    assert ledger(canary, Epoch2Collection.ARTIFACT) == []


def test_ui_070_the_artifact_card_is_a_typed_read_model_over_each_media_kind(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    approve(canary, tmp_path)
    write(canary, "reports/replay-order.md", REPORT.encode())
    write(canary, "out/run.log", b"line one\nline two\n")
    write(canary, "out/trace.bin", bytes(range(256)))
    for name, kind in (
        ("reports/replay-order.md", "markdown"),
        ("out/run.log", "plain"),
        ("out/trace.bin", "binary"),
    ):
        record(canary, tmp_path, name, media_kind=kind, run_ref=RUN_A)
    cards = [
        ArtifactCardView.model_validate(
            call(
                canary,
                tmp_path,
                CAMPAIGN_ARTIFACT_METHOD,
                campaign_key="CAM-0001",
                artifact_ref=ref,
            )
        )
        for ref in (card.artifact_ref for card in view(canary, tmp_path).artifacts)
    ]
    markdown, plain, binary = cards
    assert markdown.lines == tuple(REPORT.splitlines())
    assert markdown.digest == f"sha256:{hashlib.sha256(REPORT.encode()).hexdigest()}"
    assert (markdown.ordinal_of_total.ordinal, markdown.ordinal_of_total.total) == (1, 3)
    assert str(markdown.kept_with) == CAMPAIGN
    assert plain.lines == ("line one", "line two")
    assert (binary.lines, binary.size_bytes) == ((), 256)


# ---------- DOM-034: the promotion writes a held finding ----------


def test_dom_034_a_campaign_promotion_writes_a_held_cfn_finding(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    approve(canary, tmp_path)
    statement = "replay preserves event order across restarts"
    answer = call(
        canary,
        tmp_path,
        CAMPAIGN_FINDING_PROMOTE_METHOD,
        actor="OP-0001",
        urn=CAMPAIGN,
        statement=statement,
    )
    finding = answer["record"]
    assert (finding["key"], finding["disposition"]) == ("CFN-0001", "held")
    assert finding["urn"] == f"{SLOT}/campaign-finding/CFN-0001"
    assert finding["campaign_ref"] == CAMPAIGN
    again = call(
        canary,
        tmp_path,
        CAMPAIGN_FINDING_PROMOTE_METHOD,
        actor="OP-0001",
        urn=CAMPAIGN,
        statement=statement,
    )
    assert again["committed"] is False
    assert [line.record_key for line in ledger(canary, Epoch2Collection.CAMPAIGN_FINDING)] == [
        "CFN-0001"
    ]
    held = view(canary, tmp_path).findings
    assert [(f.key, f.disposition.value) for f in held] == [("CFN-0001", "held")]
