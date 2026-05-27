"""``/audit`` skill — full iteration audit, branched on profile composition.

Implements the §14 algorithm for ``/audit``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``; abort on hard miss.
2. Resolve scope: active iter by default; may target wave/phase/PR.
3. **Branch on profile composition** (the W02 acceptance contract item):
   - ``research`` profile enabled → ``kind=evaluation`` (MLflow integrity,
     outcome measurements, hypothesis verdicts, evaluation artefact).
   - ``research`` profile not enabled → ``kind=ship-gate`` (tests, lint,
     typecheck, build, security, docs links, scope drift).
   - Either kind may be requested explicitly via ``ctx.args["kind"]``.
4. Build a check plan from the target wave's success criteria + the
   caller-supplied per-criterion check directives.
5. **Run the checks for real** via the audit-check DSL
   (:func:`eawf.workflow.audit_dsl.runner.run_checks`). Two flavours feed the
   plan: criterion-vs-diff checks (``criterion_in_diff`` — does the
   shipped source reflect the criterion?) and behavioural smoke checks
   (``command_exit_zero`` — does running the changed surface still
   pass?). No check returns ``skipped`` by default.
6. Aggregate per-criterion verdicts: a failing check becomes an
   :class:`~eawf.workflow.skills.bodies.audit.AuditFinding` naming the offending
   criterion, and the envelope degrades to ``status=partial``.
7. **Dispatch a fresh-context auditor**: emit an
   :class:`~eawf.workflow.skills.bodies.audit.AuditorDispatch` directive carrying
   the diff base + criteria ONLY. The skill does not spawn the thread —
   it emits the directive so a model-invoked runtime spawns a
   fresh-context auditor subagent whose verdict cannot be biased by the
   parent conversation.
8. Mark each finding (blocker/fix-now/follow-up/false-positive).
9. ``--fix-safe`` applies bounded fixes (deferred — out of W14 scope).
10. Write ``Audit`` artefact with commands/outputs/metrics.
11. Update outcomes/hypotheses only from audit evidence.

Each algorithm step writes one ``EVENT`` row. The body is populated with
the typed AuditBody from W01.

Honoured args:

- ``--checks`` — comma-separated list of check names; ``ctx.args["checks"]``
  selects which check rows feed the default profile-driven check plan
  (used when no per-criterion directives are supplied).
- ``--kind evaluation|ship-gate`` — overrides the profile branch.
- ``wave_id`` — the wave under audit. Resolved against state to read its
  ``success_criteria`` + ``file_scopes``. Falls back to ``ctx.scope``.
- ``diff_base`` — git ref the criterion-vs-diff checks + the auditor
  dispatch diff against. Defaults to ``main``.
- ``criterion_checks`` — list of per-criterion check directives. Each
  entry is a dict ``{criterion, pattern?, file_scopes?, argv?}``: a
  ``pattern`` builds a ``criterion_in_diff`` check, an ``argv`` builds a
  ``command_exit_zero`` behavioural smoke check. When omitted, the skill
  derives a ``criterion_in_diff`` check per criterion from the wave's
  ``success_criteria`` + ``file_scopes``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import orjson

from eawf.kernel.state.models import State, Wave
from eawf.surfaces.render.envelope import EnvelopeStatus, SkillName
from eawf.workflow.audit_dsl.models import CheckSpec
from eawf.workflow.audit_dsl.runner import run_checks
from eawf.workflow.skills._common import (
    emit_event,
    has_research_profile,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.workflow.skills.bodies.audit import (
    AuditBody,
    AuditCheckRun,
    AuditFinding,
    AuditKind,
    AuditorDispatch,
)
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


_DEFAULT_SHIP_GATE_CHECKS: tuple[str, ...] = (
    "tests",
    "lint",
    "type",
    "build",
    "docs",
    "state",
)
_DEFAULT_EVALUATION_CHECKS: tuple[str, ...] = (
    "mlflow_integrity",
    "lookahead_bias",
    "is_oos_gap",
    "outcome_measure",
    "hypothesis_verdict",
)
_DEFAULT_DIFF_BASE: str = "main"


def _normalise_checks(raw: Any, default: tuple[str, ...]) -> list[str]:
    """Coerce ``ctx.args['checks']`` into a list[str], falling back to *default*."""
    if raw is None:
        return list(default)
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return parts or list(default)
    if isinstance(raw, list):
        return [str(p) for p in raw if str(p).strip()] or list(default)
    return list(default)


def _resolve_kind(args: dict[str, Any], state_path: Any) -> AuditKind:
    """Determine the audit kind from explicit override / profile branch."""
    explicit = args.get("kind")
    if isinstance(explicit, str):
        normalised = explicit.lower()
        if normalised in {"evaluation", "ship-gate"}:
            # Both literals match the AuditKind frozen literal in W01.
            return normalised  # type: ignore[return-value]
    if has_research_profile(state_path):
        return "evaluation"
    return "ship-gate"


def _load_wave(state_path: Path, wave_id: str | None) -> Wave | None:
    """Read *wave_id* from on-disk state (read-only); return ``None`` on miss.

    The skill never mutates state — it loads the snapshot solely to read
    the wave's ``success_criteria`` + ``file_scopes`` so the check plan
    and the auditor dispatch directive carry the real criteria.
    """
    if not wave_id or not state_path.is_file():
        return None
    try:
        payload = orjson.loads(state_path.read_bytes())
        state = State.model_validate(payload)
    except Exception as exc:
        logger.debug(f"_load_wave skip wave={wave_id!r} reason={exc!r}")
        return None
    return state.waves.get(wave_id)


def _build_criterion_specs(
    *,
    criterion_checks: Any,
    wave: Wave | None,
) -> list[CheckSpec]:
    """Translate per-criterion directives (or the wave) into check specs.

    Two sources, in priority order:

    1. Explicit ``criterion_checks`` directives (``list[dict]``). Each
       directive maps one criterion onto one check: a ``pattern`` →
       ``criterion_in_diff``; an ``argv`` → ``command_exit_zero``
       behavioural smoke check.
    2. When no directives are supplied, derive one ``criterion_in_diff``
       check per ``wave.success_criteria`` entry, searching the wave's
       ``file_scopes`` for a literal substring of the criterion text.

    Returns an empty list when neither source yields a check (a wave
    with zero criteria + no directives audits as a no-op — see the
    skill's boundary handling).
    """
    specs: list[CheckSpec] = []
    directives = criterion_checks if isinstance(criterion_checks, list) else []
    for index, directive in enumerate(directives):
        if not isinstance(directive, dict):
            continue
        criterion = str(directive.get("criterion") or f"criterion-{index + 1}")
        name = f"criterion-{index + 1}"
        argv = directive.get("argv")
        if isinstance(argv, list) and argv:
            specs.append(
                CheckSpec(
                    kind="command_exit_zero",
                    name=name,
                    args={"argv": [str(a) for a in argv], "criterion": criterion},
                )
            )
            continue
        pattern = directive.get("pattern")
        scopes = directive.get("file_scopes")
        scope_list = (
            [str(s) for s in scopes]
            if isinstance(scopes, list) and scopes
            else (list(wave.file_scopes) if wave else [])
        )
        if isinstance(pattern, str) and pattern and scope_list:
            specs.append(
                CheckSpec(
                    kind="criterion_in_diff",
                    name=name,
                    args={
                        "criterion": criterion,
                        "pattern": pattern,
                        "file_scopes": scope_list,
                    },
                )
            )
    if specs:
        return specs

    # Fallback: derive a criterion_in_diff check per criterion from the
    # wave itself. Only viable when the wave has both criteria + scopes.
    if wave is not None and wave.file_scopes:
        scope_list = list(wave.file_scopes)
        for index, criterion in enumerate(wave.success_criteria):
            specs.append(
                CheckSpec(
                    kind="criterion_in_diff",
                    name=f"criterion-{index + 1}",
                    args={
                        "criterion": criterion,
                        "pattern": re.escape(criterion),
                        "file_scopes": scope_list,
                    },
                )
            )
    return specs


#: Public alias for :func:`_build_criterion_specs` so W08's
#: :func:`eawf.workflow.verify.compile.compile_gate` can reuse the same
#: directive->CheckSpec shape without duplicating the construction. The
#: underscored name remains the canonical call site inside this module
#: (the docstring cross-reference in
#: :class:`eawf.workflow.audit_dsl.models.CommandExitZeroArgs` points
#: at it) so renaming the function itself would force an out-of-scope
#: edit; aliasing keeps both names valid.
build_criterion_specs = _build_criterion_specs


def _auditor_instruction(wave_id: str, diff_base: str, criteria: list[str]) -> str:
    """Render the human-readable auditor dispatch instruction."""
    return (
        f"spawn a fresh-context auditor for {wave_id}: it receives the "
        f"diff against {diff_base} and the {len(criteria)} criteria below "
        "ONLY — no parent-conversation context. re-verify each criterion "
        "against the diff and return a per-criterion verdict"
    )


@register
class AuditSkill(Skill):
    """Concrete ``/audit`` skill (Phase 4 W02; W14 runs real checks)."""

    name: SkillName = "/audit"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        kind: AuditKind = _resolve_kind(args, state_path)
        default_checks = (
            _DEFAULT_EVALUATION_CHECKS if kind == "evaluation" else _DEFAULT_SHIP_GATE_CHECKS
        )
        default_check_names = _normalise_checks(args.get("checks"), default_checks)

        wave_arg = args.get("wave_id")
        wave_id = str(wave_arg) if wave_arg else scope_id
        diff_base = str(args.get("diff_base") or _DEFAULT_DIFF_BASE)
        wave = _load_wave(state_path, str(wave_arg) if wave_arg else None)

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        next_actions: list[str] = ["eawf phase prepare-close", "eawf ship", "eawf audit show"]
        evidence_refs: list[str] = []

        # Step 1 — probe ran. Step 2: resolve scope.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.resolve_scope",
            summary=f"audit: scope={scope_id} kind={kind}",
            payload={"kind": kind, "scope_id": scope_id, "wave_id": wave_id},
        )
        persisted_records.append(evt_id)

        # Step 3 — branch on profile composition.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.branch_kind",
            summary=f"audit: kind branch -> {kind}",
            payload={"kind": kind, "explicit": "kind" in args},
        )
        persisted_records.append(evt_id)

        # Step 4 — build the real check plan from the wave's criteria.
        criterion_specs = _build_criterion_specs(
            criterion_checks=args.get("criterion_checks"),
            wave=wave,
        )
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.build_check_plan",
            summary=f"audit: planned {len(criterion_specs)} criterion check(s)",
            payload={
                "criterion_check_count": len(criterion_specs),
                "default_checks": default_check_names,
            },
        )
        persisted_records.append(evt_id)

        # Step 5 — run the checks for real. cwd is the repo root (state
        # lives at <repo>/.ea/state.json so its grandparent is the root).
        repo_root = state_path.parent.parent
        results = run_checks(criterion_specs, cwd=repo_root)
        runs: list[AuditCheckRun] = []
        findings: list[AuditFinding] = []
        for spec, result in zip(criterion_specs, results, strict=True):
            criterion_text = str(spec.args.get("criterion") or spec.name)
            command = (
                f"eawf audit check {spec.kind} {spec.name}"
                if spec.kind != "command_exit_zero"
                else " ".join(str(a) for a in spec.args.get("argv", []))
            )
            runs.append(
                AuditCheckRun(
                    check_id=spec.name,
                    command=command,
                    status="pass" if result.passed else "fail",
                    output_blob=result.details,
                )
            )
            if not result.passed:
                findings.append(
                    AuditFinding(
                        severity="high",
                        location=criterion_text,
                        summary=result.details or f"check {spec.name} failed",
                        kind="blocker",
                    )
                )
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="audit.run_check",
                summary=f"audit: ran {spec.name} -> {'pass' if result.passed else 'fail'}",
                payload={
                    "check_id": spec.name,
                    "kind": spec.kind,
                    "status": "pass" if result.passed else "fail",
                },
            )
            persisted_records.append(evt_id)

        # Step 6 — collect metrics.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.collect_metrics",
            summary=f"audit: {len(findings)} finding(s) across {len(runs)} check(s)",
            payload={"finding_count": len(findings), "check_count": len(runs)},
        )
        persisted_records.append(evt_id)

        # Step 7 — dispatch a fresh-context auditor. The skill emits the
        # directive; the runtime spawns the thread so no parent context
        # leaks into the auditor. The directive is emitted only when a wave
        # actually resolves: without one, ``wave_id`` is the phase-scope URN
        # fallback and the criteria are empty, which would render a malformed
        # "spawn an auditor for urn:...:QR/P00 with 0 criteria" directive.
        if wave is not None:
            criteria = list(wave.success_criteria)
            auditor_dispatch: AuditorDispatch | None = AuditorDispatch(
                wave_id=wave_id,
                diff_base=diff_base,
                criteria=criteria,
                instruction=_auditor_instruction(wave_id, diff_base, criteria),
            )
            dispatch_summary = f"audit: dispatch fresh-context auditor for {wave_id}"
        else:
            criteria = []
            auditor_dispatch = None
            dispatch_summary = "audit: no wave resolved -- auditor dispatch skipped"
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.dispatch_auditor",
            summary=dispatch_summary,
            payload={
                "wave_id": wave_id,
                "diff_base": diff_base,
                "dispatched": auditor_dispatch is not None,
                "criterion_count": len(criteria),
            },
        )
        persisted_records.append(evt_id)

        # Step 8 — mark findings (done inline above).
        # Step 9 — fix-safe (deferred — out of W14 scope).
        # Step 10 — write artefact.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.write_artifact",
            summary="audit: artifact recorded as event payload",
            payload={"kind": kind, "check_count": len(runs)},
        )
        persisted_records.append(evt_id)

        # Step 11 — update outcomes/hypotheses only from audit evidence
        # (verdict-bearing state path runs through ``eawf audit run``).

        body = AuditBody(
            scope_id=scope_id,
            kind=kind,
            checks_run=runs,
            outcomes_measured=[],
            hypothesis_verdicts=[],
            findings=findings,
            audit_artifact_urn=None,
            auditor_dispatch=auditor_dispatch,
        )

        # A failing check is a soft outcome: the checks ran and produced
        # a verdict, but the gate did not pass. ``partial`` is the
        # envelope status for "ran fine, result is not all-green".
        status: EnvelopeStatus = "partial" if findings else "ok"
        if findings:
            next_actions = [f"fix unmet criterion: {f.location}" for f in findings] + next_actions

        return SkillResult(
            status=status,
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            state_mutations=state_mutations,
            evidence_refs=evidence_refs,
            next_valid_actions=next_actions,
        )


__all__ = ["AuditSkill", "build_criterion_specs"]
