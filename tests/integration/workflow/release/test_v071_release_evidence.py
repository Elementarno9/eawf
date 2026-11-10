"""The 0.7.1 release evidence backs every runtime the release claims.

Release preflight reads the dispatch smoke receipts and the packet-run
receipts committed beside the 0.7.1 canary export. These tests read the
committed files, so a release cut without the live runs fails here first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from eawf.workflow.evidence.provider_certification import canary_evidence_path
from eawf.workflow.release.canary_receipts import (
    dispatch_smoke_gaps,
    load_dispatch_smoke_receipts,
    read_dispatch_checks,
)
from eawf.workflow.release.packet_run import (
    PacketRunReceipt,
    packet_receipts_export_path,
)

pytestmark = [pytest.mark.integration, pytest.mark.repo_state]

#: This checkout, whose committed evidence is the real 0.7.1 evidence.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

PATCH_VERSION: Final = "0.7.1"
PATCH_KEY: Final = f"REL-{PATCH_VERSION}"

#: The runtime and os_class pairs the 0.7.1 export advertises.
CLAIMS: Final = (("claude-code", "linux"), ("codex", "linux"))


def test_the_patch_smoke_backs_every_claimed_pair() -> None:
    receipts = load_dispatch_smoke_receipts(REPO_ROOT, PATCH_KEY)
    directory = canary_evidence_path(REPO_ROOT, PATCH_KEY).parent

    assert receipts is not None
    assert receipts.release_key == PATCH_KEY
    passed = {
        (item.runtime_id, item.os_class) for item in receipts.receipts if item.scorecard == "pass"
    }
    assert passed >= set(CLAIMS)
    assert dispatch_smoke_gaps(receipts, CLAIMS, read_dispatch_checks(directory, receipts)) == ()


def test_the_patch_packet_runs_pass_on_both_runtimes_and_origins() -> None:
    path = packet_receipts_export_path(REPO_ROOT, PATCH_KEY)
    assert path is not None and path.is_file()
    receipts = [
        PacketRunReceipt.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    latest: dict[tuple[str, str], PacketRunReceipt] = {}
    for receipt in sorted(receipts, key=lambda item: item.ended_at):
        latest[(receipt.runtime, receipt.origin.value)] = receipt

    pairs = {(runtime, origin) for runtime, _ in CLAIMS for origin in ("born_native", "adopted")}
    assert set(latest) >= pairs
    for pair in pairs:
        receipt = latest[pair]
        assert receipt.outcome == "pass", pair
        assert receipt.eawf_version == PATCH_VERSION, pair
        assert receipt.os_class == "linux", pair
        assert not receipt.source_dirty, pair
