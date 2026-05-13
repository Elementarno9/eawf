"""Draft artifact commands and shared promotion helper."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from eawf.artifacts.validation import validate_markdown_artifact
from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.evidence import artifact as artifact_evi
from eawf.evidence._io import append_jsonl, event_envelope, store_paths
from eawf.scrub.scan import rewrite_text, scan_text
from eawf.state.enums import ArtifactKind, StoreKind

draft_app = typer.Typer(
    name="draft",
    help="Create and validate local draft artifacts.",
    no_args_is_help=True,
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PROMOTABLE_KINDS = {"research", "audit", "plan", "hypothesis", "decision", "incident"}


def _flags(ctx: typer.Context) -> GlobalFlags:
    flags = ctx.obj
    return flags if isinstance(flags, GlobalFlags) else GlobalFlags()


def _workspace_root(state_path: Path) -> Path:
    return state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent


def _draft_path(root: Path, kind: str, slug: str) -> Path:
    return root / ".ea" / "local" / kind / f"{slug}.md"


def _artifact_path(root: Path, kind: str, slug: str) -> Path:
    return root / ".ea" / "artifacts" / f"{kind}-{slug}.md"


def _validate_kind_slug(kind: str, slug: str) -> None:
    if kind not in _PROMOTABLE_KINDS:
        raise cli_errors.InvalidInput(f"unsupported draft kind: {kind!r}")
    if not _SLUG_RE.match(slug):
        raise cli_errors.InvalidInput(f"invalid slug: {slug!r}")


def _artifact_kind_for(kind: str) -> str:
    if kind == "research":
        return ArtifactKind.RESEARCH_BRIEF.value
    if kind == "plan":
        return ArtifactKind.PLAN_SPEC.value
    if kind == "audit":
        return ArtifactKind.AUDIT_REPORT.value
    return kind


def _new_draft_text(kind: str, slug: str, title: str | None) -> str:
    heading = title or f"{kind.title()} Draft: {slug}"
    return "\n".join(
        [
            f"<!-- eawf-template: {kind} -->",
            f"# {heading}",
            "",
            "## Summary",
            "",
            "(draft)",
            "",
            "## References",
            "",
            "(none)",
            "",
            "## Provenance",
            "",
            f"- kind: {kind}",
            f"- slug: {slug}",
            "",
            "## Scrub",
            "",
            "- status: clean",
            "",
        ]
    )


def _strip_sentinel(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("<!-- eawf-template:"):
        return text
    end = stripped.find("-->")
    if end == -1:
        return text
    return stripped[end + 3 :].lstrip()


@draft_app.command("new")
def draft_new(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="research/audit/plan/hypothesis/decision/incident")],
    slug: Annotated[str, typer.Argument(help="Portable draft slug.")],
    title: Annotated[str | None, typer.Option("--title", help="Draft title.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing draft.")] = False,
) -> None:
    """Create a templated local draft under ``.ea/local/<kind>/``."""
    flags = _flags(ctx)
    try:
        _validate_kind_slug(kind, slug)
        state_path = resolve_state_path(flags.workspace)
        path = _draft_path(_workspace_root(state_path), kind, slug)
        if path.exists() and not force:
            raise cli_errors.InvalidInput(f"draft already exists: {kind}/{slug}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_new_draft_text(kind, slug, title), encoding="utf-8")
    except (FileNotFoundError, cli_errors.CliError) as exc:
        err = exc if isinstance(exc, cli_errors.CliError) else cli_errors.NotFound(str(exc))
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text({"path": str(path)}, f"draft new {kind}/{slug}", flags=flags)


@draft_app.command("validate")
def draft_validate(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Draft markdown path.")],
) -> None:
    """Validate a local draft artifact."""
    flags = _flags(ctx)
    text = path.read_text(encoding="utf-8")
    report = validate_markdown_artifact(text, require_template_sentinel=True)
    payload = {
        "ok": report.ok,
        "errors": report.errors,
        "scrub_findings": [finding.__dict__ for finding in scan_text(text)],
    }
    if not report.ok:
        emit_json_or_text(payload, "\n".join(report.errors), flags=flags)
        raise typer.Exit(code=4)
    emit_json_or_text(payload, "draft validate: ok", flags=flags)


def promote_draft(
    ctx: typer.Context,
    *,
    kind: str,
    slug: str,
    scrub: bool,
    force: bool,
) -> None:
    """Promote one local draft into ``.ea/artifacts`` and state."""
    flags = _flags(ctx)
    try:
        _validate_kind_slug(kind, slug)
        state_path = resolve_state_path(flags.workspace)
        root = _workspace_root(state_path)
        src = _draft_path(root, kind, slug)
        if not src.exists():
            raise cli_errors.NotFound(f"draft not found: {kind}/{slug}")
        text = src.read_text(encoding="utf-8")
        if scrub:
            text = rewrite_text(text)
        report = validate_markdown_artifact(text, require_template_sentinel=True)
        if not report.ok:
            raise cli_errors.ValidationFailed("; ".join(report.errors))
        text = _strip_sentinel(text)
        dest = _artifact_path(root, kind, slug)
        if dest.exists() and not force:
            raise cli_errors.InvalidInput(f"artifact file already exists: {dest.name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        artifact_id = f"ART-{kind}-{slug}"
        uri = f"repo:.ea/artifacts/{dest.name}"
        artifact_kind = _artifact_kind_for(kind)
        with state_transaction(state_path) as state:
            scope_id = state.project.code if state.project is not None else kind
            if artifact_id in state.artifacts and not force:
                raise cli_errors.InvalidInput(f"artifact {artifact_id!r} already exists")
            if artifact_id in state.artifacts and force:
                artifact = state.artifacts[artifact_id]
                now = datetime.now(UTC)
                state.artifacts[artifact_id] = artifact.model_copy(
                    update={
                        "kind": artifact_kind,
                        "uri": uri,
                    }
                )
                state.updated_at = now
                event = event_envelope(
                    event_id=f"EVT-artifact-promote-{artifact_id}-{int(now.timestamp() * 1000)}",
                    scope_id=scope_id,
                    event_type="artifact.promote",
                    actor="cli",
                    command=f"{kind} promote",
                    args={"artifact_id": artifact_id, "kind": artifact_kind, "uri": uri},
                    summary=f"artifact {artifact_id} promoted kind={artifact_kind}",
                    artifact_ids=[artifact_id],
                )
            else:
                event = artifact_evi.add_artifact(
                    state,
                    artifact_id=artifact_id,
                    kind=artifact_kind,
                    uri=uri,
                    scope_id=scope_id,
                )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except (FileNotFoundError, cli_errors.CliError) as exc:
        err = exc if isinstance(exc, cli_errors.CliError) else cli_errors.NotFound(str(exc))
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        {"artifact_id": artifact_id, "uri": uri, "kind": artifact_kind},
        f"{kind} promote {slug} -> {artifact_id}",
        flags=flags,
    )


def install_promote_command(app: typer.Typer, kind: str) -> None:
    """Attach ``promote`` command for a concrete artifact kind."""

    def _promote(
        ctx: typer.Context,
        slug: Annotated[str, typer.Argument(help="Draft slug under .ea/local.")],
        scrub: Annotated[bool, typer.Option("--scrub", help="Scrub before promotion.")] = False,
        force: Annotated[bool, typer.Option("--force", help="Overwrite promoted file.")] = False,
    ) -> None:
        promote_draft(ctx, kind=kind, slug=slug, scrub=scrub, force=force)

    _promote.__name__ = f"{kind}_promote"
    app.command("promote")(_promote)
