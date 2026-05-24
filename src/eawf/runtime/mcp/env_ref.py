"""``${ENV:NAME}`` env-reference parser/renderer for MCP installs.

Why a dedicated module? Two security-critical disciplines apply:

1. **No expansion at install-time.** The MCP launcher (Claude Code) is
   responsible for resolving ``${ENV:NAME}`` at spawn time. Eä must
   never substitute the live ``os.environ`` value into anything that
   gets written to disk. A leak here means a secret lands in
   ``settings.json`` (and probably git, and probably PR diffs).
2. **No imports of ``os``.** This file is the canonical author of the
   env-ref token surface; it has no legitimate reason to reach
   ``os.environ``. The unit-test suite asserts the source has no
   ``import os`` line, so even an accidental copy/paste from a sibling
   module gets blocked at CI time.

The actual env-var lookup happens at MCP spawn — completely outside
this codebase.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# ``[A-Z_]`` first character then ``[A-Z0-9_]*`` — POSIX-ish identifier
# constraint. Lowercase is rejected on purpose: env vars in MCP configs
# are SCREAMING_SNAKE_CASE everywhere we've seen, and accepting mixed
# case would collide with the ``state.mcp_servers[*].env_refs`` regex
# already enforced by the Pydantic model
# (``src/eawf/state/models.py:356``). Match it byte-for-byte so the two
# layers cannot drift.
ENV_REF_RE: re.Pattern[str] = re.compile(r"^\$\{ENV:([A-Z_][A-Z0-9_]*)\}$")


class InvalidEnvRef(ValueError):  # noqa: N818 — domain term, not generic Error
    """A token does not satisfy :data:`ENV_REF_RE`."""


def parse_env_ref(token: str) -> str:
    """Return the env-var name embedded in *token*.

    Args:
        token: A string such as ``"${ENV:OPENAI_API_KEY}"``. Must match
            :data:`ENV_REF_RE` exactly — leading/trailing whitespace is
            *not* trimmed, missing prefix is rejected, lowercase is
            rejected.

    Returns:
        The bare env-var name (e.g. ``"OPENAI_API_KEY"``).

    Raises:
        InvalidEnvRef: The token does not match the canonical shape.
    """
    match = ENV_REF_RE.match(token)
    if match is None:
        raise InvalidEnvRef(
            f"invalid env-ref token {token!r}; expected ${{ENV:NAME}} where "
            "NAME matches [A-Z_][A-Z0-9_]*"
        )
    return match.group(1)


def render_env_block(env_refs: Sequence[str]) -> dict[str, str]:
    """Map each token in *env_refs* to ``{NAME: token-literal}``.

    The returned dict is exactly what the MCP launcher consumes:
    keys are the bare env-var names, values are the literal
    ``${ENV:NAME}`` tokens. **Values are never expanded** — this
    function does not consult ``os.environ`` and never reads any
    ambient environment.

    Args:
        env_refs: An iterable of canonical env-ref tokens. Duplicates
            are kept (the last write wins, which mirrors dict-insert
            semantics; callers who need de-dup do so before calling).

    Returns:
        A new dict mapping ``NAME`` → ``"${ENV:NAME}"``.

    Raises:
        InvalidEnvRef: Any token fails :func:`parse_env_ref`.
    """
    block: dict[str, str] = {}
    for token in env_refs:
        name = parse_env_ref(token)
        block[name] = token
    return block


def assert_no_expansion(env_block: Mapping[str, str]) -> None:
    """Defensive assertion that *env_block* values stayed literal.

    Run this immediately before persisting an env block to disk.
    A failure means somewhere upstream a token got resolved to its
    runtime value — that is the secret-leak scenario this module
    exists to prevent.

    Args:
        env_block: A mapping shaped like the output of
            :func:`render_env_block`.

    Raises:
        InvalidEnvRef: Any value diverges from the literal
            ``${ENV:NAME}`` form, OR any value's embedded NAME does
            not match the dict key.
    """
    for name, value in env_block.items():
        embedded = ENV_REF_RE.match(value)
        if embedded is None:
            raise InvalidEnvRef(
                f"env block entry {name!r} has non-literal value {value!r}; "
                "expansion of ${ENV:NAME} must not happen at install-time"
            )
        if embedded.group(1) != name:
            raise InvalidEnvRef(
                f"env block entry {name!r} maps to token "
                f"{value!r} whose embedded name does not match the key"
            )


__all__ = [
    "ENV_REF_RE",
    "InvalidEnvRef",
    "assert_no_expansion",
    "parse_env_ref",
    "render_env_block",
]
