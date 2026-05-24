"""Unit tests for :mod:`eawf.surfaces.render.envelope`.

These tests pin the markdown wire-form: frontmatter fences, the
``<!-- eawf:footer ... -->`` HTML comment, body byte-stability, and the
malformed-input rejection path. Phase 4 W01 narrows the header/footer
to typed Pydantic models; the test fixtures here use the typed shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.surfaces.render.envelope import OutputEnvelope, from_markdown, to_markdown


def _sample_env(**overrides: object) -> OutputEnvelope:
    """Build a typed :class:`OutputEnvelope` with sensible defaults.

    The header uses the canonical :data:`SkillName` literal ``"/research"``
    and stub URN-shaped scope/session strings so the typed model
    validation passes. Tests that need to vary specific fields pass
    ``header=...`` / ``body=...`` / ``footer=...`` overrides through
    the kwargs.
    """
    base: dict[str, Any] = {
        "header": {
            "skill": "/research",
            "scope_id": "urn:eawf:v1:state:QR/P00",
            "session": "urn:eawf:v1:store:QR/sessions/SES-001",
            "started_at": "2026-05-09T00:00:00Z",
            "finished_at": "2026-05-09T00:00:01Z",
            "status": "ok",
            "instrument_probe": {"git": "ok"},
        },
        "body": "Hello, world.\n\nSecond paragraph.\n",
        "footer": {
            "persisted_artifacts": ["urn:eawf:v1:artifact:QR/A1"],
            "warnings": [],
        },
    }
    base.update(overrides)
    return OutputEnvelope.model_validate(base)


def test_envelope_to_markdown_emits_frontmatter() -> None:
    out = to_markdown(_sample_env())
    assert out.startswith("---\n"), "output must open with the frontmatter fence"
    # Two ``---\n`` fences (open + close); the footer marker is unrelated.
    assert out.count("---\n") >= 2


def test_envelope_to_markdown_includes_footer_comment() -> None:
    out = to_markdown(_sample_env())
    assert "<!-- eawf:footer\n" in out, "footer block must use the canonical marker"
    assert "-->\n" in out, "footer block must close with -->"


def test_from_markdown_parses_header() -> None:
    env = _sample_env(
        header={
            "skill": "/audit",
            "scope_id": "urn:eawf:v1:state:QR/P13-I04",
            "session": "urn:eawf:v1:store:QR/sessions/SES-2",
            "started_at": "2026-05-09T00:00:00Z",
            "finished_at": "2026-05-09T00:00:02Z",
            "status": "ok",
            "instrument_probe": {},
        }
    )
    parsed = from_markdown(to_markdown(env))
    assert parsed.header.skill == "/audit"
    assert parsed.header.scope_id == "urn:eawf:v1:state:QR/P13-I04"
    assert parsed.header.session == "urn:eawf:v1:store:QR/sessions/SES-2"
    assert parsed.header.status == "ok"


def test_from_markdown_parses_body_byte_stable() -> None:
    """Body whitespace — including leading + trailing newlines — survives."""
    body = "  leading-spaces\n\n  middle  \n\ntrailing-newline\n"
    env = _sample_env(body=body)
    parsed = from_markdown(to_markdown(env))
    assert parsed.body == body


def test_from_markdown_parses_footer() -> None:
    footer = {
        "persisted_artifacts": ["a", "b"],
        "persisted_store_records": [],
        "state_mutations": ["m-1"],
        "evidence_refs": ["ev-1"],
        "next_valid_actions": ["next"],
        "warnings": [{"code": "w", "detail": "warn detail"}],
    }
    env = _sample_env(footer=footer)
    parsed = from_markdown(to_markdown(env))
    assert parsed.footer.persisted_artifacts == ["a", "b"]
    assert parsed.footer.state_mutations == ["m-1"]
    assert parsed.footer.evidence_refs == ["ev-1"]
    assert parsed.footer.next_valid_actions == ["next"]
    assert len(parsed.footer.warnings) == 1
    assert parsed.footer.warnings[0].code == "w"
    assert parsed.footer.warnings[0].detail == "warn detail"


def test_from_markdown_rejects_missing_frontmatter() -> None:
    """No leading ``---\\n`` fence → ValueError; CLI maps it to exit 4."""
    with pytest.raises(ValueError, match="frontmatter fence"):
        from_markdown("body without any frontmatter\n")


def test_from_markdown_rejects_missing_close_fence() -> None:
    """Open fence but no close fence raises a different ValueError branch."""
    bad = "---\nskill: x\nbody and no close fence\n"
    with pytest.raises(ValueError, match="closing frontmatter"):
        from_markdown(bad)


def test_from_markdown_rejects_missing_footer_marker() -> None:
    """Frontmatter present but the eawf:footer marker is absent."""
    bad = "---\nskill: x\n---\nbody only\n"
    with pytest.raises(ValueError, match="footer comment open marker"):
        from_markdown(bad)


def test_extra_field_on_envelope_rejected() -> None:
    """``extra='forbid'`` blocks unknown top-level keys."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OutputEnvelope.model_validate(
            {
                "header": {
                    "skill": "/research",
                    "scope_id": "urn:eawf:v1:state:QR",
                    "session": "urn:eawf:v1:store:QR/sessions/SES-1",
                    "started_at": "2026-05-09T00:00:00Z",
                    "finished_at": "2026-05-09T00:00:01Z",
                    "status": "ok",
                    "instrument_probe": {},
                },
                "body": "",
                "footer": {},
                "unexpected": "oops",
            }
        )


def test_extra_field_on_header_rejected() -> None:
    """``EnvelopeHeader`` rejects unknown keys (schema-version drift guard)."""
    from pydantic import ValidationError

    from eawf.surfaces.render.envelope import EnvelopeHeader

    with pytest.raises(ValidationError):
        EnvelopeHeader.model_validate(
            {
                "skill": "/research",
                "scope_id": "urn:eawf:v1:state:QR",
                "session": "urn:eawf:v1:store:QR/sessions/SES-1",
                "started_at": "2026-05-09T00:00:00Z",
                "finished_at": "2026-05-09T00:00:01Z",
                "status": "ok",
                "instrument_probe": {},
                "schema_version": "2.0",  # drifted/unknown field
            }
        )


def test_extra_field_on_footer_rejected() -> None:
    """``EnvelopeFooter`` rejects unknown keys (schema-version drift guard)."""
    from pydantic import ValidationError

    from eawf.surfaces.render.envelope import EnvelopeFooter

    with pytest.raises(ValidationError):
        EnvelopeFooter.model_validate({"unexpected_footer_key": []})


def test_envelope_status_enum_has_exactly_five_members() -> None:
    """The frozen ``EnvelopeStatus`` Literal carries the five success-criterion values."""
    from typing import get_args

    from eawf.surfaces.render.envelope import EnvelopeStatus

    members = set(get_args(EnvelopeStatus))
    assert members == {"ok", "needs_user", "blocked", "failed", "partial"}


def test_to_markdown_is_deterministic_under_key_reorder() -> None:
    """``sort_keys=True`` makes the YAML byte-identical regardless of insertion order."""
    a = _sample_env(
        header={
            "skill": "/research",
            "scope_id": "urn:eawf:v1:state:QR",
            "session": "urn:eawf:v1:store:QR/sessions/SES-1",
            "started_at": "2026-05-09T00:00:00Z",
            "finished_at": "2026-05-09T00:00:01Z",
            "status": "ok",
            "instrument_probe": {"git": "ok", "gh": "missing"},
        }
    )
    b = _sample_env(
        header={
            "skill": "/research",
            "scope_id": "urn:eawf:v1:state:QR",
            "session": "urn:eawf:v1:store:QR/sessions/SES-1",
            "started_at": "2026-05-09T00:00:00Z",
            "finished_at": "2026-05-09T00:00:01Z",
            "status": "ok",
            "instrument_probe": {"gh": "missing", "git": "ok"},
        }
    )
    assert to_markdown(a) == to_markdown(b)
