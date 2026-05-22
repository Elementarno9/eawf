"""``eawf skill`` Typer subapp — list, render, and run Eä workflow skills.

Surface contract:

- ``eawf skill list`` (Phase 4 W07) enumerates all 10 canonical skill
  names with their body schema name and an ``installed``/``missing``
  status.
- ``eawf skill render <name>`` (Phase 10 W01) prints a registered
  skill's canonical ``SKILL.md`` body (``--format=skill-md``, default)
  or a metadata+body JSON object (``--format=json``). Bytes are
  byte-equal to the SKILL.md :mod:`eawf.runtimes.claude.plugin_install`
  writes on disk for the same skill.
- ``eawf skill run <name>`` (Phase 4 W07) invokes
  :func:`~eawf.skills.engine.run_skill` headlessly. Optional JSON args
  may be piped on stdin and are folded into :attr:`SkillContext.args`.
  The default output is the markdown envelope produced by
  :func:`~eawf.render.envelope.to_markdown`; the global ``--json``
  flag flips emission to the JSON envelope shape that
  ``eawf render-output --format markdown`` consumes.

Exit-code mapping (per design spec §4 W07 acceptance #2):

- ``status=ok`` and ``status=partial`` → exit 0 (``OK``).
- ``status=needs_user`` → exit 7 (``USER_DECLINED`` — closest canonical
  code; the v0.1 plan §5 reserves the lane for "user declined" which
  semantically subsumes "the skill is waiting on a user response").
- ``status=failed`` → exit 4 (``VALIDATION_FAILED`` — the canonical
  "skill body validation failed" lane).
- ``status=blocked`` → exit 6 (``INSTRUMENT_MISSING`` — blocked envelopes
  always carry a probe-failure root cause; the spec only enumerates 0/4/7
  but the runner must still emit *some* code on the blocked path).

The list table includes a synthetic body-schema "fingerprint" — the
fully-qualified class name of the body model — so an operator can
verify `installed` rows against `eawf.skills.bodies` without re-reading
the spec.
"""

from __future__ import annotations

import io
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import orjson
import typer
from rich.console import Console
from rich.table import Table

from eawf.cli import errors as cli_errors
from eawf.cli import exit_codes
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.render.envelope import EnvelopeStatus, OutputEnvelope, SkillName
    from eawf.render.skills import SkillSpec
    from eawf.skills.engine import Skill, SkillContext

logger = logging.getLogger(__name__)


skill_app = typer.Typer(
    name="skill",
    help="List, render, and run Eä workflow skills.",
    no_args_is_help=True,
)


# Per-skill metadata used by ``eawf skill list``. Mirrors `docs/architecture/envelope.md`:
# six core (research/prep/audit/ship/review/polish) + four meta
# (init/roadmap/differentiate/flow) + /blitz + the six skill-surface
# bodies (coauthor/memory/agent-dispatch/compress/wave-spec/security-review),
# descriptions kept short enough for a terminal table column.
_SKILL_DESCRIPTIONS: dict[SkillName, str] = {
    "/research": "Investigate questions; produce a peer-reviewed brief.",
    "/prep": "Plan a wave: enumerate steps, success criteria, instruments.",
    "/audit": "Run a structured audit against the active scope and persist findings.",
    "/ship": "Close a wave/iter: gather artefacts, persist outcomes, advance pointers.",
    "/review": "Review changes against the active hypothesis and acceptance set.",
    "/polish": "Apply finishing touches: lint, format, doc updates, link checks.",
    "/init": "Bootstrap a new Eä Workflow workspace via the install wizard.",
    "/roadmap": "Plan or update the long-running roadmap from the active scope.",
    "/differentiate": "Compare options and produce a differentiation matrix.",
    "/flow": "Composite skill that chains the six core skills end-to-end.",
    "/blitz": "Auto-chain research follow-ups when residual unknowns remain.",
    "/coauthor": "Resolve the Co-Authored-By trailer policy for the active repo.",
    "/memory": "Save, list, or forget curated durable memory entries.",
    "/agent-dispatch": "Dispatch a claimed wave to a runtime per the V8 session-reuse ladder.",
    "/compress": "Compress the session conversation when context approaches the limit.",
    "/wave-spec": "Scaffold or validate a WaveSpec deliverable for a claimed wave.",
    "/security-review": "Run the security-audit DSL against a closed scope and emit findings.",
}

# Body schema lookup. The "fingerprint" column in ``skill list`` is the
# fully-qualified class name of this model — stable enough to spot a
# drift between the canonical body schema and an installed skill at a
# glance. Built lazily (deferred import of ``eawf.skills.bodies``, which
# pulls the pydantic body models) so importing this module to register the
# command tree stays light.
_SKILL_BODY_MODELS_CACHE: dict[SkillName, type[Any]] | None = None


def _skill_body_models() -> dict[SkillName, type[Any]]:
    """Return the canonical skill-name → body-model map (cached)."""
    global _SKILL_BODY_MODELS_CACHE
    if _SKILL_BODY_MODELS_CACHE is None:
        from eawf.skills.bodies import (
            AgentDispatchBody,
            AuditBody,
            BlitzBody,
            CoauthorBody,
            CompressBody,
            DifferentiateBody,
            FlowBody,
            InitBody,
            MemoryBody,
            PolishBody,
            PrepBody,
            ResearchBody,
            ReviewBody,
            RoadmapBody,
            SecurityReviewBody,
            ShipBody,
            WaveSpecBody,
        )

        _SKILL_BODY_MODELS_CACHE = {
            "/research": ResearchBody,
            "/prep": PrepBody,
            "/audit": AuditBody,
            "/ship": ShipBody,
            "/review": ReviewBody,
            "/polish": PolishBody,
            "/init": InitBody,
            "/roadmap": RoadmapBody,
            "/differentiate": DifferentiateBody,
            "/flow": FlowBody,
            "/blitz": BlitzBody,
            "/coauthor": CoauthorBody,
            "/memory": MemoryBody,
            "/agent-dispatch": AgentDispatchBody,
            "/compress": CompressBody,
            "/wave-spec": WaveSpecBody,
            "/security-review": SecurityReviewBody,
        }
    return _SKILL_BODY_MODELS_CACHE


def _all_skill_names() -> list[SkillName]:
    """Return the 10 canonical skill names in declaration order.

    Sourced from :data:`~eawf.render.envelope.SkillName` via
    :func:`typing.get_args` so the list never drifts from the frozen
    literal.
    """
    from eawf.render.envelope import CANONICAL_SKILL_NAMES

    return list(CANONICAL_SKILL_NAMES)


def _resolve_skill_name(raw: str) -> SkillName:
    """Coerce *raw* into the frozen :data:`SkillName` literal.

    Accepts the canonical form (``/research``) and the bare form
    (``research``) so operators don't have to escape the leading slash
    in shells that interpret it.

    Raises:
        InvalidInput: ``raw`` is not one of the ten canonical names.
    """
    candidate = _normalise_skill_input(raw)
    valid = _all_skill_names()
    if candidate not in valid:
        raise cli_errors.InvalidInput(f"unknown skill {raw!r}; expected one of {sorted(valid)}")
    # mypy narrows ``candidate`` to the :data:`SkillName` literal via the
    # ``in valid`` membership test, so no cast is required here.
    return candidate


def _parse_stdin_args(stdin_text: str) -> dict[str, Any]:
    """Decode and shape-check the optional stdin JSON args mapping.

    Empty stdin is allowed and yields an empty dict; non-empty input
    that is not a JSON object is rejected with :class:`InvalidInput`.
    """
    if not stdin_text.strip():
        return {}
    try:
        decoded: Any = orjson.loads(stdin_text)
    except orjson.JSONDecodeError as exc:
        raise cli_errors.InvalidInput(f"stdin is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise cli_errors.InvalidInput(
            f"stdin payload must be a JSON object; got {type(decoded).__name__}"
        )
    return cast(dict[str, Any], decoded)


def _normalise_skill_input(raw: str) -> str:
    """Return slash-prefixed skill name."""
    return raw if raw.startswith("/") else f"/{raw}"


def _overlay_envelope(
    *,
    name: str,
    source: str,
    path: Path | None,
    body: str,
    ctx: SkillContext,
    args: dict[str, Any],
) -> OutputEnvelope:
    """Build a dispatch envelope for a discovered markdown skill overlay."""
    from eawf.render.envelope import EnvelopeFooter, EnvelopeHeader, OutputEnvelope

    started_at = datetime.now(UTC)
    finished_at = datetime.now(UTC)
    return OutputEnvelope(
        header=EnvelopeHeader(
            skill=name,
            scope_id=ctx.scope,
            session=ctx.session,
            started_at=started_at,
            finished_at=finished_at,
            status="ok",
            instrument_probe={},
        ),
        body={
            "kind": "skill_overlay_dispatch",
            "name": name,
            "source": source,
            "path": str(path) if path is not None else None,
            "args": args,
            "body": body,
        },
        footer=EnvelopeFooter(
            persisted_artifacts=[],
            persisted_store_records=[],
            state_mutations=[],
            evidence_refs=[],
            next_valid_actions=[],
            warnings=[],
            repair_commands=None,
        ),
    )


def _exit_for_status(status: EnvelopeStatus) -> int:
    """Map an envelope status to its canonical exit code.

    See module docstring for the rationale on the four-status mapping.
    """
    if status in {"ok", "partial"}:
        return exit_codes.OK
    if status == "needs_user":
        return exit_codes.USER_DECLINED
    if status == "failed":
        return exit_codes.VALIDATION_FAILED
    # status == "blocked"
    return exit_codes.INSTRUMENT_MISSING


def _emit_envelope(env: OutputEnvelope, *, as_json: bool) -> None:
    """Print *env* to stdout as JSON or as the canonical markdown form."""
    from eawf.render.envelope import to_markdown

    if as_json:
        raw = orjson.dumps(
            env.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
        typer.echo(raw.decode("utf-8"))
        return
    # Markdown wire-form is already newline-terminated.
    typer.echo(to_markdown(env), nl=False)


def _build_list_table(*, plain: bool) -> str:
    """Render the ``skill list`` table.

    Plain mode emits ``"<name>  <status>  <body>  <description>"`` lines
    (no ANSI markup) so terminals without colour stay readable. The
    Rich branch builds a :class:`Table` into a string buffer with a
    fixed width (100) so the output is deterministic for golden tests.
    """
    from eawf.skills import registry

    rows: list[tuple[SkillName, str, str, str]] = []
    for name in _all_skill_names():
        registered = registry.lookup(name)
        status = "installed" if registered is not None else "missing"
        body_cls = _skill_body_models()[name]
        fingerprint = f"{body_cls.__module__}.{body_cls.__qualname__}"
        description = _SKILL_DESCRIPTIONS[name]
        rows.append((name, status, fingerprint, description))

    if plain:
        lines: list[str] = []
        for name, status, fingerprint, description in rows:
            lines.append(f"{name:<16}  {status:<10}  {fingerprint:<48}  {description}")
        return "\n".join(lines)

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, record=False)
    table = Table(title="eawf skills", show_lines=False)
    table.add_column("name", style="cyan")
    table.add_column("status", justify="left", style="bold")
    table.add_column("body schema", style="magenta")
    table.add_column("description")
    for name, status, fingerprint, description in rows:
        style = "green" if status == "installed" else "yellow"
        table.add_row(
            name,
            f"[{style}]{status}[/{style}]",
            fingerprint,
            description,
        )
    console.print(table)
    return buf.getvalue().rstrip()


def _list_payload() -> dict[str, Any]:
    """Build the JSON shape for ``skill list --json``."""
    from eawf.skills import registry

    skills: list[dict[str, Any]] = []
    for name in _all_skill_names():
        registered = registry.lookup(name)
        body_cls = _skill_body_models()[name]
        skills.append(
            {
                "name": name,
                "status": "installed" if registered is not None else "missing",
                "body_schema": f"{body_cls.__module__}.{body_cls.__qualname__}",
                "description": _SKILL_DESCRIPTIONS[name],
            }
        )
    return {"skills": skills}


def _skill_payload(name: SkillName) -> dict[str, Any]:
    """Build the per-skill row :func:`_list_payload` would emit for *name*.

    Returns the same four keys (``name``, ``status``, ``body_schema``,
    ``description``) so the ``skill render --format=json`` surface stays
    aligned with the ``skill list --json`` surface. The caller is
    expected to splice an additional ``body`` field carrying the
    canonical SKILL.md text.
    """
    from eawf.skills import registry

    registered = registry.lookup(name)
    body_cls = _skill_body_models()[name]
    return {
        "name": name,
        "status": "installed" if registered is not None else "missing",
        "body_schema": f"{body_cls.__module__}.{body_cls.__qualname__}",
        "description": _SKILL_DESCRIPTIONS[name],
    }


def _resolve_skill_spec(name: SkillName) -> SkillSpec:
    """Return the :class:`SkillSpec` for *name* (slashed canonical form).

    :data:`SKILL_REGISTRY` stores bare skill names (e.g. ``"research"``)
    but the CLI / :data:`SkillName` literal uses the slashed canonical
    form (``"/research"``). This helper bridges the two so callers can
    work in the slashed namespace.

    Raises:
        cli_errors.InvalidInput: ``name`` has no matching :data:`SkillSpec`
            entry. This should be unreachable in practice because
            :func:`_resolve_skill_name` validates the canonical literal
            BEFORE we land here — the registry and the literal are
            frozen at the same ten names — but we still raise the
            canonical error so the surface stays defensive.
    """
    from eawf.render.skills import SKILL_REGISTRY

    bare = name.removeprefix("/")
    for spec in SKILL_REGISTRY:
        if spec.skill_name == bare:
            return spec
    raise cli_errors.InvalidInput(
        f"no SkillSpec registered for {name!r}; SKILL_REGISTRY and SkillName drifted"
    )


_SCOPE_CHOICES: frozenset[str] = frozenset({"builtin", "user", "workspace", "all"})


def _discovered_list_payload(*, workspace: Path | None, scope: str) -> dict[str, Any]:
    """Build the ``skill list`` payload spanning builtin + user + workspace.

    Each row carries the historical fields (``name``, ``status``,
    ``body_schema``, ``description``) plus the new layered metadata
    introduced by P14-W09: ``source`` (``builtin|user|workspace``),
    ``runtimes`` (per-runtime visibility hint; empty == visible to all),
    ``path``, and ``version``.
    """
    from eawf.skills import registry
    from eawf.skills.discovery import discover_skills

    rows = discover_skills(workspace=workspace)
    if scope != "all":
        rows = [r for r in rows if r.source == scope]
    items: list[dict[str, Any]] = []
    builtin_names = set(_all_skill_names())
    for entry in rows:
        name = entry.name
        if name in builtin_names:
            registered = registry.lookup(name)
            body_cls = _skill_body_models()[name]
            status = "installed" if registered is not None else "missing"
            body_schema = f"{body_cls.__module__}.{body_cls.__qualname__}"
        else:
            status = "user"
            body_schema = None
        items.append(
            {
                "name": name,
                "status": status,
                "body_schema": body_schema,
                "description": entry.description,
                "source": entry.source,
                "runtimes": list(entry.runtimes),
                "path": str(entry.path) if entry.path is not None else None,
                "version": entry.version,
            }
        )
    return {"skills": items, "scope": scope}


@skill_app.command(name="list")
def list_cmd(
    ctx: typer.Context,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Filter rows by source layer (builtin|user|workspace|all).",
        ),
    ] = "all",
) -> None:
    """List every skill resolvable across builtin / user / workspace layers."""
    from eawf.skills import _bootstrap as _skills_bootstrap  # noqa: F401 — registers skills

    flags: GlobalFlags = ctx.obj
    if scope not in _SCOPE_CHOICES:
        cli_errors.emit_error(
            cli_errors.InvalidInput(
                f"unknown scope {scope!r}; expected one of {sorted(_SCOPE_CHOICES)}"
            ),
            flags=flags,
        )
        return
    workspace = flags.workspace
    if flags.json_output:
        payload = _discovered_list_payload(workspace=workspace, scope=scope)
        raw = orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        typer.echo(raw.decode("utf-8"))
        return
    payload = _discovered_list_payload(workspace=workspace, scope=scope)
    lines = [f"# eawf skills (scope={scope})"]
    for item in payload["skills"]:
        runtimes = ",".join(item["runtimes"]) if item["runtimes"] else "*"
        lines.append(
            f"  {item['name']:<18}  {item['status']:<10}  "
            f"{item['source']:<9}  runtimes={runtimes:<24}  {item['description']}"
        )
    typer.echo("\n".join(lines))


# Frozen set of valid ``--format`` values for the ``render`` command.
# Centralised so the InvalidInput message lists the exact alternatives
# the surface accepts; bare ``Literal`` would let Typer auto-coerce but
# would not give us a stable rejection message for an arbitrary input.
_RENDER_FORMATS: frozenset[str] = frozenset({"skill-md", "json"})


@skill_app.command(name="render")
def render_cmd(
    ctx: typer.Context,
    name: Annotated[
        str,
        typer.Argument(
            help="Skill name to render; e.g. '/research' or 'research'.",
        ),
    ],
    format_: Annotated[
        str,
        typer.Option(
            "--format",
            help=(
                "Render shape: 'skill-md' for canonical SKILL.md bytes; "
                "'json' for a metadata+body object."
            ),
        ),
    ] = "skill-md",
) -> None:
    """Render a registered skill's metadata or SKILL.md body to stdout.

    ``--format=skill-md`` (the default) prints bytes byte-equal to the
    SKILL.md emitted by :mod:`eawf.runtimes.claude.plugin_install` for
    the same skill — the two code paths share the
    :func:`~eawf.render.skills.render_skill_md_from_spec` helper.

    ``--format=json`` prints a JSON object carrying the same
    ``name``/``status``/``body_schema``/``description`` keys as one
    row of ``skill list --json`` plus a ``body`` field holding the
    canonical SKILL.md string.

    Unknown skill name → :class:`~eawf.cli.errors.InvalidInput`
    (exit code 3, mirrors :func:`_resolve_skill_name`).
    Unknown ``--format`` → :class:`~eawf.cli.errors.InvalidInput`
    (same code), with the canonical alternatives listed in the
    rejection message.
    """
    from eawf.render.skills import render_skill_md_from_spec
    from eawf.skills import _bootstrap as _skills_bootstrap  # noqa: F401 — registers skills

    flags: GlobalFlags = ctx.obj

    try:
        skill_name = _resolve_skill_name(name)
        if format_ not in _RENDER_FORMATS:
            raise cli_errors.InvalidInput(
                f"unknown --format {format_!r}; expected one of {sorted(_RENDER_FORMATS)}"
            )
        spec = _resolve_skill_spec(skill_name)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    body = render_skill_md_from_spec(spec)

    if format_ == "skill-md":
        # Print SKILL.md bytes verbatim — the body already ends with "\n"
        # per render_skill_md's normalisation, and typer.echo would
        # double-newline if we let it append. Pass nl=False to mirror
        # the byte-equal contract with plugin_install.atomic_write_text.
        typer.echo(body, nl=False)
        return

    # format_ == "json"
    payload = _skill_payload(skill_name)
    payload["body"] = body
    raw = orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    typer.echo(raw.decode("utf-8"))


@skill_app.command(name="resume")
def resume_cmd(
    ctx: typer.Context,
    pause_urn: Annotated[
        str,
        typer.Argument(help="Pause URN to resume (urn:eawf:v1:event:.../needs-user-...)."),
    ],
    choice: Annotated[
        str,
        typer.Option(
            "--choice",
            help="Option label answering the paused question; must match an option.",
        ),
    ],
    workspace: Annotated[
        Path | None,
        typer.Option(
            "-w",
            "--workspace",
            help="Workspace root to anchor the state resolver.",
        ),
    ] = None,
) -> None:
    """Resume a paused needs_user question with the chosen option label.

    Resolves the pause record by *pause_urn*, validates *choice* against
    the paused question's options, and persists the answer by appending a
    resume row to the event store (the daemon-owned append path; no
    hand-written ``state.json``). The paused skill picks the answer up
    from that record on its next run.

    Raises:
        NotFound: When *pause_urn* names no open pause (it never existed
            or was already resolved) — exit non-zero.
        InvalidInput: When *choice* is not one of the question's option
            labels — exit non-zero.
        NotFound: When the resolved ``state.json`` does not exist.
    """
    from eawf.skills.needs_user import PauseError, find_open_pause, resolve_pause

    flags: GlobalFlags = ctx.obj
    effective_ws = workspace if workspace is not None else flags.workspace
    try:
        state_path = resolve_state_path(effective_ws)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.NotFound(f"state file not found: {state_path}"),
            flags=flags,
            data={"path": str(state_path)},
        )
        return

    try:
        pause = find_open_pause(state_path, pause_urn)
    except PauseError as exc:
        cli_errors.emit_error(
            cli_errors.NotFound(str(exc)),
            flags=flags,
            data={"kind": "NotFound", "pause_urn": pause_urn},
        )
        return
    try:
        resolve_pause(state_path, pause_urn=pause_urn, choice=choice)
    except PauseError as exc:
        cli_errors.emit_error(
            cli_errors.InvalidInput(str(exc)),
            flags=flags,
            data={"kind": "InvalidInput", "pause_urn": pause_urn, "choice": choice},
        )
        return

    emit_json_or_text(
        {"pause_urn": pause_urn, "choice": choice, "scope_id": pause.scope_id},
        f"skill resume {pause_urn} choice={choice}",
        flags=flags,
    )


@skill_app.command(name="run")
def run_cmd(
    ctx: typer.Context,
    name: Annotated[
        str,
        typer.Argument(
            help="Skill name to invoke; e.g. '/research' or 'research'.",
        ),
    ],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Eä state-scope URN passed to the SkillContext.",
        ),
    ] = "urn:eawf:v1:state:cli-skill-run",
    session: Annotated[
        str,
        typer.Option(
            "--session",
            help="Eä session URN passed to the SkillContext.",
        ),
    ] = "urn:eawf:v1:store:cli/sessions/SES-skill-run",
) -> None:
    """Run a registered skill headlessly and emit its envelope.

    Exit codes follow the design spec mapping (see module docstring):
    0 (ok/partial), 4 (failed), 6 (blocked), 7 (needs_user). The
    canonical envelope is emitted on stdout in markdown by default and
    in JSON when ``--json`` is set on the root.
    """
    from eawf.skills import _bootstrap as _skills_bootstrap  # noqa: F401 — registers skills
    from eawf.skills import registry
    from eawf.skills.engine import SkillContext, run_skill

    flags: GlobalFlags = ctx.obj

    try:
        candidate = _normalise_skill_input(name)
        # Skip stdin on a TTY so interactive runs don't block on EOF.
        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
        args = _parse_stdin_args(stdin_text)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    from eawf.skills.discovery import discover_skills

    overlay = next(
        (
            row
            for row in discover_skills(workspace=flags.workspace)
            if row.name == candidate and row.source in {"workspace", "user"}
        ),
        None,
    )
    if overlay is not None:
        skill_ctx = SkillContext(scope=scope, session=session, args=args)
        envelope = _overlay_envelope(
            name=overlay.name,
            source=overlay.source,
            path=overlay.path,
            body=overlay.body,
            ctx=skill_ctx,
            args=args,
        )
        _emit_envelope(envelope, as_json=flags.json_output)
        return

    if candidate not in _all_skill_names():
        cli_errors.emit_error(
            cli_errors.InvalidInput(
                f"unknown skill {name!r}; expected one of {sorted(_all_skill_names())} "
                "or a workspace/user skill"
            ),
            flags=flags,
        )
        return

    skill_name: SkillName = candidate

    skill_cls = registry.lookup(skill_name)
    if skill_cls is None:
        cli_errors.emit_error(
            cli_errors.NotFound(
                f"skill {skill_name!r} is not installed; see 'eawf skill list' for available skills"
            ),
            flags=flags,
        )
        return

    skill_instance: Skill = skill_cls()
    skill_ctx = SkillContext(scope=scope, session=session, args=args)
    envelope = run_skill(skill_instance, skill_ctx)

    _emit_envelope(envelope, as_json=flags.json_output)
    code = _exit_for_status(envelope.header.status)
    if code != exit_codes.OK:
        raise typer.Exit(code)


__all__ = [
    "list_cmd",
    "render_cmd",
    "resume_cmd",
    "run_cmd",
    "skill_app",
]
