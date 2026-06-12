"""Draft artifact commands and shared promotion helper."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from eawf.kernel.state.enums import ArtifactKind, StoreKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eawf.kernel.spec.intent import IntentBrief
    from eawf.kernel.state.models import Claim

draft_app = typer.Typer(
    name="draft",
    help="Create and validate local draft artifacts.",
    no_args_is_help=True,
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?$")

# Canonical kind -> subdir router. Each promotable draft kind has exactly one
# home under ``.ea/artifacts/``; placement resolves through this map instead of
# treating the singular kind token as the subdir (which produced ``audit/``
# rather than the canonical ``audits/``). The map IS the source of truth for
# both the promotable-kind set and the subdir layout, so adding a kind means
# adding one row here.
_KIND_SUBDIR: dict[str, str] = {
    "research": "research",
    "audit": "audits",
    "plan": "plans",
    "hypothesis": "hypotheses",
    "decision": "decisions",
    "incident": "incidents",
}
_PROMOTABLE_KINDS = frozenset(_KIND_SUBDIR)


def _flags(ctx: typer.Context) -> GlobalFlags:
    flags = ctx.obj
    return flags if isinstance(flags, GlobalFlags) else GlobalFlags()


def _workspace_root(state_path: Path) -> Path:
    return state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent


def _draft_path(root: Path, kind: str, slug: str) -> Path:
    return root / ".ea" / "local" / kind / f"{slug}.md"


def _artifact_path(root: Path, kind: str, slug: str) -> Path:
    artifacts = root / ".ea" / "artifacts"
    if "/" in slug:
        return artifacts / _KIND_SUBDIR[kind] / f"{slug}.md"
    return artifacts / f"{kind}-{slug}.md"


def _artifact_id(kind: str, slug: str) -> str:
    return f"ART-{kind}-{slug.replace('/', '-')}"


def _validate_kind_slug(kind: str, slug: str) -> None:
    if kind not in _PROMOTABLE_KINDS:
        raise cli_errors.UserError(f"unsupported draft kind: {kind!r}", kind="InvalidInput")
    if not _SLUG_RE.match(slug):
        raise cli_errors.UserError(f"invalid slug: {slug!r}", kind="InvalidInput")


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


def _validate_legacy_brief(text: str) -> _LegacyChassisReport:
    """Minimal validation for legacy long-form briefs.

    Enforces: sentinel present, scrub-status clean (if section present), no
    scrub findings. Skips chassis-heading and dense-citation checks.
    """
    from eawf.platform.artifacts.validation import _SCRUB_CLEAN_RE, _sections
    from eawf.platform.scrub.scan import scan_text

    errors: list[str] = []
    if not text.lstrip().startswith("<!-- eawf-template:"):
        errors.append("missing draft sentinel")
    sections = _sections(text)
    scrub_section = sections.get("## Scrub")
    if scrub_section is not None and not _SCRUB_CLEAN_RE.search(scrub_section):
        errors.append("scrub status must be clean")
    findings = scan_text(text)
    if findings:
        kinds = sorted({finding.kind for finding in findings})
        count = len(findings)
        errors.append(f"scrub findings present: {count} ({kinds})")
    return _LegacyChassisReport(ok=not errors, errors=errors)


@dataclass(frozen=True)
class _LegacyChassisReport:
    ok: bool
    errors: list[str] = field(default_factory=list)


def _strip_yaml_frontmatter(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip()
    return text


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
            raise cli_errors.UserError(f"draft already exists: {kind}/{slug}", kind="InvalidInput")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_new_draft_text(kind, slug, title), encoding="utf-8")
    except (FileNotFoundError, cli_errors.CliError) as exc:
        err = (
            exc
            if isinstance(exc, cli_errors.CliError)
            else cli_errors.UserError(str(exc), kind="NotFound")
        )
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text({"path": str(path)}, f"draft new {kind}/{slug}", flags=flags)


@draft_app.command("validate")
def draft_validate(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Draft markdown path.")],
) -> None:
    """Validate a local draft artifact."""
    from eawf.platform.artifacts.validation import validate_markdown_artifact
    from eawf.platform.scrub.scan import scan_text

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
    legacy_chassis: bool = False,
    intent: IntentBrief | None = None,
) -> None:
    """Promote one local draft into ``.ea/artifacts`` and state.

    When *legacy_chassis* is True the chassis-heading + dense-citation
    checks are skipped — used for long-form research briefs ratified
    before the renderer-owned chassis convention landed. Scrub-status +
    PII-scan + sentinel-presence are still enforced.

    When *intent* is supplied the EviBound rung-1 gate runs over the
    brief's ``evidence_refs`` at promotion time (the brief is promotable
    iff every ref resolves). This is the promotion call-site that
    un-idles the
    :attr:`eawf.kernel.spec.intent.IntentBrief.evidence_refs` contract —
    before it, the gate was never invoked anywhere. ``intent`` is
    ``None`` for the chassis-only promotion path so existing callers are
    unaffected; the EviBound check is skipped under *legacy_chassis*
    (long-form briefs predate the typed-intent surface).
    """
    from eawf.platform.artifacts.validation import validate_markdown_artifact
    from eawf.platform.scrub.scan import rewrite_text
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import artifact as artifact_evi
    from eawf.workflow.evidence._io import append_jsonl, event_envelope, store_paths

    flags = _flags(ctx)
    try:
        _validate_kind_slug(kind, slug)
        state_path = resolve_state_path(flags.workspace)
        root = _workspace_root(state_path)
        src = _draft_path(root, kind, slug)
        if not src.exists():
            raise cli_errors.UserError(f"draft not found: {kind}/{slug}", kind="NotFound")
        text = src.read_text(encoding="utf-8")
        if scrub:
            text = rewrite_text(text)
        report_ok: bool
        report_errors: list[str]
        if legacy_chassis:
            legacy_report = _validate_legacy_brief(text)
            report_ok, report_errors = legacy_report.ok, legacy_report.errors
        else:
            chassis_report = validate_markdown_artifact(
                text,
                require_template_sentinel=True,
                intent=intent,
                project_root=root,
            )
            report_ok, report_errors = chassis_report.ok, chassis_report.errors
        if not report_ok:
            raise cli_errors.ValidationError("; ".join(report_errors))
        text = _strip_sentinel(text)
        if legacy_chassis:
            text = _strip_yaml_frontmatter(text)
        dest = _artifact_path(root, kind, slug)
        if dest.exists() and not force:
            raise cli_errors.UserError(
                f"artifact file already exists: {dest.name}", kind="InvalidInput"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        artifact_id = _artifact_id(kind, slug)
        rel_dest = dest.relative_to(root)
        uri = f"repo:{rel_dest.as_posix()}"
        artifact_kind = _artifact_kind_for(kind)
        with state_transaction(state_path) as state:
            scope_id = state.project.code if state.project is not None else kind
            if artifact_id in state.artifacts and not force:
                raise cli_errors.UserError(
                    f"artifact {artifact_id!r} already exists", kind="InvalidInput"
                )
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
        err = (
            exc
            if isinstance(exc, cli_errors.CliError)
            else cli_errors.UserError(str(exc), kind="NotFound")
        )
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        {"artifact_id": artifact_id, "uri": uri, "kind": artifact_kind},
        f"{kind} promote {slug} -> {artifact_id}",
        flags=flags,
    )


def synthesize_campaign_brief(topic: str, claims: Sequence[Claim]) -> IntentBrief:
    """Synthesise an :class:`IntentBrief` from a campaign's surviving claims.

    The campaign synthesis step: it folds the *surviving* (live) claims of a
    research campaign into a typed :class:`~eawf.kernel.spec.intent.IntentBrief`
    so the brief can be promoted through :func:`promote_draft` with the EviBound
    rung-1 gate scoring its ``evidence_refs``. Every live claim's
    ``evidence_refs`` are aggregated (deduped, order-preserved) onto the brief,
    and each claim title becomes a planned step -- so a brief synthesised from
    claims whose evidence does not resolve is rejected at the operator promote
    path (the EviBound feed this wave un-idles), while a fully-referenced brief
    promotes.

    Only :attr:`~eawf.kernel.state.enums.ClaimStatus.OPEN` /
    :attr:`~eawf.kernel.state.enums.ClaimStatus.SUPPORTED` claims count as
    surviving; ``REFUTED`` / ``SUPERSEDED`` claims are pruned (they carry no
    forward-looking evidence), so the brief's evidence reflects only the live
    survivor set.

    Args:
        topic: The campaign topic the brief converges on.
        claims: The campaign's claim ledger; the live survivors are folded in.

    Returns:
        A :class:`~eawf.kernel.spec.intent.IntentBrief` carrying the surviving
        claims' aggregated evidence refs + a planned step per claim title.
    """
    from eawf.kernel.spec.intent import IntentBrief
    from eawf.kernel.state.enums import ClaimStatus

    survivors = [c for c in claims if c.status in (ClaimStatus.OPEN, ClaimStatus.SUPPORTED)]
    evidence: list[str] = []
    for claim in survivors:
        for ref in claim.evidence_refs:
            if ref not in evidence:
                evidence.append(ref)
    steps = [claim.title for claim in survivors][:10]
    problem = f"Synthesise the campaign findings for: {topic}"[:200]
    desired = f"A promotable brief backed by {len(survivors)} surviving claim(s)"[:200]
    return IntentBrief(
        problem=problem,
        desired_outcome=desired,
        planned_steps=steps,
        evidence_refs=evidence,
    )


def install_promote_command(app: typer.Typer, kind: str) -> None:
    """Attach ``promote`` command for a concrete artifact kind."""

    def _promote(
        ctx: typer.Context,
        slug: Annotated[str, typer.Argument(help="Draft slug under .ea/local.")],
        scrub: Annotated[bool, typer.Option("--scrub", help="Scrub before promotion.")] = False,
        force: Annotated[bool, typer.Option("--force", help="Overwrite promoted file.")] = False,
        legacy_chassis: Annotated[
            bool,
            typer.Option(
                "--legacy-chassis",
                help="Skip chassis-heading + dense-citation checks (for long-form "
                "research briefs ratified before the chassis convention).",
            ),
        ] = False,
    ) -> None:
        promote_draft(
            ctx, kind=kind, slug=slug, scrub=scrub, force=force, legacy_chassis=legacy_chassis
        )

    _promote.__name__ = f"{kind}_promote"
    app.command("promote")(_promote)
