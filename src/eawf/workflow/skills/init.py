"""``/init`` skill — bootstrap a new Eä Workflow workspace via the install wizard.

Per the design spec §4 W03 the ``/init`` skill is a thin envelope wrapper
around :func:`eawf.platform.install.wizard.run_wizard_no_input` (Phase 3 W05). The
skill never duplicates wizard logic — its job is to translate a fully
populated :class:`WizardAnswers` into a typed :class:`InitBody` and let
the engine emit the canonical envelope.

Lifecycle (per `docs/architecture/workflow.md`):

1. Probe instruments via ``EA_INSTRUMENT_PROBE``.
2. If ``ctx.args`` already carries every wizard answer → run the pure
   pipeline and emit ``status=ok`` with :class:`InitBody.steps` populated
   by the wizard's ``WizardResult``.
3. If ``ctx.args`` is missing required keys (mid-wizard / interactive
   surface degraded) → emit ``status=needs_user`` with a typed
   :class:`UserQuestion` populated under :attr:`InitBody.user_question`.
   The runtime can then re-invoke with the missing answers.

v0.1 contract: the skill does **not** drive the questionary surface.
The runtime adapter is responsible for collecting the answers — the
skill expects the validated mapping on ``ctx.args``. This keeps the
skill loop-free and matches the §14 algorithm step 6 ("Ask approval with
concrete options").

Honoured ``ctx.args`` keys:

- ``answers`` — dict matching :class:`WizardAnswers` field set; required
  for the happy path.
- ``target_dir`` — absolute path of the target repo; defaults to ``"."``.
- ``force`` — bool override for the wizard's ``--force`` gate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.platform.install.wizard import WizardAnswers, run_wizard_no_input
from eawf.surfaces.cli.errors import UserError
from eawf.surfaces.render.envelope import SkillName
from eawf.workflow.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.workflow.skills.bodies.init import InitBody, InitStep
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


# Field set the wizard requires; mirrors :class:`WizardAnswers` minus the
# defaulted booleans / runtime extras. The skill uses this to detect the
# mid-wizard degrade path before instantiating the strict Pydantic model.
_REQUIRED_ANSWER_KEYS: frozenset[str] = frozenset(
    {
        "state_path",
        "project_code",
        "project_title",
        "lifecycle_depth",
        "profiles",
        "runtime",
    }
)


def _missing_keys(answers: dict[str, Any]) -> list[str]:
    """Return the required answer keys that are missing from *answers*."""
    return sorted(_REQUIRED_ANSWER_KEYS - set(answers.keys()))


def _coerce_target_dir(raw: Any) -> Path:
    """Coerce an args-dict value into an absolute target directory."""
    if raw is None:
        return Path.cwd().resolve()
    if isinstance(raw, Path):
        return raw.resolve()
    return Path(str(raw)).resolve()


def _build_user_question(missing: list[str]) -> UserQuestion:
    """Render the canonical mid-wizard ``UserQuestion``.

    The runtime presents a 2-3 option picker so the operator can choose
    whether to fill the gaps now, accept defaults, or abort. Per the
    :class:`UserQuestion` contract every status=needs_user envelope must
    carry a populated 2-4 option list.
    """
    detail = ", ".join(missing) if missing else "answers payload"
    return UserQuestion(
        question=(f"/init needs the wizard answers to proceed. Missing: {detail}."),
        options=[
            UserQuestionOption(
                label="provide_answers",
                description="Re-run /init with the missing answers populated.",
            ),
            UserQuestionOption(
                label="run_interactive",
                description="Drive the questionary wizard surface (eawf init --interactive).",
            ),
            UserQuestionOption(
                label="cancel",
                description="Abort the init flow.",
            ),
        ],
    )


@register
class InitSkill(Skill):
    """Concrete ``/init`` skill (Phase 4 W03).

    Wraps :func:`eawf.platform.install.wizard.run_wizard_no_input` so the wizard's
    pure pipeline emits a canonical :class:`OutputEnvelope` instead of a
    plain :class:`WizardResult`. No behavioural change to the wizard
    itself — the skill only adapts the I/O.
    """

    name: SkillName = "/init"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        next_actions: list[str] = ["eawf roadmap", "eawf differentiate"]

        # Step 1 — detect current state. v0.1 emits one event per
        # algorithm milestone so the audit trail mirrors the §14 steps.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="init.detect_state",
            summary=f"init: detect current state for scope {scope_id}",
            payload={"scope_id": scope_id},
        )
        persisted_records.append(evt_id)

        raw_answers = args.get("answers")
        answers_dict: dict[str, Any]
        if isinstance(raw_answers, dict):
            answers_dict = dict(raw_answers)
        elif raw_answers is None:
            # Operator may have flattened the dict onto ctx.args directly.
            answers_dict = {k: v for k, v in args.items() if k in _REQUIRED_ANSWER_KEYS}
        else:
            answers_dict = {}

        missing = _missing_keys(answers_dict)
        if missing:
            # Step 2 — degrade: ask the operator for the missing answers.
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="init.degrade_needs_user",
                summary=f"init: missing wizard answers ({len(missing)})",
                payload={"missing": missing},
            )
            persisted_records.append(evt_id)
            body = InitBody(
                project_code=str(answers_dict.get("project_code") or "")
                if answers_dict.get("project_code")
                else None,
                workspace_root=str(answers_dict.get("state_path"))
                if answers_dict.get("state_path")
                else None,
                profile_ids=list(answers_dict.get("profiles") or []),
                steps=[
                    InitStep(
                        name="collect_answers",
                        status="needs_user",
                        detail=f"missing keys: {missing}",
                    ),
                ],
                user_question=_build_user_question(missing),
            )
            return SkillResult(
                status="needs_user",
                body=body.model_dump(mode="json"),
                persisted_store_records=persisted_records,
                state_mutations=state_mutations,
                next_valid_actions=next_actions,
            )

        # Step 3 — propose / approve / execute. v0.1 collapses these into
        # the single pure-pipeline call so the env-var-driven runtime can
        # opt out (the wizard's own contract guarantees byte-stable
        # re-runs, so an extra "approve" round-trip would be redundant).
        target_dir = _coerce_target_dir(args.get("target_dir"))
        force = bool(args.get("force", False))

        try:
            answers = WizardAnswers(**answers_dict)
        except Exception as exc:
            # A schema mismatch is operator error — surface as failed (not
            # blocked) so the engine fills repair commands and exposes the
            # validation message.
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="init.invalid_answers",
                summary=f"init: invalid wizard answers: {type(exc).__name__}",
                payload={"error": str(exc)[:240]},
            )
            persisted_records.append(evt_id)
            raise UserError(f"invalid wizard answers: {exc}", kind="InvalidInput") from exc

        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="init.run_wizard",
            summary=f"init: run wizard target_dir={target_dir}",
            payload={"target_dir": str(target_dir), "force": force},
        )
        persisted_records.append(evt_id)

        result = run_wizard_no_input(answers, target_dir, force=force)

        # Wizard succeeded — translate the WizardResult into an InitBody.
        steps: list[InitStep] = [
            InitStep(name="state_json", status="ok", detail=str(result.state_path)),
            InitStep(name="config_yaml", status="ok", detail=str(result.config_path)),
            InitStep(name="agents_md", status="ok", detail=str(result.agents_md_path)),
            InitStep(name="manifest", status="ok", detail=str(result.manifest_path)),
            InitStep(name="claude_md", status="ok", detail=str(result.claude_md_path)),
        ]
        if result.materialised_state_keys:
            steps.append(
                InitStep(
                    name="materialise_state_keys",
                    status="ok",
                    detail=f"keys={list(result.materialised_state_keys)}",
                ),
            )

        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="init.complete",
            summary=f"init: wizard complete project={result.project_code}",
            payload={
                "project_code": result.project_code,
                "profiles": result.profiles_enabled,
                "materialised_keys": result.materialised_state_keys,
            },
        )
        persisted_records.append(evt_id)

        body = InitBody(
            project_code=result.project_code,
            workspace_root=str(target_dir),
            profile_ids=list(result.profiles_enabled),
            steps=steps,
            user_question=None,
        )

        return SkillResult(
            status="ok",
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            state_mutations=state_mutations,
            next_valid_actions=next_actions,
        )


__all__ = ["InitSkill"]
