"""Render the project card and the policy projection from the compiled rule graph.

Two projections are rendered, in one transaction, from the rule graph and
never from each other:

- the **project card** (``AGENTS.md``, committed): the typed project brief,
  every constitution rule, every other ``must`` rule and the module index.
  It compiles from committed inputs only, so every machine renders the same
  bytes from one commit.
- the **policy projection** (``AGENTS.override.md``, generated and
  gitignored): the brief, every constitution rule, every other ``must``
  rule, the steering guidance and the module index. It compiles through the
  one resolver, so it also carries the machine-local workspace layer.

Both projections carry every ``must`` rule not scoped to a role, because a
runtime that loads only one of them must still receive every obligation;
the compiler refuses a ``must`` rule no projection every runtime loads would
render. Retrievable ``should`` and ``information`` rules are reachable
through the module index only.

Each projection opens with one stamp line naming the graph digest it was
rendered from and the digest of the text below the stamp, so a hand edit is
detected from the file alone, without the gitignored manifest. A render
refuses to overwrite a hand-edited projection, refuses a projection over the
smallest certified project-document cap among the runtimes that read it
(:mod:`eawf.platform.rules.host_facts`), writes every target plus the manifest sidecar or
none of them, and removes generated targets the previous render wrote that
the current one no longer does.

The same transaction writes three kinds of generated companion, from the
same graphs: one import shim per runtime that discovers policy only through
its own file (its whole content imports the policy projection), one
detailed view per selected module, stamped with the policy graph digest,
which the module index names and ``eawf rules view`` reads, and one role
carrier per role the policy graph scopes rules to; the agent definitions
of that role embed the same text rendered from the builtin rules.

:func:`render_rule_projections` is the entry point; :func:`plan_rule_projections`
computes the same output without touching disk, which is what a drift check
compares against.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tomllib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Final, Literal

from pydantic import NonNegativeInt, PositiveInt, ValidationError

from eawf.kernel.fsync import fsync_parent_dir
from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.models import Project
from eawf.observability.telemetry.models import RuntimeName
from eawf.platform.rules.carriers import carrier_stamp_matches_body, render_role_carriers
from eawf.platform.rules.compile import (
    CompiledRule,
    RuleGraph,
    compile_card_graph,
    compile_rule_graph,
)
from eawf.platform.rules.compose import BuiltinRuleProvider
from eawf.platform.rules.conduct import load_conduct_rules
from eawf.platform.rules.host_facts import (
    HostFactRegistry,
    ProjectionKind,
    StaleHostFact,
    load_host_facts,
    smallest_certified_cap,
    stale_host_facts,
)
from eawf.platform.rules.loader import RULE_SOURCE_PATH, load_rule_source
from eawf.platform.rules.modules import (
    RuleModule,
    RuleModuleSelection,
    builtin_rule_modules,
    render_module_index,
    select_rule_modules,
)
from eawf.platform.rules.records import RuleModel, RuleRecord, RuleSourceIdentity
from eawf.platform.rules.views import (
    ModuleViewRead,
    read_module_view,
    render_module_views,
    view_stamp_matches_body,
)

logger = logging.getLogger(__name__)

#: The committed project card, relative to the repository root.
CARD_TARGET: Final[str] = "AGENTS.md"

#: The generated policy projection, relative to the repository root.
POLICY_TARGET: Final[str] = "AGENTS.override.md"

#: The projection manifest sidecar, relative to the repository root.
PROJECTION_MANIFEST_PATH: Final[str] = ".ea/indexes/rule-projections.json"

#: The profile renderer's manifest; its rows for a projection target are
#: obsolete once the rule graph renders that target.
LEGACY_MANIFEST_PATH: Final[str] = ".ea/indexes/generated.json"

#: What an existing projection target is, as far as a render is concerned.
ProjectionState = Literal[
    "absent", "generated", "hand_edited", "legacy_generated", "operator_owned"
]

_STAMP: Final[re.Pattern[str]] = re.compile(
    r"^<!-- eawf:projection kind=(?P<kind>card|policy) graph=(?P<graph>sha256:[0-9a-f]{64}) "
    r"body=(?P<body>sha256:[0-9a-f]{64}) .*-->$"
)
# An import shim is one ``@path`` line per imported file and nothing else.
_IMPORT_ONLY: Final[re.Pattern[str]] = re.compile(r"(?:@\S+\n)*@\S+\n?")
_STAMP_NOTE: Final[str] = (
    f"generated from {RULE_SOURCE_PATH.as_posix()} by eawf sync; a hand edit fails validation"
)


class RuleProjectionError(ValueError):
    """Base class for a refusal raised while rendering rule projections.

    Attributes:
        code: The stable failure code.
    """

    code: ClassVar[str] = "rule_projection_error"


class RuleProjectionBudgetError(RuleProjectionError):
    """A projection exceeds, or has no, certified project-document cap."""

    code: ClassVar[str] = "rule_projection_budget"


class RuleProjectionHandEditError(RuleProjectionError):
    """A generated projection was edited by hand since it was rendered."""

    code: ClassVar[str] = "rule_projection_hand_edit"


class RuleProjectionOwnedError(RuleProjectionError):
    """A projection target holds prose no renderer wrote."""

    code: ClassVar[str] = "rule_projection_operator_owned"


class RuleProjectionSelectionError(RuleProjectionError):
    """The builtin provider was asked for modules other than the ones selected."""

    code: ClassVar[str] = "rule_projection_selection"


class ProjectBrief(RuleModel):
    """Stable repository orientation, read from committed typed sources.

    Attributes:
        title: The project title.
        purpose: One sentence on what the project is.
        code: The project code.
        domains: The project's declared domains.
        package: The distributed package name.
        python: The supported Python requirement.
        default_branch: The branch deliveries merge into.
    """

    title: str
    purpose: str | None = None
    code: str | None = None
    domains: tuple[str, ...] = ()
    package: str | None = None
    python: str | None = None
    default_branch: str | None = None


class RuleSpan(RuleModel):
    """Where one rule's line sits in a rendered projection.

    Attributes:
        rule_id: The rendered rule.
        start_byte: UTF-8 offset of the line's first byte.
        end_byte: UTF-8 offset one past the line's newline.
    """

    rule_id: str
    start_byte: NonNegativeInt
    end_byte: PositiveInt


class ProjectionRecord(RuleModel):
    """The manifest row of one rendered projection.

    Attributes:
        kind: ``card`` or ``policy``.
        target: Repository-relative path of the projection.
        graph_digest: Digest of the rule graph it was rendered from.
        selection_digest: Digest of the exact rule set it renders; equal
            only for projections rendering equivalent rule sets.
        output_digest: Digest of the rendered bytes.
        byte_count: Rendered size in UTF-8 bytes.
        line_count: Rendered line count.
        cap_bytes: The byte cap the projection was held to: the smallest
            certified default among the runtimes that read it.
        cap_runtime: The runtime that certified ``cap_bytes``.
        uncertified_readers: Runtimes that read the projection with no
            certified cap of their own; ``cap_bytes`` does not certify
            delivery to them.
        headroom_bytes: ``cap_bytes - byte_count``.
        rule_spans: One span per rendered rule.
    """

    kind: ProjectionKind
    target: str
    graph_digest: Sha256DigestStr
    selection_digest: Sha256DigestStr
    output_digest: Sha256DigestStr
    byte_count: PositiveInt
    line_count: PositiveInt
    cap_bytes: PositiveInt
    cap_runtime: RuntimeName
    uncertified_readers: tuple[RuntimeName, ...] = ()
    headroom_bytes: NonNegativeInt
    rule_spans: tuple[RuleSpan, ...]


class ProjectionManifest(RuleModel):
    """The sidecar recording one render generation.

    Attributes:
        schema_version: Gates unknown future formats.
        generation: Digest of every written output; equal renders share it,
            so a fresh render equals an incremental one.
        sources: Every rule source the projections were compiled from.
        projections: One record per rendered projection.
        generated: Repository-relative targets of the import shims, the
            module views and the role carriers the same render wrote.
        stale_host_facts: Certified host facts past their re-measure age on
            the day of the render.
    """

    schema_version: Literal[1] = 1
    generation: Sha256DigestStr
    sources: tuple[RuleSourceIdentity, ...]
    projections: tuple[ProjectionRecord, ...]
    generated: tuple[str, ...] = ()
    stale_host_facts: tuple[StaleHostFact, ...] = ()

    @property
    def host_fact_warnings(self) -> tuple[str, ...]:
        """Return one operator line per uncertified reader and stale fact."""
        uncertified = tuple(
            f"{record.target} is read by {', '.join(record.uncertified_readers)} with no "
            f"certified project-document cap; the {record.cap_bytes}-byte cap certified for "
            f"{record.cap_runtime} does not certify delivery to them"
            for record in self.projections
            if record.uncertified_readers
        )
        return (*uncertified, *(fact.note for fact in self.stale_host_facts))

    @property
    def targets(self) -> tuple[str, ...]:
        """Return the repository-relative target of every written output."""
        return (*(record.target for record in self.projections), *self.generated)


class RenderedProjection(RuleModel):
    """One projection's text with its manifest row.

    Attributes:
        record: The manifest row.
        text: The complete file content.
    """

    record: ProjectionRecord
    text: str


class GeneratedFile(RuleModel):
    """A generated companion of the projections: the import shim or a view.

    Attributes:
        target: Repository-relative path.
        text: The complete file content.
    """

    target: str
    text: str


class ProjectionPlan(RuleModel):
    """Everything one render would write.

    Attributes:
        projections: The rendered projections.
        generated: The import shims, the module views, then the role
            carriers.
        manifest: The manifest sidecar describing them.
    """

    projections: tuple[RenderedProjection, ...]
    generated: tuple[GeneratedFile, ...]
    manifest: ProjectionManifest

    @property
    def outputs(self) -> tuple[tuple[str, str], ...]:
        """Return ``(target, text)`` of every output, projections first."""
        return (
            *((p.record.target, p.text) for p in self.projections),
            *((g.target, g.text) for g in self.generated),
        )


class ProjectionWrite(RuleModel):
    """The outcome of a render transaction.

    Attributes:
        manifest: The manifest that was written.
        changed: Targets whose bytes changed, in render order: the
            projections, then the import shim, the module views and the
            role carriers.
        removed: Obsolete targets that were deleted.
    """

    manifest: ProjectionManifest
    changed: tuple[str, ...]
    removed: tuple[str, ...]


def rule_source_present(repo_root: Path) -> bool:
    """Return whether the repository authors its rules in ``.ea/rules.yaml``.

    A repository with a rule source has switched its steering files to the
    rule graph; one without keeps the profile renderer.

    Args:
        repo_root: The repository root.

    Returns:
        ``True`` when ``.ea/rules.yaml`` exists.
    """
    return repo_root.joinpath(*RULE_SOURCE_PATH.parts).is_file()


def builtin_rule_provider(selection: RuleModuleSelection) -> BuiltinRuleProvider:
    """Build the provider that resolves module references to builtin records.

    Core modules and the conduct module carry obligations that bind every
    repository, so they apply whether or not the repository selects them;
    a repository cannot drop a protected constitution rule by leaving its
    module out. Catalog modules apply only when selected.

    Args:
        selection: The repository's resolved module selection.

    Returns:
        A provider returning the selected modules' records, every core
        module's records and the conduct records.
    """
    selected = frozenset(entry.reference for entry in selection.entries)

    def provide(modules: tuple[str, ...]) -> tuple[RuleRecord, ...]:
        if frozenset(modules) != selected:
            raise RuleProjectionSelectionError(
                f"the rule source selects {sorted(set(modules))} but the render resolved "
                f"{sorted(selected)}; {RULE_SOURCE_PATH.as_posix()} changed during the render"
            )
        applied: dict[str, RuleModule] = {
            entry.reference: entry.module for entry in selection.entries if entry.module
        }
        for reference, module in builtin_rule_modules().items():
            if module.document.kind == "core":
                applied.setdefault(reference, module)
        records = [record for reference in sorted(applied) for record in applied[reference].records]
        return (*records, *load_conduct_rules())

    return provide


def load_project_brief(repo_root: Path) -> ProjectBrief:
    """Read the project brief from the committed project record and package data.

    Only stable orientation is read: never the current phase, branch
    position, daemon health or recent decisions.

    Args:
        repo_root: The repository root.

    Returns:
        The brief; fields with no committed source stay empty.

    Raises:
        RuleProjectionError: When ``.ea/state.json`` or ``pyproject.toml``
            exists but is unreadable, or the project record fails its schema.
    """
    project = _read_project_record(repo_root)
    package = _read_package_table(repo_root)
    name = package.get("name")
    description = package.get("description")
    python = package.get("requires-python")
    title = project.title if project else (name if isinstance(name, str) else repo_root.name)
    purpose = (project.description if project else None) or (
        description if isinstance(description, str) else None
    )
    return ProjectBrief(
        title=title,
        purpose=purpose,
        code=project.code if project else None,
        domains=tuple(project.domains) if project else (),
        package=name if isinstance(name, str) else None,
        python=python if isinstance(python, str) else None,
        default_branch=project.default_branch if project else None,
    )


def plan_rule_projections(repo_root: Path, *, home: Path | None = None) -> ProjectionPlan:
    """Compute the card, the policy projection and the manifest without writing.

    Reads the rule source and the brief sources only; no rendered projection
    is an input.

    Args:
        repo_root: The repository root holding ``.ea/rules.yaml``.
        home: The directory holding the ``.eawf`` home that registers a
            workspace; ``None`` for the user's home directory.

    Returns:
        The plan a render transaction writes.

    Raises:
        RuleSourceError: When a rule source fails to load.
        RuleCompileError: When the rules fail compilation.
        RuleProjectionBudgetError: When a projection exceeds the smallest
            certified cap among the runtimes that read it, or none of them
            has a certified cap.
        RuleProjectionError: When a brief source is unreadable.
        HostFactError: When the shipped host-fact record is untrustworthy.
    """
    # The shim renderer lives with the runtime adapters; importing it lazily
    # keeps that module free to import this package's view refusal.
    from eawf.surfaces.render.claude_shim import render_import_shim

    host_facts = load_host_facts()
    selection, policy_graph = _policy_graph(repo_root, home=home)
    card_graph = compile_card_graph(repo_root, builtin_rules=builtin_rule_provider(selection))
    brief = load_project_brief(repo_root)
    index = render_module_index(selection)
    projections = (
        _render_projection(
            kind="card", graph=card_graph, brief=brief, index=index, host_facts=host_facts
        ),
        _render_projection(
            kind="policy", graph=policy_graph, brief=brief, index=index, host_facts=host_facts
        ),
    )
    shim = render_import_shim((POLICY_TARGET,))
    generated = (
        *(
            GeneratedFile(target=record.import_shim, text=shim)
            for record in host_facts.records
            if record.import_shim is not None
        ),
        *(
            GeneratedFile(target=view.target, text=view.text)
            for view in render_module_views(selection, policy_graph)
        ),
        *(
            GeneratedFile(target=carrier.target, text=carrier.text)
            for carrier in render_role_carriers(policy_graph)
        ),
    )
    sources = sorted(
        {*card_graph.sources, *policy_graph.sources},
        key=lambda source: (source.kind, source.locator, source.digest),
    )
    output_digests = [
        *(p.record.output_digest for p in projections),
        *(_sha256_text(g.text) for g in generated),
    ]
    manifest = ProjectionManifest(
        generation=_sha256_text(json.dumps(output_digests, separators=(",", ":"))),
        sources=tuple(sources),
        projections=tuple(p.record for p in projections),
        generated=tuple(g.target for g in generated),
        stale_host_facts=stale_host_facts(host_facts, today=datetime.now(UTC).date()),
    )
    return ProjectionPlan(projections=projections, generated=generated, manifest=manifest)


def read_rule_view(repo_root: Path, reference: str, *, home: Path | None = None) -> ModuleViewRead:
    """Read a selected module's view and judge it against the current graph.

    Args:
        repo_root: The repository root holding ``.ea/rules.yaml``.
        reference: The module reference the module index names.
        home: The directory holding the ``.eawf`` home; ``None`` for the
            user's home directory.

    Returns:
        The view with its ``current`` / ``stale`` / ``absent`` status.

    Raises:
        RuleSourceError: When a rule source fails to load.
        RuleCompileError: When the rules fail compilation.
        RuleViewNotFoundError: When ``reference`` names no selected,
            available module.
    """
    selection, graph = _policy_graph(repo_root, home=home)
    return read_module_view(repo_root, reference, selection=selection, graph_digest=graph.digest)


def _policy_graph(repo_root: Path, *, home: Path | None) -> tuple[RuleModuleSelection, RuleGraph]:
    """Resolve the module selection and compile the policy graph.

    Args:
        repo_root: The repository root.
        home: The directory holding the ``.eawf`` home.

    Returns:
        The selection and the graph the policy projection and the views
        render from.
    """
    selection = select_rule_modules(load_rule_source(repo_root))
    graph = compile_rule_graph(repo_root, builtin_rules=builtin_rule_provider(selection), home=home)
    return selection, graph


def classify_projection(path: Path) -> ProjectionState:
    """Classify an existing projection target before a render replaces it.

    Args:
        path: The target path.

    Returns:
        ``absent``; ``generated`` when a projection or view stamp matches
        the text below it, a carrier whose frontmatter and stamp match, or
        an import-only shim, which carries no prose; ``hand_edited`` when a stamp does not match;
        ``legacy_generated`` for an unstamped file made only of
        profile-renderer managed regions; or ``operator_owned`` for anything
        else.
    """
    if not path.is_file():
        return "absent"
    text = path.read_text(encoding="utf-8")
    stamp, _, body = text.partition("\n")
    match = _STAMP.fullmatch(stamp)
    if match is not None:
        return "generated" if _sha256_text(body) == match.group("body") else "hand_edited"
    view = view_stamp_matches_body(text)
    if view is not None:
        return "generated" if view else "hand_edited"
    carrier = carrier_stamp_matches_body(text)
    if carrier is not None:
        return "generated" if carrier else "hand_edited"
    if _IMPORT_ONLY.fullmatch(text):
        return "generated"
    return "legacy_generated" if _only_managed_regions(text) else "operator_owned"


def projection_drift(repo_root: Path, plan: ProjectionPlan) -> tuple[str, ...]:
    """Name every projection target whose bytes differ from the plan.

    A hand-edited or missing projection counts as drift.

    Args:
        repo_root: The repository root.
        plan: The plan to compare against.

    Returns:
        Drifted targets, in render order.
    """
    drifted: list[str] = []
    for target, text in plan.outputs:
        path = repo_root / target
        if not path.is_file() or path.read_bytes() != text.encode("utf-8"):
            drifted.append(target)
    return tuple(drifted)


def render_rule_projections(repo_root: Path, *, home: Path | None = None) -> ProjectionWrite:
    """Render the card and the policy projection and write them in one transaction.

    Args:
        repo_root: The repository root holding ``.ea/rules.yaml``.
        home: The directory holding the ``.eawf`` home; ``None`` for the
            user's home directory.

    Returns:
        What the transaction wrote and removed.

    Raises:
        RuleSourceError: When a rule source fails to load.
        RuleCompileError: When the rules fail compilation.
        RuleProjectionError: When a projection is over the cap, a target was
            edited by hand or holds operator prose, or a brief source is
            unreadable.
        OSError: When a write fails; every target is restored first.
    """
    return write_rule_projections(repo_root, plan_rule_projections(repo_root, home=home))


def refresh_rule_projections(repo_root: Path) -> tuple[str, ...]:
    """Render the projections when the repository has a rule source.

    Args:
        repo_root: The repository root.

    Returns:
        The targets whose bytes changed; empty for a repository without
        ``.ea/rules.yaml``, which keeps the profile renderer.

    Raises:
        RuleSourceError: When a rule source fails to load.
        RuleCompileError: When the rules fail compilation.
        RuleProjectionError: See :func:`render_rule_projections`.
    """
    if not rule_source_present(repo_root):
        return ()
    return render_rule_projections(repo_root).changed


def write_rule_projections(repo_root: Path, plan: ProjectionPlan) -> ProjectionWrite:
    """Write a plan's projections and manifest, or nothing.

    Every target is validated before any byte is written. The prior bytes of
    every touched path are held in memory, and any failure part-way restores
    them, so an interrupted render leaves the previous generation intact.

    Args:
        repo_root: The repository root.
        plan: The plan to write.

    Returns:
        What was written and removed.

    Raises:
        RuleProjectionHandEditError: When a target was edited by hand.
        RuleProjectionOwnedError: When a target holds operator prose.
        OSError: When a write fails, after restoring every target.
    """
    outputs = plan.outputs
    for target, _text in outputs:
        _require_replaceable(repo_root / target)
    manifest_path = repo_root / PROJECTION_MANIFEST_PATH
    writes: dict[Path, bytes] = {
        repo_root / target: text.encode("utf-8") for target, text in outputs
    }
    writes[manifest_path] = _manifest_bytes(plan.manifest)
    legacy_path, legacy_bytes = _legacy_manifest_update(repo_root, plan.manifest.targets)
    if legacy_bytes is not None:
        writes[legacy_path] = legacy_bytes
    obsolete = _obsolete_targets(repo_root, plan.manifest)
    prior = {path: _read_or_none(path) for path in (*writes, *obsolete)}
    try:
        for path, payload in writes.items():
            if prior[path] != payload:
                _atomic_write_bytes(path, payload)
        for path in obsolete:
            path.unlink(missing_ok=True)
    except BaseException:
        _restore(prior)
        raise
    changed = tuple(
        target
        for target, _text in outputs
        if prior[repo_root / target] != writes[repo_root / target]
    )
    removed = tuple(path.relative_to(repo_root).as_posix() for path in obsolete)
    logger.info(
        f"rule projections written generation={plan.manifest.generation} "
        f"changed={list(changed)} removed={list(removed)}"
    )
    return ProjectionWrite(manifest=plan.manifest, changed=changed, removed=removed)


def _render_projection(
    *,
    kind: ProjectionKind,
    graph: RuleGraph,
    brief: ProjectBrief,
    index: str,
    host_facts: HostFactRegistry,
) -> RenderedProjection:
    """Render one projection and measure it against its certified cap.

    Args:
        kind: ``card`` or ``policy``.
        graph: The graph the projection renders.
        brief: The project brief.
        index: The rendered module index.
        host_facts: The certified host facts the cap resolves from.

    Returns:
        The projection with its manifest row.

    Raises:
        RuleProjectionBudgetError: When the projection exceeds the smallest
            certified cap among its readers, or no reader has one.
    """
    target = CARD_TARGET if kind == "card" else POLICY_TARGET
    builder = _TextBuilder()
    builder.add(_brief_text(brief, kind=kind))
    for heading, intro, rules in _sections(kind, graph):
        if not rules:
            continue
        builder.add(f"\n## {heading}\n\n{intro}\n\n")
        for rule in rules:
            builder.add_rule(rule)
    if index:
        builder.add(f"\n## Rule modules\n\n{index}")
    body = builder.text
    stamp = (
        f"<!-- eawf:projection kind={kind} graph={graph.digest} "
        f"body={_sha256_text(body)} {_STAMP_NOTE} -->\n"
    )
    text = stamp + body
    size = len(text.encode("utf-8"))
    offset = len(stamp.encode("utf-8"))
    cap = smallest_certified_cap(host_facts, kind)
    if cap is None:
        raise RuleProjectionBudgetError(
            f"{target} has no certified project-document cap: no runtime that reads it "
            f"has one, and a render never publishes against an assumed limit"
        )
    if size > cap.cap_bytes:
        lost = next(
            (span.rule_id for span in builder.spans if span.end_byte + offset > cap.cap_bytes),
            None,
        )
        first_lost = f"; the first rule lost past the cut is {lost}" if lost else ""
        raise RuleProjectionBudgetError(
            f"{target} renders {size} bytes, over the {cap.cap_bytes}-byte project-document "
            f"cap certified for {cap.runtime}{first_lost}; move rules out of the "
            f"constitution or into a module"
        )
    record = ProjectionRecord(
        kind=kind,
        target=target,
        graph_digest=graph.digest,
        selection_digest=_sha256_text(json.dumps(sorted(builder.rendered), separators=(",", ":"))),
        output_digest=_sha256_text(text),
        byte_count=size,
        line_count=text.count("\n"),
        cap_bytes=cap.cap_bytes,
        cap_runtime=cap.runtime,
        uncertified_readers=cap.uncertified,
        headroom_bytes=cap.cap_bytes - size,
        rule_spans=tuple(
            RuleSpan(
                rule_id=span.rule_id,
                start_byte=span.start_byte + offset,
                end_byte=span.end_byte + offset,
            )
            for span in builder.spans
        ),
    )
    return RenderedProjection(record=record, text=text)


def _sections(
    kind: ProjectionKind, graph: RuleGraph
) -> tuple[tuple[str, str, tuple[CompiledRule, ...]], ...]:
    """Split a graph into the headed sections one projection renders.

    Args:
        kind: The projection being rendered.
        graph: The graph to split.

    Returns:
        ``(heading, intro, rules)`` per section, in render order. A rule
        scoped to a role is left out: the agent definitions of that role
        embed it.
    """
    rules = tuple(rule for rule in graph.rules if not rule.record.scope.roles)
    constitution = (
        "Constitution",
        "Each rule below binds every session in this repository.",
        tuple(rule for rule in rules if rule.record.zone == "constitution"),
    )
    obligations = (
        "Obligations",
        "Each rule below binds within the scope it names.",
        tuple(
            rule
            for rule in rules
            if rule.record.force == "must" and rule.record.zone != "constitution"
        ),
    )
    if kind == "card":
        return (constitution, obligations)
    return (
        constitution,
        obligations,
        (
            "Guidance",
            "Follow each rule below unless a stated reason in the task overrides it.",
            _steering(rules, force="should"),
        ),
        (
            "Information",
            "Context that shapes how the rules above apply.",
            _steering(rules, force="information"),
        ),
    )


def _steering(rules: Iterable[CompiledRule], *, force: str) -> tuple[CompiledRule, ...]:
    """Select the steering rules of one force.

    Args:
        rules: The graph's rules.
        force: ``should`` or ``information``.

    Returns:
        The matching steering rules.
    """
    return tuple(
        rule for rule in rules if rule.record.zone == "steering" and rule.record.force == force
    )


def _brief_text(brief: ProjectBrief, *, kind: ProjectionKind) -> str:
    """Render the project brief that opens a projection.

    Args:
        brief: The brief.
        kind: The projection being rendered.

    Returns:
        The heading, purpose and brief bullets.
    """
    lines = [f"# {brief.title}", ""]
    if brief.purpose:
        lines += [brief.purpose, ""]
    lines += ["## Project brief", ""]
    if brief.code:
        domains = f"; domains: {', '.join(brief.domains)}" if brief.domains else ""
        lines.append(f"- Project code `{brief.code}`{domains}.")
    if brief.package:
        python = f"Python {brief.python}, " if brief.python else ""
        lines.append(f"- Stack: {python}distributed as the `{brief.package}` package.")
    if brief.default_branch:
        lines.append(f"- Default branch: `{brief.default_branch}`.")
    source = RULE_SOURCE_PATH.as_posix()
    if kind == "card":
        lines.append(
            f"- Rules come from `{source}`, the only rule source. This card carries every "
            f"binding rule; the complete agent policy, guidance included, arrives with the "
            f"installed tool, which renders it into `{POLICY_TARGET}` on `eawf sync`."
        )
    else:
        lines.append(
            f"- This file is the complete agent policy, rendered from `{source}` by "
            f"`eawf sync`; it takes the place of `{CARD_TARGET}` for a runtime that reads it."
        )
    return "\n".join(lines) + "\n"


class _TextBuilder:
    """Accumulate projection text and the byte span of every rule line."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._size = 0
        self.spans: list[RuleSpan] = []
        self.rendered: list[tuple[str, str]] = []

    @property
    def text(self) -> str:
        """Return the accumulated text."""
        return "".join(self._parts)

    def add(self, text: str) -> None:
        """Append ``text``.

        Args:
            text: The text to append.
        """
        self._parts.append(text)
        self._size += len(text.encode("utf-8"))

    def add_rule(self, rule: CompiledRule) -> None:
        """Append one rule's line and record its span.

        Args:
            rule: The rule to render.
        """
        start = self._size
        self.add(_rule_line(rule.record))
        self.spans.append(
            RuleSpan(rule_id=rule.record.rule_id, start_byte=start, end_byte=self._size)
        )
        self.rendered.append((rule.record.rule_id, rule.digest))


def _rule_line(record: RuleRecord) -> str:
    """Render one rule as a single list line.

    Args:
        record: The rule.

    Returns:
        ``- **Title.** instruction`` plus the scope and procedure, newline
        terminated.
    """
    scope = record.scope
    tokens = (*scope.activities, *scope.roles, *scope.paths)
    applies = f" Applies to: {', '.join(tokens)}." if tokens else ""
    procedure = f" Procedure: `{record.procedure_ref}`." if record.procedure_ref else ""
    return f"- **{record.title}.** {record.instruction}{applies}{procedure}\n"


def _require_replaceable(path: Path) -> None:
    """Refuse to overwrite a projection target the render does not own.

    Args:
        path: The target path.

    Raises:
        RuleProjectionHandEditError: When the target was edited by hand.
        RuleProjectionOwnedError: When the target holds operator prose.
    """
    state = classify_projection(path)
    if state == "hand_edited":
        raise RuleProjectionHandEditError(
            f"{path.name} was edited by hand since eawf sync rendered it; move the change "
            f"into {RULE_SOURCE_PATH.as_posix()}, restore or delete {path.name}, and re-run "
            f"eawf sync"
        )
    if state == "operator_owned":
        raise RuleProjectionOwnedError(
            f"{path.name} holds prose no renderer wrote; author it as rules in "
            f"{RULE_SOURCE_PATH.as_posix()}, then delete {path.name} and re-run eawf sync"
        )


def _only_managed_regions(text: str) -> bool:
    """Return whether ``text`` is made only of profile-renderer managed regions.

    Args:
        text: The file content.

    Returns:
        ``True`` for empty text or text whose every non-blank byte sits in a
        well-formed managed region.
    """
    # The profile renderer's region grammar lives with that renderer.
    from eawf.surfaces.render.regions import RegionParseError, find_regions

    try:
        regions = find_regions(text)
    except RegionParseError:
        return False
    cursor = 0
    for region in regions:
        start, end = region.span
        if text[cursor:start].strip():
            return False
        cursor = end
    return not text[cursor:].strip()


def _obsolete_targets(repo_root: Path, manifest: ProjectionManifest) -> tuple[Path, ...]:
    """Name the targets the previous render wrote that this one does not.

    Args:
        repo_root: The repository root.
        manifest: The manifest about to be written.

    Returns:
        Existing obsolete targets the render generated; a target edited since
        is refused rather than deleted.

    Raises:
        RuleProjectionHandEditError: When an obsolete target was edited.
    """
    prior = _read_prior_manifest(repo_root)
    if prior is None:
        return ()
    obsolete: list[Path] = []
    for target in prior.targets:
        path = repo_root / target
        if target in manifest.targets or not path.is_file():
            continue
        _require_replaceable(path)
        obsolete.append(path)
    return tuple(obsolete)


def _read_prior_manifest(repo_root: Path) -> ProjectionManifest | None:
    """Read the previous render's manifest, if one is readable.

    Args:
        repo_root: The repository root.

    Returns:
        The manifest, or ``None`` when it is absent or unreadable; an
        unreadable sidecar only loses obsolete-target cleanup, and the
        render rewrites it.
    """
    path = repo_root / PROJECTION_MANIFEST_PATH
    if not path.is_file():
        return None
    try:
        return ProjectionManifest.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        logger.warning(f"rule projection manifest unreadable path={path} error={exc}")
        return None


def _legacy_manifest_update(repo_root: Path, targets: Iterable[str]) -> tuple[Path, bytes | None]:
    """Drop the profile renderer's rows for targets the rule graph now renders.

    Args:
        repo_root: The repository root.
        targets: Repository-relative projection targets.

    Returns:
        The legacy manifest path and its new bytes, or ``None`` when no row
        needs dropping.

    Raises:
        RuleProjectionError: When the legacy manifest is unreadable.
    """
    from eawf.surfaces.render.manifest import Manifest, load

    path = repo_root / LEGACY_MANIFEST_PATH
    if not path.is_file():
        return path, None
    try:
        legacy = load(path)
    except (ValueError, ValidationError) as exc:
        raise RuleProjectionError(f"{LEGACY_MANIFEST_PATH} is unreadable: {exc}") from exc
    owned = {(repo_root / target).resolve() for target in targets}
    kept = {
        key: entry
        for key, entry in legacy.generated.items()
        if _resolve_target(repo_root, entry.target) not in owned
    }
    if len(kept) == len(legacy.generated):
        return path, None
    updated = Manifest(version=legacy.version, generated=kept)
    payload = json.dumps(updated.model_dump(mode="json"), sort_keys=True, indent=2)
    return path, payload.encode("utf-8")


def _resolve_target(repo_root: Path, target: str) -> Path:
    """Resolve a manifest target string against the repository root.

    Args:
        repo_root: The repository root.
        target: An absolute or repository-relative POSIX path.

    Returns:
        The resolved path.
    """
    path = Path(target)
    return (path if path.is_absolute() else repo_root / path).resolve()


def _manifest_bytes(manifest: ProjectionManifest) -> bytes:
    """Serialise the manifest deterministically.

    Args:
        manifest: The manifest.

    Returns:
        Sorted, indented JSON with a trailing newline.
    """
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2)
    return f"{payload}\n".encode()


def _read_or_none(path: Path) -> bytes | None:
    """Return a file's bytes, or ``None`` when it does not exist.

    Args:
        path: The file.

    Returns:
        The bytes or ``None``.
    """
    return path.read_bytes() if path.is_file() else None


def _restore(prior: dict[Path, bytes | None]) -> None:
    """Put every touched path back to the bytes it held before the render.

    Args:
        prior: Each touched path's prior bytes, ``None`` for absent.
    """
    for path, payload in prior.items():
        if _read_or_none(path) == payload:
            continue
        if payload is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(path, payload)
    logger.warning(f"rule projection render rolled back paths={len(prior)}")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` through a synced sibling temp file.

    Args:
        path: The destination.
        payload: The bytes to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_parent_dir(path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_project_record(repo_root: Path) -> Project | None:
    """Read the committed project record from ``.ea/state.json``.

    Args:
        repo_root: The repository root.

    Returns:
        The project record, or ``None`` when there is no state or no project.

    Raises:
        RuleProjectionError: When the state file is unreadable or the project
            record fails its schema.
    """
    path = repo_root / ".ea" / "state.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_bytes())
        project = payload.get("project") if isinstance(payload, dict) else None
        return None if project is None else Project.model_validate(project)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RuleProjectionError(f".ea/state.json project record is unreadable: {exc}") from exc


def _read_package_table(repo_root: Path) -> dict[str, object]:
    """Read the ``[project]`` table of ``pyproject.toml``.

    Args:
        repo_root: The repository root.

    Returns:
        The table, or an empty mapping when there is no package file.

    Raises:
        RuleProjectionError: When ``pyproject.toml`` is not valid TOML.
    """
    path = repo_root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    except tomllib.TOMLDecodeError as exc:
        raise RuleProjectionError(f"pyproject.toml is not valid TOML: {exc}") from exc
    return table if isinstance(table, dict) else {}


def _sha256_text(text: str) -> str:
    """Digest UTF-8 text.

    Args:
        text: The text.

    Returns:
        ``sha256:`` followed by the hex digest.
    """
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


__all__ = [
    "CARD_TARGET",
    "LEGACY_MANIFEST_PATH",
    "POLICY_TARGET",
    "PROJECTION_MANIFEST_PATH",
    "GeneratedFile",
    "ProjectBrief",
    "ProjectionManifest",
    "ProjectionPlan",
    "ProjectionRecord",
    "ProjectionWrite",
    "RenderedProjection",
    "RuleProjectionBudgetError",
    "RuleProjectionError",
    "RuleProjectionHandEditError",
    "RuleProjectionOwnedError",
    "RuleProjectionSelectionError",
    "RuleSpan",
    "builtin_rule_provider",
    "classify_projection",
    "load_project_brief",
    "plan_rule_projections",
    "projection_drift",
    "read_rule_view",
    "refresh_rule_projections",
    "render_rule_projections",
    "rule_source_present",
    "write_rule_projections",
]
