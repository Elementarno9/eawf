"""Unit tests for the C04b :class:`SkillManifest` schema.

Covers the three wave success criteria + the frozen-enum parity guard:

* (a) ``extra="forbid"`` rejection — a stray top-level key fails fast.
* (b) non-``Literal`` runtime rejection (C04b F-b02) — an unknown runtime
  id is rejected by the closed ``RuntimeId`` literal.
* (c) ``target_dir`` → ``output_dir`` rename (BOT-06 / D-b3) — the canonical
  write-destination field is ``output_dir`` and the legacy ``target_dir``
  key is rejected by ``extra="forbid"``.

Plus boundary/error-path coverage (empty ``runtime`` list, missing required
fields) per the project test-discipline rule, and a parity assertion that
the re-exported ``EnvelopeStatus`` is the same frozen literal as the
envelope module owns (D-b1 single-source freeze).
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from eawf.render.envelope import EnvelopeStatus as EnvelopeStatusFromEnvelope
from eawf.runtimes.plugin_manifest import (
    EnvelopeStatus,
    RuntimeId,
    SkillManifest,
)

pytestmark = pytest.mark.unit


def _valid_manifest_dict() -> dict[str, object]:
    """Return one well-formed dict that round-trips through SkillManifest."""
    return {
        "name": "/research",
        "description": "Read-only investigation of an open question.",
        "runtime": ["claude-code", "codex", "opencode"],
        "dispatch": {"session_policy": "continue", "parallel_within_run": True},
        "output_envelope_kind": "ResearchBody",
        "output_dir": ".ea/artifacts/research",
    }


def test_skill_manifest_round_trip() -> None:
    """A well-formed dict validates and round-trips losslessly."""
    raw = _valid_manifest_dict()
    manifest = SkillManifest.model_validate(raw)
    assert manifest.name == "/research"
    assert manifest.runtime == ["claude-code", "codex", "opencode"]
    assert manifest.dispatch == {"session_policy": "continue", "parallel_within_run": True}
    assert manifest.output_envelope_kind == "ResearchBody"
    assert manifest.output_dir == ".ea/artifacts/research"
    dumped = manifest.model_dump()
    assert dumped["name"] == "/research"
    assert dumped["output_dir"] == ".ea/artifacts/research"


def test_skill_manifest_dispatch_and_output_dir_default() -> None:
    """``dispatch`` defaults to an empty dict and ``output_dir`` to ``None``."""
    manifest = SkillManifest(
        name="/audit",
        description="Run the audit DSL against a closed scope.",
        runtime=["claude-code"],
        output_envelope_kind="AuditBody",
    )
    assert manifest.dispatch == {}
    assert manifest.output_dir is None


# ---------------------------------------------------------------------------
# (a) extra="forbid" rejection
# ---------------------------------------------------------------------------


def test_skill_manifest_rejects_extra_top_level_key() -> None:
    """A stray top-level key fails fast under ``extra='forbid'``."""
    raw = _valid_manifest_dict()
    raw["mystery"] = "boom"
    with pytest.raises(ValidationError):
        SkillManifest.model_validate(raw)


# ---------------------------------------------------------------------------
# (b) non-Literal runtime rejection (C04b F-b02)
# ---------------------------------------------------------------------------


def test_skill_manifest_rejects_non_literal_runtime() -> None:
    """An unknown runtime id is rejected by the closed ``RuntimeId`` literal."""
    raw = _valid_manifest_dict()
    raw["runtime"] = ["claude-code", "aider"]
    with pytest.raises(ValidationError):
        SkillManifest.model_validate(raw)


def test_skill_manifest_rejects_blank_runtime_id() -> None:
    """The empty string is not a valid runtime id (closed Literal)."""
    raw = _valid_manifest_dict()
    raw["runtime"] = [""]
    with pytest.raises(ValidationError):
        SkillManifest.model_validate(raw)


def test_skill_manifest_rejects_empty_runtime_list() -> None:
    """An empty ``runtime`` list is rejected — a skill no runtime can host."""
    raw = _valid_manifest_dict()
    raw["runtime"] = []
    with pytest.raises(ValidationError, match="at least one runtime"):
        SkillManifest.model_validate(raw)


# ---------------------------------------------------------------------------
# (c) target_dir -> output_dir rename (BOT-06 / D-b3)
# ---------------------------------------------------------------------------


def test_skill_manifest_accepts_canonical_output_dir() -> None:
    """The canonical BOT-06 field name ``output_dir`` is accepted."""
    raw = _valid_manifest_dict()
    raw["output_dir"] = "build/eawf-plugin"
    manifest = SkillManifest.model_validate(raw)
    assert manifest.output_dir == "build/eawf-plugin"


def test_skill_manifest_rejects_legacy_target_dir_key() -> None:
    """The legacy ``target_dir`` key is rejected — renamed to ``output_dir``.

    BOT-06 (D-b3) renamed the write-destination param to the canonical
    ``output_dir``; ``extra='forbid'`` enforces the rename so a manifest
    still carrying ``target_dir`` fails fast rather than silently dropping
    the value.
    """
    raw = _valid_manifest_dict()
    del raw["output_dir"]
    raw["target_dir"] = "build/eawf-plugin"
    with pytest.raises(ValidationError):
        SkillManifest.model_validate(raw)


# ---------------------------------------------------------------------------
# Error paths — missing required fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["name", "description", "runtime", "output_envelope_kind"])
def test_skill_manifest_rejects_missing_required_field(field: str) -> None:
    """Each required field is rejected when absent."""
    raw = _valid_manifest_dict()
    del raw[field]
    with pytest.raises(ValidationError):
        SkillManifest.model_validate(raw)


# ---------------------------------------------------------------------------
# D-b1 frozen-enum parity guard
# ---------------------------------------------------------------------------


def test_envelope_status_reexport_is_single_sourced() -> None:
    """The re-exported ``EnvelopeStatus`` is the same frozen literal (D-b1)."""
    assert EnvelopeStatus is EnvelopeStatusFromEnvelope
    assert set(get_args(EnvelopeStatus)) == {"ok", "needs_user", "blocked", "failed", "partial"}


def test_runtime_id_literal_has_three_members() -> None:
    """``RuntimeId`` is closed to the three canonical runtime ids."""
    assert set(get_args(RuntimeId)) == {"claude-code", "codex", "opencode"}


# ---------------------------------------------------------------------------
# Shipped-skill manifest session_policy is on the canonical vocabulary
# ---------------------------------------------------------------------------


def test_shipped_skill_manifests_use_canonical_session_policy() -> None:
    """Every shipped skill manifest's ``session_policy`` is on the canonical set.

    The dispatcher accepts only
    :data:`~eawf.daemon.methods.agent.SessionPolicy`
    (``fresh`` / ``continue`` / ``hybrid``); an off-vocabulary value (e.g. the
    legacy ``reuse``) would never match the dispatch policy.
    """
    from eawf.daemon.methods.agent import SessionPolicy
    from eawf.workflow.skills.coauthor import MANIFEST as COAUTHOR_MANIFEST
    from eawf.workflow.skills.compress import MANIFEST as COMPRESS_MANIFEST
    from eawf.workflow.skills.memory import MANIFEST as MEMORY_MANIFEST
    from eawf.workflow.skills.wave_spec import MANIFEST as WAVE_SPEC_MANIFEST

    canonical = set(get_args(SessionPolicy))
    for manifest in (
        COMPRESS_MANIFEST,
        MEMORY_MANIFEST,
        COAUTHOR_MANIFEST,
        WAVE_SPEC_MANIFEST,
    ):
        policy = manifest.dispatch.get("session_policy")
        assert policy in canonical, f"{manifest.name} session_policy={policy!r} off-vocabulary"
