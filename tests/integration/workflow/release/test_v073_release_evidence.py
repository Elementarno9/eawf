"""The 0.7.3 release evidence backs every runtime the release claims.

Release preflight reads the dispatch smoke receipts and the packet-run
receipts committed beside the 0.7.3 canary export. These tests read the
committed files, so a release cut without the live runs fails here first.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.workflow.evidence.provider_certification import canary_evidence_path
from eawf.workflow.release.admission import checkpoint_release_config
from eawf.workflow.release.canary_receipts import (
    dispatch_smoke_gaps,
    load_dispatch_smoke_receipts,
    read_dispatch_checks,
)
from eawf.workflow.release.packet_run import (
    PacketRunReceipt,
    packet_receipts_export_path,
)
from eawf.workflow.release.signal_probes import membership_probe, provider_probe
from eawf.workflow.verify.release_readiness import ReleaseSignalContext

pytestmark = [pytest.mark.integration, pytest.mark.repo_state]

#: This checkout, whose committed evidence is the real 0.7.3 evidence.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

VERSION: Final = "0.7.3"
RELEASE_KEY: Final = f"REL-{VERSION}"

#: The runtime and os_class pairs the 0.7.3 export advertises.
CLAIMS: Final = (("claude-code", "linux"), ("codex", "linux"))

#: The check-document name each runtime's smoke files beside the receipts.
CHECK_NAMES: Final = {
    "claude-code": "dispatch-check-RUN-*.json",
    "codex": "dispatch-check-codex-RUN-*.json",
}


def is_ancestor_of_head(commit: str) -> bool:
    """Return whether *commit* is reachable from this checkout's HEAD.

    Args:
        commit: The full commit id a receipt names.

    Returns:
        ``True`` when git reports *commit* as an ancestor of HEAD; ``False``
        when it is off the line or unknown to this repository.
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def test_the_v073_smoke_backs_every_claimed_pair() -> None:
    receipts = load_dispatch_smoke_receipts(REPO_ROOT, RELEASE_KEY)
    directory = canary_evidence_path(REPO_ROOT, RELEASE_KEY).parent

    assert receipts is not None
    assert receipts.release_key == RELEASE_KEY
    passed = {
        (item.runtime_id, item.os_class): item
        for item in receipts.receipts
        if item.scorecard == "pass"
    }
    assert set(passed) >= set(CLAIMS)
    for runtime_id, os_class in CLAIMS:
        refs = passed[(runtime_id, os_class)].evidence_refs
        assert refs, (runtime_id, os_class)
        for ref in refs:
            name = Path(str(ref)).name
            assert fnmatch.fnmatchcase(name, CHECK_NAMES[runtime_id]), ref
            assert (directory / name).is_file(), ref
    assert dispatch_smoke_gaps(receipts, CLAIMS, read_dispatch_checks(directory, receipts)) == ()


def test_the_v073_packet_runs_pass_on_both_runtimes_and_origins() -> None:
    path = packet_receipts_export_path(REPO_ROOT, RELEASE_KEY)
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
    assert set(latest) == pairs
    for pair in pairs:
        receipt = latest[pair]
        assert receipt.outcome == "pass", pair
        assert receipt.eawf_version == VERSION, pair
        assert receipt.os_class == "linux", pair
        assert not receipt.source_dirty, pair
        assert is_ancestor_of_head(receipt.eawf_commit), pair


def test_a_commit_off_the_line_is_not_an_ancestor() -> None:
    assert not is_ancestor_of_head("0" * 40)


def test_the_v073_membership_and_provider_rows_pass() -> None:
    config = checkpoint_release_config(VERSION, repo_root=REPO_ROOT)
    assert config.release_key == RELEASE_KEY

    provider = provider_probe(
        ReleaseSignalContext(config, ReleaseSignalName.PROVIDER, None), repo_root=REPO_ROOT
    )
    membership = membership_probe(
        ReleaseSignalContext(config, ReleaseSignalName.MEMBERSHIP, None), repo_root=REPO_ROOT
    )

    assert provider.status is ReleaseSignalStatus.PASS, provider.remediation
    assert membership.status is ReleaseSignalStatus.PASS, membership.remediation
