"""``runtime.run.content.read``: the bounded text a Run's transcript blocks unfold to.

A transcript block names what it holds by reference -- a tool result by its call or
receipt, a diff or a trace by its artifact -- and the console unfolds the block by
reading those references back here. Every answer is read through the scrubbed store:
stored content was bounded and scrubbed when it was filed, and a brokered call's typed
output, which the gateway keeps on its receipt, is bounded and scrubbed as it is read.
A reference this Run holds nothing under is named as missing rather than failing the
read, so one unreadable block never blanks the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from eawf.kernel.runtime.content import ResolvedContent, StoredContent, bound_content
from eawf.kernel.runtime.semantic import SemanticResult
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.runtime.daemon.content_store import stored_contents
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.host_calls import host_receipts
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.native_guard import native_params, require_native_call
from eawf.runtime.daemon.semantic_gateway import run_receipts

logger = logging.getLogger(__name__)

#: Read the content a Run's transcript references resolve to.
RUN_CONTENT_READ_METHOD: Final = "runtime.run.content.read"

#: The most references one read resolves.
CONTENT_READ_REFS: Final = 256

_Ref = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class RunContentRead(BaseModel):
    """What the read is asked for.

    Attributes:
        urn: The Run whose transcript names the references.
        refs: The references to resolve: artifact references, call ids or receipt ids.
    """

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn
    refs: Annotated[tuple[_Ref, ...], Field(min_length=1, max_length=CONTENT_READ_REFS)]


class RunContentAnswer(BaseModel):
    """What the read answers with.

    Attributes:
        contents: The content each resolved reference names, in the order asked.
        missing: The references this Run holds nothing under.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contents: tuple[ResolvedContent, ...] = ()
    missing: tuple[str, ...] = ()


def _stored(ref: str, content: StoredContent) -> ResolvedContent:
    return ResolvedContent(
        ref=ref,
        lines=content.lines,
        total_lines=content.total_lines,
        withheld_lines=content.withheld_lines,
    )


def _from_result(
    ref: str, result: SemanticResult, stored: dict[str, StoredContent]
) -> ResolvedContent | None:
    """Return what one gateway answer holds: its stored output or trace, else its own words."""
    error = result.error
    artifact = result.output_ref if error is None else error.diagnostic_ref
    if artifact is not None:
        held = stored.get(artifact)
        return None if held is None else _stored(ref, held)
    if error is not None:
        text = error.message
    elif result.bounded_output is not None:
        text = json.dumps(result.bounded_output.model_dump(mode="json"), indent=2)
    else:
        return None
    bounded = bound_content(text)
    return ResolvedContent(
        ref=ref,
        lines=bounded.lines,
        total_lines=bounded.total_lines,
        withheld_lines=bounded.withheld_lines,
    )


def resolve_contents(context: Epoch2RootContext, args: RunContentRead) -> RunContentAnswer:
    """Resolve each reference one Run's transcript names to its bounded content.

    Args:
        context: The native context of the root the Run belongs to.
        args: The Run and the references.

    Returns:
        The resolved contents and the references nothing was held under.
    """
    with context.session([args.urn]) as session:
        stored = stored_contents(session, args.urn)
        results: dict[str, SemanticResult] = {}
        for receipt in (*run_receipts(session, args.urn), *host_receipts(session, args.urn)):
            results[receipt.call_id] = results[receipt.receipt_id] = receipt.result
    contents: list[ResolvedContent] = []
    missing: list[str] = []
    for ref in dict.fromkeys(args.refs):
        if ref in stored:
            resolved: ResolvedContent | None = _stored(ref, stored[ref])
        elif ref in results:
            resolved = _from_result(ref, results[ref], stored)
        else:
            resolved = None
        if resolved is None:
            missing.append(ref)
        else:
            contents.append(resolved)
    logger.debug(
        f"resolve_contents run={args.urn.entity_key} held={len(contents)} missing={len(missing)}"
    )
    return RunContentAnswer(contents=tuple(contents), missing=tuple(missing))


@register(RUN_CONTENT_READ_METHOD)
async def _read_run_content(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the references one Run's transcript names to their bounded content."""
    authority = require_native_call(ctx, params)
    args = native_params(RunContentRead, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(resolve_contents, context, args)
    return answer.model_dump(mode="json")


__all__ = [
    "CONTENT_READ_REFS",
    "RUN_CONTENT_READ_METHOD",
    "RunContentAnswer",
    "RunContentRead",
    "resolve_contents",
]
