"""Tests for the profile-fed verify spine schema.

Pins the W10 success criteria:

* :class:`AuditCadence` widened to 5 values (``"ship"`` added).
* :class:`VerifyBlock` + :class:`FloorCheck` accept the documented
  shape and reject unknown fields (``extra="forbid"`` per AGENTS
  rule 2).
* The 8 built-in gate-pack profiles parse via the layered loader and
  each carries a non-empty floor pack.
* ``waiver_mode`` defaults to ``"B"`` on a freshly-constructed
  :class:`VerifyBlock`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.audit import AUDIT_CADENCE_VALUES
from eawf.platform.profiles import (
    CheckpointBlock,
    FloorCheck,
    ProfileBody,
    VerifyBlock,
    list_profiles,
    load_profile,
)
from eawf.workflow.verify.compile import compile_floor_pack
from eawf.workflow.verify.readiness import (
    _merge_verify_blocks,
    _overlay_repo_verify_leaves,
)

# ---- AuditCadence 5-value enum ---------------------------------------------


def test_audit_cadence_values_carry_ship() -> None:
    """The 5-value :data:`AUDIT_CADENCE_VALUES` set carries the W10 ``ship`` entry."""
    assert (
        frozenset({"every-task", "every-batch", "every-milestone", "ship", "manual"})
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
    # The Literal also accepts the other four members.
    for other in ("every-task", "every-batch", "every-milestone", "manual"):
        FloorCheck(
            name=f"c-{other}",
            cmd=["uv", "run", "pytest", "-q"],
            scope="all",
            cadence=other,
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
                "cadence": "every-task",
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
        cadence="every-task",
        policy="warn",
    )
    assert default.requires_gpu is False
    assert default.runs_outside_jail is False
    assert default.timeout_class == "standard"

    hil = FloorCheck(
        name="hil",
        cmd=["uv", "run", "pytest"],
        scope="all",
        cadence="every-milestone",
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
            cadence="every-task",
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
                "cadence": "every-task",
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
                        "cadence": "every-task",
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


# ---- 14-profile reconcile sweep --------------------------------------------


def test_robotics_floor_check_carries_hil_flags() -> None:
    """The robotics profile's HIL sentinel sets ``requires_gpu`` + ``runs_outside_jail``."""
    body = load_profile("robotics")
    assert body.verify is not None
    hil_check = body.verify.floor_checks[0]
    assert hil_check.requires_gpu is True
    assert hil_check.runs_outside_jail is True
    assert hil_check.cadence == "every-milestone"


def test_apps_floor_check_carries_docs_build_at_every_batch() -> None:
    """The apps profile's docs-build check fires at every-batch cadence."""
    body = load_profile("apps")
    assert body.verify is not None
    docs = next(
        (c for c in body.verify.floor_checks if c.name == "docs-build"),
        None,
    )
    assert docs is not None
    assert docs.cadence == "every-batch"
    assert docs.timeout_class == "slow"


def test_verify_block_waiver_mode_default_is_b() -> None:
    """The W11 default IS mode B per the typed field default."""
    assert VerifyBlock().waiver_mode == "B"


# ---- P30-I01-W05: checkpoint drift-cadence dial -----------------------------


def test_checkpoint_block_defaults_to_optimistic_with_budget() -> None:
    """A freshly-constructed :class:`CheckpointBlock` defaults to the optimistic cadence.

    The default shape is the shift-left v0.6 cadence: ``optimistic`` mode plus
    non-negative drift budgets a later wave reads to size the pulse window.
    """
    checkpoint = CheckpointBlock()
    assert checkpoint.checkpoint_mode == "optimistic"
    assert checkpoint.drift_budget_waves >= 0
    assert checkpoint.drift_budget_eu >= 0.0


def test_verify_block_carries_optimistic_checkpoint_by_default() -> None:
    """A fresh :class:`VerifyBlock` mounts an optimistic checkpoint via the factory."""
    block = VerifyBlock()
    assert block.checkpoint.checkpoint_mode == "optimistic"
    assert block.checkpoint.drift_budget_waves >= 0
    assert block.checkpoint.drift_budget_eu >= 0.0


def test_agent_driven_profile_resolves_optimistic_checkpoint() -> None:
    """The ``agent_driven`` profile resolves the optimistic checkpoint default.

    The shipped ``agent_driven`` verify block sets ``enforce: true`` but
    declares no ``checkpoint`` leaf, so the default factory resolves the
    optimistic cadence -- the binding-proof for the criterion's positive
    direction.
    """
    body = load_profile("agent_driven")
    assert body.verify is not None
    assert body.verify.checkpoint.checkpoint_mode == "optimistic"
    assert body.verify.checkpoint.drift_budget_waves >= 0
    assert body.verify.checkpoint.drift_budget_eu >= 0.0


def test_merge_verify_blocks_downstream_barrier_wins_over_optimistic() -> None:
    """A downstream ``barrier`` checkpoint wins composition over an upstream optimistic.

    ``_merge_verify_blocks`` takes the last contributor's ``checkpoint`` dial,
    so a downstream profile setting ``barrier`` overrides an upstream default
    ``optimistic`` block -- the binding-proof for the criterion's composition
    direction.
    """
    upstream = VerifyBlock()  # default optimistic checkpoint
    downstream = VerifyBlock(checkpoint=CheckpointBlock(checkpoint_mode="barrier"))
    merged = _merge_verify_blocks([upstream, downstream])
    assert merged is not None
    assert merged.checkpoint.checkpoint_mode == "barrier"
    # The default drift budgets survive the composition.
    assert merged.checkpoint.drift_budget_waves >= 0
    assert merged.checkpoint.drift_budget_eu >= 0.0


def test_merge_verify_blocks_keeps_optimistic_when_no_barrier() -> None:
    """An all-optimistic active set yields an optimistic merged checkpoint."""
    merged = _merge_verify_blocks([VerifyBlock(), VerifyBlock()])
    assert merged is not None
    assert merged.checkpoint.checkpoint_mode == "optimistic"


def test_checkpoint_block_rejects_invalid_mode() -> None:
    """``checkpoint_mode`` is a closed Literal -- ``yolo`` raises at the boundary."""
    with pytest.raises(ValidationError):
        CheckpointBlock.model_validate({"checkpoint_mode": "yolo"})


def test_verify_block_rejects_invalid_checkpoint_mode() -> None:
    """A profile leaf with ``checkpoint_mode: yolo`` fails the ``VerifyBlock`` load."""
    with pytest.raises(ValidationError):
        VerifyBlock.model_validate({"checkpoint": {"checkpoint_mode": "yolo"}})


def test_checkpoint_block_rejects_negative_drift_budget_waves() -> None:
    """``drift_budget_waves`` is ``Field(ge=0)`` -- a negative budget raises."""
    with pytest.raises(ValidationError):
        CheckpointBlock.model_validate({"drift_budget_waves": -1})


def test_checkpoint_block_rejects_negative_drift_budget_eu() -> None:
    """``drift_budget_eu`` is ``Field(ge=0)`` -- a negative budget raises."""
    with pytest.raises(ValidationError):
        CheckpointBlock.model_validate({"drift_budget_eu": -0.5})


def test_checkpoint_block_rejects_unknown_field() -> None:
    """Strict Pydantic v2 -- an extra key on :class:`CheckpointBlock` raises."""
    with pytest.raises(ValidationError):
        CheckpointBlock.model_validate(
            {
                "checkpoint_mode": "optimistic",
                "phantom_field": True,
            }
        )


# ---- W48: the merge preserves every field the block carries ------------------


def test_merge_verify_blocks_preserves_the_juror_ceiling() -> None:
    """A profile-set juror ceiling survives the merge instead of resetting to 600s.

    The merge rebuilds ``VerifyBlock`` field by field, and this field was not in
    the list -- so a profile that raised its auditor's wall clock to 1800s got a
    merged block back that said 600. Idle contract: declared as config, dropped
    on the way to the close path.
    """
    merged = _merge_verify_blocks([VerifyBlock(juror_wall_clock_seconds=1800.0)])

    assert merged is not None
    assert merged.juror_wall_clock_seconds == pytest.approx(1800.0)


def test_merge_verify_blocks_preserves_the_odr_dials_and_jury_authority() -> None:
    """The ODR dials and the jury's trust floors survive the merge too.

    ``odr_blocking`` is a gating bit, so it OR-folds like ``enforce``: one profile
    opting into a hard ODR gate gates the composed block. ``odr_floor`` and
    ``jury_authority`` are scalar dials, so the last contributor wins, as
    ``waiver_mode`` and ``checkpoint`` already did.
    """
    from eawf.platform.profiles.models import JuryAuthorityConfig

    authority = JuryAuthorityConfig(min_labeled_waves=50)
    merged = _merge_verify_blocks(
        [
            VerifyBlock(odr_blocking=True),
            VerifyBlock(odr_floor=0.5, jury_authority=authority),
        ]
    )

    assert merged is not None
    assert merged.odr_blocking is True
    assert merged.odr_floor == pytest.approx(0.5)
    assert merged.jury_authority == authority


def test_a_profile_still_setting_the_retired_iter_audit_gate_loads_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = ProfileBody.model_validate(
        {"name": "house", "verify": {"require_iter_audit_accepted": True, "enforce": True}}
    )

    assert body.verify == VerifyBlock(enforce=True)
    assert "require_iter_audit_accepted" not in VerifyBlock.model_fields
    assert "field=verify.require_iter_audit_accepted retired" in caplog.text


def test_a_profile_still_setting_the_retired_uiux_bands_loads_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = ProfileBody.model_validate(
        {"name": "house", "verify": {"uiux_bands": ["tui"], "enforce": True}}
    )

    assert body.verify == VerifyBlock(enforce=True)
    assert "uiux_bands" not in VerifyBlock.model_fields
    assert "field=verify.uiux_bands retired" in caplog.text


def test_a_verify_block_without_the_retired_gate_loads_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert VerifyBlock.model_validate({}) == VerifyBlock()
    assert "retired" not in caplog.text


@pytest.mark.parametrize("payload", [{"bogus": 1}, {"enforce": "not-a-bool"}])
def test_a_verify_block_still_refuses_an_unknown_or_ill_typed_key(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        VerifyBlock.model_validate(payload)


@pytest.mark.parametrize(
    "blocks",
    [
        [VerifyBlock(waiver_mode="disabled"), VerifyBlock(waiver_mode="A")],
        [VerifyBlock(waiver_mode="C"), VerifyBlock(waiver_mode="disabled")],
    ],
)
def test_merge_verify_blocks_disabled_waiver_is_absorbing(blocks: list[VerifyBlock]) -> None:
    merged = _merge_verify_blocks(blocks)

    assert merged is not None
    assert merged.waiver_mode == "disabled"


def test_overlay_repo_verify_leaves_preserves_profile_values_when_defaults_are_builtin() -> None:
    block = VerifyBlock(waiver_mode="C", juror_wall_clock_seconds=1800.0)
    merged = {
        "verify": {
            "odr_blocking": False,
            "waiver_mode": "B",
        }
    }
    source_map = {
        "verify.odr_blocking": "built-in",
        "verify.waiver_mode": "built-in",
    }

    resolved = _overlay_repo_verify_leaves(block, merged, source_map=source_map)

    assert resolved is not None
    assert resolved.waiver_mode == "C"
    assert resolved.juror_wall_clock_seconds == pytest.approx(1800.0)


def test_overlay_repo_verify_leaves_builtin_only_without_profile_stays_absent() -> None:
    merged = {
        "verify": {
            "juror_wall_clock_seconds": 600.0,
            "odr_blocking": False,
            "waiver_mode": "B",
        }
    }
    source_map = {f"verify.{key}": "built-in" for key in merged["verify"]}

    resolved = _overlay_repo_verify_leaves(None, merged, source_map=source_map)

    assert resolved is None


def test_overlay_repo_verify_leaves_explicit_leaf_without_profile_synthesizes() -> None:
    merged = {
        "verify": {
            "odr_blocking": True,
            "waiver_mode": "B",
        }
    }
    source_map = {
        "verify.odr_blocking": "repo",
        "verify.waiver_mode": "built-in",
    }

    resolved = _overlay_repo_verify_leaves(None, merged, source_map=source_map)

    assert resolved is not None
    assert resolved.odr_blocking is True


def test_overlay_repo_verify_leaves_tightens_odr_and_disables_waivers() -> None:
    block = VerifyBlock(odr_blocking=False, waiver_mode="A")
    merged = {
        "verify": {
            "odr_blocking": True,
            "waiver_mode": "disabled",
        }
    }
    source_map = {
        "verify.odr_blocking": "repo",
        "verify.waiver_mode": "repo",
    }

    resolved = _overlay_repo_verify_leaves(block, merged, source_map=source_map)

    assert resolved is not None
    assert resolved.odr_blocking is True
    assert resolved.waiver_mode == "disabled"


def test_overlay_repo_verify_leaves_cannot_loosen_disabled_profile() -> None:
    block = VerifyBlock(waiver_mode="disabled")
    merged = {
        "verify": {
            "odr_blocking": False,
            "waiver_mode": "A",
        }
    }

    resolved = _overlay_repo_verify_leaves(
        block,
        merged,
        source_map={"verify.waiver_mode": "repo"},
    )

    assert resolved is not None
    assert resolved.waiver_mode == "disabled"


def test_merge_verify_blocks_covers_every_field_on_the_model() -> None:
    """No field may be added to VerifyBlock without being merged.

    The failure this pins is structural, not about any one field: a field the
    merge forgets is silently reset to its default, and the profile that set it
    has no way to find out. Rather than re-testing each field, assert the merged
    block round-trips a fully non-default one.
    """
    from eawf.platform.profiles.models import CheckpointBlock, JuryAuthorityConfig

    source = VerifyBlock(
        argv_allowlist=["uv"],
        timeout_class_seconds={"quick": 5},
        waiver_mode="C",
        enforce=True,
        cross_vendor_jury=True,
        juror_wall_clock_seconds=1800.0,
        jury_vendors=["claude"],
        odr_floor=0.55,
        odr_blocking=True,
        jury_authority=JuryAuthorityConfig(min_labeled_waves=42),
        checkpoint=CheckpointBlock(checkpoint_mode="barrier"),
    )

    merged = _merge_verify_blocks([source])

    assert merged is not None
    for field in VerifyBlock.model_fields:
        assert getattr(merged, field) == getattr(source, field), field
