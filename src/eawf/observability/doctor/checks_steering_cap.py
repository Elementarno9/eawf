"""Doctor checks measuring a rendered steering projection against its cap.

Split out of :mod:`eawf.observability.doctor.checks` to keep that module
under the EAWF010 line budget once a second capped projection joined the
card: a repository that authors ``.ea/rules.yaml`` also renders
``AGENTS.override.md`` (the policy file), which Codex reads through
:data:`~eawf.platform.rules.render.POLICY_TARGET` alongside Claude's import
shim. :mod:`eawf.observability.doctor.checks` imports and re-exports every
public name below, so ``checks.check_agents_md_byte_cap`` and
``checks.CODEX_PROJECT_DOC_BYTE_CAP`` keep resolving unchanged.
"""

from __future__ import annotations

from pathlib import Path

from eawf.observability.doctor.models import CheckResult
from eawf.platform.rules.host_facts import (
    certified_document_cap,
    load_host_facts,
    measure_codex_document,
    smallest_certified_cap,
)
from eawf.platform.rules.render import POLICY_TARGET
from eawf.surfaces.render.agents_md import measure_agents_md_byte_cap
from eawf.surfaces.render.regions import RegionParseError

# Codex's measured truncation boundary, read from the certified host-fact
# record rather than restated here. A test can monkeypatch this constant to
# exercise the over-cap path against a small fixture.
CODEX_PROJECT_DOC_BYTE_CAP = certified_document_cap("codex")


def _resolve_anchor(workspace: Path | None) -> Path | None:
    """Resolve the workspace anchor (the ``.ea/`` parent directory).

    Mirrors :func:`eawf.observability.doctor.checks._resolve_anchor`, kept as
    a small local copy rather than an import so this module carries no
    dependency back on ``checks`` (which imports this module at load time).

    Args:
        workspace: The anchor to use verbatim, or ``None`` to walk upward
            from the process cwd for the nearest ``.ea/`` ancestor.

    Returns:
        The anchor directory, or ``None`` when no ``.ea/`` ancestor exists.
    """
    if workspace is not None:
        return Path(workspace)
    cur = Path.cwd().resolve()
    for directory in [cur, *cur.parents]:
        if (directory / ".ea").is_dir():
            return directory
    return None


def check_agents_md_byte_cap(*, workspace: Path | None) -> CheckResult:
    """Fail when the rendered AGENTS.md exceeds Codex's project-doc byte cap.

    Codex truncates a project doc at :data:`CODEX_PROJECT_DOC_BYTE_CAP` bytes,
    silently dropping every render block past the cut, so a too-large AGENTS.md
    loses its guidance tail without any error. This check reads the on-disk
    AGENTS.md at the workspace anchor, measures its UTF-8 byte size via
    :func:`~eawf.surfaces.render.agents_md.measure_agents_md_byte_cap`, and
    returns a **blocking** ``fail`` when over — naming the managed render blocks
    that fall past the cut so the operator knows exactly what Codex never sees.

    Behaviour:

    - No workspace anchor, or no AGENTS.md at the anchor → ``ok`` (nothing to
      measure; the initialised-tree angle is :func:`check_state_present`'s job).
    - AGENTS.md within the cap → ``ok`` with the measured byte size.
    - AGENTS.md over the cap → ``fail`` naming the dropped block ids.
    - Malformed managed-region markers → the byte total is still measurable, so
      the cap verdict stands, but the dropped-block list is left empty (marker
      validity is :func:`check_manifest_in_sync`'s remit).

    Args:
        workspace: Workspace anchor holding the rendered AGENTS.md; ``None``
            defers to the pwd-upward ``.ea/`` walk.

    Returns:
        The ``agents_md_byte_cap`` check result.
    """
    name = "agents_md_byte_cap"
    anchor = _resolve_anchor(workspace)
    if anchor is None:
        return CheckResult(name=name, status="ok", detail="no workspace anchor")
    doc_path = anchor / "AGENTS.md"
    if not doc_path.is_file():
        return CheckResult(name=name, status="ok", detail=f"no AGENTS.md at {doc_path}")
    text = doc_path.read_text(encoding="utf-8")
    try:
        report = measure_agents_md_byte_cap(text, cap=CODEX_PROJECT_DOC_BYTE_CAP)
    except RegionParseError as exc:
        total = len(text.encode("utf-8"))
        over = total > CODEX_PROJECT_DOC_BYTE_CAP
        return CheckResult(
            name=name,
            status="fail" if over else "ok",
            detail=(
                f"AGENTS.md {total}B vs {CODEX_PROJECT_DOC_BYTE_CAP}B cap; "
                f"blocks unnameable (malformed markers: {exc})"
            ),
        )
    if report.over_cap:
        dropped = report.dropped_block_ids
        shown = ", ".join(dropped[:8])
        suffix = f" (+{len(dropped) - 8} more)" if len(dropped) > 8 else ""
        return CheckResult(
            name=name,
            status="fail",
            detail=(
                f"AGENTS.md {report.total_bytes}B exceeds {report.cap}B cap; "
                f"{len(dropped)} block(s) past the cut: {shown}{suffix}"
            ),
        )
    # A cap this machine raised is diagnostic only: the verdict stands on the
    # certified default, and the note keeps the observed fit from being cited.
    measurement = measure_codex_document(report.total_bytes)
    note = "" if measurement.portability == "portable" else f"; {measurement.note}"
    return CheckResult(
        name=name,
        status="ok",
        detail=f"AGENTS.md {report.total_bytes}B within {report.cap}B cap{note}",
    )


def check_agents_override_byte_cap(*, workspace: Path | None) -> CheckResult:
    """Fail when AGENTS.override.md exceeds the smallest certified cap of its readers.

    A repository composing a workspace layer renders the policy file beside
    the card; Claude discovers it only through its import shim (so its
    contents reach a Claude session too) and Codex reads it directly when
    present. Neither the card's Codex-only cap nor a single machine's
    configuration answers what the *policy file* must fit, so this check
    resolves :func:`~eawf.platform.rules.host_facts.smallest_certified_cap`
    for the ``"policy"`` projection kind itself rather than reusing
    :data:`CODEX_PROJECT_DOC_BYTE_CAP`.

    Behaviour:

    - No workspace anchor, or no AGENTS.override.md at the anchor → ``ok``
      (nothing to measure; most repositories never compose a workspace
      layer).
    - No reader of the policy file has a certified cap → ``ok`` (nothing to
      hold the file to; the render transaction itself already refuses to
      publish an uncapped policy file, so this is unreachable in practice).
    - AGENTS.override.md within the cap → ``ok`` with the measured byte size.
    - AGENTS.override.md over the cap → **blocking** ``fail`` naming the
      dropped render blocks, mirroring :func:`check_agents_md_byte_cap`.

    Args:
        workspace: Workspace anchor holding the rendered policy file;
            ``None`` defers to the pwd-upward ``.ea/`` walk.

    Returns:
        The ``agents_override_byte_cap`` check result.
    """
    name = "agents_override_byte_cap"
    anchor = _resolve_anchor(workspace)
    if anchor is None:
        return CheckResult(name=name, status="ok", detail="no workspace anchor")
    doc_path = anchor / POLICY_TARGET
    if not doc_path.is_file():
        return CheckResult(name=name, status="ok", detail=f"no {POLICY_TARGET} at {doc_path}")
    cap = smallest_certified_cap(load_host_facts(), "policy")
    if cap is None:
        return CheckResult(
            name=name,
            status="ok",
            detail=f"no certified cap among {POLICY_TARGET}'s readers",
        )
    text = doc_path.read_text(encoding="utf-8")
    try:
        report = measure_agents_md_byte_cap(text, cap=cap.cap_bytes)
    except RegionParseError as exc:
        total = len(text.encode("utf-8"))
        over = total > cap.cap_bytes
        return CheckResult(
            name=name,
            status="fail" if over else "ok",
            detail=(
                f"{POLICY_TARGET} {total}B vs {cap.cap_bytes}B cap certified for "
                f"{cap.runtime}; blocks unnameable (malformed markers: {exc})"
            ),
        )
    if report.over_cap:
        dropped = report.dropped_block_ids
        shown = ", ".join(dropped[:8])
        suffix = f" (+{len(dropped) - 8} more)" if len(dropped) > 8 else ""
        return CheckResult(
            name=name,
            status="fail",
            detail=(
                f"{POLICY_TARGET} {report.total_bytes}B exceeds {report.cap}B cap certified "
                f"for {cap.runtime}; {len(dropped)} block(s) past the cut: {shown}{suffix}"
            ),
        )
    return CheckResult(
        name=name,
        status="ok",
        detail=f"{POLICY_TARGET} {report.total_bytes}B within {report.cap}B cap ({cap.runtime})",
    )


__all__ = [
    "CODEX_PROJECT_DOC_BYTE_CAP",
    "check_agents_md_byte_cap",
    "check_agents_override_byte_cap",
]
