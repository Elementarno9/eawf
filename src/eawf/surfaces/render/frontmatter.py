"""YAML frontmatter scalar helpers shared across plugin renderers.

The Claude / Codex ``SKILL.md`` and agent templates and the opencode
command / agent renderers all emit a ``description:`` frontmatter line.
A description that contains a ``: `` (colon-space) parses as a nested
mapping when emitted unquoted, so a strict YAML loader (e.g. Codex's
plugin loader) rejects the whole frontmatter. Every emit site routes the
value through :func:`yaml_scalar` so the on-disk frontmatter is always a
valid double-quoted scalar.
"""

from __future__ import annotations

import json


def yaml_scalar(value: str) -> str:
    """Quote *value* as a YAML-safe double-quoted flow scalar.

    A JSON string literal is also a valid YAML double-quoted scalar, so
    :func:`json.dumps` gives correct escaping for embedded quotes,
    backslashes, control characters, and — the case that motivates this
    helper — a ``: `` (colon-space) that would otherwise make an unquoted
    value parse as a nested mapping. Mirrors the ``json.dumps``-for-keys
    idiom in the opencode YAML writer.

    Args:
        value: The raw scalar text (e.g. a skill or agent description).

    Returns:
        The value wrapped in double quotes with YAML-safe escaping.
    """
    return json.dumps(value, ensure_ascii=False)


__all__ = ["yaml_scalar"]
