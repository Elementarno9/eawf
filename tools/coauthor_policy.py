"""Shared co-author trailer policy for local git hooks."""

from __future__ import annotations

import re
from collections.abc import Mapping

CLAUDE_TRAILER: str = "Co-Authored-By: Claude <noreply@anthropic.com>"
CODEX_TRAILER: str = "Co-Authored-By: Codex <noreply@openai.com>"
TRAILER_REGISTRY: dict[str, str] = {
    "claude": CLAUDE_TRAILER,
    "codex": CODEX_TRAILER,
}
SUPPORTED_TRAILERS: tuple[str, ...] = tuple(TRAILER_REGISTRY.values())

OVERRIDE_ENV_VAR: str = "EAWF_COAUTHOR_HARNESS"
MODE_ENV_VAR: str = "EAWF_COAUTHOR_MODE"
_CLAUDE_ALIASES: frozenset[str] = frozenset({"claude", "claude-code", "anthropic"})
_CODEX_ALIASES: frozenset[str] = frozenset({"codex", "codex-cli"})
_COAUTHOR_LINE_RE = re.compile(r"^Co-Authored-By:\s+.+<[^>]+>\s*$", re.MULTILINE)


def _normalise_harness(value: str) -> str:
    return value.strip().casefold().replace("_", "-")


def _trailer_for_harness(value: str) -> str | None:
    harness = _normalise_harness(value)
    if harness in _CLAUDE_ALIASES:
        return TRAILER_REGISTRY["claude"]
    if harness in _CODEX_ALIASES:
        return TRAILER_REGISTRY["codex"]
    return None


def coauthor_disabled(env: Mapping[str, str]) -> bool:
    """Return whether local hook policy disables co-author trailers."""
    return env.get(MODE_ENV_VAR, "").strip().casefold() == "disabled"


def select_trailer(env: Mapping[str, str]) -> str | None:
    """Return the active harness trailer, or ``None`` when undetected."""
    if coauthor_disabled(env):
        return None
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


def has_any_coauthor_trailer(text: str) -> bool:
    """Return whether *text* contains any visible co-author trailer."""
    return _COAUTHOR_LINE_RE.search(text) is not None


def has_supported_trailer(text: str) -> bool:
    """Return whether *text* contains a recognized co-author trailer."""
    return any(
        line.strip() in SUPPORTED_TRAILERS
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )
