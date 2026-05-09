"""Render-output envelope (JSON ⇄ markdown) for tool/agent I/O.

Eä commands and skills exchange a structured envelope with three parts:

- ``header`` — small dict carrying ``skill``, ``scope``, ``session``,
  ``status``, ``instrument_probe`` (Phase 4 W01 will narrow these to
  typed Pydantic models; v0.1 keeps them as ``dict[str, Any]``).
- ``body`` — free-form markdown payload; whitespace is preserved
  byte-for-byte across the JSON ⇄ markdown round-trip.
- ``footer`` — sidecar dict with ``artifacts``, ``store_records``,
  ``mutations``, ``evidence``, ``next_actions``, ``warnings``.

The markdown wire-form interleaves a YAML frontmatter block (header), the
raw body, and an HTML comment block carrying the footer YAML so the
markdown is renderable as-is by any markdown viewer that ignores HTML
comments while still being losslessly recoverable. ``yaml.safe_dump`` is
called with ``sort_keys=True`` so the rendering is deterministic.

The two helpers — :func:`to_markdown` and :func:`from_markdown` — must be
exact inverses for any envelope built from primitive YAML-safe values:
``from_markdown(to_markdown(env)) == env`` and
``to_markdown(env) == to_markdown(from_markdown(to_markdown(env)))``.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict


class OutputEnvelope(BaseModel):
    """Three-part envelope for skill/agent output.

    Attributes:
        header: Free-form metadata (skill, scope, session, status,
            instrument_probe). Phase 4 W01 will narrow this with a
            dedicated Pydantic model.
        body: Markdown body. Leading and trailing whitespace are
            preserved exactly across the round-trip.
        footer: Sidecar fields (artifacts, store_records, mutations,
            evidence, next_actions, warnings).
    """

    model_config = ConfigDict(extra="forbid")

    header: dict[str, Any]
    body: str
    footer: dict[str, Any]


# Markers that frame the YAML frontmatter and the footer comment in the
# markdown wire-form. Kept as module-level constants so tests can grep
# for them without re-deriving the string literal.
_FRONTMATTER_FENCE: str = "---"
_FOOTER_OPEN: str = "<!-- eawf:footer"
_FOOTER_CLOSE: str = "-->"


def _dump_yaml(data: dict[str, Any]) -> str:
    """Deterministic YAML dump used for both header and footer.

    ``sort_keys=True`` is required for the byte-stable round-trip; we also
    set ``default_flow_style=False`` so nested containers render as block
    YAML (the more readable form).
    """
    return yaml.safe_dump(
        data,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    )


def _load_yaml(text: str) -> dict[str, Any]:
    """Load YAML and require a mapping at the top level.

    The envelope contract states header/footer are dicts. ``yaml.safe_load``
    happily returns ``None`` for empty input or a list/scalar for malformed
    input — we coerce ``None`` to an empty dict and reject anything else.
    """
    parsed = yaml.safe_load(text) if text.strip() else {}
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"expected YAML mapping for header/footer; got {type(parsed).__name__}")
    return parsed


def to_markdown(env: OutputEnvelope) -> str:
    """Serialise *env* into the markdown wire-form.

    Layout::

        ---
        <yaml.safe_dump(header)>
        ---
        <body>
        <!-- eawf:footer
        <yaml.safe_dump(footer)>
        -->

    The body is interpolated verbatim — including its leading and
    trailing whitespace — so a round-trip preserves the user-visible
    rendering byte-for-byte.
    """
    header_yaml = _dump_yaml(env.header)
    footer_yaml = _dump_yaml(env.footer)
    # ``yaml.safe_dump`` always terminates output with ``\n``; we rely on
    # that so the fence/marker layout below stays well-formed without
    # extra rstrip dance. The body is sandwiched with explicit ``\n``
    # markers we strip back out in :func:`from_markdown`.
    return (
        f"{_FRONTMATTER_FENCE}\n"
        f"{header_yaml}"
        f"{_FRONTMATTER_FENCE}\n"
        f"{env.body}"
        f"{_FOOTER_OPEN}\n"
        f"{footer_yaml}"
        f"{_FOOTER_CLOSE}\n"
    )


def from_markdown(text: str) -> OutputEnvelope:
    """Parse the markdown wire-form back into an :class:`OutputEnvelope`.

    Args:
        text: Markdown produced by :func:`to_markdown` (or hand-authored
            in the same shape).

    Raises:
        ValueError: ``text`` does not start with the frontmatter fence,
            is missing the closing fence, or lacks the footer comment
            block. The CLI handler maps these to :class:`ValidationFailed`.
    """
    open_fence = f"{_FRONTMATTER_FENCE}\n"
    if not text.startswith(open_fence):
        raise ValueError(f"missing frontmatter fence: input must start with {open_fence!r}")

    after_open = text[len(open_fence) :]
    close_marker = f"\n{_FRONTMATTER_FENCE}\n"
    close_idx = after_open.find(close_marker)
    if close_idx < 0:
        raise ValueError(f"missing closing frontmatter fence: expected {close_marker!r}")
    # ``close_idx`` is the index of the leading ``\n`` of the closing fence;
    # the YAML payload runs from start-of-after_open to that ``\n`` (the
    # ``\n`` belongs to ``yaml.safe_dump``'s trailing newline so we keep it).
    header_yaml = after_open[: close_idx + 1]
    after_header = after_open[close_idx + len(close_marker) :]

    footer_open_marker = f"{_FOOTER_OPEN}\n"
    footer_idx = after_header.rfind(footer_open_marker)
    if footer_idx < 0:
        raise ValueError(f"missing footer comment open marker: expected {footer_open_marker!r}")

    body = after_header[:footer_idx]
    after_footer_open = after_header[footer_idx + len(footer_open_marker) :]
    footer_close_marker = f"{_FOOTER_CLOSE}\n"
    if not after_footer_open.endswith(footer_close_marker):
        # Tolerate the no-trailing-newline variant for hand-authored input.
        if not after_footer_open.endswith(_FOOTER_CLOSE):
            raise ValueError(f"missing footer comment close marker: expected {_FOOTER_CLOSE!r}")
        footer_yaml = after_footer_open[: -len(_FOOTER_CLOSE)]
    else:
        footer_yaml = after_footer_open[: -len(footer_close_marker)]

    header = _load_yaml(header_yaml)
    footer = _load_yaml(footer_yaml)
    return OutputEnvelope(header=header, body=body, footer=footer)


__all__ = [
    "OutputEnvelope",
    "from_markdown",
    "to_markdown",
]
