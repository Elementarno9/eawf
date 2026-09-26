"""Render and read the detailed view of each selected rule module.

A view carries the full record of every rule a module contributes to the
effective graph: instruction, rationale, scope, procedure and verification,
which the projections deliberately compress to one line. Views are rendered
by the same transaction that renders the projections, from the same graph,
so an index entry never points at a view nobody generated.

Each view opens with a stamp naming the graph digest it was rendered from and
the digest of the text below it. Views are not committed, so they can lag the
rule source; :func:`read_module_view` compares the stamp against the current
graph and reports a lagging or edited view as stale instead of returning it
as current guidance.

Views are retrieved on demand and never imported at session start: an import
shim that names a view is refused by :func:`refuse_view_imports`, because a
startup chain that loads every view rebuilds the oversized root the module
index exists to avoid.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import ClassVar, Final, Literal

from pydantic import model_validator

from eawf.kernel.spec.release import Sha256DigestStr
from eawf.platform.rules.compile import CompiledRule, RuleGraph
from eawf.platform.rules.modules import (
    MODULE_VIEW_COMMAND,
    RuleModule,
    RuleModuleEntry,
    RuleModuleSelection,
)
from eawf.platform.rules.records import QualifiedId, RuleModel, RuleRecord

logger = logging.getLogger(__name__)

#: Where views are written, relative to the repository root.
VIEW_DIRECTORY: Final[str] = ".ea/rules/views"

#: The routine that renders views together with the projections.
VIEW_RENDER_COMMAND: Final[str] = "eawf sync"

_VIEW_STAMP: Final[re.Pattern[str]] = re.compile(
    r"^<!-- eawf:projection kind=view graph=(?P<graph>sha256:[0-9a-f]{64}) "
    r"body=(?P<body>sha256:[0-9a-f]{64}) module=(?P<module>\S+) .*-->$"
)

ViewStatus = Literal["current", "stale", "absent"]


class RuleViewError(ValueError):
    """Base class for a refusal raised while rendering or reading a view.

    Attributes:
        code: The stable failure code.
    """

    code: ClassVar[str] = "rule_view_error"


class RuleViewStartupImportError(RuleViewError):
    """A startup import chain names a module view."""

    code: ClassVar[str] = "rule_view_startup_import"


class RuleViewNotFoundError(RuleViewError):
    """The requested reference names no view the repository can render."""

    code: ClassVar[str] = "rule_view_not_found"


class RenderedView(RuleModel):
    """One module view ready to write.

    Attributes:
        reference: The module reference the view renders.
        target: Repository-relative path of the view.
        text: The complete file content, stamp included.
    """

    reference: QualifiedId
    target: str
    text: str


class ModuleViewRead(RuleModel):
    """The outcome of reading one module view.

    Attributes:
        reference: The module reference that was read.
        target: Repository-relative path of the view.
        status: ``current`` when the stamp matches the current graph and the
            text below it; ``stale`` when either differs; ``absent`` when no
            view is on disk.
        text: The view body below the stamp, or ``None`` when absent.
        message: What the reader should do; ``None`` for a current view.
    """

    reference: QualifiedId
    target: str
    status: ViewStatus
    text: str | None
    message: str | None

    @model_validator(mode="after")
    def _status_matches_payload(self) -> ModuleViewRead:
        """Keep the body and the message consistent with the status.

        Returns:
            The validated read.

        Raises:
            ValueError: When an absent view carries text, a present view
                carries none, or a non-current view carries no message.
        """
        if (self.status == "absent") != (self.text is None):
            raise ValueError("only an absent view carries no text")
        if (self.status == "current") != (self.message is None):
            raise ValueError("every view that is not current carries a message")
        return self


def view_target(reference: str) -> str:
    """Return the repository-relative path of a module's view.

    Args:
        reference: The module reference.

    Returns:
        ``.ea/rules/views/<reference>.md``.
    """
    return f"{VIEW_DIRECTORY}/{reference}.md"


def render_module_views(
    selection: RuleModuleSelection, graph: RuleGraph
) -> tuple[RenderedView, ...]:
    """Render one view per selected module that resolved.

    A module's view lists the rules it contributes to ``graph``, so a rule a
    higher layer superseded is not rendered as if it still applied.

    Args:
        selection: The repository's resolved module selection; its entries
            are the modules the index names.
        graph: The effective graph the projections render from.

    Returns:
        One view per available entry, in reference order.
    """
    views: list[RenderedView] = []
    for entry in selection.entries:
        if entry.module is None:
            continue
        locator = entry.module.source.locator
        rules = tuple(
            rule
            for rule in graph.rules
            if rule.record.source.kind == "builtin" and rule.record.source.locator == locator
        )
        body = _view_body(entry.reference, entry.module, rules)
        stamp = (
            f"<!-- eawf:projection kind=view graph={graph.digest} body={_sha256_text(body)} "
            f"module={entry.reference} generated by {VIEW_RENDER_COMMAND}; "
            f"read with {MODULE_VIEW_COMMAND} {entry.reference} -->\n"
        )
        views.append(
            RenderedView(
                reference=entry.reference, target=view_target(entry.reference), text=stamp + body
            )
        )
    return tuple(views)


def view_stamp_matches_body(text: str) -> bool | None:
    """Check a view's stamp against the text below it.

    Args:
        text: The complete file content.

    Returns:
        ``None`` when ``text`` does not open with a view stamp, else whether
        the stamped body digest equals the digest of the text below it.
    """
    stamp, _, body = text.partition("\n")
    match = _VIEW_STAMP.fullmatch(stamp)
    if match is None:
        return None
    return _sha256_text(body) == match.group("body")


def read_module_view(
    repo_root: Path,
    reference: str,
    *,
    selection: RuleModuleSelection,
    graph_digest: Sha256DigestStr,
) -> ModuleViewRead:
    """Read a module's view and judge it against the current graph.

    Args:
        repo_root: The repository root.
        reference: The module reference to read.
        selection: The repository's current module selection.
        graph_digest: The digest of the current effective graph.

    Returns:
        The view body with its status; an absent view names the command that
        renders it.

    Raises:
        RuleViewNotFoundError: When ``reference`` is not selected or its
            module is unavailable.
    """
    entry = _selected_entry(selection, reference)
    target = view_target(entry.reference)
    path = repo_root.joinpath(*PurePosixPath(target).parts)
    if not path.is_file():
        return ModuleViewRead(
            reference=entry.reference,
            target=target,
            status="absent",
            text=None,
            message=(
                f"{target} has not been generated; run {VIEW_RENDER_COMMAND} to render the "
                f"module views with the projections"
            ),
        )
    text = path.read_text(encoding="utf-8")
    stamp, _, body = text.partition("\n")
    match = _VIEW_STAMP.fullmatch(stamp)
    reason: str | None = None
    if match is None or match.group("module") != entry.reference:
        reason = "it carries no view stamp for this module"
    elif match.group("body") != _sha256_text(body):
        reason = "it was edited since it was rendered"
    elif match.group("graph") != graph_digest:
        reason = (
            f"it was rendered from graph {match.group('graph')}, not the current {graph_digest}"
        )
    if reason is None:
        return ModuleViewRead(
            reference=entry.reference, target=target, status="current", text=body, message=None
        )
    logger.info(f"rule view stale reference={entry.reference} reason={reason!r}")
    return ModuleViewRead(
        reference=entry.reference,
        target=target,
        status="stale",
        text=body if match is not None else text,
        message=(
            f"{target} is stale: {reason}; run {VIEW_RENDER_COMMAND} to regenerate it before "
            f"relying on it"
        ),
    )


def refuse_view_imports(imports: Iterable[str]) -> None:
    """Refuse a startup import chain that names a module view.

    Args:
        imports: The repository-relative paths a startup file imports.

    Raises:
        RuleViewStartupImportError: When any path lies under the view
            directory.
    """
    views = sorted(
        path
        for path in imports
        if PurePosixPath(path).is_relative_to(PurePosixPath(VIEW_DIRECTORY))
    )
    if views:
        raise RuleViewStartupImportError(
            f"a startup import may not load module views ({', '.join(views)}); views are read "
            f"on demand with {MODULE_VIEW_COMMAND} <module>, and importing them at startup "
            f"rebuilds the oversized root the module index replaces"
        )


def _selected_entry(selection: RuleModuleSelection, reference: str) -> RuleModuleEntry:
    """Find the selected, available entry for ``reference``.

    Args:
        selection: The module selection.
        reference: The requested reference.

    Returns:
        The entry.

    Raises:
        RuleViewNotFoundError: When ``reference`` is not selected or is
            unavailable.
    """
    for entry in selection.entries:
        if entry.reference != reference:
            continue
        if entry.module is None:
            raise RuleViewNotFoundError(
                f"module {reference} is selected but unavailable: {entry.unavailable_reason}"
            )
        return entry
    selected = ", ".join(entry.reference for entry in selection.entries) or "none"
    raise RuleViewNotFoundError(
        f"module {reference!r} is not selected in .ea/rules.yaml; selected modules: {selected}"
    )


def _view_body(reference: str, module: RuleModule, rules: tuple[CompiledRule, ...]) -> str:
    """Render the text of one view below its stamp.

    Args:
        reference: The selected module reference.
        module: The module it resolved to.
        rules: The module's rules as they stand in the effective graph.

    Returns:
        The heading, the module orientation and one section per rule.
    """
    document = module.document
    lines = [
        f"# {document.title}",
        "",
        f"Module `{reference}` version {document.version}, topic {document.topic}. "
        f"Load when: {document.when_to_use}.",
        "",
    ]
    if not rules:
        lines += ["No rule of this module is in effect in this repository.", ""]
    for rule in rules:
        lines += _rule_section(rule.record)
    return "\n".join(lines)


def _rule_section(record: RuleRecord) -> list[str]:
    """Render one rule's full record.

    Args:
        record: The rule.

    Returns:
        The section lines, ending with a blank line.
    """
    lines = [
        f"## {record.title}",
        "",
        f"- Rule: `{record.rule_id}` revision {record.revision}; {record.force}, "
        f"{record.zone} zone.",
        f"- Instruction: {record.instruction}",
    ]
    if record.rationale:
        lines.append(f"- Rationale: {record.rationale}")
    scope = record.scope
    tokens = (*scope.activities, *scope.roles, *scope.paths)
    if tokens:
        lines.append(f"- Applies to: {', '.join(tokens)}.")
    if record.procedure_ref:
        lines.append(f"- Procedure: `{record.procedure_ref}`.")
    checks = (
        f" ({', '.join(record.verification.check_refs)})" if record.verification.check_refs else ""
    )
    lines.append(f"- Verified by: {record.verification.method}{checks}.")
    if record.enforcement_ref:
        lines.append(f"- Enforced by: `{record.enforcement_ref}`.")
    return [*lines, ""]


def _sha256_text(text: str) -> str:
    """Digest UTF-8 text.

    Args:
        text: The text.

    Returns:
        ``sha256:`` followed by the hex digest.
    """
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


__all__ = [
    "VIEW_DIRECTORY",
    "VIEW_RENDER_COMMAND",
    "ModuleViewRead",
    "RenderedView",
    "RuleViewError",
    "RuleViewNotFoundError",
    "RuleViewStartupImportError",
    "ViewStatus",
    "read_module_view",
    "refuse_view_imports",
    "render_module_views",
    "view_stamp_matches_body",
    "view_target",
]
