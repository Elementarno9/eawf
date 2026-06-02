"""Tests for the ``codex`` runtime in the dispatch envelope renderer (P28-I03-W57).

Pre-W57 :data:`eawf.workflow.dispatch.renderer.DISPATCH_RUNTIMES` listed
only ``claude-code`` and ``claude-agent-sdk``; a codex-runtime wave
could not render a dispatch envelope. These tests pin the fix: codex
shares the claude-code envelope shape (single-string prompt body, no
SDK MCP wiring on the envelope — codex reads its config from
``.codex/config.toml`` per D12).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import ProjectStatus, ScopeKind
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.workflow.dispatch import render_dispatch_envelope, render_wave_prompt
from eawf.workflow.dispatch.renderer import DISPATCH_RUNTIMES, DispatchEnvelope
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave


def _empty_state() -> State:
    """Return a minimal :class:`State` for envelope-rendering tests."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _seed_single_wave(state: State, wave_id: str = "P01-I01-W01") -> None:
    """Seed P01 → P01-I01 → *wave_id*. The wave has no deps."""
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id="P01-I01",
        title="Solo wave",
        file_scopes=["src/"],
        effort_bucket="M",
    )


def test_dispatch_runtimes_includes_codex() -> None:
    """The exported allow-list now carries ``codex`` alongside the claude entries."""
    assert "codex" in DISPATCH_RUNTIMES
    assert "claude-code" in DISPATCH_RUNTIMES
    assert "claude-agent-sdk" in DISPATCH_RUNTIMES


def test_dispatch_runtimes_includes_opencode() -> None:
    """The allow-list carries ``opencode`` (P29-I04-W15) so its spawn is reachable."""
    assert "opencode" in DISPATCH_RUNTIMES


def test_opencode_envelope_renders_same_prompt_as_claude_code() -> None:
    """The opencode envelope mirrors claude-code: same prompt body, empty MCP wiring.

    opencode shares the claude-code envelope shape (prompt carries the full
    body; MCP wiring rides through ``opencode.json``, not the envelope), so the
    live-spawn renderer reaches the opencode lane the same way.
    """
    state = _empty_state()
    _seed_single_wave(state)
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "opencode")
    assert isinstance(envelope, DispatchEnvelope)
    assert envelope.runtime == "opencode"
    assert envelope.prompt == render_wave_prompt(state, "P01-I01-W01")
    assert envelope.mcp_servers == []
    assert envelope.allowed_tools == []


def test_codex_envelope_uses_claude_code_shape() -> None:
    """The codex envelope mirrors claude-code: prompt body, empty MCP wiring."""
    state = _empty_state()
    _seed_single_wave(state)
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "codex")
    assert isinstance(envelope, DispatchEnvelope)
    assert envelope.runtime == "codex"
    assert envelope.wave_id == "P01-I01-W01"
    # Body byte-equal to render_wave_prompt — the codex branch must not
    # mutate the prompt, mirroring the claude-code branch invariant.
    assert envelope.prompt == render_wave_prompt(state, "P01-I01-W01")
    # MCP wiring stays off the envelope — codex reads its config from
    # ``.codex/config.toml``, not from the dispatch envelope.
    assert envelope.mcp_servers == []
    assert envelope.allowed_tools == []


def test_codex_envelope_renders_same_prompt_as_claude_code() -> None:
    """The codex envelope prompt is byte-equal to the claude-code envelope prompt."""
    state = _empty_state()
    _seed_single_wave(state)
    codex_env = render_dispatch_envelope(state, "P01-I01-W01", "codex")
    claude_env = render_dispatch_envelope(state, "P01-I01-W01", "claude-code")
    assert codex_env.prompt == claude_env.prompt


def test_unknown_runtime_error_message_lists_codex_among_supported() -> None:
    """The unsupported-runtime error names every supported entry.

    ``opencode`` joined :data:`DISPATCH_RUNTIMES` (P29-I04-W15), so the
    unsupported example here is a genuinely-unknown runtime; the error must
    still name every supported entry including codex + opencode.
    """
    state = _empty_state()
    _seed_single_wave(state)
    with pytest.raises(ValueError) as excinfo:
        render_dispatch_envelope(state, "P01-I01-W01", "gemini")
    msg = str(excinfo.value)
    assert "codex" in msg
    assert "opencode" in msg
    assert "claude-code" in msg
    assert "claude-agent-sdk" in msg


def test_codex_envelope_unknown_wave_propagates_key_error() -> None:
    """A missing wave id propagates :class:`KeyError` on the codex branch too."""
    state = _empty_state()
    _seed_single_wave(state)
    with pytest.raises(KeyError, match="unknown wave"):
        render_dispatch_envelope(state, "P01-I01-W99", "codex")
