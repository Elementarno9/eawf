"""``/security-review`` skill — run the security-audit DSL on a scope.

C04b §5.6 lands ``/security-review`` as the skill surface over the
audit-check DSL (:mod:`eawf.audit_dsl.runner`). It runs a declarative
security-audit spec against a closed scope and emits audit envelopes per
C03 §5.6. When the active profile is ``security`` (a C08 contribution),
this skill is a required gate for ``phase close``.

The skill loads a caller-supplied audit spec (a YAML file of declarative
checks) via :func:`eawf.audit_dsl.runner.load_spec`, dispatches every
check through :func:`eawf.audit_dsl.runner.run_checks`, and folds the
pass/fail tally into a dict envelope body. The terminal status reflects
the run: ``ok`` when every check passes, ``failed`` when any check fails
(with the failing check names surfaced as repair commands). A missing or
unreadable spec degrades to ``status=needs_user`` so the operator can
point the skill at a real spec rather than the skill guessing one.

Honoured args:

- ``spec_path`` — path to the declarative audit spec (required; a
  missing path degrades to ``status=needs_user``).
- ``cwd`` — optional directory the checks run against; defaults to the
  process working tree (mirrors ``run_checks``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.audit_dsl.runner import load_spec, run_checks
from eawf.render.envelope import EnvelopeStatus, SkillName
from eawf.runtimes.plugin_manifest import SkillManifest
from eawf.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.skills.registry import register

logger = logging.getLogger(__name__)


MANIFEST = SkillManifest(
    name="/security-review",
    description="Run the security-audit DSL against a closed scope and emit findings.",
    runtime=["claude-code", "codex", "opencode"],
    dispatch={"session_policy": "fresh"},
    output_envelope_kind="security_review_report",
)


@register
class SecurityReviewSkill(Skill):
    """Concrete ``/security-review`` skill (C04b §5.6)."""

    name: SkillName = "/security-review"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        spec_arg = args.get("spec_path")
        spec_path = str(spec_arg) if spec_arg else None
        if not spec_path or not Path(spec_path).is_file():
            return SkillResult(
                status="needs_user",
                body={
                    "kind": "security_review_report",
                    "scope_id": scope_id,
                    "spec_path": spec_path,
                    "checks_run": 0,
                    "findings": [],
                    "reason": "spec_path is required and must point at a readable audit spec",
                },
                next_valid_actions=["eawf audit run --spec <path>"],
            )

        cwd_arg = args.get("cwd")
        cwd = Path(str(cwd_arg)) if cwd_arg else None

        specs = load_spec(Path(spec_path))
        results = run_checks(specs, cwd=cwd)
        findings = [{"name": r.name, "passed": r.passed, "details": r.details} for r in results]
        failed = [r.name for r in results if not r.passed]

        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="security_review.run",
            summary=f"security-review: {len(results)} check(s), {len(failed)} failing",
            payload={
                "spec_path": spec_path,
                "checks_run": len(results),
                "failed": failed,
            },
        )

        status: EnvelopeStatus = "failed" if failed else "ok"
        repair = [f"fix failing check: {name}" for name in failed] if failed else None
        return SkillResult(
            status=status,
            body={
                "kind": "security_review_report",
                "scope_id": scope_id,
                "spec_path": spec_path,
                "checks_run": len(results),
                "findings": findings,
            },
            persisted_store_records=[evt_id],
            repair_commands=repair,
            next_valid_actions=[f"eawf audit run --spec {spec_path}"],
        )


__all__ = ["MANIFEST", "SecurityReviewSkill"]
