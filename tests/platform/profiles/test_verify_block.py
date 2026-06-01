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
from eawf.platform.profiles import FloorCheck, ProfileBody, VerifyBlock, list_profiles, load_profile
from eawf.workflow.lifecycle.waivers import DEFAULT_WAIVER_MODE, resolve_waiver_mode
from eawf.workflow.verify.compile import compile_floor_pack
from eawf.workflow.verify.readiness import _merge_verify_blocks

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
    """Exactly eight built-in profiles currently ship floor packs."""
    expected = {"apps", "docs", "infra", "ml", "python", "quality", "research", "robotics"}
    actual = {
        profile_id for profile_id in list_profiles() if (load_profile(profile_id).verify or False)
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


# ---- 14-profile reconcile sweep --------------------------------------------


def test_every_shipped_profile_loads_and_validates() -> None:
    """Every bundled profile parses through the loader against the closed schema.

    The reconcile sweep guards against verify-block shape drift landing
    on any shipped profile: a malformed leaf would raise at the
    ``ProfileBody`` ingestion boundary here. Profiles that carry a
    ``verify:`` leaf must produce a strict :class:`VerifyBlock`; profiles
    without one keep ``verify=None`` (advisory, no floor pack).
    """
    for profile_id in list_profiles():
        body = load_profile(profile_id)
        assert body.name, f"profile {profile_id!r} has an empty name"
        if body.verify is not None:
            assert isinstance(body.verify, VerifyBlock)
            # enforce defaults False on every shipped profile; none flip
            # to gating (gating is exercised via test fixtures only).
            assert body.verify.enforce is False, (
                f"shipped profile {profile_id!r} unexpectedly sets verify.enforce=true"
            )


def test_no_shipped_profile_enables_enforce() -> None:
    """No bundled profile ships ``verify.enforce: true`` — gating stays dormant.

    The advisory->gating flip is a mechanism, not a default any shipped
    profile turns on. Operators opt a workspace profile into gating
    deliberately; the bundled set stays advisory so existing repos keep
    their non-blocking close behaviour after an upgrade.
    """
    enforcing = [
        profile_id
        for profile_id in list_profiles()
        if (block := load_profile(profile_id).verify) is not None and block.enforce
    ]
    assert enforcing == []


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
