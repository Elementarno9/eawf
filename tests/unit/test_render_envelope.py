"""Unit tests for :mod:`eawf.render.envelope`.

These tests pin the markdown wire-form: frontmatter fences, the
``<!-- eawf:footer ... -->`` HTML comment, body byte-stability, and the
malformed-input rejection path.
"""

from __future__ import annotations

import pytest

from eawf.render.envelope import OutputEnvelope, from_markdown, to_markdown


def _sample_env(**overrides: object) -> OutputEnvelope:
    base: dict[str, object] = {
        "header": {"skill": "research-spike", "scope": "PROJECT"},
        "body": "Hello, world.\n\nSecond paragraph.\n",
        "footer": {"artifacts": ["doc-1"], "warnings": []},
    }
    base.update(overrides)
    return OutputEnvelope(**base)  # type: ignore[arg-type]


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
    env = _sample_env(header={"skill": "audit", "scope": "ITER", "session": "s1"})
    parsed = from_markdown(to_markdown(env))
    assert parsed.header == {"skill": "audit", "scope": "ITER", "session": "s1"}


def test_from_markdown_parses_body_byte_stable() -> None:
    """Body whitespace — including leading + trailing newlines — survives."""
    body = "  leading-spaces\n\n  middle  \n\ntrailing-newline\n"
    env = _sample_env(body=body)
    parsed = from_markdown(to_markdown(env))
    assert parsed.body == body


def test_from_markdown_parses_footer() -> None:
    footer = {
        "artifacts": ["a", "b"],
        "store_records": [],
        "mutations": ["m-1"],
        "evidence": {},
        "next_actions": ["next"],
        "warnings": ["w"],
    }
    env = _sample_env(footer=footer)
    parsed = from_markdown(to_markdown(env))
    assert parsed.footer == footer


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
        OutputEnvelope(  # type: ignore[call-arg]
            header={},
            body="",
            footer={},
            unexpected="oops",  # type: ignore[call-arg]
        )


def test_to_markdown_is_deterministic_under_key_reorder() -> None:
    """``sort_keys=True`` makes the YAML byte-identical regardless of insertion order."""
    a = _sample_env(header={"a": 1, "b": 2})
    b = _sample_env(header={"b": 2, "a": 1})
    assert to_markdown(a) == to_markdown(b)
