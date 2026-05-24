"""Wave read / dispatch / budget command handlers.

Split out of :mod:`eawf.cli.commands.lifecycle` (P27-W06). The ``wave_app``
and ``wave_budget_app`` Typer apps and the shared transaction helpers live
in the parent module; this module attaches the read-only DAG verbs
(graph / next-ready / blocks-rebuild), the dispatch verbs
(dispatch / dispatch-batch), and the wave-budget verbs
(set / consume / show) via ``@wave_app.command(...)`` /
``@wave_budget_app.command(...)``. The wave mutators live in
:mod:`eawf.cli.commands.lifecycle_wave`.
"""

from __future__ import annotations

import bisect
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.commands.lifecycle import (
    _atomic_write_text,
    _load_state_readonly,
    _read_state_payload,
    _resolve_iter_for_query,
    _run_mutation,
    _write_state_unlocked,
    wave_app,
    wave_budget_app,
)
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.ids import is_wave_id
from eawf.lock import portalock

if TYPE_CHECKING:
    from eawf.dispatch import DispatchEnvelope
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


# ---- Wave DAG read-only verbs (B026) ---------------------------------------


_WAVE_STATUS_EMOJI: dict[WaveStatus, str] = {
    WaveStatus.PENDING: "⏳",  # hourglass
    WaveStatus.CLAIMED: "🚧",  # construction
    WaveStatus.IN_PROGRESS: "🚧",
    WaveStatus.CLOSED: "✅",  # white heavy check mark
    WaveStatus.FAILED: "❌",  # cross mark
    WaveStatus.ABANDONED: "❌",
}


def _topo_order_with_depth(waves: list[tuple[str, list[str]]]) -> list[tuple[str, int]]:
    """Topo-sort *waves* and assign each node its longest-path depth.

    Each entry is ``(wave_id, deps_in_iter)`` — deps that point outside
    *waves* are ignored. The output preserves topological order; nodes
    at the same depth are emitted in ascending id order.
    """
    ids = [wid for wid, _ in waves]
    id_set = set(ids)
    deps_in: dict[str, list[str]] = {wid: [d for d in deps if d in id_set] for wid, deps in waves}
    children: dict[str, list[str]] = {wid: [] for wid in ids}
    for wid, deps in deps_in.items():
        for d in deps:
            children[d].append(wid)
    in_degree = {wid: len(deps_in[wid]) for wid in ids}
    depth: dict[str, int] = dict.fromkeys(ids, 0)
    # Kahn with deterministic id-sorted ready queue.
    ready = sorted([wid for wid, deg in in_degree.items() if deg == 0])
    order: list[tuple[str, int]] = []
    while ready:
        # Pop deterministically: smallest id at the current frontier.
        node = ready.pop(0)
        order.append((node, depth[node]))
        for child in sorted(children[node]):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                depth[child] = max(depth[child], depth[node] + 1)
                # Insert in sort order so the next pop stays deterministic.
                bisect.insort(ready, child)
            else:
                depth[child] = max(depth[child], depth[node] + 1)
    # Any nodes left unprocessed (cycles) get appended at the end in id order
    # — defensive: ``plan_wave`` rejects cycles, so this branch is unreachable
    # for state.json produced by the state CLI alone.
    remaining = sorted(wid for wid in ids if wid not in {n for n, _ in order})
    for wid in remaining:
        order.append((wid, depth[wid]))
    return order


@wave_app.command("graph")
def wave_graph_cmd(
    ctx: typer.Context,
    iter_flag: Annotated[
        str | None,
        typer.Option("--iter", help="Iter ID to graph (defaults to current iter)."),
    ] = None,
) -> None:
    """Print the wave DAG for an iter in topological order.

    Each row is ``<emoji> <wave-id> <title-truncated-60> blocks=[...]
    blocked_by=[...]`` indented two spaces per topo-depth level. Sort
    order: topo first, then ascending wave id at each frontier.
    """
    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, flags = loaded
    target_iter = _resolve_iter_for_query(state, flags, iter_flag=iter_flag)
    if target_iter is None:
        return
    rows = [(wid, w) for wid, w in state.waves.items() if w.iter_id == target_iter]
    rows.sort(key=lambda kv: kv[0])
    deps_pairs = [(wid, list(w.deps)) for wid, w in rows]
    order = _topo_order_with_depth(deps_pairs)
    wave_by_id = dict(rows)
    json_rows: list[dict[str, Any]] = []
    text_lines: list[str] = []
    for wid, depth in order:
        w = wave_by_id[wid]
        emoji = _WAVE_STATUS_EMOJI.get(w.status, "?")
        title = w.title if len(w.title) <= 60 else w.title[:57] + "..."
        indent = "  " * depth
        text_lines.append(
            f"{indent}{emoji} {wid} {title} blocks={list(w.blocks)} blocked_by={list(w.deps)}"
        )
        json_rows.append(
            {
                "id": wid,
                "status": w.status.value,
                "title": w.title,
                "depth": depth,
                "blocks": list(w.blocks),
                "blocked_by": list(w.deps),
            }
        )
    payload: dict[str, Any] = {"iter": target_iter, "waves": json_rows}
    text = "\n".join(text_lines) if text_lines else f"iter {target_iter}: no waves"
    emit_json_or_text(payload, text, flags=flags)


@wave_app.command("next-ready")
def wave_next_ready_cmd(
    ctx: typer.Context,
    iter_flag: Annotated[
        str | None,
        typer.Option("--iter", help="Iter ID to inspect (defaults to current iter)."),
    ] = None,
) -> None:
    """List pending waves whose every dep is ``closed``.

    Failed deps do NOT make a child ready — children of failed deps are
    surfaced in the ``blocked_by_failure`` section so the operator can
    decide whether to re-plan or unblock manually.
    """
    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, flags = loaded
    target_iter = _resolve_iter_for_query(state, flags, iter_flag=iter_flag)
    if target_iter is None:
        return
    ready: list[str] = []
    blocked_by_failure: list[str] = []
    for wid, w in sorted(state.waves.items()):
        if w.iter_id != target_iter:
            continue
        if w.status != WaveStatus.PENDING:
            continue
        dep_waves = [state.waves[d] for d in w.deps if d in state.waves]
        if any(dw.status == WaveStatus.FAILED for dw in dep_waves):
            blocked_by_failure.append(wid)
            continue
        if all(dw.status == WaveStatus.CLOSED for dw in dep_waves):
            ready.append(wid)
    payload: dict[str, Any] = {
        "iter": target_iter,
        "ready": ready,
        "blocked_by_failure": blocked_by_failure,
    }
    text_lines = [f"ready: {ready}"]
    if blocked_by_failure:
        text_lines.append(f"blocked by failure: {blocked_by_failure}")
    emit_json_or_text(payload, "\n".join(text_lines), flags=flags)


@wave_app.command("blocks-rebuild")
def wave_blocks_rebuild_cmd(
    ctx: typer.Context,
    apply_all: Annotated[
        bool,
        typer.Option("--all", help="Rebuild blocks for every wave (vs no-op)."),
    ] = False,
) -> None:
    """Rebuild ``Wave.blocks`` reverse-index from sister waves' ``deps``.

    Legacy fix-up: waves planned BEFORE the W02 (B026) feature landed
    do not have their ``blocks`` list maintained. This verb walks
    ``state.waves`` and rewrites each wave's ``blocks`` to ``[
    child_id for child in state.waves.values() if wave_id in child.deps
    ]``, sorted.

    After the rewrite, primes the typed :class:`WaveDagEdges` cache
    (P20-W15 / B026) by validating every wave's DAG edges through
    :func:`eawf.kernel.state.wave_graph.edges`. The validation exposes the
    typed deps/blocks/blocked_by triple on the payload so downstream
    consumers (the TUI wave-board in W03) can confirm the rebuild
    landed against the canonical typed surface, not the inline list.
    """
    from eawf.kernel.state import wave_graph
    from eawf.kernel.state.models import State

    flags: GlobalFlags = ctx.obj
    if not apply_all:
        cli_errors.emit_error(
            cli_errors.UserError(
                "pass --all to rebuild every wave's blocks index", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return

    rewritten: list[dict[str, Any]] = []
    edge_summary: list[dict[str, Any]] = []
    with portalock.acquire(state_path, timeout=5.0):
        raw = state_path.read_bytes()
        payload = orjson.loads(raw)
        state = State.model_validate(payload)
        for wave_id, wave in state.waves.items():
            new_blocks = sorted(
                child_id for child_id, child in state.waves.items() if wave_id in child.deps
            )
            if list(wave.blocks) != new_blocks:
                rewritten.append({"id": wave_id, "from": list(wave.blocks), "to": new_blocks})
                wave.blocks = new_blocks
        if rewritten:
            new_payload = state.model_dump(mode="json")
            _write_state_unlocked(state_path, new_payload)
        # Prime the typed-edges cache view post-rewrite. We rebuild
        # the State document from the on-disk payload (if we wrote)
        # so the typed view is exactly what readers will load.
        if rewritten:
            raw = state_path.read_bytes()
            payload = orjson.loads(raw)
            state = State.model_validate(payload)
        for wave_id in sorted(state.waves):
            view = wave_graph.edges(wave_id, state)
            edge_summary.append(
                {
                    "id": view.wave_id,
                    "deps": list(view.deps),
                    "blocks": list(view.blocks),
                    "blocked_by": list(view.blocked_by),
                }
            )

    emit_json_or_text(
        {
            "rewritten": rewritten,
            "count": len(rewritten),
            "edges": edge_summary,
        },
        f"wave blocks-rebuild: rewrote {len(rewritten)} wave(s)",
        flags=flags,
    )


# ---- Wave dispatch (subagent prompt rendering, B025) ------------------------


def _waves_in_iter(state: State, iter_id: str) -> list[tuple[str, Any]]:
    """Return (wave_id, Wave) pairs in id-ascending order for *iter_id*."""
    return sorted(
        ((wid, w) for wid, w in state.waves.items() if w.iter_id == iter_id),
        key=lambda kv: kv[0],
    )


def _ready_wave_ids(state: State, iter_id: str) -> list[str]:
    """Same logic as ``wave next-ready``: pending waves with every dep closed.

    A wave whose dep is FAILED is excluded (matches the
    blocked_by_failure surface in :func:`wave_next_ready_cmd`).
    """
    ready: list[str] = []
    for wid, w in _waves_in_iter(state, iter_id):
        if w.status != WaveStatus.PENDING:
            continue
        dep_waves = [state.waves[d] for d in w.deps if d in state.waves]
        if any(dw.status == WaveStatus.FAILED for dw in dep_waves):
            continue
        if all(dw.status == WaveStatus.CLOSED for dw in dep_waves):
            ready.append(wid)
    return ready


@wave_app.command("dispatch")
def wave_dispatch_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to render a prompt for.")],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Write the prompt to this path atomically (still emit envelope summary).",
        ),
    ] = None,
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help=(
                "Target runtime adapter. ``claude-code`` (default) prints the "
                "subagent prompt verbatim; ``claude-agent-sdk`` wraps the prompt "
                "in an SDK invocation envelope with ``mcp_servers`` and "
                "``allowed_tools`` projected from state.mcp_grants."
            ),
        ),
    ] = "claude-code",
) -> None:
    """Render the subagent prompt for *wave_id* (read-only).

    Prints the prompt to stdout in text mode or wraps it in a JSON
    envelope under ``--json``. With ``--output PATH`` the prompt is
    instead written to *PATH* atomically and the envelope/summary is
    surfaced to stdout. A wave that is already CLOSED / FAILED /
    ABANDONED still renders successfully (history view); a stderr note
    flags the terminal status.

    ``--runtime=claude-agent-sdk`` switches the renderer to the
    pure-dispatch SDK adapter — the prompt body is identical to the
    claude-code branch, but the JSON envelope (or ``--output`` payload)
    gains ``runtime``, ``mcp_servers`` and ``allowed_tools`` fields. No
    runtime dependency on the SDK package is added; the adapter is
    render-only.
    """
    from eawf.dispatch import DISPATCH_RUNTIMES, render_dispatch_envelope

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if runtime not in DISPATCH_RUNTIMES:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"unknown runtime {runtime!r}; expected one of {list(DISPATCH_RUNTIMES)}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, flags = loaded
    if wave_id not in state.waves:
        cli_errors.emit_error(
            cli_errors.UserError(f"unknown wave: {wave_id}", kind="NotFound"),
            flags=flags,
        )
        return
    try:
        dispatch_envelope = render_dispatch_envelope(state, wave_id, runtime)
    except KeyError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    prompt = dispatch_envelope.prompt
    wave = state.waves[wave_id]
    if wave.status in {WaveStatus.CLOSED, WaveStatus.FAILED, WaveStatus.ABANDONED}:
        print(
            f"note: wave {wave_id!r} has terminal status {wave.status.value!r}; "
            f"prompt rendered for history-only inspection",
            file=sys.stderr,
        )
    if output is not None:
        _atomic_write_text(output, prompt)
        envelope: dict[str, Any] = {
            "wave": wave_id,
            "runtime": dispatch_envelope.runtime,
            "output": str(output),
            "bytes_written": len(prompt.encode("utf-8")),
        }
        if runtime == "claude-agent-sdk":
            envelope["mcp_servers"] = dispatch_envelope.mcp_servers
            envelope["allowed_tools"] = dispatch_envelope.allowed_tools
        text = f"wave dispatch {wave_id} written to {output}"
        emit_json_or_text(envelope, text, flags=flags)
        return
    envelope = _build_dispatch_stdout_envelope(dispatch_envelope)
    text_body = _build_dispatch_stdout_text(dispatch_envelope)
    emit_json_or_text(envelope, text_body, flags=flags)


def _build_dispatch_stdout_envelope(env: DispatchEnvelope) -> dict[str, Any]:
    """Return the ``--json`` payload for a ``wave dispatch`` invocation."""
    payload: dict[str, Any] = {
        "wave": env.wave_id,
        "runtime": env.runtime,
        "prompt": env.prompt,
    }
    if env.runtime == "claude-agent-sdk":
        payload["mcp_servers"] = env.mcp_servers
        payload["allowed_tools"] = env.allowed_tools
    return payload


def _build_dispatch_stdout_text(env: DispatchEnvelope) -> str:
    """Return the text-mode stdout for a ``wave dispatch`` invocation.

    ``claude-code`` prints the prompt verbatim (back-compat with the
    pre-P10 surface). ``claude-agent-sdk`` prepends a short SDK
    invocation banner naming the wired-in MCP servers and the projected
    allow-list so the operator sees the envelope shape without reaching
    for ``--json``.
    """
    if env.runtime == "claude-code":
        return env.prompt
    server_ids = [server["id"] for server in env.mcp_servers]
    lines = [
        "## claude-agent-sdk envelope",
        "",
        f"runtime: {env.runtime}",
        f"wave: {env.wave_id}",
        f"mcp_servers: {server_ids}",
        f"allowed_tools: {env.allowed_tools}",
        "",
        "---- prompt ----",
        "",
        env.prompt,
    ]
    return "\n".join(lines)


@wave_app.command("dispatch-batch")
def wave_dispatch_batch_cmd(
    ctx: typer.Context,
    iter_flag: Annotated[
        str | None,
        typer.Option("--iter", help="Iter ID to enumerate (defaults to current iter)."),
    ] = None,
    ready_only: Annotated[
        bool,
        typer.Option("--ready-only", help="Restrict output to waves returned by next-ready."),
    ] = False,
) -> None:
    """Render prompts for every (or every ready) pending wave under an iter.

    Without ``--ready-only`` the verb walks every pending wave under
    the iter. With ``--ready-only`` only the waves
    :func:`wave_next_ready_cmd` would surface (deps all closed, no
    failed-dep blockers) are rendered.
    """
    from eawf.dispatch import render_wave_prompt

    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, flags = loaded
    target_iter = _resolve_iter_for_query(state, flags, iter_flag=iter_flag)
    if target_iter is None:
        return
    if ready_only:
        wave_ids = _ready_wave_ids(state, target_iter)
    else:
        wave_ids = [
            wid for wid, w in _waves_in_iter(state, target_iter) if w.status == WaveStatus.PENDING
        ]
    prompts: list[dict[str, Any]] = []
    text_chunks: list[str] = []
    for wid in wave_ids:
        try:
            prompt = render_wave_prompt(state, wid)
        except KeyError as exc:
            cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
            return
        prompts.append({"wave": wid, "prompt": prompt})
        text_chunks.append(f"---- WAVE {wid} ----\n{prompt}")
    payload: dict[str, Any] = {"iter": target_iter, "prompts": prompts}
    text = "\n".join(text_chunks) if text_chunks else f"iter {target_iter}: no waves to dispatch"
    emit_json_or_text(payload, text, flags=flags)


# ---- Wave budget handlers --------------------------------------------------


@wave_budget_app.command("set")
def wave_budget_set_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID whose budget is being set.")],
    tokens: Annotated[int, typer.Argument(help="Non-negative token cap (0 allowed).")],
) -> None:
    """Set ``Wave.token_budget`` for *wave_id* (non-negative integer)."""
    from eawf.budget.service import set_budget as budget_set

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if tokens < 0:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--tokens must be non-negative; got {tokens}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return

    def _mutator(state: State) -> None:
        try:
            budget_set(state, wave_id, tokens)
        except KeyError as exc:
            raise cli_errors.UserError(str(exc), kind="NotFound") from exc

    _run_mutation(
        ctx,
        command="wave budget set",
        args={"id": wave_id, "tokens": tokens},
        scope_id=wave_id,
        text=f"wave budget set {wave_id} tokens={tokens}",
        envelope=lambda: {"wave": wave_id, "token_budget": tokens},
        mutate=_mutator,
    )


@wave_budget_app.command("consume")
def wave_budget_consume_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID accumulating consumption.")],
    tokens: Annotated[int, typer.Argument(help="Non-negative token delta to add.")],
) -> None:
    """Add *tokens* to ``Wave.tokens_consumed`` and surface the policy verdict.

    Exits ``VALIDATION_FAILED`` (4) when the post-add classification is
    ``block:over-budget``. A ``warn:75-percent`` classification prints a
    stderr warning but exits zero.

    On the block path the transaction is rolled back — the on-disk
    ``tokens_consumed`` keeps its pre-call value, and the rejected delta
    is named explicitly in the error message (``would consume X+N=Y``)
    so the operator can see what was attempted before deciding to raise
    the budget or split the work.
    """
    from eawf.budget.policy import BLOCK_TAG, WARN_TAG
    from eawf.budget.service import record_consumption as budget_record

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if tokens < 0:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--tokens must be non-negative; got {tokens}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return

    result: dict[str, Any] = {}

    def _mutator(state: State) -> None:
        wave_before = state.waves.get(wave_id)
        if wave_before is None:
            raise cli_errors.UserError(f"unknown wave {wave_id!r}", kind="NotFound")
        tokens_before = wave_before.tokens_consumed
        # ``budget_record`` raises ``KeyError`` only when *wave_id* is
        # absent — the pre-check above already filtered that, so no
        # further catch is needed here.
        wave, tag = budget_record(state, wave_id, tokens)
        result["classification"] = tag
        result["tokens_consumed"] = wave.tokens_consumed
        result["token_budget"] = wave.token_budget
        if tag == BLOCK_TAG:
            raise cli_errors.ValidationError(
                f"wave {wave_id!r} would be over token budget "
                f"(would consume {tokens_before}+{tokens}={wave.tokens_consumed}, "
                f"budget {wave.token_budget}); "
                f"delta of {tokens} discarded — raise budget or split work"
            )

    _run_mutation(
        ctx,
        command="wave budget consume",
        args={"id": wave_id, "tokens": tokens},
        scope_id=wave_id,
        text=f"wave budget consume {wave_id} tokens={tokens}",
        envelope=lambda: {
            "wave": wave_id,
            "delta": tokens,
            "tokens_consumed": result.get("tokens_consumed"),
            "token_budget": result.get("token_budget"),
            "classification": result.get("classification"),
        },
        mutate=_mutator,
    )

    if result.get("classification") == WARN_TAG:
        consumed = result.get("tokens_consumed")
        budget_val = result.get("token_budget")
        logger.warning(
            f"wave_budget_consume_cmd wave={wave_id!r} pct=75 "
            f"consumed={consumed} budget={budget_val}"
        )
        print(
            f"warn: wave {wave_id!r} at 75% of token budget ({consumed}/{budget_val})",
            file=sys.stderr,
        )


@wave_budget_app.command("show")
def wave_budget_show_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to inspect (read-only).")],
) -> None:
    """Print *wave_id*'s budget, consumption, remainder, and policy verdict."""
    from pydantic import ValidationError as PydValidationError

    from eawf.budget.service import check_budget as budget_check
    from eawf.kernel.state.models import State

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound"),
            flags=flags,
        )
        return
    try:
        payload = _read_state_payload(state_path)
        try:
            state = State.model_validate(payload)
        except PydValidationError as exc:
            raise cli_errors.StateConflict(
                f"state at {state_path} fails schema validation: {exc}", kind="IntegrityViolation"
            ) from exc
        try:
            classification = budget_check(state, wave_id)
        except KeyError as exc:
            raise cli_errors.UserError(str(exc), kind="NotFound") from exc
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    wave = state.waves[wave_id]
    budget_val = wave.token_budget
    consumed = wave.tokens_consumed
    remaining = None if budget_val is None else budget_val - consumed
    envelope = {
        "wave": wave_id,
        "token_budget": budget_val,
        "tokens_consumed": consumed,
        "remaining": remaining,
        "classification": classification,
    }
    budget_display = "unset" if budget_val is None else str(budget_val)
    remaining_display = "n/a" if remaining is None else str(remaining)
    text = (
        f"wave {wave_id} budget={budget_display} consumed={consumed} "
        f"remaining={remaining_display} status={classification or 'ok'}"
    )
    emit_json_or_text(envelope, text, flags=flags)
