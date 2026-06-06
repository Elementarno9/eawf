"""Tests for the profile-fed verify spine schema (P28-I01-W10).

Pins the W10 success criteria:

* :class:`AuditCadence` widened to 5 values (``"ship"`` added).
* :class:`VerifyBlock` + :class:`FloorCheck` accept the documented
  shape and reject unknown fields (``extra="forbid"`` per AGENTS
  rule 2).
* The 8 built-in gate-pack profiles parse via the layered loader and
  each carries a non-empty floor pack.
* ``waiver_mode`` defaults to ``"B"`` on a freshly-constructed
  :class:`VerifyBlock`.
* :func:`resolve_waiver_mode` reads from a typed :class:`VerifyBlock`
  AND the legacy merged-config dict.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.audit import AUDIT_CADENCE_VALUES
from eawf.kernel.state.models import Wave
from eawf.platform.profiles import FloorCheck, ProfileBody, VerifyBlock, list_profiles, load_profile
from eawf.workflow.lifecycle.waivers import DEFAULT_WAIVER_MODE, resolve_waiver_mode
from eawf.workflow.verify.compile import compile_floor_pack
from eawf.workflow.verify.readiness import _merge_verify_blocks, resolve_wave_verify_block

# ---- AuditCadence 5-value enum ---------------------------------------------


def test_audit_cadence_values_carry_ship() -> None:
    """The 5-value :data:`AUDIT_CADENCE_VALUES` set carries the W10 ``ship`` entry."""
    assert (
        frozenset({"every-wave", "every-iter", "every-phase", "ship", "manual"})
        == AUDIT_CADENCE_VALUES
    )


def test_audit_cadence_is_5_value_literal() -> None:
    """:data:`AuditCadence` accepts the W10 ``"ship"`` member without raising."""
    # Build a FloorCheck whose cadence is the new value — Pydantic
    # narrows the Literal at construction so this is the load-time
    # contract pin.
    check = FloorCheck(
        name="ship-gate",
        cmd=["uv", "run", "pytest", "-q"],
        scope="all",
        cadence="ship",
        policy="warn",
    )
    assert check.cadence == "ship"
    # The Literal also accepts the legacy 4.
    for legacy in ("every-wave", "every-iter", "every-phase", "manual"):
        FloorCheck(
            name=f"c-{legacy}",
            cmd=["uv", "run", "pytest", "-q"],
            scope="all",
            cadence=legacy,  # type: ignore[arg-type]
            policy="warn",
        )


# ---- VerifyBlock + FloorCheck shape ---------------------------------------


def test_verify_block_defaults() -> None:
    """An empty :class:`VerifyBlock` carries every default the W10 contract pins."""
    block = VerifyBlock()
    assert block.floor_checks == []
    assert block.argv_allowlist == []
    assert block.timeout_class_seconds is None
    assert block.waiver_mode == "B"


def test_verify_block_enforce_defaults_to_false() -> None:
    """The advisory->gating flip defaults OFF: a fresh block stays advisory.

    Every shipped profile inherits this default, so close readiness is
    advisory-only until a profile explicitly opts into gating.
    """
    assert VerifyBlock().enforce is False


def test_verify_block_enforce_round_trips_true() -> None:
    """A profile leaf can opt into gating by setting ``enforce: true``."""
    block = VerifyBlock.model_validate({"enforce": True})
    assert block.enforce is True


def test_verify_block_rejects_invalid_enforce() -> None:
    """``enforce`` is a strict bool — a non-bool value raises.

    Pydantic v2 will not coerce an arbitrary string into a bool, so a
    typo in the profile YAML (``enforce: maybe``) fails the load
    boundary instead of silently defaulting one way or the other.
    """
    with pytest.raises(ValidationError):
        VerifyBlock.model_validate({"enforce": "not-a-bool"})


def test_verify_block_rejects_unknown_field() -> None:
    """Strict Pydantic v2 — extra keys raise."""
    with pytest.raises(ValidationError):
        VerifyBlock.model_validate(
            {
                "floor_checks": [],
                "phantom_field": True,
            }
        )


def test_floor_check_rejects_unknown_field() -> None:
    """Strict Pydantic v2 — extra keys raise on :class:`FloorCheck` too."""
    with pytest.raises(ValidationError):
        FloorCheck.model_validate(
            {
                "name": "x",
                "cmd": ["uv", "run", "pytest"],
                "scope": "changed",
                "cadence": "every-wave",
                "policy": "warn",
                "phantom_field": True,
            }
        )


def test_floor_check_carries_hil_escape_hatches() -> None:
    """The HIL-shaped fields default off and accept overrides."""
    default = FloorCheck(
        name="d",
        cmd=["uv", "run", "pytest"],
        scope="all",
        cadence="every-wave",
        policy="warn",
    )
    assert default.requires_gpu is False
    assert default.runs_outside_jail is False
    assert default.timeout_class == "standard"

    hil = FloorCheck(
        name="hil",
        cmd=["uv", "run", "pytest"],
        scope="all",
        cadence="every-phase",
        policy="warn",
        requires_gpu=True,
        runs_outside_jail=True,
        timeout_class="very_slow",
    )
    assert hil.requires_gpu is True
    assert hil.runs_outside_jail is True
    assert hil.timeout_class == "very_slow"


def test_floor_check_rejects_empty_cmd() -> None:
    """``cmd`` MUST be non-empty per Field(min_length=1)."""
    with pytest.raises(ValidationError):
        FloorCheck(
            name="x",
            cmd=[],
            scope="all",
            cadence="every-wave",
            policy="warn",
        )


def test_floor_check_rejects_invalid_cadence() -> None:
    """``cadence`` is the closed 5-value AuditCadence Literal."""
    with pytest.raises(ValidationError):
        FloorCheck.model_validate(
            {
                "name": "x",
                "cmd": ["uv", "run", "pytest"],
                "scope": "all",
                "cadence": "bogus",
                "policy": "warn",
            }
        )


def test_floor_check_rejects_invalid_policy() -> None:
    """``policy`` is the closed 3-value Literal."""
    with pytest.raises(ValidationError):
        FloorCheck.model_validate(
            {
                "name": "x",
                "cmd": ["uv", "run", "pytest"],
                "scope": "all",
                "cadence": "every-wave",
                "policy": "bogus",
            }
        )


# ---- ProfileBody.verify integration ---------------------------------------


def test_profile_body_verify_defaults_to_none() -> None:
    """A profile without a ``verify:`` leaf parses with ``verify=None``."""
    body = ProfileBody.model_validate({"name": "tiny"})
    assert body.verify is None


def test_profile_body_verify_round_trips_block() -> None:
    """A profile YAML with ``verify:`` lands a typed :class:`VerifyBlock`."""
    body = ProfileBody.model_validate(
        {
            "name": "with-verify",
            "verify": {
                "argv_allowlist": ["pytest"],
                "floor_checks": [
                    {
                        "name": "pytest",
                        "cmd": ["uv", "run", "pytest"],
                        "scope": "touched",
                        "cadence": "every-wave",
                        "policy": "warn",
                    }
                ],
                "waiver_mode": "A",
            },
        }
    )
    assert body.verify is not None
    assert body.verify.waiver_mode == "A"
    assert len(body.verify.floor_checks) == 1
    assert body.verify.floor_checks[0].name == "pytest"


# ---- 8 fixture profiles -----------------------------------------------------


@pytest.mark.parametrize(
    "profile_id, expected_check_count, expected_waiver_mode",
    [
        ("docs", 1, "B"),
        ("infra", 1, "B"),
        ("ml", 1, "B"),
        ("quality", 1, "B"),
        ("python", 3, "B"),
        ("apps", 4, "B"),
        ("research", 1, "B"),
        ("robotics", 1, "A"),
    ],
)
def test_fixture_profile_parses_with_verify_block(
    profile_id: str,
    expected_check_count: int,
    expected_waiver_mode: str,
) -> None:
    """Each fixture profile loads with the documented floor-pack size."""
    body = load_profile(profile_id)
    assert body.verify is not None, f"profile {profile_id!r} missing verify block"
    assert len(body.verify.floor_checks) == expected_check_count
    assert body.verify.waiver_mode == expected_waiver_mode
    assert body.verify.argv_allowlist, f"profile {profile_id!r} has empty argv_allowlist"
    assert compile_floor_pack(body.verify.floor_checks, allowlist=body.verify.argv_allowlist)


def test_builtin_gate_pack_profile_count_is_eight() -> None:
    """Exactly eight built-in profiles currently ship floor packs.

    Counts profiles that carry a non-empty floor pack, not merely any
    ``verify:`` leaf -- ``agent_driven`` ships a ``verify`` block for the
    risk-weighted ``enforce`` flip (Fidelity Spine) without a floor pack, so
    it is not a floor-pack profile.
    """
    expected = {"apps", "docs", "infra", "ml", "python", "quality", "research", "robotics"}
    actual = {
        profile_id
        for profile_id in list_profiles()
        if (block := load_profile(profile_id).verify) is not None and block.floor_checks
    }
    assert actual == expected


# ---- enforce OR-merge across active profiles -------------------------------


def test_merge_verify_blocks_enforce_is_any_true() -> None:
    """One enforcing profile in the active set flips the merged block to gating.

    ``_merge_verify_blocks`` folds ``enforce`` with OR so a single
    enforcing profile promotes the whole composed verify block, mirroring
    how ``profiles.enabled`` stacks multiple domain profiles at once.
    """
    merged = _merge_verify_blocks([VerifyBlock(enforce=False), VerifyBlock(enforce=True)])
    assert merged is not None
    assert merged.enforce is True


def test_merge_verify_blocks_enforce_stays_false_when_all_advisory() -> None:
    """An all-advisory active set yields an advisory merged block (no gating)."""
    merged = _merge_verify_blocks([VerifyBlock(), VerifyBlock(), VerifyBlock()])
    assert merged is not None
    assert merged.enforce is False


def test_merge_verify_blocks_empty_returns_none() -> None:
    """No active verify blocks yields ``None`` (nothing to gate on)."""
    assert _merge_verify_blocks([]) is None


# ---- jury_vendors: default + union-merge ------------------------------------


def test_verify_block_jury_vendors_defaults_to_full_panel() -> None:
    """A freshly-constructed :class:`VerifyBlock` defaults to the cross-vendor triple."""
    assert VerifyBlock().jury_vendors == ["claude", "codex", "opencode"]


def test_merge_verify_blocks_unions_jury_vendors() -> None:
    """``jury_vendors`` union-merges (dedup, first-occurrence order)."""
    merged = _merge_verify_blocks(
        [
            VerifyBlock(jury_vendors=["claude", "codex"]),
            VerifyBlock(jury_vendors=["codex", "opencode"]),
        ]
    )
    assert merged is not None
    assert merged.jury_vendors == ["claude", "codex", "opencode"]


# ---- resolve_wave_verify_block: band-conditional enforcement ----------------


def _wave(*, wave_id: str, title: str, file_scopes: list[str]) -> Wave:
    """Build a minimal claimed :class:`Wave` for band-resolution tests."""
    return Wave.model_validate(
        {
            "id": wave_id,
            "iter_id": "P29-I08",
            "title": title,
            "status": "claimed",
            "file_scopes": file_scopes,
            "success_criteria": [
                {
                    "id": "CR-01",
                    "text": "c1",
                    "kind": "legacy",
                    "acceptance_style": "binary",
                    "evidence_kind": "attested",
                    "quality_dimension": "functional_suitability",
                    "measurable_signal": "grandfathered legacy criterion",
                }
            ],
            "opened_at": "2026-06-03T00:00:00Z",
            "claimed_at": "2026-06-03T00:00:00Z",
        }
    )


def _band_scoped_block() -> VerifyBlock:
    """The shipped band-scoped intent: enforce + jury on at the fleet level."""
    return VerifyBlock(
        enforce=True,
        cross_vendor_jury=True,
        uiux_bands=["tui", "render"],
        jury_vendors=["claude", "codex", "opencode"],
    )


def test_resolve_wave_verify_block_band_wave_keeps_enforce_and_jury() -> None:
    """A UI-surface-file_scopes wave resolves to enforce=True AND cross_vendor_jury=True.

    This is the acceptance criterion's positive direction: band-scoped intent
    stays ON for a UI/UX-band wave (here banded by its ``surfaces/tui/``
    file_scopes, the structural arm of ``wave_in_uiux_band``).
    """
    wave = _wave(
        wave_id="P29-I08-W06",
        title="footer widget",
        file_scopes=["src/eawf/surfaces/tui/widgets/footer.py"],
    )
    resolved = resolve_wave_verify_block(_band_scoped_block(), wave)
    assert resolved is not None
    assert resolved.enforce is True
    assert resolved.cross_vendor_jury is True
    # The configured panel survives the resolution untouched.
    assert resolved.jury_vendors == ["claude", "codex", "opencode"]


def test_resolve_wave_verify_block_non_band_wave_disables_enforce() -> None:
    """A non-UI-file_scopes wave resolves to enforce=False (and no jury).

    This is the acceptance criterion's negative direction: the band-scoped
    intent does NOT gate a non-band wave. Enforcement is narrowed per wave,
    not flipped fleet-wide.
    """
    wave = _wave(
        wave_id="P29-I08-W06",
        title="band-scoped verify resolution",
        file_scopes=["src/eawf/kernel/spec/wave.py"],
    )
    resolved = resolve_wave_verify_block(_band_scoped_block(), wave)
    assert resolved is not None
    assert resolved.enforce is False
    assert resolved.cross_vendor_jury is False


def test_resolve_wave_verify_block_band_by_token_keeps_enforce() -> None:
    """A wave banded only by a ``uiux_bands`` token (not file_scopes) still enforces."""
    wave = _wave(
        wave_id="P29-I05-W11",
        title="native tui modes chassis",
        file_scopes=["src/eawf/observability/poll.py"],
    )
    resolved = resolve_wave_verify_block(_band_scoped_block(), wave)
    assert resolved is not None
    assert resolved.enforce is True
    assert resolved.cross_vendor_jury is True


def test_resolve_wave_verify_block_plain_enforce_not_narrowed() -> None:
    """A whole-fleet enforce block (no ``uiux_bands``) gates every wave unchanged.

    Only a band-scoped block (non-empty ``uiux_bands``) is narrowed per wave;
    a plain ``enforce: true`` profile deliberately gates all waves, so a
    non-band wave under it keeps ``enforce=True`` (the pre-W06 shape).
    """
    block = VerifyBlock(enforce=True)
    wave = _wave(
        wave_id="P29-I08-W06",
        title="backend rollup",
        file_scopes=["src/eawf/kernel/spec/wave.py"],
    )
    resolved = resolve_wave_verify_block(block, wave)
    assert resolved is block
    assert resolved.enforce is True


def test_resolve_wave_verify_block_passes_through_none() -> None:
    """A ``None`` merged block (no active verify) passes through unchanged."""
    wave = _wave(wave_id="P29-I08-W06", title="x", file_scopes=["src/eawf/kernel/spec/wave.py"])
    assert resolve_wave_verify_block(None, wave) is None


def test_resolve_wave_verify_block_advisory_block_untouched() -> None:
    """A fleet-advisory block (enforce=False) is returned untouched for any wave."""
    block = VerifyBlock(enforce=False, uiux_bands=["tui"])
    wave = _wave(
        wave_id="P29-I08-W06",
        title="footer",
        file_scopes=["src/eawf/surfaces/tui/widgets/footer.py"],
    )
    resolved = resolve_wave_verify_block(block, wave)
    assert resolved is block
    assert resolved.enforce is False


# ---- 14-profile reconcile sweep --------------------------------------------


def test_every_shipped_profile_loads_and_validates() -> None:
    """Every bundled profile parses through the loader against the closed schema.

    The reconcile sweep guards against verify-block shape drift landing
    on any shipped profile: a malformed leaf would raise at the
    ``ProfileBody`` ingestion boundary here. Profiles that carry a
    ``verify:`` leaf must produce a strict :class:`VerifyBlock`; profiles
    without one keep ``verify=None`` (advisory, no floor pack).

    Two shipped profiles declare ``enforce: true``: ``quality`` (band-scoped
    via ``uiux_bands``, narrowed per wave by
    :func:`~eawf.workflow.verify.readiness.resolve_wave_verify_block`) and
    ``agent_driven`` (whole-fleet enforce scoped to the high-risk band by the
    daemon's ``verdict_requirement`` risk-weighting -- the Fidelity Spine
    verifier-on flip). Every OTHER shipped profile keeps ``enforce=False``.
    """
    enforcing = {"quality", "agent_driven"}
    for profile_id in list_profiles():
        body = load_profile(profile_id)
        assert body.name, f"profile {profile_id!r} has an empty name"
        if body.verify is not None:
            assert isinstance(body.verify, VerifyBlock)
            if profile_id not in enforcing:
                assert body.verify.enforce is False, (
                    f"shipped profile {profile_id!r} unexpectedly sets verify.enforce=true"
                )


def test_only_quality_ships_band_scoped_enforce() -> None:
    """``quality`` is the sole bundled profile that opts into band-scoped gating.

    Pre-W06 the invariant was "no bundled profile sets ``enforce: true``"
    because a fleet-wide flip would block every close on upgrade. W06 makes
    enforcement band-conditional, so ``quality`` may ship ``enforce: true`` +
    ``cross_vendor_jury: true``: the wave-aware resolver
    (:func:`~eawf.workflow.verify.readiness.resolve_wave_verify_block`)
    narrows the bits to the UI/UX band, leaving non-band closes advisory.
    ``quality`` is also opt-in (NOT in the default enabled set, which ships
    just ``core``), so an existing repo is unaffected until it enables it.
    Every other bundled profile stays advisory.
    """
    band_scoped = [
        profile_id
        for profile_id in list_profiles()
        if (block := load_profile(profile_id).verify) is not None
        and block.enforce
        and block.uiux_bands
    ]
    fleet_enforce = [
        profile_id
        for profile_id in list_profiles()
        if (block := load_profile(profile_id).verify) is not None
        and block.enforce
        and not block.uiux_bands
    ]
    # quality is the sole BAND-SCOPED enforcer (enforce + uiux_bands).
    assert band_scoped == ["quality"]
    # agent_driven is the whole-fleet enforcer (enforce, no uiux_bands): the
    # Fidelity Spine verifier-on flip, scoped to the high-risk band by the
    # daemon's verdict_requirement risk-weighting rather than by uiux_bands.
    assert fleet_enforce == ["agent_driven"]
    quality = load_profile("quality").verify
    assert quality is not None
    assert quality.enforce is True
    assert quality.cross_vendor_jury is True
    # The configured cross-vendor panel is the full disjoint triple.
    assert quality.jury_vendors == ["claude", "codex", "opencode"]
    # The band tokens cover the UI/UX surfaces by name alongside the
    # structural file_scopes arm baked into ``wave_in_uiux_band``.
    assert "tui" in quality.uiux_bands
    assert "render" in quality.uiux_bands
    agent_driven = load_profile("agent_driven").verify
    assert agent_driven is not None
    assert agent_driven.enforce is True
    assert not agent_driven.uiux_bands


def test_robotics_floor_check_carries_hil_flags() -> None:
    """The robotics profile's HIL sentinel sets ``requires_gpu`` + ``runs_outside_jail``."""
    body = load_profile("robotics")
    assert body.verify is not None
    hil_check = body.verify.floor_checks[0]
    assert hil_check.requires_gpu is True
    assert hil_check.runs_outside_jail is True
    assert hil_check.cadence == "every-phase"


def test_apps_floor_check_carries_docs_build_at_every_iter() -> None:
    """The apps profile's docs-build check fires at every-iter cadence."""
    body = load_profile("apps")
    assert body.verify is not None
    docs = next(
        (c for c in body.verify.floor_checks if c.name == "docs-build"),
        None,
    )
    assert docs is not None
    assert docs.cadence == "every-iter"
    assert docs.timeout_class == "slow"


# ---- W11 resolve_waiver_mode now reads typed VerifyBlock --------------------


def test_resolve_waiver_mode_reads_typed_verify_block() -> None:
    """The W11 helper now accepts a typed :class:`VerifyBlock` directly."""
    block_a = VerifyBlock(waiver_mode="A")
    block_b = VerifyBlock()  # default B
    block_c = VerifyBlock(waiver_mode="C")

    assert resolve_waiver_mode(block_a) == "A"
    assert resolve_waiver_mode(block_b) == "B"
    assert resolve_waiver_mode(block_c) == "C"


def test_resolve_waiver_mode_legacy_dict_path_still_works() -> None:
    """The legacy merged-config dict path is preserved (no regressions)."""
    assert resolve_waiver_mode({}) == DEFAULT_WAIVER_MODE
    assert resolve_waiver_mode({"verify": {"waiver_mode": "A"}}) == "A"
    assert resolve_waiver_mode({"verify": {"waiver_mode": "C"}}) == "C"


def test_resolve_waiver_mode_none_falls_back_to_default() -> None:
    """``None`` (no source) returns :data:`DEFAULT_WAIVER_MODE`."""
    assert resolve_waiver_mode(None) == DEFAULT_WAIVER_MODE


def test_verify_block_waiver_mode_default_is_b() -> None:
    """The W11 default IS mode B per the typed field default."""
    assert VerifyBlock().waiver_mode == "B"
    assert DEFAULT_WAIVER_MODE == "B"
