"""Shared co-author trailer policy for local git hooks."""

from __future__ import annotations

from collections.abc import Mapping

CLAUDE_TRAILER: str = "Co-Authored-By: Claude <noreply@anthropic.com>"
CODEX_TRAILER: str = "Co-Authored-By: Codex <noreply@openai.com>"
SUPPORTED_TRAILERS: tuple[str, ...] = (CLAUDE_TRAILER, CODEX_TRAILER)

OVERRIDE_ENV_VAR: str = "EAWF_COAUTHOR_HARNESS"
_CLAUDE_ALIASES: frozenset[str] = frozenset({"claude", "claude-code", "anthropic"})
_CODEX_ALIASES: frozenset[str] = frozenset({"codex", "codex-cli"})


def _normalise_harness(value: str) -> str:
    return value.strip().casefold().replace("_", "-")


def _trailer_for_harness(value: str) -> str | None:
    harness = _normalise_harness(value)
    if harness in _CLAUDE_ALIASES:
        return CLAUDE_TRAILER
    if harness in _CODEX_ALIASES:
        return CODEX_TRAILER
    return None


def select_trailer(env: Mapping[str, str]) -> str | None:
    """Return the active harness trailer, or ``None`` when undetected."""
    override = env.get(OVERRIDE_ENV_VAR)
    if override is not None:
        trailer = _trailer_for_harness(override)
        if trailer is not None:
            return trailer
    if any(key.startswith("CLAUDE") for key in env):
        return CLAUDE_TRAILER
    if any(key.startswith("CODEX") for key in env):
        return CODEX_TRAILER
    return None


def has_supported_trailer(text: str) -> bool:
    """Return whether *text* contains a recognized co-author trailer."""
    return any(trailer in text for trailer in SUPPORTED_TRAILERS)
