"""Integration tests for the v2 profile composition loader (P25-W15).

Covers the three W15 success criteria:

1. ``ProfileBody`` v2 is a closed schema with ``conflicts_with`` /
   ``overrides`` / ``dispatch_session_policy``.
2. The composition loader:
   - performs a deterministic deep-merge by id (sorted profile-id order
     produces the same output bytes for the same input set);
   - applies strictest-wins on ``instrument_requirements.kind``
     (``hard`` > ``soft``);
   - fails-fast on undeclared conflict per V3.
3. ``extra="forbid"`` rejects unknown profile keys at load time.

The tests write workspace-overlay YAMLs under a tmp workspace and exercise
:func:`eawf.profiles.load_composed_profile` end-to-end (discovery → load →
compose). This is the "composition loader entry point" surface — the only
public seam callers should use when they need a deterministic merged view
keyed by profile id.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from eawf.cli.errors import ValidationError
from eawf.profiles import (
    ComposedProfile,
    ProfileConflict,
    discovery,  # type: ignore[attr-defined]
    load_composed_profile,
)


@pytest.fixture(autouse=True)
def _clear_profile_cache() -> None:  # type: ignore[misc]
    """Drop the discovery-layer cache around each test."""
    discovery._clear_cache_for_tests()
    yield
    discovery._clear_cache_for_tests()


def _write_profile(ws: Path, profile_id: str, body_yaml: str) -> None:
    """Drop a YAML body under ``<ws>/.ea/profiles/<id>.yaml``."""
    root = ws / ".ea" / "profiles"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{profile_id}.yaml").write_text(textwrap.dedent(body_yaml), encoding="utf-8")


# --- Deterministic composition --------------------------------------------


def test_loader_composes_sorted_by_id(tmp_path: Path) -> None:
    """Two callers, same profile set in different order, same output bytes.

    W15 success criterion 2 (deterministic deep-merge by id): the loader
    sorts ids before dispatch so the merged envelope is bit-identical
    regardless of caller-passed order.
    """
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        skills_referenced: [research]
        """,
    )
    _write_profile(
        ws,
        "beta",
        """
        name: beta
        skills_referenced: [audit]
        """,
    )
    forward = load_composed_profile(["alpha", "beta"], workspace=ws)
    swapped = load_composed_profile(["beta", "alpha"], workspace=ws)
    # Sorted-id dispatch means both calls compose [alpha, beta] internally.
    assert forward.name == "alpha+beta"
    assert swapped.name == "alpha+beta"
    assert forward.model_dump(mode="json") == swapped.model_dump(mode="json")


def test_loader_deduplicates_ids(tmp_path: Path) -> None:
    """Duplicate ids in the input are collapsed before dispatch."""
    ws = tmp_path / "ws"
    _write_profile(ws, "alpha", "name: alpha\nskills_referenced: [a]\n")
    composed = load_composed_profile(["alpha", "alpha"], workspace=ws)
    assert composed.name == "alpha"
    assert composed.skills_referenced == ["a"]


def test_loader_empty_input_returns_empty_composed(tmp_path: Path) -> None:
    composed = load_composed_profile([], workspace=tmp_path)
    assert composed.name == "composed:empty"
    assert composed.skills_referenced == []
    assert composed.dispatch_session_policy is None


# --- Strictest-wins on instrument_requirements.kind ------------------------


def test_loader_strictest_wins_hard_beats_soft(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        instrument_requirements:
          - {name: git, kind: soft}
        """,
    )
    _write_profile(
        ws,
        "beta",
        """
        name: beta
        instrument_requirements:
          - {name: git, kind: hard}
        """,
    )
    composed = load_composed_profile(["alpha", "beta"], workspace=ws)
    by_name = {req.name: req for req in composed.instrument_requirements}
    assert by_name["git"].kind == "hard"


def test_loader_strictest_wins_independent_of_caller_order(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        instrument_requirements:
          - {name: git, kind: hard}
        """,
    )
    _write_profile(
        ws,
        "beta",
        """
        name: beta
        instrument_requirements:
          - {name: git, kind: soft}
        """,
    )
    forward = load_composed_profile(["alpha", "beta"], workspace=ws)
    swapped = load_composed_profile(["beta", "alpha"], workspace=ws)
    fwd_by_name = {req.name: req.kind for req in forward.instrument_requirements}
    swp_by_name = {req.name: req.kind for req in swapped.instrument_requirements}
    assert fwd_by_name == swp_by_name
    assert fwd_by_name["git"] == "hard"


# --- Fail-fast on undeclared conflict (V3) ---------------------------------


def test_loader_fails_fast_on_undeclared_conflict(tmp_path: Path) -> None:
    """Two profiles that declare each other in ``conflicts_with`` without
    a matching ``overrides`` edge raise :class:`ProfileConflict` under the
    default ``"fail"`` resolution (V3 fail-fast).
    """
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        conflicts_with: [beta]
        """,
    )
    _write_profile(
        ws,
        "beta",
        """
        name: beta
        """,
    )
    with pytest.raises(ProfileConflict) as excinfo:
        load_composed_profile(["alpha", "beta"], workspace=ws)
    assert "undeclared profile conflict" in str(excinfo.value)
    assert "alpha" in str(excinfo.value)
    assert "beta" in str(excinfo.value)


def test_loader_fails_fast_when_conflict_declared_on_either_side(tmp_path: Path) -> None:
    """``b.conflicts_with: [a]`` alone is enough to raise — the loader
    treats the conflict graph as unordered.
    """
    ws = tmp_path / "ws"
    _write_profile(ws, "alpha", "name: alpha\n")
    _write_profile(ws, "beta", "name: beta\nconflicts_with: [alpha]\n")
    with pytest.raises(ProfileConflict):
        load_composed_profile(["alpha", "beta"], workspace=ws)


def test_loader_override_discharges_conflict(tmp_path: Path) -> None:
    """An ``overrides: [b]`` edge discharges the ``(a, b)`` conflict."""
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        conflicts_with: [beta]
        overrides: [beta]
        """,
    )
    _write_profile(ws, "beta", "name: beta\n")
    composed = load_composed_profile(["alpha", "beta"], workspace=ws)
    assert isinstance(composed, ComposedProfile)
    assert composed.conflict_warnings == []


def test_loader_override_audit_records_field_path_chain(tmp_path: Path) -> None:
    """When ``alpha.overrides: [beta]`` and both ship a ``git`` instrument,
    the audit chain ``[alpha, beta]`` lands under
    ``"instrument_requirements[name=git]"``.
    """
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        overrides: [beta]
        instrument_requirements:
          - {name: git, kind: hard}
        skills_referenced: [research]
        """,
    )
    _write_profile(
        ws,
        "beta",
        """
        name: beta
        instrument_requirements:
          - {name: git, kind: soft}
        skills_referenced: [research]
        """,
    )
    composed = load_composed_profile(["alpha", "beta"], workspace=ws)
    assert "instrument_requirements[name=git]" in composed.override_audit
    chain = composed.override_audit["instrument_requirements[name=git]"]
    assert chain == ["alpha", "beta"]
    assert composed.override_audit["skills_referenced[research]"] == ["alpha", "beta"]


def test_loader_first_wins_mode_drops_later_contributor(tmp_path: Path) -> None:
    """With ``conflict_resolution="first-wins"``, the alphabetically-second
    profile's contributions drop from the merge and a warning is recorded.
    """
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        conflicts_with: [beta]
        skills_referenced: [research]
        """,
    )
    _write_profile(
        ws,
        "beta",
        """
        name: beta
        skills_referenced: [audit]
        """,
    )
    composed = load_composed_profile(
        ["alpha", "beta"],
        workspace=ws,
        conflict_resolution="first-wins",
    )
    # Only alpha's contributions land; beta dropped.
    assert composed.skills_referenced == ["research"]
    assert composed.conflict_warnings, "expected first-wins warning"
    assert "first-wins" in composed.conflict_warnings[0]


def test_loader_profile_conflict_subclasses_validation_failed(tmp_path: Path) -> None:
    """:class:`ProfileConflict` is a :class:`ValidationFailed` so callers
    that surface the canonical CLI envelope get the right exit code."""
    ws = tmp_path / "ws"
    _write_profile(ws, "alpha", "name: alpha\nconflicts_with: [beta]\n")
    _write_profile(ws, "beta", "name: beta\n")
    with pytest.raises(ValidationError):
        load_composed_profile(["alpha", "beta"], workspace=ws)


# --- dispatch_session_policy composition ----------------------------------


def test_loader_dispatch_session_policy_last_non_none_wins(tmp_path: Path) -> None:
    """The composed policy is the last-non-``None`` contribution in caller
    (sorted-id) order.
    """
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        dispatch_session_policy: fresh
        """,
    )
    _write_profile(
        ws,
        "beta",
        """
        name: beta
        dispatch_session_policy: continue
        """,
    )
    composed = load_composed_profile(["alpha", "beta"], workspace=ws)
    # Sorted order: [alpha, beta]; beta is the last non-None contributor.
    assert composed.dispatch_session_policy == "continue"
    assert composed.provenance["dispatch_session_policy"] == ["alpha", "beta"]


def test_loader_dispatch_session_policy_none_is_skipped(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        dispatch_session_policy: hybrid
        """,
    )
    _write_profile(ws, "beta", "name: beta\n")
    composed = load_composed_profile(["alpha", "beta"], workspace=ws)
    assert composed.dispatch_session_policy == "hybrid"
    assert composed.provenance["dispatch_session_policy"] == ["alpha"]


# --- extra="forbid" rejects unknown keys ----------------------------------


def test_loader_rejects_unknown_top_level_key_in_yaml(tmp_path: Path) -> None:
    """A YAML body with an unknown top-level key (e.g. typo) fails with
    :class:`ValidationFailed` per AGENTS.md rule 2.
    """
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        unknown_future_key: 42
        """,
    )
    with pytest.raises(ValidationError) as excinfo:
        load_composed_profile(["alpha"], workspace=ws)
    assert "alpha" in str(excinfo.value)


def test_loader_rejects_invalid_dispatch_session_policy_in_yaml(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        dispatch_session_policy: not-a-policy
        """,
    )
    with pytest.raises(ValidationError):
        load_composed_profile(["alpha"], workspace=ws)


# --- Non-fatal warnings ---------------------------------------------------


def test_loader_warns_on_render_block_overlap_without_overrides(tmp_path: Path) -> None:
    """Two non-overriding profiles declaring the same render-block id emit
    a non-fatal warning; the later body still wins.
    """
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        render_blocks:
          - {id: shared-block, target: AGENTS.md, body_template: from-alpha}
        """,
    )
    _write_profile(
        ws,
        "beta",
        """
        name: beta
        render_blocks:
          - {id: shared-block, target: AGENTS.md, body_template: from-beta}
        """,
    )
    composed = load_composed_profile(["alpha", "beta"], workspace=ws)
    assert any("shared-block" in w for w in composed.conflict_warnings), (
        f"expected overlap warning, got {composed.conflict_warnings!r}"
    )
    # Later body (sorted: beta) wins.
    by_id = {block.id: block for block in composed.render_blocks}
    assert by_id["shared-block"].body_template == "from-beta"


def test_loader_render_block_overlap_silenced_by_override(tmp_path: Path) -> None:
    """When the overriding profile claims the overlap explicitly, no
    conflict warning is emitted.
    """
    ws = tmp_path / "ws"
    _write_profile(
        ws,
        "alpha",
        """
        name: alpha
        overrides: [beta]
        render_blocks:
          - {id: shared-block, target: AGENTS.md, body_template: from-alpha}
        """,
    )
    _write_profile(
        ws,
        "beta",
        """
        name: beta
        render_blocks:
          - {id: shared-block, target: AGENTS.md, body_template: from-beta}
        """,
    )
    composed = load_composed_profile(["alpha", "beta"], workspace=ws)
    assert composed.conflict_warnings == []
    assert "render_blocks[id=shared-block]" in composed.override_audit
