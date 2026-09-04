"""Two clean builds, one epoch, and what a mutated file does to the receipt.

The claim under test is the strong one: building the same source twice
produces byte-identical artifacts. Asserting it needs a builder that can
actually be made to diverge, so these cases drive a real deterministic
archive writer over a real scratch tree rather than a stub that returns
canned digests -- a stub would agree with itself no matter what the
source did, which is precisely the failure the ``artifacts`` signal
exists to catch.

``uv build`` is deliberately not the builder here. It resolves a build
backend over the network and takes tens of seconds per invocation, so
using it would trade a fast, hermetic proof of the comparison logic for
a slow, flaky proof of the same thing. The production default stays
:func:`~eawf.workflow.release.reproducibility.uv_build`; what is pinned
here is everything the receipt does with whatever the builder produced.
"""

from __future__ import annotations

import gzip
import tarfile
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.release.signals import (
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.release.producers import build_receipt_probes
from eawf.workflow.release.receipts import write_receipt
from eawf.workflow.release.reproducibility import (
    BUILD_ATTEMPTS,
    SOURCE_DATE_EPOCH_ENV,
    ArtifactDigest,
    ArtifactKind,
    ReproducibilityFinding,
    ReproducibleBuildReceipt,
    artifacts_component,
    classify_artifact,
    compare_builds,
    digest_build_output,
    double_build_receipt,
    source_date_epoch,
)
from eawf.workflow.verify.release_readiness import compute_readiness
from tests.unit.kernel.release.conftest import dev1_config

pytestmark = pytest.mark.integration

#: Repository root, four directories above ``tests/integration/...``.
REPO_ROOT = Path(__file__).resolve().parents[4]

SOURCE_SHA = "c" * 40

#: A fixed epoch: 2026-09-04T12:00:00Z. Any constant works, which is the
#: point -- what must not vary is that both builds see the same one.
EPOCH = 1788523200

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

WHEEL_NAME = "demo-0.1.0-py3-none-any.whl"
SDIST_NAME = "demo-0.1.0.tar.gz"


def _scratch_tree(root: Path) -> Path:
    """Write a two-file source tree under *root* and return it."""
    tree = root / "src"
    tree.mkdir(parents=True)
    (tree / "__init__.py").write_text("VERSION = '0.1.0'\n", encoding="utf-8")
    (tree / "core.py").write_text("def run() -> int:\n    return 0\n", encoding="utf-8")
    return tree


def _write_wheel(tree: Path, out_dir: Path, epoch: int) -> None:
    """Write a zip whose member order and mtimes come only from *tree* and *epoch*."""
    stamp = datetime.fromtimestamp(epoch, tz=UTC).timetuple()[:6]
    with zipfile.ZipFile(out_dir / WHEEL_NAME, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(tree.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(path.relative_to(tree).as_posix(), date_time=stamp)
                info.external_attr = 0o644 << 16
                archive.writestr(info, path.read_bytes())


def _write_sdist(tree: Path, out_dir: Path, epoch: int) -> None:
    """Write a gzipped tar whose every timestamp is pinned to *epoch*."""
    with (
        (out_dir / SDIST_NAME).open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for path in sorted(tree.rglob("*")):
            if not path.is_file():
                continue
            info = tarfile.TarInfo(path.relative_to(tree).as_posix())
            info.size = path.stat().st_size
            info.mtime = epoch
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                archive.addfile(info, handle)


def _builder(tree: Path, *, mutate_before: int | None = None) -> Callable[[Path, Path, int], None]:
    """Return a deterministic builder, optionally mutating before one attempt.

    Args:
        tree: Source tree both builds read.
        mutate_before: 1-based attempt index to rewrite one source file
            just before, or ``None`` to leave the tree alone.

    Returns:
        A builder honouring the reproducibility contract.
    """
    attempts = {"count": 0}

    def build(repo_root: Path, out_dir: Path, epoch: int) -> None:
        attempts["count"] += 1
        if mutate_before == attempts["count"]:
            (tree / "core.py").write_text("def run() -> int:\n    return 1\n", encoding="utf-8")
        _write_wheel(tree, out_dir, epoch)
        _write_sdist(tree, out_dir, epoch)

    return build


# --- the double build --------------------------------------------------------


def test_two_clean_builds_of_one_commit_reproduce(tmp_path: Path) -> None:
    """Same source, same epoch, both artifacts byte-identical."""
    tree = _scratch_tree(tmp_path)
    receipt = double_build_receipt(
        tmp_path,
        source_sha=SOURCE_SHA,
        epoch=EPOCH,
        out_root=tmp_path / "out",
        builder=_builder(tree),
    )
    assert receipt.reproduced is True
    assert receipt.divergent_artifacts == ()
    assert receipt.findings == ()
    assert len(receipt.attempts) == BUILD_ATTEMPTS
    assert [artifact.filename for artifact in receipt.attempts[0].artifacts] == [
        WHEEL_NAME,
        SDIST_NAME,
    ]
    assert {artifact.kind for artifact in receipt.attempts[0].artifacts} == {
        ArtifactKind.SDIST,
        ArtifactKind.WHEEL,
    }
    assert receipt.attempts[0].artifacts == receipt.attempts[1].artifacts
    assert receipt.source_date_epoch == EPOCH


def test_a_mutated_file_makes_the_second_build_diverge(tmp_path: Path) -> None:
    """One rewritten source file names both artifacts in the receipt."""
    tree = _scratch_tree(tmp_path)
    receipt = double_build_receipt(
        tmp_path,
        source_sha=SOURCE_SHA,
        epoch=EPOCH,
        out_root=tmp_path / "out",
        builder=_builder(tree, mutate_before=2),
    )
    assert receipt.reproduced is False
    assert ReproducibilityFinding.DIGEST_DIVERGED in receipt.findings
    assert WHEEL_NAME in receipt.divergent_artifacts
    assert SDIST_NAME in receipt.divergent_artifacts


def test_the_artifacts_signal_fails_naming_the_divergent_artifact(tmp_path: Path) -> None:
    """``artifact_nonreproducible`` names the file, not just the fact."""
    tree = _scratch_tree(tmp_path)
    receipt = double_build_receipt(
        tmp_path,
        source_sha=SOURCE_SHA,
        epoch=EPOCH,
        out_root=tmp_path / "out",
        builder=_builder(tree, mutate_before=2),
    )
    outcome = artifacts_component(receipt)
    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.failure_code is ReleaseSignalFailureCode.ARTIFACT_NONREPRODUCIBLE
    assert WHEEL_NAME in outcome.remediation
    assert f"artifact_nonreproducible:{WHEEL_NAME}" in outcome.evidence_refs


def test_the_artifacts_signal_passes_a_reproduced_receipt(tmp_path: Path) -> None:
    """A green receipt carries every published digest as its evidence."""
    tree = _scratch_tree(tmp_path)
    receipt = double_build_receipt(
        tmp_path,
        source_sha=SOURCE_SHA,
        epoch=EPOCH,
        out_root=tmp_path / "out",
        builder=_builder(tree),
    )
    outcome = artifacts_component(receipt)
    assert outcome.status is ReleaseSignalStatus.PASS
    assert f"source_date_epoch:{EPOCH}" in outcome.evidence_refs
    assert any(ref.startswith(f"artifact:{WHEEL_NAME}:") for ref in outcome.evidence_refs)


def test_a_receipt_without_the_pre_upload_recomputation_is_unavailable(tmp_path: Path) -> None:
    """Two agreeing builds say nothing about the files that get uploaded."""
    tree = _scratch_tree(tmp_path)
    receipt = double_build_receipt(
        tmp_path,
        source_sha=SOURCE_SHA,
        epoch=EPOCH,
        out_root=tmp_path / "out",
        builder=_builder(tree),
        recompute_before_upload=False,
    )
    outcome = artifacts_component(receipt, pre_upload_ran=False)
    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert "before upload" in outcome.remediation


def test_a_file_touched_before_upload_reds_the_receipt(tmp_path: Path) -> None:
    """The pre-upload reading is what covers the artifacts actually published."""
    tree = _scratch_tree(tmp_path)
    out_root = tmp_path / "out"
    out_root.mkdir()
    attempts = []
    build = _builder(tree)
    for index in (1, 2):
        out_dir = out_root / f"build-{index}"
        out_dir.mkdir()
        build(tmp_path, out_dir, EPOCH)
        attempts.append(digest_build_output(out_dir, attempt=index, source_date_epoch=EPOCH))
    tampered = ArtifactDigest(filename=WHEEL_NAME, kind=ArtifactKind.WHEEL, sha256="d" * 64)
    receipt = compare_builds(
        attempts,
        source_sha=SOURCE_SHA,
        pre_upload=(tampered, attempts[1].artifacts[1]),
    )
    assert receipt.reproduced is False
    assert receipt.findings == (ReproducibilityFinding.UPLOAD_DRIFT,)
    assert receipt.divergent_artifacts == (WHEEL_NAME,)


# --- boundary and error paths ------------------------------------------------


def test_compare_builds_rejects_a_single_attempt(tmp_path: Path) -> None:
    """One build is not a comparison, so it cannot make a receipt."""
    tree = _scratch_tree(tmp_path)
    out_dir = tmp_path / "solo"
    out_dir.mkdir()
    _builder(tree)(tmp_path, out_dir, EPOCH)
    only = digest_build_output(out_dir, attempt=1, source_date_epoch=EPOCH)
    with pytest.raises(ValueError, match="exactly 2 attempts"):
        compare_builds([only], source_sha=SOURCE_SHA)


def test_digest_build_output_rejects_an_empty_output_directory(tmp_path: Path) -> None:
    """A build that produced nothing must not read as a reproducible one."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="produced no wheel or sdist"):
        digest_build_output(empty, attempt=1, source_date_epoch=EPOCH)


def test_digest_build_output_ignores_a_stray_non_distribution_file(tmp_path: Path) -> None:
    """A checksum listing beside the wheel must not enter the receipt."""
    tree = _scratch_tree(tmp_path)
    out_dir = tmp_path / "out1"
    out_dir.mkdir()
    _builder(tree)(tmp_path, out_dir, EPOCH)
    (out_dir / "SHA256SUMS.txt").write_text("ignored\n", encoding="utf-8")
    attempt = digest_build_output(out_dir, attempt=1, source_date_epoch=EPOCH)
    assert [artifact.filename for artifact in attempt.artifacts] == [WHEEL_NAME, SDIST_NAME]
    assert classify_artifact(out_dir / "SHA256SUMS.txt") is None


def test_double_build_refuses_to_reuse_an_output_directory(tmp_path: Path) -> None:
    """A second build finding the first one's artifacts would always agree."""
    tree = _scratch_tree(tmp_path)
    out_root = tmp_path / "out"
    (out_root / "build-1").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        double_build_receipt(
            tmp_path,
            source_sha=SOURCE_SHA,
            epoch=EPOCH,
            out_root=out_root,
            builder=_builder(tree),
        )


def test_artifacts_component_rejects_an_untyped_receipt() -> None:
    """Handing the component a mapping is a caller bug, not a pass."""
    with pytest.raises(TypeError, match="ReproducibleBuildReceipt"):
        artifacts_component({"reproduced": True})  # type: ignore[arg-type]


def test_a_receipt_cannot_claim_a_verdict_its_rows_contradict(tmp_path: Path) -> None:
    """``reproduced=True`` beside a divergence is a lie the record refuses."""
    tree = _scratch_tree(tmp_path)
    receipt = double_build_receipt(
        tmp_path,
        source_sha=SOURCE_SHA,
        epoch=EPOCH,
        out_root=tmp_path / "out",
        builder=_builder(tree),
    )
    with pytest.raises(ValueError, match="claims reproduced"):
        ReproducibleBuildReceipt(
            source_sha=SOURCE_SHA,
            source_date_epoch=EPOCH,
            attempts=receipt.attempts,
            divergent_artifacts=(WHEEL_NAME,),
            reproduced=True,
        )


def test_source_date_epoch_reads_the_commit_not_the_wall_clock() -> None:
    """The epoch is a property of the source, so it is read from the commit."""
    epoch = source_date_epoch(REPO_ROOT, "HEAD")
    assert epoch > 0
    assert SOURCE_DATE_EPOCH_ENV == "SOURCE_DATE_EPOCH"


def test_source_date_epoch_rejects_an_unresolvable_commit() -> None:
    """A commit git cannot resolve must not silently become epoch zero."""
    with pytest.raises(ValueError, match="cannot read the timestamp"):
        source_date_epoch(REPO_ROOT, "f" * 40)


# --- wiring through to the gate row ------------------------------------------


def test_a_divergent_receipt_reds_the_artifact_reproducibility_gate(tmp_path: Path) -> None:
    """The receipt reaches the gate row, not just the component function."""
    tree = _scratch_tree(tmp_path)
    receipt = double_build_receipt(
        tmp_path,
        source_sha=SOURCE_SHA,
        epoch=EPOCH,
        out_root=tmp_path / "out",
        builder=_builder(tree, mutate_before=2),
    )
    write_receipt(tmp_path, "reproducible-build-receipt", receipt)
    readiness = compute_readiness(
        dev1_config(),
        probes=build_receipt_probes(tmp_path),
        computed_at=NOW,
    )
    row = readiness.gate_row(ReleaseGateName.ARTIFACT_REPRODUCIBILITY)
    assert row.evidence_ref == "artifacts"
    assert row.status is ReleaseSignalStatus.FAIL
    assert WHEEL_NAME in row.remediation
    assert readiness.row(ReleaseSignalName.ARTIFACTS).failure_code is (
        ReleaseSignalFailureCode.ARTIFACT_NONREPRODUCIBLE
    )
    assert readiness.ready is False


def test_a_reproduced_receipt_greens_the_artifact_reproducibility_gate(tmp_path: Path) -> None:
    """The gate passes only on a receipt from two agreeing clean builds."""
    tree = _scratch_tree(tmp_path)
    receipt = double_build_receipt(
        tmp_path,
        source_sha=SOURCE_SHA,
        epoch=EPOCH,
        out_root=tmp_path / "out",
        builder=_builder(tree),
    )
    write_receipt(tmp_path, "reproducible-build-receipt", receipt)
    readiness = compute_readiness(
        dev1_config(),
        probes=build_receipt_probes(tmp_path),
        computed_at=NOW,
    )
    assert readiness.gate_row(ReleaseGateName.ARTIFACT_REPRODUCIBILITY).status is (
        ReleaseSignalStatus.PASS
    )
