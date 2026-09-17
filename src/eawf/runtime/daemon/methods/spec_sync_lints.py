"""Validation helpers for the ``spec.sync`` daemon method."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import click

from eawf.kernel.spec.common import CriterionSpec, GateSpec, ObserveVerb
from eawf.kernel.spec.heuristics import is_ui_scope
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.spec.promotion import ARGV_BEARING_GATE_KINDS
from eawf.platform.lint.eawf021_measurable_criterion import (
    MeasurabilityViolation,
    check_criterion_spec,
)
from eawf.platform.lint.eawf022_propose_coverage import (
    CoverageGapViolation,
    missing_intent_finding,
    missing_planned_steps_finding,
)
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.workflow.propose.coverage import coverage_gaps, source_brief_coverage_gaps

_AFFORDANCE_PARITY_KIND: Final[str] = "affordance_parity"
_TRANSITION_COVERAGE_KIND: Final[str] = "transition_coverage"

#: Console-script head of the project's own CLI.
_EAWF_HEAD: Final[str] = "eawf"

#: Wrapper prefix an author writes in front of the CLI head when the
#: command runs out of the project virtualenv.
_UV_RUN_EAWF: Final[tuple[str, str, str]] = ("uv", "run", _EAWF_HEAD)

#: Punctuation prose wraps a command in. Stripped from both ends of a
#: signal token so a backticked or sentence-final verb still matches.
_PROSE_TRIM: Final[str] = "`'\"(),;:.[]{}<>!?"


def measure_criteria(criteria: list[CriterionSpec]) -> list[MeasurabilityViolation]:
    """Return every EAWF021 measurability finding across *criteria*."""
    findings: list[MeasurabilityViolation] = []
    for criterion in criteria:
        findings.extend(check_criterion_spec(criterion))
    return findings


def find_coverage_gaps(
    criteria: list[CriterionSpec],
    *,
    wave_id: str,
    intent: IntentBrief | None,
    repo_root: Path,
) -> list[CoverageGapViolation]:
    """Return every EAWF022 coverage gap of a wave's brief detail by *criteria*."""
    if intent is None:
        return [missing_intent_finding(wave_id)]
    findings: list[CoverageGapViolation] = []
    if intent.is_required_intent and not intent.planned_steps:
        findings.append(missing_planned_steps_finding(wave_id))
    findings += coverage_gaps(criteria, planned_steps=list(intent.planned_steps))
    findings += source_brief_coverage_gaps(criteria, intent=intent, repo_root=repo_root)
    return findings


def render_lint_findings(
    measurability: list[MeasurabilityViolation],
    coverage: list[CoverageGapViolation],
) -> str:
    """Render a combined ``validation_failed`` message for lint findings."""
    bodies = [v.render() for v in measurability] + [v.render() for v in coverage]
    return "validation_failed: spec sync lint findings: " + "; ".join(bodies)


def require_affordance_parity_for_ui_scope(
    *,
    wave_id: str,
    file_scopes: list[str],
    gates: list[GateSpec],
) -> None:
    """Reject a UI-scope wave whose synced gates omit an affordance_parity gate."""
    if not is_ui_scope(file_scopes):
        return
    if any(gate.kind == _AFFORDANCE_PARITY_KIND for gate in gates):
        return
    raise DaemonValidationError(
        f"validation_failed: ui-scope wave {wave_id!r} requires an "
        f"{_AFFORDANCE_PARITY_KIND} gate; none found in synced gates"
    )


def require_transition_coverage_for_ui_transitions(
    *,
    wave_id: str,
    file_scopes: list[str],
    criteria: list[CriterionSpec],
    gates: list[GateSpec],
) -> None:
    """Reject a UI-scope transition criterion whose gates omit transition_coverage."""
    if not is_ui_scope(file_scopes):
        return
    gate_by_id = {gate.id: gate for gate in gates}
    for criterion in criteria:
        if (
            criterion.response is None
            or criterion.response.observe is not ObserveVerb.TRANSITIONS_TO
        ):
            continue
        if any(
            gate_by_id[gate_id].kind == _TRANSITION_COVERAGE_KIND for gate_id in criterion.gate_ids
        ):
            continue
        raise DaemonValidationError(
            f"validation_failed: ui-scope wave {wave_id!r} criterion "
            f"{criterion.id!r} has a transitions_to response and requires a "
            f"{_TRANSITION_COVERAGE_KIND} gate; none found in criterion gates"
        )


@dataclass(frozen=True)
class UnknownVerbFinding:
    """One eawf verb path a wave spec names that the CLI cannot resolve.

    Attributes:
        origin: Where the command was read from -- a gate id plus ``argv``,
            or a criterion id plus ``measurable_signal``.
        resolved: The command path that did resolve, head included.
        verb: The token naming no subcommand of *resolved*.
    """

    origin: str
    resolved: str
    verb: str

    @property
    def path(self) -> str:
        """Return the full verb path as authored, unresolved token included."""
        return f"{self.resolved} {self.verb}"

    def render(self) -> str:
        """Return a one-line finding body naming the unresolved verb."""
        return (
            f"{self.origin} names the unresolved eawf verb path {self.path!r}: "
            f"{self.verb!r} is not a command of {self.resolved!r}"
        )


@lru_cache(maxsize=1)
def _eawf_command_tree() -> click.Group:
    """Return the Click command tree the ``eawf`` Typer app builds.

    Cached for the process: building the tree imports every CLI command
    module, so the cost lands once at the first resolution rather than at
    daemon import.

    Returns:
        The root Click group whose ``commands`` map carries every verb.

    Raises:
        TypeError: When the app does not build a group, which would mean the
            CLI has no subcommands left to walk.
    """
    import typer.main

    from eawf.surfaces.cli.app import app

    command = typer.main.get_command(app)
    if not isinstance(command, click.Group):
        raise TypeError("the eawf CLI app did not build a Click group")
    return command


def _value_taking_options(command: click.Command) -> frozenset[str]:
    """Return the option flags of *command* that consume the next token.

    Args:
        command: A node of the command tree whose params are read.

    Returns:
        Every long / short flag string that is an option carrying a value.
    """
    flags: set[str] = set()
    for param in command.params:
        if isinstance(param, click.Option) and not param.is_flag:
            flags.update(param.opts)
            flags.update(param.secondary_opts)
    return frozenset(flags)


def _first_unresolved_verb(tokens: Sequence[str]) -> tuple[str, str] | None:
    """Walk *tokens* down the eawf command tree until a leaf or a miss.

    Options are skipped, and the value of a value-taking option is skipped
    with it, so a global flag such as ``--workspace <path>`` never reads as
    a verb. Once the walk reaches a leaf command the remaining tokens are
    that command's own arguments and are left alone.

    Args:
        tokens: The argv tail after the ``eawf`` head, options included.

    Returns:
        ``None`` when every verb token resolves; otherwise the resolved
        command path and the token that names no subcommand of it.
    """
    node: click.Command = _eawf_command_tree()
    value_options = set(_value_taking_options(node))
    resolved = [_EAWF_HEAD]
    skip_value = False
    for token in tokens:
        if skip_value:
            skip_value = False
            continue
        if token.startswith("-"):
            skip_value = token in value_options
            continue
        if not isinstance(node, click.Group):
            return None
        child = node.commands.get(token)
        if child is None:
            return " ".join(resolved), token
        node = child
        resolved.append(token)
        value_options |= _value_taking_options(child)
    return None


def _eawf_argv_tail(argv: Sequence[str]) -> list[str] | None:
    """Return the verb tokens of *argv* when it invokes the eawf CLI.

    Args:
        argv: A gate's command vector.

    Returns:
        The tokens after the ``eawf`` head, or ``None`` when the argv runs
        some other program (``pytest``, ``just``) whose verbs this walk
        knows nothing about.
    """
    if list(argv[:1]) == [_EAWF_HEAD]:
        return list(argv[1:])
    if tuple(argv[:3]) == _UV_RUN_EAWF:
        return list(argv[3:])
    return None


def _signal_command_sequences(signal: str) -> list[list[str]]:
    """Return each ``uv run eawf`` command tail written in *signal*.

    A tail runs to the next ``uv run eawf`` occurrence or to the end of the
    signal; the walk itself stops at the first leaf command, so prose
    trailing a resolved command is never read as a verb.

    Args:
        signal: A criterion's ``measurable_signal`` prose.

    Returns:
        One token list per command sequence, in authored order.
    """
    tokens = [trimmed for token in signal.split() if (trimmed := token.strip(_PROSE_TRIM))]
    starts = [
        index
        for index in range(len(tokens) - 2)
        if tuple(tokens[index : index + 3]) == _UV_RUN_EAWF
    ]
    sequences: list[list[str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(tokens)
        sequences.append(tokens[start + 3 : end])
    return sequences


def _gate_eawf_argv(gate: GateSpec) -> list[str] | None:
    """Return the eawf verb tokens of *gate*'s argv, or ``None``.

    Args:
        gate: A parsed gate row of the wave being synced.

    Returns:
        The tokens after the ``eawf`` head when the gate is a command gate
        whose argv invokes the CLI; ``None`` otherwise.
    """
    if gate.kind not in ARGV_BEARING_GATE_KINDS:
        return None
    argv = gate.args.get("argv")
    if not isinstance(argv, list) or not all(isinstance(element, str) for element in argv):
        return None
    return _eawf_argv_tail(argv)


def find_unknown_eawf_verbs(
    criteria: list[CriterionSpec],
    gates: list[GateSpec],
) -> list[UnknownVerbFinding]:
    """Return every eawf verb path in *gates* / *criteria* the CLI lacks.

    A command gate's argv headed ``eawf`` or ``uv run eawf`` is walked
    whole; a criterion's ``measurable_signal`` is scanned for ``uv run
    eawf`` sequences and each one is walked. Argv under any other head is
    left alone -- this check knows only the eawf command tree.

    Args:
        criteria: The parsed criterion rows of the wave being synced.
        gates: The parsed gate rows of the same wave.

    Returns:
        One finding per unresolved verb path, gates before criteria.
    """
    findings: list[UnknownVerbFinding] = []
    for gate in gates:
        tokens = _gate_eawf_argv(gate)
        if tokens is None:
            continue
        miss = _first_unresolved_verb(tokens)
        if miss is not None:
            findings.append(
                UnknownVerbFinding(origin=f"gate {gate.id!r} argv", resolved=miss[0], verb=miss[1])
            )
    for criterion in criteria:
        for sequence in _signal_command_sequences(criterion.measurable_signal):
            miss = _first_unresolved_verb(sequence)
            if miss is not None:
                findings.append(
                    UnknownVerbFinding(
                        origin=f"criterion {criterion.id!r} measurable_signal",
                        resolved=miss[0],
                        verb=miss[1],
                    )
                )
    return findings


def require_resolvable_eawf_verbs(
    *,
    wave_id: str,
    criteria: list[CriterionSpec],
    gates: list[GateSpec],
) -> None:
    """Reject a sync whose gate argv or signal names a verb the CLI lacks.

    Args:
        wave_id: The wave being synced, named in the reject message.
        criteria: The parsed criterion rows of that wave.
        gates: The parsed gate rows of that wave.

    Raises:
        DaemonValidationError: When any named eawf verb path does not
            resolve against the command tree.
    """
    findings = find_unknown_eawf_verbs(criteria, gates)
    if not findings:
        return
    bodies = "; ".join(finding.render() for finding in findings)
    raise DaemonValidationError(
        f"validation_failed: wave {wave_id!r} names eawf verbs that do not "
        f"resolve against the command tree: {bodies}"
    )
