"""Measure the instruction chain each host loads and charge it to the prompt budget.

The certified host cap (:mod:`eawf.platform.rules.host_facts`) bounds what one
host can deliver; it is not the budget. The budget is the one prompt-budget
policy of :mod:`eawf.kernel.economics.prompt_budget`, and this module charges
it with what a direct host session actually loads, not with a labelled subset:
the file the host discovers, every file an import pulls in, and every skill the
host discovers. The files are read from the render's own output, so the
measurement follows whatever the renderer emits and never re-enumerates what a
projection contains.

The operator's global instruction document is read from the host's own home
and charged to zone 1 like the repository chain, because a rule loaded through
a host mechanism counts against the ceiling wherever it lives. A contributor no
render can see -- tool results, the host's own advertising block -- is declared
as unmeasured rather than left out, because a budget report that cannot name a
contributor cannot hold it; a chain report missing one fails validation.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final, Literal, Self

from pydantic import Field, NonNegativeInt, model_validator

from eawf.kernel.economics.prompt_budget import (
    BudgetClassId,
    PromptBudgetOutcome,
    PromptBudgetPolicy,
    RenderedSize,
    evaluate_prompt_budget,
)
from eawf.kernel.runtime.host_facts import HostFactRegistry, ProjectionKind, RuntimeHostFacts
from eawf.observability.telemetry.models import RuntimeName
from eawf.platform.rules.host_probe import SKILL_DISCOVERY
from eawf.platform.rules.records import RuleModel
from eawf.workflow.skills.catalog import SKILL_CATALOG

#: How a render judges what only this machine loads: a ``local`` render warns
#: when the operator's global instruction documents put a chain over its
#: ceiling and renders anyway; a ``certified`` render refuses.
RenderMode = Literal["local", "certified"]


class ContextContributor(StrEnum):
    """The accounting classes a host session's context is charged in."""

    ROOT = "root"
    IMPORTED = "imported"
    SKILL = "skill"
    GLOBAL = "global"
    TOOL_RESULT = "tool_result"
    HOST_ADVERTISING = "host_advertising"


#: Contributors a render cannot measure, the budget class each is charged to,
#: and why it stays unmeasured. Tool results enter through the tool surface, so
#: they are charged to the tool class; the host's advertising block is always
#: on, so it is charged to zone 1.
_UNMEASURED: Final[Mapping[ContextContributor, tuple[BudgetClassId, str]]] = {
    ContextContributor.TOOL_RESULT: (
        BudgetClassId.TOOL_CATALOG,
        "tool-result bytes arrive during a session, after the render",
    ),
    ContextContributor.HOST_ADVERTISING: (
        BudgetClassId.STEERING_ZONE1,
        "no certified host fact records the host's advertising block",
    ),
}


class ContributorMeasure(RuleModel):
    """What one contributor adds to a host session's context.

    Attributes:
        contributor: The accounting class.
        class_id: The prompt-budget class the contributor is charged to.
        sources: Repository-relative files measured, in load order.
        byte_count: The measured UTF-8 bytes; ``None`` when unmeasured.
        unmeasured_reason: Why the contributor has no measurement.
    """

    contributor: ContextContributor
    class_id: BudgetClassId
    sources: tuple[str, ...] = ()
    byte_count: NonNegativeInt | None
    unmeasured_reason: str | None = None

    @model_validator(mode="after")
    def _measured_or_explained(self) -> Self:
        """Require exactly one of a measurement and the reason it is missing.

        Raises:
            ValueError: Both or neither are set.
        """
        if (self.byte_count is None) == (self.unmeasured_reason is None):
            raise ValueError(
                f"{self.contributor.value} needs exactly one of byte_count and unmeasured_reason"
            )
        return self


class ChainBudget(RuleModel):
    """One runtime's loaded chain, contributor by contributor, and its verdict.

    Attributes:
        runtime: The runtime whose session loads the chain.
        contributors: One row per contributor and budget class.
        outcome: The prompt budget's class-by-class verdict on the chain.
    """

    runtime: RuntimeName
    contributors: tuple[ContributorMeasure, ...]
    outcome: PromptBudgetOutcome

    @model_validator(mode="after")
    def _names_every_contributor(self) -> Self:
        """Refuse a report that leaves a declared contributor out.

        Raises:
            ValueError: A contributor class has no row.
        """
        named = {row.contributor for row in self.contributors}
        missing = sorted(item.value for item in ContextContributor if item not in named)
        if missing:
            raise ValueError(f"the {self.runtime} chain report names no {', '.join(missing)}")
        return self


class GlobalDocumentFile(RuleModel):
    """One file of a host's global instruction chain, as the budget measured it.

    Attributes:
        path: The file, home-relative (``~/...``) where it lies under the home.
        byte_count: Its UTF-8 bytes.
    """

    path: str = Field(min_length=1)
    byte_count: NonNegativeInt


class GlobalDocumentOverage(RuleModel):
    """A chain the operator's global instruction documents put over its ceiling.

    Attributes:
        runtime: The runtime whose chain is over.
        class_id: The budget class that is over.
        files: The global documents the chain charges.
        measured_bytes: What the class measures, the global documents included.
        ceiling_bytes: The class's byte ceiling.
    """

    runtime: RuntimeName
    class_id: BudgetClassId
    files: tuple[GlobalDocumentFile, ...] = Field(min_length=1)
    measured_bytes: NonNegativeInt
    ceiling_bytes: NonNegativeInt

    @property
    def note(self) -> str:
        """Return the operator line naming each file, its bytes, the cap and the overage."""
        files = ", ".join(f"{item.path} ({item.byte_count} bytes)" for item in self.files)
        return (
            f"the {self.runtime} chain charges {self.class_id.value} {self.measured_bytes} "
            f"bytes with the global instruction documents {files}, "
            f"{self.measured_bytes - self.ceiling_bytes} bytes over its "
            f"{self.ceiling_bytes}-byte ceiling"
        )


class ChainBudgetReport(RuleModel):
    """Every supported runtime's loaded chain held to one prompt-budget policy.

    Attributes:
        chains: One chain per supported runtime.
        global_overages: The chains only the global instruction documents put
            over a ceiling, which a local render warns about and renders.
    """

    chains: tuple[ChainBudget, ...] = Field(min_length=1)
    global_overages: tuple[GlobalDocumentOverage, ...] = ()


def judge_loaded_chains(
    policy: PromptBudgetPolicy,
    registry: HostFactRegistry,
    *,
    projections: Mapping[ProjectionKind, str],
    outputs: Mapping[str, str],
    installed_skills: Mapping[str, str],
    global_documents: Mapping[RuntimeName, Mapping[str, str]],
) -> ChainBudgetReport:
    """Measure what each runtime loads once the render lands, and judge it.

    Args:
        policy: The prompt budget in force.
        registry: The certified host facts naming each runtime's discovery.
        projections: The repository-relative target of each projection kind.
        outputs: Every file the render writes, by repository-relative path.
        installed_skills: Skill files already on disk that the render does
            not write, by repository-relative path.
        global_documents: Each runtime's global instruction chain, text by
            display path; a runtime with none on this machine is absent.

    Returns:
        One chain per runtime, in registry order.
    """
    return ChainBudgetReport(
        chains=tuple(
            _chain_budget(
                policy,
                record,
                projections,
                outputs,
                installed_skills,
                global_documents.get(record.runtime, {}),
            )
            for record in registry.records
        )
    )


def _skill_budget_class(path: str) -> BudgetClassId:
    """Return the budget class the skill at *path* declares.

    A catalog skill declares its class on its catalog entry; a role carrier or
    a skill the catalog does not know loads on demand like any other skill, so
    it is charged to zone 2.

    Args:
        path: The skill file, ``<skill directory>/<name>/SKILL.md``.

    Returns:
        The class the skill's bytes are charged to.
    """
    entry = SKILL_CATALOG.entry(PurePosixPath(path).parent.name)
    return BudgetClassId.STEERING_ZONE2 if entry is None else entry.budget_class


def _chain_budget(
    policy: PromptBudgetPolicy,
    record: RuntimeHostFacts,
    projections: Mapping[ProjectionKind, str],
    outputs: Mapping[str, str],
    installed_skills: Mapping[str, str],
    global_document: Mapping[str, str],
) -> ChainBudget:
    """Measure and judge one runtime's chain.

    Args:
        policy: The prompt budget in force.
        record: The runtime's certified host facts.
        projections: The repository-relative target of each projection kind.
        outputs: Every file the render writes.
        installed_skills: Skill files on disk the render does not write.
        global_document: The runtime's global instruction chain.

    Returns:
        The runtime's chain with its verdict.
    """
    if record.import_shim is not None:
        root = record.import_shim
    else:
        # A runtime reading both projections takes the policy projection in
        # place of the card, and a render always writes the policy projection.
        root = projections["policy" if "policy" in record.reads else record.reads[0]]
    imported = _imports(root, outputs)
    skills = _discovered_skills(record.runtime, outputs, installed_skills)
    rows = [
        _measured(ContextContributor.ROOT, BudgetClassId.STEERING_ZONE1, (root,), outputs),
        _measured(ContextContributor.IMPORTED, BudgetClassId.STEERING_ZONE1, imported, outputs),
        _measured(
            ContextContributor.GLOBAL,
            BudgetClassId.STEERING_ZONE1,
            tuple(global_document),
            global_document,
        ),
    ]
    by_class: dict[BudgetClassId, list[str]] = {}
    for path in skills:
        by_class.setdefault(_skill_budget_class(path), []).append(path)
    if not by_class:
        by_class[BudgetClassId.STEERING_ZONE2] = []
    loaded = {**installed_skills, **outputs}
    rows.extend(
        _measured(ContextContributor.SKILL, class_id, tuple(paths), loaded)
        for class_id, paths in by_class.items()
    )
    rows.extend(
        ContributorMeasure(
            contributor=contributor, class_id=class_id, byte_count=None, unmeasured_reason=reason
        )
        for contributor, (class_id, reason) in _UNMEASURED.items()
    )
    totals: dict[BudgetClassId, int] = {}
    for row in rows:
        if row.byte_count is not None:
            totals[row.class_id] = totals.get(row.class_id, 0) + row.byte_count
    sizes = tuple(RenderedSize(class_id=class_id, bytes=size) for class_id, size in totals.items())
    return ChainBudget(
        runtime=record.runtime,
        contributors=tuple(rows),
        outcome=evaluate_prompt_budget(policy, sizes),
    )


def _measured(
    contributor: ContextContributor,
    class_id: BudgetClassId,
    sources: tuple[str, ...],
    texts: Mapping[str, str],
) -> ContributorMeasure:
    """Return one contributor's row, measured over *sources*.

    Args:
        contributor: The accounting class.
        class_id: The budget class it is charged to.
        sources: The files it loads.
        texts: The content of every file that can be loaded.

    Returns:
        The measured row.
    """
    return ContributorMeasure(
        contributor=contributor,
        class_id=class_id,
        sources=sources,
        byte_count=sum(len(texts[path].encode("utf-8")) for path in sources),
    )


def _imports(root: str, outputs: Mapping[str, str]) -> tuple[str, ...]:
    """Return every rendered file *root* pulls in through ``@path`` imports.

    Args:
        root: The file the host discovers.
        outputs: Every file the render writes.

    Returns:
        The imported files in load order, each once, *root* excluded.
    """
    chain: list[str] = []
    pending = [root]
    while pending:
        current = pending.pop(0)
        for line in outputs.get(current, "").splitlines():
            if not line.startswith("@") or len(line) == 1:
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(current), line[1:]))
            if target in outputs and target != root and target not in chain:
                chain.append(target)
                pending.append(target)
    return tuple(chain)


def _discovered_skills(
    runtime: RuntimeName, outputs: Mapping[str, str], installed_skills: Mapping[str, str]
) -> tuple[str, ...]:
    """Return the skill files *runtime* discovers once the render lands.

    Args:
        runtime: The runtime.
        outputs: Every file the render writes.
        installed_skills: Skill files on disk the render does not write.

    Returns:
        Sorted repository-relative skill files; empty for a runtime that
        discovers no skills.
    """
    pattern = SKILL_DISCOVERY.get(runtime)
    if pattern is None:
        return ()
    return tuple(
        sorted(
            path
            for path in (*outputs, *installed_skills)
            if PurePosixPath(path).full_match(pattern)
        )
    )


__all__ = [
    "ChainBudget",
    "ChainBudgetReport",
    "ContextContributor",
    "ContributorMeasure",
    "GlobalDocumentFile",
    "GlobalDocumentOverage",
    "RenderMode",
    "judge_loaded_chains",
]
