"""The realization assertions the ``perfect_realization`` release row is judged on.

The row is a conjunction. Each assertion is one fact a checkout either
holds or breaks: every lint rule has a disposition, every re-typed rule is
triaged, the console grid reconciles with its routes, no module-length
grant has lapsed, the requirement census is fresh with every id
accounted for, every committed rule projection is its own render, and the
command tree is closed over the verb contract. The first four are decided
in :mod:`eawf.workflow.verify.release_probes`, because they read the
chokepoint's inputs; the rest are decided here from the working copy and
the running package.

One assertion cannot be decided from a checkout at all: the packet run,
which drives the perfect-realization packet across two runtime profiles
and two repository origins. It has no producer before the stable
checkpoint, which is the one that runs it, so it is reported
``unavailable`` by name. A release candidate does not require it -- the
candidate's milestone excludes that run -- so it greens the row only
there, and the stable checkpoint stays ``unavailable`` until the run has
a producer.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Final

from eawf.kernel.release.signals import ReleaseSignalOutcome, ReleaseSignalStatus
from eawf.kernel.spec.release import ReleaseChannel
from eawf.platform.rules.render import plan_rule_projections, projection_drift, rule_source_present

logger = logging.getLogger(__name__)

#: The committed requirement census, relative to the repo root. A working
#: copy without one traces no requirement ids, so it has no census to check.
REQUIREMENT_CATALOG_PATH: Final[str] = ".ea/requirements.json"

#: The census tool that recomputes the trace and compares it by value.
REQUIREMENT_TRACE_TOOL: Final[str] = "tools/requirement_trace.py"

#: Wall-clock ceiling on one census run, so a hung tool cannot stall the
#: sweep.
REQUIREMENT_TRACE_TIMEOUT_SECONDS: Final[int] = 300


class RealizationAssertion(StrEnum):
    """The closed set of assertions the realization row conjoins.

    Values:
        RULE_DISPOSITION: Every shipped lint rule has a governing
            disposition.
        RETYPED_RULE_TRIAGE: No rule re-typed past the threshold since the
            previous release lacks a disposition.
        COVERAGE_GRID: The console coverage grid reconciles with the
            route registry.
        MODULE_LENGTH_EXCLUSION: No module-length exemption has outlived
            its grant or rests on an unrecorded decision.
        REQUIREMENT_TRACE: The requirement census is fresh and every id
            is owned, deferred or satisfied.
        RENDERED_RULES: Every committed rule projection equals its render.
        CLI_CLOSURE: Every root entry and every verb of the command tree
            is declared, and every declared verb is on the tree.
        PACKET_RUN: The perfect-realization packet passes across its
            runtime profiles and repository origins.
    """

    RULE_DISPOSITION = "rule_disposition"
    RETYPED_RULE_TRIAGE = "retyped_rule_triage"
    COVERAGE_GRID = "coverage_grid"
    MODULE_LENGTH_EXCLUSION = "module_length_exclusion"
    REQUIREMENT_TRACE = "requirement_trace"
    RENDERED_RULES = "rendered_rules"
    CLI_CLOSURE = "cli_closure"
    PACKET_RUN = "packet_run"


#: Assertions only the stable checkpoint requires. Every other assertion
#: is required at every checkpoint.
STABLE_ONLY_ASSERTIONS: Final[frozenset[RealizationAssertion]] = frozenset(
    {RealizationAssertion.PACKET_RUN}
)


def _held(*evidence: str) -> ReleaseSignalOutcome:
    """Return the outcome of an assertion that holds, carrying *evidence*."""
    return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, evidence_refs=evidence)


def _broken(remediation: str, *evidence: str) -> ReleaseSignalOutcome:
    """Return the outcome of an assertion that breaks, naming the repair."""
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.FAIL, remediation=remediation, evidence_refs=evidence
    )


def requirement_trace_outcome(repo_root: Path) -> ReleaseSignalOutcome:
    """Decide whether the committed requirement census is fresh and fully accounted.

    The census tool is the one CI runs, so the release and CI cannot
    disagree about what a fresh census is.

    Args:
        repo_root: Working copy holding the census and the tool.

    Returns:
        Holding when the tool's value-equality check passes with every id
        owned, deferred or satisfied, or when the working copy carries no
        census; broken with the tool's report otherwise.
    """
    name = RealizationAssertion.REQUIREMENT_TRACE.value
    if not (repo_root / REQUIREMENT_CATALOG_PATH).is_file():
        return _held(f"{name}:no-catalog")
    checked = subprocess.run(
        [
            sys.executable,
            str(repo_root / REQUIREMENT_TRACE_TOOL),
            "--repo-root",
            str(repo_root),
            "check",
            "--require-owned",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=REQUIREMENT_TRACE_TIMEOUT_SECONDS,
    )
    if checked.returncode:
        report = "; ".join(
            line.removeprefix("requirement-trace: ")
            for line in (checked.stderr or checked.stdout).strip().splitlines()
        )
        return _broken(
            f"{name}: {report}; then commit {REQUIREMENT_CATALOG_PATH}",
            f"{name}:{REQUIREMENT_CATALOG_PATH}",
        )
    return _held(f"{name}:{REQUIREMENT_CATALOG_PATH}:fresh")


def _tracked(repo_root: Path, targets: tuple[str, ...]) -> tuple[str, ...]:
    """Return the *targets* git tracks in *repo_root*, in the given order."""
    listed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--", *targets],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = set(listed.stdout.splitlines())
    return tuple(target for target in targets if target in tracked)


def rendered_rules_outcome(repo_root: Path) -> ReleaseSignalOutcome:
    """Decide whether every committed rule projection is its own render.

    Only committed targets are judged: a local-only projection does not
    ship, so its drift is the operator's checkout and not the release.

    Args:
        repo_root: Working copy holding ``.ea/rules.yaml``.

    Returns:
        Holding when no committed projection differs from the plan, or
        when the working copy authors no rule source; broken naming every
        drifted target otherwise.

    Raises:
        RuleSourceError: The rule source fails to load, which the sweep
            converts into a blocked row.
        RuleCompileError: The rules fail compilation.
    """
    name = RealizationAssertion.RENDERED_RULES.value
    if not rule_source_present(repo_root):
        return _held(f"{name}:no-rule-source")
    plan = plan_rule_projections(repo_root)
    committed = _tracked(repo_root, tuple(target for target, _ in plan.outputs))
    drifted = tuple(target for target in projection_drift(repo_root, plan) if target in committed)
    if drifted:
        return _broken(
            f"{name}: {len(drifted)} committed projection(s) differ from the render of "
            f".ea/rules.yaml: {', '.join(drifted)}; run `eawf sync` and commit the result",
            *(f"{name}:{target}:drifted" for target in drifted),
        )
    if not committed:
        return _held(f"{name}:no-committed-projection")
    return _held(*(f"{name}:{target}" for target in committed))


def cli_closure_outcome() -> ReleaseSignalOutcome:
    """Decide whether the running command tree is closed over the verb contract.

    Every root entry must be an entity group or a declared exception, and
    the verb catalog must build: every verb classified, every classified
    verb on the tree, every named route registered.

    Returns:
        Holding with the verb count, else broken naming the first breach.
    """
    import click
    import typer

    from eawf.surfaces.cli.app import app
    from eawf.surfaces.cli.verb_catalog import VerbCatalogError, verb_catalog
    from eawf.surfaces.cli.verb_closure import ROOT_ENTRY_EXCEPTIONS
    from eawf.surfaces.cli.verb_contract import ENTITY_GROUPS

    name = RealizationAssertion.CLI_CLOSURE.value
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group), "the eawf root command is a group"
    declared = {*ENTITY_GROUPS, *(row.name for row in ROOT_ENTRY_EXCEPTIONS)}
    undeclared = sorted(set(root.commands) - declared)
    if undeclared:
        return _broken(
            f"{name}: root entr(ies) {undeclared} are neither an entity group nor a declared "
            f"exception; group them or declare them in eawf.surfaces.cli.verb_closure",
            *(f"{name}:root:{entry}" for entry in undeclared),
        )
    try:
        catalog = verb_catalog()
    except VerbCatalogError as exc:
        return _broken(f"{name}: {exc}", f"{name}:verb-catalog")
    return _held(f"{name}:{len(catalog.entries)}-verbs")


def packet_run_outcome() -> ReleaseSignalOutcome:
    """Report the packet run, which no checkout can decide.

    Returns:
        An unavailable outcome naming the missing producer.
    """
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.UNAVAILABLE,
        remediation=(
            f"{RealizationAssertion.PACKET_RUN.value}: the perfect-realization packet run across "
            f"two runtime profiles and two repository origins has no producer before the stable "
            f"checkpoint runs it"
        ),
    )


def realization_outcome(
    verdicts: Mapping[RealizationAssertion, ReleaseSignalOutcome], *, channel: ReleaseChannel
) -> ReleaseSignalOutcome:
    """Conjoin every assertion's verdict into the realization row.

    A broken assertion reds the row with only the broken ones' repairs and
    evidence. Otherwise an assertion the checkpoint requires that has no
    verdict keeps the row ``unavailable``. Otherwise the row passes, and
    its evidence names every assertion's proof, an unrequired unavailable
    one included, so the pass says what it did not measure.

    Args:
        verdicts: One outcome per member of :class:`RealizationAssertion`.
        channel: The checkpoint's channel, which decides whether the
            stable-only assertions are required.

    Returns:
        The row's outcome.
    """
    broken = [
        outcome for outcome in verdicts.values() if outcome.status is ReleaseSignalStatus.FAIL
    ]
    if broken:
        logger.warning(f"realization_outcome broken={len(broken)} channel={channel.value!r}")
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.FAIL,
            remediation="; ".join(outcome.remediation for outcome in broken),
            evidence_refs=tuple(ref for outcome in broken for ref in outcome.evidence_refs),
        )
    required = (
        set(RealizationAssertion)
        if channel is ReleaseChannel.STABLE
        else set(RealizationAssertion) - STABLE_ONLY_ASSERTIONS
    )
    undecided = [
        outcome
        for assertion, outcome in verdicts.items()
        if outcome.status is not ReleaseSignalStatus.PASS and assertion in required
    ]
    if undecided:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.UNAVAILABLE,
            remediation="; ".join(outcome.remediation for outcome in undecided),
        )
    evidence: list[str] = []
    for assertion, outcome in verdicts.items():
        if outcome.status is ReleaseSignalStatus.PASS:
            evidence.extend(outcome.evidence_refs)
        else:
            evidence.append(f"{assertion.value}:{outcome.status.value}:not-required-before-stable")
    return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, evidence_refs=tuple(evidence))


__all__ = [
    "REQUIREMENT_CATALOG_PATH",
    "REQUIREMENT_TRACE_TIMEOUT_SECONDS",
    "REQUIREMENT_TRACE_TOOL",
    "STABLE_ONLY_ASSERTIONS",
    "RealizationAssertion",
    "cli_closure_outcome",
    "packet_run_outcome",
    "realization_outcome",
    "rendered_rules_outcome",
    "requirement_trace_outcome",
]
