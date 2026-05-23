"""``eawf render-output`` — convert between JSON and markdown envelopes.

Surface contract::

    eawf render-output --format markdown [--strict]   # JSON on stdin → markdown
    eawf render-output --format json     [--strict]   # markdown on stdin → JSON

The command is the operator-facing edge of :mod:`eawf.render.envelope`.
Skills emit a JSON envelope; chat-runtime adapters pipe it through this
command to produce a renderable markdown blob (and back again, e.g. to
parse a hand-authored response into the canonical JSON shape for the
state CLI).

``--strict`` is the loud-failure switch: malformed input raises
:class:`~eawf.cli.errors.ValidationError` (exit 4). Without ``--strict``
the same parse errors still raise (the spec only enumerates strict
behaviour); we keep the surface symmetric so callers don't have to
remember which side is permissive.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli._stdin import require_piped_stdin
from eawf.cli.flags import GlobalFlags

if TYPE_CHECKING:
    from eawf.render.envelope import OutputEnvelope

logger = logging.getLogger(__name__)

_FORMAT_MARKDOWN: str = "markdown"
_FORMAT_JSON: str = "json"
_VALID_FORMATS: frozenset[str] = frozenset({_FORMAT_MARKDOWN, _FORMAT_JSON})


def _emit_json(env: OutputEnvelope) -> None:
    """Print the JSON serialisation of *env* to stdout (newline-terminated)."""
    payload = env.model_dump()
    raw = orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    typer.echo(raw.decode("utf-8"))


def _emit_markdown(env: OutputEnvelope) -> None:
    """Print the markdown serialisation of *env* to stdout."""
    from eawf.render.envelope import to_markdown

    # ``to_markdown`` already terminates with ``\n``; we suppress
    # ``typer.echo``'s own newline so the output is byte-identical to
    # ``to_markdown(env)``.
    typer.echo(to_markdown(env), nl=False)


def render_output_cmd(
    ctx: typer.Context,
    format: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: 'markdown' (JSON in, markdown out) or "
            "'json' (markdown in, JSON out).",
            case_sensitive=False,
        ),
    ] = _FORMAT_MARKDOWN,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Reject malformed input with exit 4 instead of best-effort "
            "emission. The spec mandates this on the strict path.",
        ),
    ] = False,
) -> None:
    """Convert between the JSON and markdown forms of the output envelope."""
    from pydantic import ValidationError

    from eawf.render.envelope import OutputEnvelope, from_markdown

    flags: GlobalFlags = ctx.obj
    fmt = format.lower()
    if fmt not in _VALID_FORMATS:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--format must be one of {sorted(_VALID_FORMATS)}; got {format!r}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    require_piped_stdin("eawf render-output")
    stdin_text = sys.stdin.read()

    try:
        if fmt == _FORMAT_MARKDOWN:
            # JSON envelope on stdin → markdown on stdout.
            try:
                env = OutputEnvelope.model_validate_json(stdin_text)
            except ValidationError as exc:
                raise cli_errors.ValidationError(
                    f"input is not a valid OutputEnvelope JSON: {exc.errors()[0]['msg']}"
                ) from exc
            except (orjson.JSONDecodeError, ValueError) as exc:
                raise cli_errors.ValidationError(f"input is not valid JSON: {exc}") from exc
            _emit_markdown(env)
        else:
            # Markdown envelope on stdin → JSON on stdout.
            try:
                env = from_markdown(stdin_text)
            except (ValueError, ValidationError) as exc:
                raise cli_errors.ValidationError(
                    f"input is not a valid envelope markdown: {exc}"
                ) from exc
            _emit_json(env)
    except cli_errors.ValidationError as err:
        # ``--strict`` and the default both emit-and-exit on malformed
        # input; there is no permissive fallback that would still produce
        # a meaningful envelope, so we treat the two paths identically.
        # The flag is kept on the surface for forward compatibility with
        # phase-4 work that may add lenient warning emission.
        _ = strict
        cli_errors.emit_error(err, flags=flags)
        return


__all__ = [
    "render_output_cmd",
]
