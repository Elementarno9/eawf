"""Coverage-lift tests for :mod:`eawf.runtime.vcs.coauthor`.

Targets the validator + resolver branches not exercised by
``test_vcs_coauthor.py``: empty/normalised runtime keys, the
``require_trailer`` toggle, env-var runtime detection, and the
disabled/project return paths.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.runtime.vcs.coauthor import (
    CoauthorConfig,
    CoauthorIdentity,
    CoauthorPolicyError,
    has_any_coauthor_trailer,
    resolve_coauthor_trailer,
)

# --- CoauthorIdentity ----------------------------------------------------


def test_identity_trailer_renders_canonical_line() -> None:
    ident = CoauthorIdentity(name="Claude", email="noreply@anthropic.com")
    assert ident.trailer() == "Co-Authored-By: Claude <noreply@anthropic.com>"


def test_identity_rejects_malformed_email() -> None:
    with pytest.raises(ValidationError):
        CoauthorIdentity(name="X", email="not-an-email")


def test_identity_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        CoauthorIdentity(name="", email="noreply@anthropic.com")


# --- CoauthorConfig validators ------------------------------------------


def test_config_rejects_empty_default_runtime() -> None:
    with pytest.raises(ValidationError, match="default_runtime must not be empty"):
        CoauthorConfig(default_runtime="   ")


def test_config_normalises_trailer_keys() -> None:
    config = CoauthorConfig(
        trailers={
            "My_Bot": CoauthorIdentity(name="Bot", email="noreply@anthropic.com"),
            "claude": CoauthorIdentity(name="Claude", email="noreply@anthropic.com"),
        },
        default_runtime="claude",
    )
    # Key normalised to ``my-bot`` (casefold + underscore->dash); the raw
    # ``My_Bot`` form is gone.
    assert "my-bot" in config.trailers
    assert "My_Bot" not in config.trailers


def test_config_rejects_empty_trailer_key() -> None:
    with pytest.raises(ValidationError, match="trailer runtime key must not be empty"):
        CoauthorConfig(
            trailers={"  ": CoauthorIdentity(name="X", email="noreply@anthropic.com")},
        )


def test_config_runtime_mode_requires_configured_default_trailer() -> None:
    with pytest.raises(ValidationError, match="has no configured trailer"):
        CoauthorConfig(
            mode="runtime",
            default_runtime="ghost-runtime",
            trailers={"claude": CoauthorIdentity(name="Claude", email="noreply@anthropic.com")},
        )


# --- resolve_coauthor_trailer -------------------------------------------


def test_resolve_disabled_returns_none_without_text() -> None:
    config = CoauthorConfig(mode="disabled")
    assert resolve_coauthor_trailer(config) is None


def test_resolve_disabled_returns_none_with_clean_text() -> None:
    config = CoauthorConfig(mode="disabled")
    assert resolve_coauthor_trailer(config, message_text="subject\n\nbody only\n") is None


def test_resolve_project_returns_project_trailer() -> None:
    email = "bot" + "@" + "example" + ".org"
    config = CoauthorConfig(mode="project", project=CoauthorIdentity(name="Bot", email=email))
    assert resolve_coauthor_trailer(config) == f"Co-Authored-By: Bot <{email}>"


def test_resolve_project_missing_identity_raises_policy_error() -> None:
    """The defensive guard fires when ``project`` is cleared post-construction.

    The model validator rejects ``mode='project'`` with ``project=None`` at
    build time, so we construct a valid config then null the field to drive
    the resolver's runtime guard.
    """
    config = CoauthorConfig(
        mode="project",
        project=CoauthorIdentity(name="Bot", email="noreply@anthropic.com"),
    )
    config.project = None
    with pytest.raises(CoauthorPolicyError, match="project co-author identity is not configured"):
        resolve_coauthor_trailer(config)


def test_resolve_runtime_detects_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-var runtime detection routes through the explicit opt-in resolver."""
    monkeypatch.setattr(
        "eawf.runtime.runtimes.coauthor.resolve_runtime_explicit",
        lambda env: "codex",
    )
    config = CoauthorConfig()
    trailer = resolve_coauthor_trailer(config, env={"EAWF_RUNTIME": "codex"})
    assert trailer == "Co-Authored-By: Codex <noreply@openai.com>"


def test_resolve_runtime_env_none_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eawf.runtime.runtimes.coauthor.resolve_runtime_explicit",
        lambda env: None,
    )
    config = CoauthorConfig(default_runtime="claude")
    trailer = resolve_coauthor_trailer(config, env={})
    assert trailer == "Co-Authored-By: Claude <noreply@anthropic.com>"


def test_resolve_unknown_runtime_require_trailer_raises() -> None:
    config = CoauthorConfig(require_trailer=True)
    with pytest.raises(CoauthorPolicyError, match="no co-author trailer configured"):
        resolve_coauthor_trailer(config, runtime="ghost")


def test_resolve_unknown_runtime_no_require_returns_none() -> None:
    config = CoauthorConfig(require_trailer=False)
    assert resolve_coauthor_trailer(config, runtime="ghost") is None


# --- has_any_coauthor_trailer -------------------------------------------


def test_has_any_coauthor_trailer_detects_line() -> None:
    text = "subject\n\nbody\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
    assert has_any_coauthor_trailer(text) is True


def test_has_any_coauthor_trailer_false_when_absent() -> None:
    assert has_any_coauthor_trailer("subject\n\nbody only\n") is False
