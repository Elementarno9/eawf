"""Tests for :func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`.

A band-scoped ``verify`` block narrows to advisory for a mechanical wave
outside the bands, and stays enforcing for a wave whose verdict is always
required.
"""

from __future__ import annotations

from eawf.kernel.state.models import Wave
from eawf.platform.profiles.models import VerifyBlock
from eawf.workflow.verify import readiness as readiness_mod

# ---- resolve_wave_verify_block: verdict-always band narrowing ----------


def _band_scoped_block() -> VerifyBlock:
    """The shipped band-scoped intent: enforce + jury on at the fleet level.

    Mirrors the fixture in ``tests/platform/profiles/test_verify_block.py``:
    a non-empty ``uiux_bands`` records the band-scoped enforcement intent that
    :func:`~eawf.workflow.verify.readiness.resolve_wave_verify_block` narrows
    per wave.
    """
    return VerifyBlock(
        enforce=True,
        cross_vendor_jury=True,
        uiux_bands=["tui", "render"],
        jury_vendors=["claude", "codex", "opencode"],
    )


def _non_band_wave(
    *,
    wave_id: str,
    title: str,
    effort_bucket: str | None = None,
) -> Wave:
    """Build a minimal claimed, gateless, non-band :class:`Wave`.

    The wave carries a backend ``file_scopes`` (no UI surface) and a
    band-free ``title`` so :func:`wave_in_uiux_band` misses; it declares no
    gates so :func:`classify_risk_tier` returns ``MECH`` (the gate-risk arm
    misses too). The only lever that varies is *effort_bucket*: an ``XL``
    bucket makes :func:`verdict_requirement` return ``"always"`` while an
    omitted (``None``) bucket leaves the wave mechanical.
    """
    payload: dict[str, object] = {
        "id": wave_id,
        "iter_id": "P30-I23",
        "title": title,
        "status": "claimed",
        "file_scopes": ["src/eawf/kernel/spec/wave.py"],
        "gates": [],
        "success_criteria": [
            {
                "id": "CR-01",
                "text": "backend rollup criterion",
                "kind": "legacy",
                "acceptance_style": "binary",
                "evidence_kind": "attested",
                "quality_dimension": "functional_suitability",
                "measurable_signal": "grandfathered legacy criterion",
            }
        ],
        "opened_at": "2026-06-12T00:00:00Z",
        "claimed_at": "2026-06-12T00:00:00Z",
    }
    if effort_bucket is not None:
        payload["effort_bucket"] = effort_bucket
    return Wave.model_validate(payload)


def test_resolve_wave_verify_block_verdict_always_wave_never_narrows() -> None:
    """CR-01: a non-band, gateless, verdict-always wave keeps enforce=True.

    The wave misses the band arm (backend file_scope, band-free title) and the
    gate-risk arm (no gates -> ``MECH``); its ``XL`` effort bucket makes
    :func:`~eawf.workflow.dispatch.verdict.verdict_requirement` return
    ``"always"``, so the new third preservation arm holds the block
    un-narrowed rather than de-scoping the mandatory fresh-auditor verdict.
    """
    wave = _non_band_wave(
        wave_id="P30-I23-W12",
        title="backend rollup",
        effort_bucket="XL",
    )
    resolved = readiness_mod.resolve_wave_verify_block(_band_scoped_block(), wave)
    assert resolved is not None
    assert resolved.enforce is True
    assert resolved.cross_vendor_jury is True


def test_resolve_wave_verify_block_mechanical_wave_still_narrows() -> None:
    """CR-02: a non-band mechanical (non-verdict-always) wave narrows to enforce=False.

    Same shape as the verdict-always case but with no forcing effort bucket,
    judgment role, gate, or security keyword, so
    :func:`~eawf.workflow.dispatch.verdict.verdict_requirement` returns
    ``"sampled"`` / ``"skip"`` (never ``"always"``). The low-risk band
    narrowing is preserved: only verdict-always waves are held un-narrowed.
    """
    wave = _non_band_wave(
        wave_id="P30-I23-W98",
        title="backend rollup",
    )
    resolved = readiness_mod.resolve_wave_verify_block(_band_scoped_block(), wave)
    assert resolved is not None
    assert resolved.enforce is False
    assert resolved.cross_vendor_jury is False
