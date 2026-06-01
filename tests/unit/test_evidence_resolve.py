"""Unit tests for :mod:`eawf.workflow.evidence.resolve` (EviBound resolver).

Covers the routing contract (route on ``CriterionEvidenceKind``, NOT
``EvidenceKind``), each of the four deterministic checks (portability,
dense ``[N]`` marker, URN grammar, disk-exists) on both pass and fail
paths, the explicit OQ-R deferred markers (URN -> record dereference,
file:line anchor, and the whole-result jury / attested deferrals), plus
boundary cases (empty ref, anchor stripping).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.spec.common import CriterionEvidenceKind, EvidenceKind
from eawf.workflow.evidence.resolve import (
    DeferredAspect,
    ResolveCheck,
    ResolveResult,
    ResolveStatus,
    resolve,
)


# --------------------------------------------------------------------------- #
# Landmine guard: routing is on CriterionEvidenceKind, never EvidenceKind.
# --------------------------------------------------------------------------- #
def test_resolve_routes_on_criterion_evidence_kind_not_evidence_kind() -> None:
    """The dispatcher's routing key is CriterionEvidenceKind, not EvidenceKind.

    The two enums are disjoint by value: CriterionEvidenceKind is
    {deterministic, jury, attested}; EvidenceKind is {audit, artifact,
    decision, store_record, external_url}. None of the EvidenceKind
    values is a valid resolve() route, so a regression that swapped the
    routing enum would have to accept an EvidenceKind value here — and
    it must NOT.
    """
    criterion_values = set(CriterionEvidenceKind.__args__)  # type: ignore[attr-defined]
    evidence_values = set(EvidenceKind.__args__)  # type: ignore[attr-defined]
    assert criterion_values == {"deterministic", "jury", "attested"}
    assert criterion_values.isdisjoint(evidence_values)

    # Every CriterionEvidenceKind value is accepted by resolve().
    for kind in criterion_values:
        result = resolve("docs/x.md", kind, project_root=Path("/nonexistent-root"))  # type: ignore[arg-type]
        assert result.evidence_kind == kind


def test_resolve_records_routing_kind_on_result() -> None:
    """The result echoes the CriterionEvidenceKind it routed on."""
    result = resolve("urn:eawf:v1:decision:owner/D17", "deterministic", project_root=Path("/"))
    assert result.evidence_kind == "deterministic"
    assert isinstance(result, ResolveResult)


# --------------------------------------------------------------------------- #
# Deterministic: portability check (reuses Citation._ref_must_be_portable).
# --------------------------------------------------------------------------- #
def test_resolve_portability_rejects_absolute_path(tmp_path: Path) -> None:
    """An absolute path fails the portability precheck before any other check."""
    result = resolve("/etc/passwd", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.UNRESOLVED
    assert result.check is ResolveCheck.PORTABILITY
    assert "repo-relative" in result.reason


def test_resolve_portability_rejects_home_relative(tmp_path: Path) -> None:
    """A ``~/`` ref fails portability (the Citation seam's home-dir rule)."""
    result = resolve("~/secret.txt", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.UNRESOLVED
    assert result.check is ResolveCheck.PORTABILITY


def test_resolve_portability_rejects_local_host_url(tmp_path: Path) -> None:
    """A localhost URL fails the portability local-host rule."""
    result = resolve("http://localhost:8080/x", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.UNRESOLVED
    assert result.check is ResolveCheck.PORTABILITY


# --------------------------------------------------------------------------- #
# Deterministic: URN-grammar check (reuses urn.parse; deref deferred OQ-R).
# --------------------------------------------------------------------------- #
def test_resolve_urn_grammar_pass_flags_dereference_deferred(tmp_path: Path) -> None:
    """A well-formed URN resolves on grammar but flags deref as deferred (OQ-R)."""
    result = resolve("urn:eawf:v1:decision:owner/D17", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.RESOLVED
    assert result.check is ResolveCheck.URN_GRAMMAR
    assert DeferredAspect.URN_RECORD_DEREFERENCE in result.deferred_aspects


def test_resolve_urn_grammar_rejects_unknown_kind(tmp_path: Path) -> None:
    """An unknown URN kind fails the grammar check (urn.parse raises)."""
    result = resolve("urn:eawf:v1:boguskind:owner/x", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.UNRESOLVED
    assert result.check is ResolveCheck.URN_GRAMMAR
    assert "kind" in result.reason


def test_resolve_urn_grammar_rejects_bad_version(tmp_path: Path) -> None:
    """An unsupported URN version fails the grammar check."""
    result = resolve("urn:eawf:v2:decision:owner/x", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.UNRESOLVED
    assert result.check is ResolveCheck.URN_GRAMMAR


# --------------------------------------------------------------------------- #
# Deterministic: dense [N] marker check (reuses citation_numbers_in_text).
# --------------------------------------------------------------------------- #
def test_resolve_dense_marker_pass(tmp_path: Path) -> None:
    """Prose carrying a dense ``[N]`` marker resolves on the dense-marker check."""
    result = resolve("see prior work [3] for context", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.RESOLVED
    assert result.check is ResolveCheck.DENSE_MARKER


def test_resolve_dense_marker_image_alt_is_not_a_marker(tmp_path: Path) -> None:
    """``![1]`` image alt-text is not a dense marker; ref falls through to disk-exists.

    Mirrors the Citation seam's ``test_citation_numbers_in_text_ignores_image_alt_text``:
    the ``!`` prefix disqualifies the bracket, so this ref is treated as a
    repo-relative path and (absent on disk) fails the disk-exists check.
    """
    result = resolve("![1] alt only", "deterministic", project_root=tmp_path)
    assert result.check is ResolveCheck.DISK_EXISTS
    assert result.status is ResolveStatus.UNRESOLVED


# --------------------------------------------------------------------------- #
# Deterministic: disk-exists check (reuses Path.is_file under project_root).
# --------------------------------------------------------------------------- #
def test_resolve_disk_exists_pass(tmp_path: Path) -> None:
    """A repo-relative path that exists under project_root resolves."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("hi", encoding="utf-8")
    result = resolve("docs/x.md", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.RESOLVED
    assert result.check is ResolveCheck.DISK_EXISTS
    assert result.deferred_aspects == ()


def test_resolve_disk_exists_missing_file(tmp_path: Path) -> None:
    """A repo-relative path absent on disk is UNRESOLVED, not silently passed."""
    result = resolve("docs/missing.md", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.UNRESOLVED
    assert result.check is ResolveCheck.DISK_EXISTS
    assert "no file" in result.reason


def test_resolve_disk_exists_directory_is_not_a_file(tmp_path: Path) -> None:
    """A directory ref fails disk-exists (is_file, not exists)."""
    (tmp_path / "docs").mkdir()
    result = resolve("docs", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.UNRESOLVED
    assert result.check is ResolveCheck.DISK_EXISTS


def test_resolve_disk_exists_strips_line_anchor_and_flags_it(tmp_path: Path) -> None:
    """A ``path:line`` anchor is stripped for the file check and flagged deferred (OQ-R)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    result = resolve("src/mod.py:42", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.RESOLVED
    assert result.check is ResolveCheck.DISK_EXISTS
    assert DeferredAspect.FILE_LINE_ANCHOR in result.deferred_aspects


def test_resolve_disk_exists_anchor_flagged_even_when_file_missing(tmp_path: Path) -> None:
    """The anchor-deferred flag is attached even on the UNRESOLVED (missing-file) path."""
    result = resolve("src/missing.py:7", "deterministic", project_root=tmp_path)
    assert result.status is ResolveStatus.UNRESOLVED
    assert DeferredAspect.FILE_LINE_ANCHOR in result.deferred_aspects


# --------------------------------------------------------------------------- #
# Non-deterministic criterion flavors are DEFERRED, not silently passed (OQ-R).
# --------------------------------------------------------------------------- #
def test_resolve_jury_is_deferred(tmp_path: Path) -> None:
    """The ``jury`` flavor has no deterministic check this wave -> DEFERRED."""
    result = resolve("anything", "jury", project_root=tmp_path)
    assert result.status is ResolveStatus.DEFERRED
    assert result.check is ResolveCheck.NONE
    assert DeferredAspect.JURY_VOTE in result.deferred_aspects
    assert "deferred" in result.reason.lower()


def test_resolve_attested_is_deferred(tmp_path: Path) -> None:
    """The ``attested`` flavor defers to operator sign-off -> DEFERRED."""
    result = resolve("anything", "attested", project_root=tmp_path)
    assert result.status is ResolveStatus.DEFERRED
    assert result.check is ResolveCheck.NONE
    assert DeferredAspect.OPERATOR_ATTESTATION in result.deferred_aspects


# --------------------------------------------------------------------------- #
# Boundary / error paths.
# --------------------------------------------------------------------------- #
def test_resolve_empty_ref_raises(tmp_path: Path) -> None:
    """An empty ref cannot be routed to any check -> ValueError (fail fast)."""
    with pytest.raises(ValueError, match="non-empty"):
        resolve("", "deterministic", project_root=tmp_path)


def test_resolve_whitespace_ref_raises(tmp_path: Path) -> None:
    """A whitespace-only ref is treated as empty -> ValueError."""
    with pytest.raises(ValueError, match="non-empty"):
        resolve("   ", "jury", project_root=tmp_path)


def test_resolve_result_is_frozen(tmp_path: Path) -> None:
    """ResolveResult is an immutable value object."""
    result = resolve("docs/x.md", "deterministic", project_root=tmp_path)
    with pytest.raises((AttributeError, TypeError)):
        result.status = ResolveStatus.RESOLVED  # type: ignore[misc]


def test_resolve_public_exports() -> None:
    """The package re-exports the resolver surface."""
    from eawf.workflow import evidence

    assert evidence.resolve is resolve
    assert evidence.ResolveResult is ResolveResult
    assert evidence.ResolveStatus is ResolveStatus
    assert evidence.ResolveCheck is ResolveCheck
    assert evidence.DeferredAspect is DeferredAspect
    # The submodule is importable by its dotted path. ``getattr`` on the
    # package object would return the re-exported function (which shadows
    # the submodule attribute), so go through ``importlib`` to grab the
    # module object itself.
    import importlib

    submodule = importlib.import_module("eawf.workflow.evidence.resolve")
    assert submodule.resolve is resolve
