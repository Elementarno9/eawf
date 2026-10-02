"""Check a render against the prompt budget, model prose, the ignore file and each host.

Two checks refuse a plan before it exists
(:func:`eawf.platform.rules.render.plan_rule_projections`):

- the chain every host loads once the render lands is charged to the
  prompt-budget policy resolved from the ``economics`` configuration
  (:mod:`eawf.platform.rules.chain_budget`), and a repository-authored
  class over its ceiling fails the render rather than warning, because the
  host cap bounds delivery, not what the project has chosen to spend; the
  operator's global instruction documents are charged too, and fail only a
  certified render;
- no output names a model or recommends choosing one outside a fenced
  example, because the model is a typed configuration leaf and a sentence
  naming one is a second source of truth nothing reconciles against it.

Two checks bracket the writes of :func:`eawf.platform.rules.render.write_rule_projections`:

- before any write, the managed ``.gitignore`` block must ignore every
  generated path, because the block is the one committed declaration of
  what is generated and an output missing from it would be committed by
  every clone;
- around the removal of obsolete outputs, the files each host loads are
  probed (:mod:`eawf.platform.rules.host_probe`), because the manifest is
  only the render's own record and a generated file it lost track of stays
  in the host's chain.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, Final

from pydantic import ValidationError

from eawf.kernel.config.layered import merge_config
from eawf.kernel.economics.governor import economics_policy_from
from eawf.kernel.economics.prompt_budget import ClassStatus
from eawf.kernel.runtime.host_facts import HostFactRegistry, ProjectionKind
from eawf.observability.telemetry.models import RuntimeName
from eawf.platform.install.gitignore_writer import (
    GitignoreBlockPlan,
    plan_gitignore_block,
    unenumerated_paths,
)
from eawf.platform.rules.chain_budget import (
    ChainBudgetReport,
    GlobalDocumentFile,
    GlobalDocumentOverage,
    RenderMode,
    judge_loaded_chains,
)
from eawf.platform.rules.host_facts import load_host_facts
from eawf.platform.rules.host_probe import (
    SKILL_DISCOVERY,
    global_document_chain,
    host_loaded_files,
)
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    PROJECTION_MANIFEST_PATH,
    ProjectionManifest,
    RuleProjectionBudgetError,
    RuleProjectionError,
    RuleProjectionUnenumeratedError,
    classify_projection,
    plan_rule_projections,
    rule_source_present,
)

logger = logging.getLogger(__name__)

_FENCE: Final[re.Pattern[str]] = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_MODEL_PROSE: Final[re.Pattern[str]] = re.compile(
    r"\bclaude[\s-]+(?:opus|sonnet|haiku|fable|instant)\b"
    r"|\b(?:opus|sonnet|haiku|fable)[\s-]+\d+(?:[.-]\d+)*\b"
    r"|\bgpt-?\d[\w.-]*|\bgemini[\s-]+\d[\w.-]*|\bo[134](?:-mini|-pro)?\b"
    r"|\b(?:use|prefer|choose|select|pick|switch\s+to|default\s+to)\s+(?:a|an|the)\s+"
    r"(?:cheaper|smaller|larger|bigger|faster|stronger|weaker|more\s+capable|premium|frontier)"
    r"\s+model\b",
    re.IGNORECASE,
)


class RuleProjectionModelNamingError(RuleProjectionError):
    """A rendered output names a model or recommends choosing one."""

    code: ClassVar[str] = "rule_projection_model_naming"


def model_naming_spans(target: str, text: str) -> tuple[str, ...]:
    """Name every span of *text* that names a model or recommends choosing one.

    A span inside a fenced example is not prose the reader is told to follow,
    so it is not named.

    Args:
        target: The output's repository-relative path, for the location.
        text: The output's content.

    Returns:
        ``<target>:<line>: <span>`` per span, in reading order.
    """
    spans: list[str] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        marker = _FENCE.match(line)
        if marker is not None:
            opened = marker.group(1)
            if fence is None:
                fence = opened
            elif opened[0] == fence[0] and len(opened) >= len(fence):
                fence = None
            continue
        if fence is None:
            spans.extend(f"{target}:{number}: {m.group(0)}" for m in _MODEL_PROSE.finditer(line))
    return tuple(spans)


def refuse_model_naming(outputs: Mapping[str, str]) -> None:
    """Refuse a render whose outputs name a model anywhere outside a fence.

    Args:
        outputs: Every file the render writes, by repository-relative path.

    Raises:
        RuleProjectionModelNamingError: Naming every offending span.
    """
    spans = [span for target, text in outputs.items() for span in model_naming_spans(target, text)]
    if spans:
        raise RuleProjectionModelNamingError(
            f"a rendered instruction surface names a model or recommends choosing one: "
            f"{'; '.join(spans)}; the model is a configuration setting, so remove the prose"
        )


def budgeted_chains(
    repo_root: Path, outputs: Mapping[str, str], registry: HostFactRegistry, *, mode: RenderMode
) -> ChainBudgetReport:
    """Charge every host's loaded chain to the configured prompt budget.

    Each chain is charged with the operator's global instruction documents too,
    since the host loads them. A repository-authored chain over a ceiling on its
    own fails every render. A chain the global documents put over a ceiling
    fails a certified render, which claims what an agent on this machine
    receives, and is recorded as a warning by a local one, which renders anyway.

    Args:
        repo_root: The repository root whose layered configuration resolves
            the policy.
        outputs: Every file the render writes, by repository-relative path.
        registry: The certified host facts naming each runtime's discovery.
        mode: ``local`` for an ordinary render, ``certified`` for the render a
            release certifies.

    Returns:
        The report the manifest records, its global overages among it.

    Raises:
        RuleProjectionBudgetError: When the ``economics`` configuration is
            invalid, a class of some repository-authored chain is over its
            ceiling, or a certified render's chain is over one with the global
            documents charged.
    """
    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    try:
        policy = economics_policy_from(merged).prompt_budget
    except ValidationError as error:
        raise RuleProjectionBudgetError(
            f"the economics configuration is invalid, so no prompt-budget ceiling resolves: {error}"
        ) from error
    installed = _installed_skills(repo_root, outputs)
    projections: dict[ProjectionKind, str] = {"card": CARD_TARGET, "policy": POLICY_TARGET}
    global_documents = {
        record.runtime: global_document_chain(record.runtime, os.environ, Path.home())
        for record in registry.records
    }
    report = judge_loaded_chains(
        policy,
        registry,
        projections=projections,
        outputs=outputs,
        installed_skills=installed,
        global_documents=global_documents,
    )
    authored = judge_loaded_chains(
        policy,
        registry,
        projections=projections,
        outputs=outputs,
        installed_skills=installed,
        global_documents={},
    )
    over = _over_ceiling(authored)
    if over:
        raise RuleProjectionBudgetError(
            f"{'; '.join(over.values())} (prompt-budget policy revision {policy.revision}); "
            f"move rules out of the constitution or into a module, or raise the ceiling in the "
            f"economics configuration"
        )
    overages = _global_overages(report, global_documents)
    if overages and mode == "certified":
        raise RuleProjectionBudgetError(
            f"{'; '.join(item.note for item in overages)}; a certified render claims what an "
            f"agent on this machine receives, so trim the global instruction documents"
        )
    for item in overages:
        logger.warning(
            f"budgeted_chains status=global_document_over_ceiling runtime={item.runtime} "
            f"class_id={item.class_id.value} over_bytes={item.measured_bytes - item.ceiling_bytes}"
        )
    return report.model_copy(update={"global_overages": overages})


def certify_steering_chain(repo_root: Path) -> None:
    """Refuse a release whose steering chain a certified render would refuse.

    A release certifies what an agent on this machine receives, so the
    repository's rule projections are planned in certified mode: a chain the
    operator's global instruction documents put over a ceiling refuses here,
    naming each file, its bytes, the ceiling and the overage. A repository
    that authors no rule source has no steering chain to certify.

    Args:
        repo_root: The checkout the release is cut from.

    Raises:
        RuleProjectionError: When the certified render refuses; a
            :class:`RuleProjectionBudgetError` for a chain over a ceiling.
        RuleSourceError: When the rule source fails to load.
        RuleCompileError: When the rules fail compilation.
    """
    if rule_source_present(repo_root):
        plan_rule_projections(repo_root, mode="certified")


def _global_overages(
    report: ChainBudgetReport, global_documents: Mapping[RuntimeName, Mapping[str, str]]
) -> tuple[GlobalDocumentOverage, ...]:
    """Return every class the global documents put over its byte ceiling.

    Args:
        report: The chains, global documents charged.
        global_documents: Each runtime's global chain, text by display path.

    Returns:
        One overage per runtime and class, in report order.
    """
    return tuple(
        GlobalDocumentOverage(
            runtime=chain.runtime,
            class_id=verdict.class_id,
            files=tuple(
                GlobalDocumentFile(path=path, byte_count=len(text.encode("utf-8")))
                for path, text in global_documents.get(chain.runtime, {}).items()
            ),
            measured_bytes=verdict.measured_bytes,
            ceiling_bytes=verdict.ceiling_bytes,
        )
        for chain in report.chains
        for verdict in chain.outcome.verdicts
        if verdict.status in (ClassStatus.EXHAUSTED, ClassStatus.DROPPED)
        and verdict.measured_bytes is not None
        and verdict.ceiling_bytes is not None
        and global_documents.get(chain.runtime)
    )


def _over_ceiling(report: ChainBudgetReport) -> dict[tuple[str, str], str]:
    """Name every class of every chain over its ceiling, by runtime and class.

    A render cannot drop the steering it is writing, so a class the policy would
    drop under degrade_by_priority counts like an exhausted one.
    """
    return {
        (chain.runtime, verdict.class_id.value): (
            f"the {chain.runtime} chain charges {verdict.class_id.value} "
            f"{verdict.measured_bytes} bytes, over its {verdict.ceiling_bytes}-byte ceiling"
        )
        for chain in report.chains
        for verdict in chain.outcome.verdicts
        if verdict.status in (ClassStatus.EXHAUSTED, ClassStatus.DROPPED)
    }


def _installed_skills(repo_root: Path, outputs: Mapping[str, str]) -> dict[str, str]:
    """Read the skills a host discovers on disk that the render neither writes nor removes.

    Args:
        repo_root: The repository root.
        outputs: Every file the render writes.

    Returns:
        Skill text by repository-relative path. An eawf-stamped file is left
        out: the render either rewrites it or removes it as obsolete.
    """
    return {
        relative: path.read_text(encoding="utf-8")
        for pattern in set(SKILL_DISCOVERY.values())
        for path in sorted(repo_root.glob(pattern))
        if (relative := path.relative_to(repo_root).as_posix()) not in outputs
        and not _stamped(path)
    }


def checked_ignore_block(repo_root: Path, manifest: ProjectionManifest) -> GitignoreBlockPlan:
    """Plan the managed ``.gitignore`` block and refuse a generated path it misses.

    Every output but the committed card is generated, the manifest sidecar
    included.

    Args:
        repo_root: The repository root.
        manifest: The manifest of the render in progress.

    Returns:
        The planned ``.gitignore``.

    Raises:
        RuleProjectionUnenumeratedError: When the block does not ignore a
            generated path.
        ManagedBlockError: When the ``.gitignore`` markers are not exactly
            one ordered pair.
    """
    ignore = plan_gitignore_block(repo_root)
    generated = (
        *(record.target for record in manifest.projections if record.kind != "card"),
        *manifest.generated,
        PROJECTION_MANIFEST_PATH,
    )
    unenumerated = unenumerated_paths(ignore.patterns, generated)
    if unenumerated:
        raise RuleProjectionUnenumeratedError(
            f"the managed .gitignore block does not ignore generated {list(unenumerated)}; "
            f"add a pattern for each to the shipped block before rendering"
        )
    return ignore


def stale_host_loaded(repo_root: Path, manifest: ProjectionManifest) -> tuple[Path, ...]:
    """Name the eawf-stamped files a host loads that *manifest* does not write.

    Args:
        repo_root: The repository root.
        manifest: The manifest of the render in progress.

    Returns:
        Loaded files carrying an eawf stamp, hand-edited or not, outside the
        render's targets. A file made only of imports is never named: its
        shape alone does not prove eawf wrote it.
    """
    loaded = host_loaded_files(
        repo_root,
        load_host_facts(),
        projections={"card": CARD_TARGET, "policy": POLICY_TARGET},
    )
    targets = frozenset(manifest.targets)
    return tuple(
        repo_root / relative
        for relative in loaded
        if relative not in targets and _stamped(repo_root / relative)
    )


def _stamped(path: Path) -> bool:
    """Return whether *path* carries an eawf stamp, matching its body or not.

    Args:
        path: An existing file.

    Returns:
        ``True`` for a stamped projection, view or carrier.
    """
    state = classify_projection(path)
    if state == "hand_edited":
        return True
    lines = path.read_text(encoding="utf-8").splitlines()
    return state == "generated" and not all(line.startswith("@") for line in lines)


__all__ = [
    "RuleProjectionModelNamingError",
    "budgeted_chains",
    "checked_ignore_block",
    "model_naming_spans",
    "refuse_model_naming",
    "stale_host_loaded",
]
