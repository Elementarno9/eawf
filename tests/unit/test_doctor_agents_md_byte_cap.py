"""Tests for the AGENTS.md byte-cap doctor diagnostic.

Two layers are exercised:

- The pure measurement helpers in
  :mod:`eawf.surfaces.render.agents_md` (:func:`block_byte_spans`,
  :func:`measure_agents_md_byte_cap`) — boundary + error paths.
- The blocking doctor check
  :func:`eawf.observability.doctor.checks.check_agents_md_byte_cap` — a
  small doc passes; an over-cap doc fails, naming the dropped render blocks.
- The card this repository renders from its committed ``.ea/rules.yaml``:
  under the cap, with the headroom recorded in the projection manifest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.observability.doctor import checks, checks_steering_cap
from eawf.platform.rules.host_facts import CertifiedCap
from eawf.platform.rules.render import CARD_TARGET, POLICY_TARGET, plan_rule_projections
from eawf.surfaces.render import regions
from eawf.surfaces.render.agents_md import (
    block_byte_spans,
    measure_agents_md_byte_cap,
)
from eawf.surfaces.render.regions import RegionParseError

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _doc_with_blocks(*ids: str, body: str = "line one\nline two") -> str:
    """Build an AGENTS.md-shaped doc with one managed region per id.

    Each block is emitted through :func:`regions.replace_region` so the BEGIN
    marker carries a correctly computed hash and the document parses cleanly.
    """
    text = ""
    for block_id in ids:
        text = regions.replace_region(text, id=block_id, version="1.0", body=body)
    return text


# ---- pure helpers: measure_agents_md_byte_cap / block_byte_spans -----------


def test_measure_byte_cap_empty_doc() -> None:
    """An empty document is zero bytes with no blocks past any positive cap."""
    report = measure_agents_md_byte_cap("", cap=100)
    assert report.total_bytes == 0
    assert report.dropped_block_ids == []
    assert not report.over_cap


def test_measure_byte_cap_plain_text_no_regions() -> None:
    """Text with no managed regions measures its byte total and drops nothing."""
    text = "just prose, no markers\n"
    report = measure_agents_md_byte_cap(text, cap=8)
    assert report.total_bytes == len(text.encode("utf-8"))
    assert report.dropped_block_ids == []
    # Over the cap by total bytes even though no *block* is dropped.
    assert report.over_cap


def test_measure_byte_cap_names_blocks_past_cut() -> None:
    """The report names exactly the blocks whose BEGIN starts at/after the cap."""
    doc = _doc_with_blocks("alpha", "beta", "gamma")
    spans = block_byte_spans(doc)
    assert [s.id for s in spans] == ["alpha", "beta", "gamma"]

    # Cap that lands exactly on the last block's start byte: gamma is dropped
    # (start_byte >= cap), alpha/beta are not.
    cap = spans[-1].start_byte
    report = measure_agents_md_byte_cap(doc, cap=cap)
    assert report.dropped_block_ids == ["gamma"]
    assert report.over_cap


def test_measure_byte_cap_boundary_off_by_one() -> None:
    """A block starting one byte before the cap survives; at the cap it drops."""
    doc = _doc_with_blocks("head", "tail")
    tail_start = block_byte_spans(doc)[-1].start_byte

    just_under = measure_agents_md_byte_cap(doc, cap=tail_start + 1)
    assert just_under.dropped_block_ids == []

    at_boundary = measure_agents_md_byte_cap(doc, cap=tail_start)
    assert at_boundary.dropped_block_ids == ["tail"]


def test_measure_byte_cap_all_blocks_fit_under_generous_cap() -> None:
    """A cap above the whole document drops nothing and is not over-cap."""
    doc = _doc_with_blocks("only")
    report = measure_agents_md_byte_cap(doc, cap=1_000_000)
    assert report.dropped_block_ids == []
    assert not report.over_cap


def test_block_byte_spans_counts_utf8_bytes_not_chars() -> None:
    """Byte offsets exceed char offsets when multibyte prose precedes a block.

    A caller comparing block positions against a *byte* cap must not use the
    character offsets that :func:`regions.find_regions` reports directly.
    """
    prefix = "café中\n"  # é = 2 bytes, 中 = 3 bytes; longer in bytes than chars
    doc = prefix + _doc_with_blocks("x")
    span = block_byte_spans(doc)[0]
    assert span.start_byte == len(prefix.encode("utf-8"))
    assert span.start_byte > len(prefix)  # byte offset outruns the char offset


def test_measure_byte_cap_rejects_nonpositive_cap() -> None:
    """A non-positive cap fails fast with ValueError (the API contract)."""
    with pytest.raises(ValueError, match="cap must be positive"):
        measure_agents_md_byte_cap("x", cap=0)
    with pytest.raises(ValueError, match="cap must be positive"):
        measure_agents_md_byte_cap("x", cap=-5)


def test_measure_byte_cap_propagates_region_parse_error() -> None:
    """A malformed marker block surfaces as RegionParseError, not silence."""
    broken = "<!-- BEGIN EAWF:managed id=x version=1.0 hash=0123456789abcdef -->\nbody\n"
    with pytest.raises(RegionParseError):
        measure_agents_md_byte_cap(broken, cap=100)


# ---- doctor check: check_agents_md_byte_cap --------------------------------


def test_codex_project_doc_byte_cap_is_the_measured_boundary() -> None:
    """The cap is Codex's measured truncation point, not a padded budget.

    A probe run had its last received rule end mid-sentence exactly at byte
    32768; a control run that raised only the cap received the complete final
    rule. Pinning the constant here keeps a future "give it some headroom"
    edit from silently reintroducing a cap the consumer does not honour.
    """
    assert checks.CODEX_PROJECT_DOC_BYTE_CAP == 32768


def test_check_under_cap_passes(tmp_path: Path) -> None:
    """A small on-disk AGENTS.md is under the default cap -> ok."""
    doc = _doc_with_blocks("small")
    (tmp_path / "AGENTS.md").write_text(doc, encoding="utf-8")

    result = checks.check_agents_md_byte_cap(workspace=tmp_path)
    assert result.name == "agents_md_byte_cap"
    assert result.status == "ok"
    assert "within" in (result.detail or "")


def test_check_over_cap_fails_naming_dropped_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-cap AGENTS.md yields a blocking fail naming the dropped blocks."""
    doc = _doc_with_blocks("keep", "cut_one", "cut_two")
    (tmp_path / "AGENTS.md").write_text(doc, encoding="utf-8")

    # Pin the cap onto the first block that must fall past the cut, so the two
    # trailing blocks are dropped and the leading one survives.
    spans = block_byte_spans(doc)
    cut_start = next(s.start_byte for s in spans if s.id == "cut_one")
    monkeypatch.setattr(checks_steering_cap, "CODEX_PROJECT_DOC_BYTE_CAP", cut_start)

    result = checks.check_agents_md_byte_cap(workspace=tmp_path)
    assert result.status == "fail"
    detail = result.detail or ""
    assert "cut_one" in detail
    assert "cut_two" in detail
    assert "keep" not in detail


def test_check_no_agents_md_is_ok(tmp_path: Path) -> None:
    """No AGENTS.md at the anchor -> ok (nothing to measure)."""
    result = checks.check_agents_md_byte_cap(workspace=tmp_path)
    assert result.status == "ok"
    assert "no AGENTS.md" in (result.detail or "")


def test_check_no_anchor_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable workspace anchor -> ok, never a spurious fail."""
    monkeypatch.setattr(checks_steering_cap, "_resolve_anchor", lambda _ws: None)
    result = checks.check_agents_md_byte_cap(workspace=None)
    assert result.status == "ok"
    assert "no workspace anchor" in (result.detail or "")


def test_check_malformed_markers_over_cap_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken marker cannot name blocks, but the over-cap verdict stands."""
    broken = "<!-- BEGIN EAWF:managed id=x version=1.0 hash=0123456789abcdef -->\nbody\n"
    (tmp_path / "AGENTS.md").write_text(broken, encoding="utf-8")
    monkeypatch.setattr(checks_steering_cap, "CODEX_PROJECT_DOC_BYTE_CAP", 10)

    result = checks.check_agents_md_byte_cap(workspace=tmp_path)
    assert result.status == "fail"
    assert "malformed markers" in (result.detail or "")


# ---- the card rendered from the committed rule source ----------------------


def test_rule_projection_cap_is_the_doctor_cap() -> None:
    """The render refuses at the same boundary the doctor check measures."""
    plan = plan_rule_projections(_REPO_ROOT)
    caps = {record.cap_bytes for record in plan.manifest.projections}
    assert caps == {checks.CODEX_PROJECT_DOC_BYTE_CAP}


def test_check_under_cap_labels_a_raised_codex_cap_non_portable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap this machine raised keeps the verdict but labels the fit non-portable."""
    (tmp_path / "AGENTS.md").write_text(_doc_with_blocks("small"), encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("project_doc_max_bytes = 65536\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    result = checks.check_agents_md_byte_cap(workspace=tmp_path)
    assert result.status == "ok"
    assert "non-portable: codex project_document_cap_bytes configured 65536" in (
        result.detail or ""
    )


def test_rendered_card_from_committed_rules_fits_the_cap_with_recorded_headroom() -> None:
    """The committed rule source renders a card and a policy file under the cap."""
    plan = plan_rule_projections(_REPO_ROOT)
    records = {record.target: record for record in plan.manifest.projections}
    card = records[CARD_TARGET]
    assert card.byte_count < checks.CODEX_PROJECT_DOC_BYTE_CAP
    assert card.cap_bytes == checks.CODEX_PROJECT_DOC_BYTE_CAP
    assert card.headroom_bytes == card.cap_bytes - card.byte_count > 0
    assert records[POLICY_TARGET].headroom_bytes > 0


def test_committed_agents_md_is_the_rendered_card() -> None:
    """The committed AGENTS.md is byte-identical to the card the graph renders."""
    plan = plan_rule_projections(_REPO_ROOT)
    card = next(p for p in plan.projections if p.record.target == CARD_TARGET)
    assert (_REPO_ROOT / CARD_TARGET).read_text(encoding="utf-8") == card.text, (
        "AGENTS.md drifted from .ea/rules.yaml; re-render it with eawf sync"
    )
    result = checks.check_agents_md_byte_cap(workspace=_REPO_ROOT)
    assert result.status == "ok"


# ---- doctor check: check_agents_override_byte_cap --------------------------


def test_policy_check_under_cap_passes(tmp_path: Path) -> None:
    """A small on-disk AGENTS.override.md is under the certified cap -> ok."""
    doc = _doc_with_blocks("small")
    (tmp_path / "AGENTS.override.md").write_text(doc, encoding="utf-8")

    result = checks.check_agents_override_byte_cap(workspace=tmp_path)
    assert result.name == "agents_override_byte_cap"
    assert result.status == "ok"
    assert "within" in (result.detail or "")


def test_policy_check_over_cap_fails_naming_dropped_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-cap policy file yields a blocking fail naming the dropped blocks."""
    doc = _doc_with_blocks("keep", "cut_one", "cut_two")
    (tmp_path / "AGENTS.override.md").write_text(doc, encoding="utf-8")

    spans = block_byte_spans(doc)
    cut_start = next(s.start_byte for s in spans if s.id == "cut_one")
    cap = CertifiedCap(runtime="codex", cap_bytes=cut_start, readers=("codex",), uncertified=())
    monkeypatch.setattr(checks_steering_cap, "smallest_certified_cap", lambda *_a, **_k: cap)

    result = checks.check_agents_override_byte_cap(workspace=tmp_path)
    assert result.status == "fail"
    detail = result.detail or ""
    assert "cut_one" in detail
    assert "cut_two" in detail
    assert "keep" not in detail


def test_policy_check_no_agents_override_is_ok(tmp_path: Path) -> None:
    """No AGENTS.override.md at the anchor -> ok (nothing to measure)."""
    result = checks.check_agents_override_byte_cap(workspace=tmp_path)
    assert result.status == "ok"
    assert "no AGENTS.override.md" in (result.detail or "")


def test_policy_check_no_anchor_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable workspace anchor -> ok, never a spurious fail."""
    monkeypatch.setattr(checks_steering_cap, "_resolve_anchor", lambda _ws: None)
    result = checks.check_agents_override_byte_cap(workspace=None)
    assert result.status == "ok"
    assert "no workspace anchor" in (result.detail or "")


def test_policy_check_no_certified_reader_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No reader of the policy file has a certified cap -> ok, not a fail.

    Unreachable today (Codex always certifies the ``"policy"`` cap), but the
    render transaction already refuses to publish in that case, so the
    doctor check must degrade rather than crash.
    """
    (tmp_path / "AGENTS.override.md").write_text(_doc_with_blocks("small"), encoding="utf-8")
    monkeypatch.setattr(checks_steering_cap, "smallest_certified_cap", lambda *_a, **_k: None)

    result = checks.check_agents_override_byte_cap(workspace=tmp_path)
    assert result.status == "ok"
    assert "no certified cap" in (result.detail or "")


def test_policy_check_malformed_markers_over_cap_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken marker cannot name blocks, but the over-cap verdict stands."""
    broken = "<!-- BEGIN EAWF:managed id=x version=1.0 hash=0123456789abcdef -->\nbody\n"
    (tmp_path / "AGENTS.override.md").write_text(broken, encoding="utf-8")
    cap = CertifiedCap(runtime="codex", cap_bytes=10, readers=("codex",), uncertified=())
    monkeypatch.setattr(checks_steering_cap, "smallest_certified_cap", lambda *_a, **_k: cap)

    result = checks.check_agents_override_byte_cap(workspace=tmp_path)
    assert result.status == "fail"
    assert "malformed markers" in (result.detail or "")


def test_policy_projection_cap_is_certified_for_its_own_readers() -> None:
    """The policy projection's cap resolves independently of the card's."""
    from eawf.platform.rules.host_facts import load_host_facts, smallest_certified_cap

    cap = smallest_certified_cap(load_host_facts(), "policy")
    assert cap is not None
    # Today both projections' smallest certified reader is Codex, so the
    # numbers agree; the resolution path (readers of "policy") is distinct
    # from the card's, and would diverge the moment another reader certifies.
    assert cap.cap_bytes == checks.CODEX_PROJECT_DOC_BYTE_CAP
