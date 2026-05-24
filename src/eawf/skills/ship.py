"""``/ship`` skill — commit / push / PR open-close controller.

Implements the §14 algorithm for ``/ship``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``; abort on hard miss.
2. Require current audit passed or explicit allowed exception.
3. Inspect git status / diff / log and state scope.
4. Review memory: extract durable lessons; promote useful entries.
5. Build pending-ship artefact: commit groups, messages, files, evidence.
6. Default policy is ask before commit; ``--commit`` opts in to auto-commit.
7. Default policy is ask before push; ``--push`` opts in to auto-push.
8. PR action: open draft/ready, update body, close/merge.
9. Merge / close gates: CI green, required reviews, state valid.
10. Record commits / PR / merge / audit artefacts and final estimate-vs-actual.
11. Remove clean worktrees per policy.

Honoured flags:

- ``--commit`` — toggles ``body.commit_groups`` population (else empty).
- ``--push`` — toggles ``body.push`` population.
- ``--pr <action>`` — populates ``body.pr.action`` (open/ready/draft/close/none).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.artifacts.validation import validate_markdown_artifact, validate_text_surface
from eawf.kernel.config.layered import merge_config
from eawf.kernel.state.enums import AuditKind, AuditVerdict
from eawf.kernel.state.ids import is_phase_id, parents_of
from eawf.kernel.state.models import Audit, State
from eawf.render.envelope import SkillName
from eawf.skills.bodies.ship import (
    ShipBody,
    ShipCommitGroup,
    ShipPr,
    ShipPrGates,
    ShipPush,
)
from eawf.skills.engine import ActionRun, SkillAction, SkillResult
from eawf.skills.registry import register
from eawf.vcs.coauthor import CoauthorPolicyError, VcsConfig, resolve_coauthor_trailer

logger = logging.getLogger(__name__)


_SHIP_NEXT_ACTIONS: tuple[str, ...] = ("eawf wave close", "eawf audit")
_ZERO_ESTIMATE: dict[str, float] = {"estimated_eu": 0.0, "actual_eu": 0.0}


_VALID_PR_ACTIONS: tuple[str, ...] = ("open", "ready", "draft", "close", "none")

#: Audit verdicts that clear the ship gate. ``pass`` is clean; ``minor``
#: carries triage-later findings but does not block ship (mirrors the
#: ``AgentReportVerdict.PASS_WITH_FOLLOWUPS`` semantics surfaced by the
#: TUI audit overlay). ``major`` and a missing verdict both block.
_SHIP_ALLOWED_VERDICTS: frozenset[AuditVerdict] = frozenset({AuditVerdict.PASS, AuditVerdict.MINOR})

#: Wall-clock ceiling for any single gauntlet gate. A red tree must fail
#: fast; a wedged hook should surface as a timeout rather than hang ship.
_GAUNTLET_TIMEOUT_SECONDS: float = 1200.0

#: Stdout/stderr tail length captured into the failure envelope. The full
#: output of a red gauntlet is large; the operator needs the tail, not the
#: whole transcript, to triage. Bytes beyond this from the end are dropped.
_GAUNTLET_OUTPUT_TAIL_CHARS: int = 4000

#: Canonical gauntlet commands per gate name when the layered
#: ``acceptance.commands.*`` leaf is unset (its default is ``None``). These
#: mirror the project's documented gauntlet (AGENTS.md rule 13 + the
#: dispatch renderer) and the engineering init template. ``pre-commit`` has
#: no ``acceptance.commands`` leaf — it is always the fixed invocation.
_DEFAULT_GATE_COMMANDS: dict[str, str] = {
    "pre-commit": "uv run pre-commit run --all-files",
    "lint": "uv run ruff check .",
    "typecheck": "uv run mypy .",
    "tests": "uv run pytest",
}


class _AcceptanceCommands(BaseModel):
    """Validated ``acceptance.commands`` block — per-gate command overrides."""

    model_config = ConfigDict(extra="forbid")

    tests: str | None = None
    lint: str | None = None
    typecheck: str | None = None
    build: str | None = None


class AcceptanceConfig(BaseModel):
    """Validated ``acceptance`` config surface.

    ``required_before_ship`` names the gates the ship gauntlet must run and
    pass before proceeding. The built-in default (``["state"]``) names no
    gauntlet gate, so ship runs no external check unless the operator opts
    in by listing ``pre-commit`` / ``lint`` / ``typecheck`` / ``tests``.
    """

    model_config = ConfigDict(extra="forbid")

    commands: _AcceptanceCommands = Field(default_factory=_AcceptanceCommands)
    required_before_ship: list[str] = Field(default_factory=list)


class _GateResult(BaseModel):
    """Per-gate outcome of one gauntlet command — the canonical gate shape.

    This is the shape ``_gate_failure`` surfaces for each gate; downstream
    refactors and the failure envelope depend on it. ``passed`` is the only
    field the gate logic branches on; the rest carry triage detail.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    command: str
    passed: bool
    returncode: int | None
    output: str


def _gate_failure(result: _GateResult) -> dict[str, Any]:
    """Render one failed :class:`_GateResult` into its envelope-payload shape.

    The single source of truth for how a red gate is reported, so the
    event payload, the failure body, and any future caller agree on the
    field names.

    Args:
        result: The gate outcome to render (expected ``passed=False``).

    Returns:
        A JSON-ready dict with the gate's name, command, return code, and
        captured output tail.
    """
    return {
        "gate": result.name,
        "command": result.command,
        "returncode": result.returncode,
        "passed": result.passed,
        "output": result.output,
    }


def _coerce_bool(value: Any) -> bool:
    """Best-effort string→bool coercion for stdin-piped JSON args."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _resolve_pr_action(value: Any) -> str | None:
    """Normalise ``--pr`` argument; ``None`` when unset."""
    if value is None:
        return None
    if isinstance(value, bool) and value:
        return "open"
    candidate = str(value).strip().lower()
    if candidate in _VALID_PR_ACTIONS:
        return candidate
    if candidate in {"true", "yes", "on", "1"}:
        return "open"
    return None


def _coerce_path_list(value: Any) -> list[Path]:
    """Coerce stdin JSON args into a list of artifact paths."""
    if value is None:
        return []
    if isinstance(value, str):
        return [Path(p.strip()) for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        return [Path(str(p)) for p in value]
    return [Path(str(value))]


def _load_state(state_path: Path) -> State | None:
    """Return the validated :class:`State`, or ``None`` when unreadable.

    Read-only and best-effort (rule 4: the daemon is the sole mutator;
    reads are free). A missing file, malformed JSON, or schema mismatch
    all degrade to ``None`` so the ship gate can fall through rather than
    crash when no state document is available to gate against.

    Args:
        state_path: Resolved path of the active ``state.json``.

    Returns:
        The validated state document, or ``None`` when absent or invalid.
    """
    if not state_path.exists():
        return None
    try:
        return State.model_validate(orjson.loads(state_path.read_bytes()))
    except (orjson.JSONDecodeError, ValidationError) as exc:
        logger.warning(f"_load_state path={state_path} reason={exc!r}")
        return None


def _phase_id_from_scope(scope_id: str) -> str | None:
    """Resolve the bare phase id from a state-scope URN.

    The skill scope arrives as a URN (``urn:eawf:v1:state:QR/P00``) or a
    bare lifecycle id; we take the tail after the final ``/`` then walk up
    via :func:`eawf.kernel.state.ids.parents_of` so an iter / wave scope resolves
    to its owning phase.

    Args:
        scope_id: The skill's state-scope URN or bare lifecycle id.

    Returns:
        The phase id, or ``None`` when the tail is not a recognised
        lifecycle id.
    """
    tail = scope_id.rsplit("/", 1)[-1]
    if is_phase_id(tail):
        return tail
    try:
        parents = parents_of(tail)
    except ValueError:
        return None
    return parents[0] if parents else None


def _latest_audit_for_phase(state: State, phase_id: str) -> Audit | None:
    """Return the most-recent audit recorded against *phase_id*.

    Ship-gate audits win over other kinds; within a kind the latest
    ``created_at`` wins. Audits scoped to the phase's iters / waves are not
    considered — the ship gate cares about the phase-level verdict.

    Args:
        state: The loaded state document.
        phase_id: The phase id whose audit verdict gates the ship.

    Returns:
        The selected :class:`Audit`, or ``None`` when the phase has none.
    """
    candidates = [a for a in (state.audits or {}).values() if a.scope_id == phase_id]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda a: (a.kind == AuditKind.SHIP_GATE, a.created_at),
    )


def _load_vcs_config(state_path: Path) -> VcsConfig:
    """Load the layered ``vcs`` config block as a validated :class:`VcsConfig`.

    The merge anchors on the repo root (``state.json`` lives at
    ``<repo>/.ea/state.json``) so the repo, branch, and local layers are
    consulted on top of the built-in defaults. The config registry already
    owns ``vcs.pr_merge_method`` / ``vcs.squash_allowed`` / ``vcs.coauthor``;
    this helper only reads them.

    Args:
        state_path: Resolved path of the active ``state.json``.

    Returns:
        The validated ``vcs`` config surface (built-in defaults when no
        overlay file is present).
    """
    anchor = state_path.parent.parent
    merged, _sources = merge_config(repo=anchor, workspace=anchor)
    return VcsConfig.model_validate(merged.get("vcs", {}))


def _resolve_coauthor_trailer_for_ship(vcs_config: VcsConfig) -> str | None:
    """Resolve the ship run's co-author trailer.

    Delegates entirely to :func:`eawf.vcs.coauthor.resolve_coauthor_trailer`;
    co-author policy is never reimplemented here. A
    :class:`~eawf.vcs.coauthor.CoauthorPolicyError` (e.g. a runtime with no
    configured identity) degrades to ``None`` so trailer resolution never
    aborts a ship — the commit-time pre-commit hooks still enforce the
    trailer at commit time.

    Args:
        vcs_config: The validated ``vcs`` config surface.

    Returns:
        The resolved trailer line, or ``None`` when trailers are disabled or
        cannot be inferred.
    """
    try:
        return resolve_coauthor_trailer(vcs_config.coauthor, env=os.environ)
    except CoauthorPolicyError as exc:
        logger.warning(
            f"_resolve_coauthor_trailer_for_ship coauthor={vcs_config.coauthor!r} "
            f"resolution=failed reason={exc!r}"
        )
        return None


def _load_acceptance_config(state_path: Path) -> AcceptanceConfig:
    """Load the layered ``acceptance`` config block as :class:`AcceptanceConfig`.

    Mirrors :func:`_load_vcs_config`: the merge anchors on the repo root
    (``state.json`` lives at ``<repo>/.ea/state.json``) so repo / branch /
    local layers overlay the built-in defaults. This helper only reads.

    Args:
        state_path: Resolved path of the active ``state.json``.

    Returns:
        The validated ``acceptance`` config surface (built-in defaults when
        no overlay file is present).
    """
    anchor = state_path.parent.parent
    merged, _sources = merge_config(repo=anchor, workspace=anchor)
    return AcceptanceConfig.model_validate(merged.get("acceptance", {}))


def _resolve_gate_command(gate: str, acceptance: AcceptanceConfig) -> str | None:
    """Resolve the shell command for one gauntlet *gate*.

    A configured ``acceptance.commands.<gate>`` override wins; otherwise the
    canonical built-in from :data:`_DEFAULT_GATE_COMMANDS` is used. Gate
    names with neither (``state`` and any other lifecycle gate the operator
    lists) resolve to ``None`` so the runner skips them — only the four
    external gauntlet gates have runnable commands.

    Args:
        gate: The gate name from ``acceptance.required_before_ship``.
        acceptance: The validated acceptance config surface.

    Returns:
        The resolved command string, or ``None`` when the gate has no
        runnable gauntlet command.
    """
    override = getattr(acceptance.commands, gate, None)
    if isinstance(override, str) and override.strip():
        return override
    return _DEFAULT_GATE_COMMANDS.get(gate)


def _run_gate_command(name: str, command: str, cwd: Path) -> _GateResult:
    """Run one gauntlet *command* as a subprocess and capture its outcome.

    The single subprocess seam for the gauntlet; tests monkeypatch this to
    stay fast and deterministic. The command runs with ``check=False`` (the
    return code is the gate verdict, not an exception), a bounded timeout,
    and combined stdout/stderr captured to the failure envelope. A missing
    binary or timeout collapses to a failed gate rather than raising, so one
    misconfigured gate cannot crash the whole ship.

    Args:
        name: The gate name (for the result + logs).
        command: The shell command line to execute.
        cwd: Working directory for the subprocess (the repo root).

    Returns:
        The :class:`_GateResult` for this gate; ``passed`` is true only when
        the process exits zero.
    """
    argv = shlex.split(command)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GAUNTLET_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        logger.warning(f"_run_gate_command gate={name!r} command={command!r} reason=not-found")
        return _GateResult(
            name=name,
            command=command,
            passed=False,
            returncode=None,
            output=f"command not found: {exc}",
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            f"_run_gate_command gate={name!r} command={command!r} "
            f"reason=timeout seconds={_GAUNTLET_TIMEOUT_SECONDS}"
        )
        return _GateResult(
            name=name,
            command=command,
            passed=False,
            returncode=None,
            output=f"gate timed out after {_GAUNTLET_TIMEOUT_SECONDS}s",
        )
    combined = f"{proc.stdout}{proc.stderr}"
    return _GateResult(
        name=name,
        command=command,
        passed=proc.returncode == 0,
        returncode=proc.returncode,
        output=combined[-_GAUNTLET_OUTPUT_TAIL_CHARS:],
    )


def _ordered_gauntlet_gates(acceptance: AcceptanceConfig) -> list[str]:
    """Return the requested gate names that map to a runnable command, ordered.

    Every gate named in ``acceptance.required_before_ship`` that
    :func:`_resolve_gate_command` can resolve is included. The canonical
    default gates (``pre-commit`` → ``lint`` → ``typecheck`` → ``tests``) lead
    in their fixed order so the cheapest broad check runs first; any extra
    configured gate (e.g. ``build``, whose command comes from
    ``acceptance.commands.build``) follows in the operator's list order. Gates
    with no runnable command (e.g. ``state``) are dropped. The result is
    deduplicated while preserving first-seen order.

    Args:
        acceptance: The validated acceptance config surface.

    Returns:
        The deterministic, deduplicated list of runnable gate names.
    """
    requested = acceptance.required_before_ship
    requested_set = set(requested)
    leading = [g for g in _DEFAULT_GATE_COMMANDS if g in requested_set]
    extras = [
        g
        for g in requested
        if g not in _DEFAULT_GATE_COMMANDS and _resolve_gate_command(g, acceptance) is not None
    ]
    ordered: list[str] = []
    for gate in (*leading, *extras):
        if gate not in ordered:
            ordered.append(gate)
    return ordered


def _run_gauntlet(acceptance: AcceptanceConfig, cwd: Path) -> list[_GateResult]:
    """Run every external gate named in ``acceptance.required_before_ship``.

    Each named gate that maps to a runnable command (see
    :func:`_resolve_gate_command`) is executed independently so each gate's
    pass/fail is reported on its own. Gate order is the deterministic order
    from :func:`_ordered_gauntlet_gates` — the canonical default gates
    (``pre-commit`` → ``lint`` → ``typecheck`` → ``tests``) lead, then any
    extra configured gate such as ``build``. Non-runnable gates (e.g.
    ``state``) are skipped silently. The caller aborts the ship when ANY
    returned gate is red (the W15 abort-on-red contract).

    Args:
        acceptance: The validated acceptance config surface.
        cwd: Working directory for each gate subprocess (the repo root).

    Returns:
        One :class:`_GateResult` per executed gate, in deterministic order.
    """
    results: list[_GateResult] = []
    for gate in _ordered_gauntlet_gates(acceptance):
        command = _resolve_gate_command(gate, acceptance)
        if command is None:
            continue
        results.append(_run_gate_command(gate, command, cwd))
    return results


@dataclass
class _ShipInputs:
    """Resolved ``/ship`` inputs gathered before any gate runs.

    Attributes:
        do_commit: Whether ``--commit`` opted into commit-group population.
        do_push: Whether ``--push`` opted into push population.
        pr_action: The normalised ``--pr`` action, or ``None``.
        artifact_paths: The artifact paths to validate.
        pr_body: The optional PR body text to validate.
        state: The loaded state document, or ``None``.
        phase_id: The phase id resolved from the scope, or ``None``.
        vcs_config: The validated ``vcs`` config surface.
        acceptance: The validated ``acceptance`` config surface.
    """

    do_commit: bool
    do_push: bool
    pr_action: str | None
    artifact_paths: list[Path]
    pr_body: Any
    state: State | None
    phase_id: str | None
    vcs_config: VcsConfig
    acceptance: AcceptanceConfig


@dataclass
class _ShipArtefacts:
    """The commit / push / PR artefacts built by the execute stage.

    Attributes:
        commit_groups: The built commit groups (empty without ``--commit``).
        push: The push descriptor, or ``None`` without ``--push``.
        pr: The PR descriptor, or ``None`` without ``--pr``.
    """

    commit_groups: list[ShipCommitGroup] = field(default_factory=list)
    push: ShipPush | None = None
    pr: ShipPr | None = None


@register
class ShipSkill(SkillAction):
    """Concrete ``/ship`` skill (Phase 4 W02)."""

    name: SkillName = "/ship"

    def _gather(self, run: ActionRun) -> _ShipInputs:
        return _ShipInputs(
            do_commit=_coerce_bool(run.args.get("commit", False)),
            do_push=_coerce_bool(run.args.get("push", False)),
            pr_action=_resolve_pr_action(run.args.get("pr")),
            artifact_paths=_coerce_path_list(
                run.args.get("artifact_paths") or run.args.get("artifacts")
            ),
            pr_body=run.args.get("pr_body"),
            state=_load_state(run.state_path),
            phase_id=_phase_id_from_scope(run.scope_id),
            vcs_config=_load_vcs_config(run.state_path),
            acceptance=_load_acceptance_config(run.state_path),
        )

    def _validate(self, run: ActionRun, inputs: _ShipInputs) -> SkillResult | None:
        # Each gate emits its own event (pass and fail) and short-circuits on
        # failure; the gates run in the documented pipeline order.
        for gate in (
            self._gate_artifacts,
            self._gate_audit,
            self._gate_merge_method,
            self._gate_gauntlet,
        ):
            failure = gate(run, inputs)
            if failure is not None:
                return failure
        return None

    def _gate_artifacts(self, run: ActionRun, inputs: _ShipInputs) -> SkillResult | None:
        validation_errors: list[str] = []
        for path in inputs.artifact_paths:
            try:
                artifact_report = validate_markdown_artifact(path.read_text(encoding="utf-8"))
            except OSError as exc:
                validation_errors.append(f"{path}: {exc}")
                continue
            validation_errors.extend(f"{path}: {error}" for error in artifact_report.errors)
        if isinstance(inputs.pr_body, str):
            text_report = validate_text_surface(inputs.pr_body, surface="pr")
            validation_errors.extend(text_report.errors)
        if not validation_errors:
            return None
        self._trace(
            run,
            "ship.artifact_gate",
            "ship: artifact validation failed",
            {"errors": validation_errors},
        )
        return self._ship_failure(
            run,
            rollback_notes="artifact validation failed",
            repair_commands=["fix artifact validation errors and rerun /ship"],
        )

    def _gate_audit(self, run: ActionRun, inputs: _ShipInputs) -> SkillResult | None:
        # Step 1 — probe ran. Step 2: gate on the recorded audit verdict.
        audit = (
            _latest_audit_for_phase(inputs.state, inputs.phase_id)
            if inputs.state is not None and inputs.phase_id is not None
            else None
        )
        # When state or a matching audit is unavailable we cannot gate on a
        # verdict; degrade open rather than block (the audit row is created by
        # /audit, which is a precondition the operator owns). When an audit
        # *does* exist its verdict must be ship-clearing.
        if audit is not None and audit.verdict not in _SHIP_ALLOWED_VERDICTS:
            verdict_label = audit.verdict.value if audit.verdict is not None else "none"
            self._trace(
                run,
                "ship.audit_gate",
                f"ship: audit gate blocked (verdict={verdict_label})",
                {"audit_required": True, "passed": False, "verdict": verdict_label},
            )
            assert inputs.phase_id is not None  # narrowed: audit set only when phase_id resolved
            return self._ship_failure(
                run,
                rollback_notes=f"audit verdict {verdict_label!r} does not clear the ship gate",
                repair_commands=[f"/audit {inputs.phase_id} --kind ship-gate"],
            )
        verdict_label = (
            audit.verdict.value if audit is not None and audit.verdict is not None else "ungated"
        )
        self._trace(
            run,
            "ship.audit_gate",
            f"ship: audit gate passed (verdict={verdict_label})",
            {"audit_required": True, "passed": True, "verdict": verdict_label},
        )
        return None

    def _gate_merge_method(self, run: ActionRun, inputs: _ShipInputs) -> SkillResult | None:
        # Step 2b — gate on the configured PR merge method. Squash is rejected
        # unless explicitly allowed; rebase / merge clear.
        merge_method = inputs.vcs_config.pr_merge_method
        if merge_method != "squash" or inputs.vcs_config.squash_allowed:
            return None
        self._trace(
            run,
            "ship.merge_method_gate",
            "ship: merge-method gate blocked (squash not allowed)",
            {"pr_merge_method": merge_method, "squash_allowed": False},
        )
        return self._ship_failure(
            run,
            rollback_notes="squash merge not permitted (set vcs.squash_allowed)",
            repair_commands=["set vcs.pr_merge_method to rebase (or enable vcs.squash_allowed)"],
        )

    def _gate_gauntlet(self, run: ActionRun, inputs: _ShipInputs) -> SkillResult | None:
        # Step 2c — run the local gauntlet. Each gate named in
        # ``acceptance.required_before_ship`` that maps to a runnable command
        # is executed for real; ANY red gate aborts the ship so a broken tree
        # can never pass. The repo root anchors each subprocess
        # (``state.json`` lives at ``<repo>/.ea/state.json``).
        repo_root = run.state_path.parent.parent
        gate_results = _run_gauntlet(inputs.acceptance, repo_root)
        failed_gates = [r for r in gate_results if not r.passed]
        if failed_gates:
            failed_names = ", ".join(r.name for r in failed_gates)
            self._trace(
                run,
                "ship.gauntlet_gate",
                f"ship: gauntlet gate blocked ({failed_names})",
                {"passed": False, "gates": [_gate_failure(r) for r in failed_gates]},
            )
            return self._ship_failure(
                run,
                rollback_notes=f"gauntlet gate failed: {failed_names}",
                repair_commands=[r.command for r in failed_gates],
            )
        if gate_results:
            self._trace(
                run,
                "ship.gauntlet_gate",
                f"ship: gauntlet gate passed ({len(gate_results)} gate(s))",
                {"passed": True, "gates": [r.name for r in gate_results]},
            )
        return None

    def _ship_failure(
        self, run: ActionRun, *, rollback_notes: str, repair_commands: list[str]
    ) -> SkillResult:
        """Build a failed ship result with an empty-artefact :class:`ShipBody`."""
        return self._fail(
            run,
            ShipBody(
                commit_groups=[],
                push=None,
                pr=None,
                estimate_vs_actual=dict(_ZERO_ESTIMATE),
                rollback_notes=rollback_notes,
            ).model_dump(mode="json"),
            next_valid_actions=list(_SHIP_NEXT_ACTIONS),
            repair_commands=repair_commands,
        )

    def _execute(self, run: ActionRun, inputs: _ShipInputs) -> _ShipArtefacts:
        # Resolve the co-author trailer once for every commit group's message.
        # Reuses the W12 resolver; never reimplemented here.
        coauthor_trailer = _resolve_coauthor_trailer_for_ship(inputs.vcs_config)
        # Step 3 — inspect git.
        self._trace(
            run,
            "ship.inspect_git",
            "ship: inspect git status / diff / scope",
            {"scope_id": run.scope_id},
        )
        # Step 4 — memory review.
        self._trace(
            run,
            "ship.memory_review",
            "ship: review session memory",
            {"promoted": 0, "pruned": 0},
        )
        # Step 5 — build pending-ship artefact. The commit-group message
        # carries the resolved co-author trailer so the commit-time pre-commit
        # hooks find the required trailer already present.
        artefacts = _ShipArtefacts(
            commit_groups=self._build_commit_groups(run, inputs, coauthor_trailer),
        )
        self._trace(
            run,
            "ship.build_pending",
            f"ship: built pending artefact ({len(artefacts.commit_groups)} commit group(s))",
            {"commit_groups": len(artefacts.commit_groups)},
        )
        # Step 6 — commit gate.
        self._trace(
            run,
            "ship.commit",
            f"ship: commit={inputs.do_commit}",
            {"applied": inputs.do_commit},
        )
        # Step 7 — push gate.
        if inputs.do_push:
            artefacts.push = ShipPush(ref="HEAD", status="planned")
        self._trace(
            run,
            "ship.push",
            f"ship: push={inputs.do_push}",
            {"applied": inputs.do_push},
        )
        # Step 8 — PR action.
        if inputs.pr_action is not None:
            artefacts.pr = ShipPr(
                action=inputs.pr_action,
                url=None,
                template="iter",
                gates=ShipPrGates(ci="pending", reviews="pending", state_valid=True),
            )
        self._trace(
            run,
            "ship.pr",
            f"ship: pr={inputs.pr_action or 'none'}",
            {"action": inputs.pr_action or "none"},
        )
        # Step 9 — gate evaluation already inside ShipPrGates.
        # Step 10 — record artefacts.
        self._trace(
            run,
            "ship.record",
            "ship: artefacts recorded",
            {
                "commit": inputs.do_commit,
                "push": inputs.do_push,
                "pr": inputs.pr_action or "none",
            },
        )
        # Step 11 — worktree cleanup (v0.1: skipped; the `eawf worktree`
        # surface owns this).
        return artefacts

    def _build_commit_groups(
        self, run: ActionRun, inputs: _ShipInputs, coauthor_trailer: str | None
    ) -> list[ShipCommitGroup]:
        if not inputs.do_commit:
            return []
        message = f"[{run.scope_id}] feat: pending ship"
        if coauthor_trailer is not None:
            message = f"{message}\n\n{coauthor_trailer}"
        return [ShipCommitGroup(message=message, files=[], evidence_refs=[])]

    def _render(self, run: ActionRun, inputs: _ShipInputs, outcome: _ShipArtefacts) -> SkillResult:
        body = ShipBody(
            commit_groups=outcome.commit_groups,
            push=outcome.push,
            pr=outcome.pr,
            estimate_vs_actual=dict(_ZERO_ESTIMATE),
            rollback_notes=None,
        )
        return self._ok(
            run,
            body.model_dump(mode="json"),
            next_valid_actions=list(_SHIP_NEXT_ACTIONS),
        )


__all__ = ["ShipSkill"]
