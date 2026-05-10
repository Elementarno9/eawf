"""``/flow`` skill — composite controller running the six core skills in order.

Per `ea-proposal.md` §14 ``/flow`` drives a one-click ADD iteration:
research → prep → execute (includes audit) → ship. The v0.1 plan §4 W03
constrains this further: ``/flow`` runs all six core skills sequentially
(research → prep → audit → ship → review → polish), accumulating per-step
envelopes under :attr:`FlowBody.steps`.

Short-circuit semantics (the W03 acceptance contract):

- The flow runs each core skill in order.
- After each step the flow inspects ``env.header.status``. Anything other
  than ``ok`` (``needs_user``, ``blocked``, ``failed``, ``partial``)
  triggers an immediate short-circuit. The flow's terminal envelope
  inherits the failing step's ``status`` and ``footer.repair_commands``.
- If every step returns ``ok``, the flow's terminal envelope is ``ok``
  and the body's ``terminal_status`` mirrors the last step's status.

The flow does **not** literally call :func:`eawf.skills.engine.run_skill`
on each subskill — instead it constructs a fresh :class:`SkillContext`
copy and routes through the engine so the per-step envelope is fully
populated (header status, instrument probe, footer mutations). This
keeps the short-circuit decision focused on the canonical envelope
shape rather than the action-side return type.

Honoured ``ctx.args`` keys:

- ``topic`` — free-form description recorded on :attr:`FlowBody.topic`.
- ``stop_after`` — short-circuit before the named step (matches §14's
  ``--stop-after`` flag). v0.1 honours the canonical names
  ``research|prep|audit|ship|review|polish``.
- ``args_per_step`` — optional dict of ``skill_name → ctx.args`` to
  forward to specific steps; absent steps inherit the flow's own args.
- ``resume_from`` — Phase 5 W02. Optional :class:`FlowCheckpointPayload`
  dict carrying the replay anchor; when present, the runner skips the
  prefix ``flow_order[:step_index + 1]`` and starts at step ``step_index
  + 1``.

Phase 5 W02 also adds:

- After every step boundary, the runner appends a
  :class:`FlowCheckpointPayload` envelope to ``flow.jsonl`` (under the
  per-file portalock + fsync).
- ``--resume`` reads the latest ``last_safe=True`` checkpoint and
  computes drift (state.json sha, git HEAD, profile id list, args hash).
  Drift refuses with exit ``INTEGRITY_VIOLATION`` and a populated
  ``drift`` body field.

The implementation is intentionally explicit: each subskill is an
attribute on the flow class so a test can monkey-patch a single subskill
without rewriting the registry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from pydantic import ValidationError

from eawf.cli import errors as cli_errors
from eawf.render.envelope import OutputEnvelope, SkillName
from eawf.skills._common import (
    emit_event,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.skills.audit import AuditSkill
from eawf.skills.bodies.flow import FlowBody
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult, run_skill
from eawf.skills.polish import PolishSkill
from eawf.skills.prep import PrepSkill
from eawf.skills.registry import register
from eawf.skills.research import ResearchSkill
from eawf.skills.review import ReviewSkill
from eawf.skills.ship import ShipSkill
from eawf.state.enums import FlowStatus, StoreKind
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.kinds.flow import FlowCheckpointPayload, FlowPayload
from eawf.store.paths import store_path

logger = logging.getLogger(__name__)


# Canonical core-skill order for ``/flow`` per §14 + v0.1 plan §4 W03.
# The list lives at module level so tests can iterate it without copying
# the order from the docstring.
_CORE_FLOW_ORDER: tuple[tuple[SkillName, type[Skill]], ...] = (
    ("/research", ResearchSkill),
    ("/prep", PrepSkill),
    ("/audit", AuditSkill),
    ("/ship", ShipSkill),
    ("/review", ReviewSkill),
    ("/polish", PolishSkill),
)


# Hard cap per git invocation. ``flow`` is operator-driven so a 5 s
# budget is well above the happy path while still bounding a hung daemon.
_GIT_TIMEOUT_SECONDS: float = 5.0


def _stop_after_short_name(skill_name: SkillName) -> str:
    """Strip the leading ``/`` so ``stop_after`` can be a bare name.

    Mirrors the §14 ``--stop-after`` flag values: ``research|prep|audit|
    ship|review|polish`` (no leading slash) — the flow honours either
    form so operators don't have to escape the slash in shells.
    """
    return skill_name.lstrip("/")


def _resolve_stop_after(raw: Any) -> str | None:
    """Return a normalised ``stop_after`` short name or ``None``.

    Empty / unrecognised values yield ``None`` (the flow runs the full
    pipeline). Recognised values match a member of :data:`_CORE_FLOW_ORDER`.
    """
    if raw is None:
        return None
    candidate = str(raw).strip().lower().lstrip("/")
    if not candidate:
        return None
    valid = {_stop_after_short_name(name) for name, _ in _CORE_FLOW_ORDER}
    if candidate not in valid:
        return None
    return candidate


def short_circuit_terminal_status(statuses: list[str]) -> str:
    """Compute the flow's terminal status from a sequence of step statuses.

    Contract (mirrored by the test_skill_flow property test):

    - Empty input → ``"ok"`` (the flow ran nothing; nothing failed).
    - First non-``ok`` status wins (short-circuit); the rest of the list
      is ignored.
    - All-``ok`` input → ``"ok"`` (terminal status mirrors the last step).

    Returns:
        The terminal status string.
    """
    for s in statuses:
        if s != "ok":
            return s
    return "ok"


# ---- Drift detection helpers ------------------------------------------------


def _workspace_root_for_state(state_path: Path) -> Path:
    """Return the workspace root containing *state_path*.

    The canonical layout is ``<workspace>/.ea/state.json``; the workspace
    root is therefore ``state_path.parent.parent`` when the parent
    directory is named ``.ea``. Falls back to the parent so test fixtures
    that drop ``state.json`` directly under a temp dir still resolve.
    """
    ea_dir = state_path.parent
    return ea_dir.parent if ea_dir.name == ".ea" else ea_dir


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Return stripped git stdout, or ``None`` on any failure.

    Mirrors the safe-degrade pattern in
    :mod:`eawf.runtimes.claude.statusline_modules.git`: any non-zero exit,
    missing binary, or timeout collapses to ``None`` so callers can
    distinguish "not a git repo / git unavailable" from a real value.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        logger.debug(f"_run_git: {' '.join(args)!r} failed: {exc}")
        return None
    return proc.stdout.strip()


def _current_git_head(workspace_root: Path) -> str | None:
    """Return the current ``git rev-parse HEAD`` SHA or ``None``."""
    head = _run_git(["rev-parse", "HEAD"], workspace_root)
    if head is None:
        return None
    # ``rev-parse`` may return short forms in odd configs; require the
    # canonical 40-char hex form so the pattern validator on the
    # checkpoint payload accepts the round-trip.
    if len(head) != 40:
        logger.debug(f"_current_git_head: unexpected length {len(head)} for {head!r}")
        return None
    return head


def _state_hash(state_path: Path) -> str:
    """Return ``sha256:<hex>`` of the on-disk state.json bytes.

    Missing-file collapses to a synthetic ``sha256:<all-zeros>``-style
    sentinel — the canonical pattern still matches so the checkpoint
    payload validates, but a real state.json materialising between two
    appends would be flagged as drift.
    """
    try:
        data = state_path.read_bytes()
    except FileNotFoundError:
        # Empty bytes hash so the sentinel still fits the regex.
        data = b""
    digest = hashlib.sha256(data).hexdigest()
    return f"sha256:{digest}"


def _canonical_args_per_step_hash(args_per_step: dict[str, Any] | None) -> str:
    """Return ``sha256:<hex>`` of the canonical-form per-step args JSON.

    Canonicalisation: sorted keys, no whitespace. None / empty input
    hashes to the SHA of an empty JSON object (``{}``) so the absent and
    all-defaults cases are indistinguishable.

    The runner emits one checkpoint per step and hashes the **per-step**
    args dict (the value forwarded to that step's :class:`SkillContext`)
    so per-step arg mutations between resume and the original run are
    detected by :func:`compute_drift`. Hashing the whole multi-step
    mapping would yield the same value for every checkpoint and provide
    no per-step granularity.
    """
    payload = args_per_step or {}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    return f"sha256:{digest}"


def _resolve_step_args(
    args_per_step: dict[str, Any] | None,
    skill_name: SkillName,
) -> dict[str, Any]:
    """Return the per-step args dict for *skill_name*.

    Preference order: full skill name (``"/research"``), then short name
    (``"research"``). An explicit-key empty dict no longer collapses to
    the short-name fallback (an ``or`` chain dropped empty dicts because
    they are falsy).
    """
    mapping = args_per_step or {}
    short = _stop_after_short_name(skill_name)
    if skill_name in mapping:
        forwarded = mapping[skill_name]
    elif short in mapping:
        forwarded = mapping[short]
    else:
        return {}
    return dict(forwarded) if isinstance(forwarded, dict) else {}


def _payload_hash(body: Any) -> str:
    """Return ``sha256:<hex>`` of an envelope body's canonical JSON."""
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    return f"sha256:{digest}"


def _current_profile_ids(state_path: Path) -> list[str]:
    """Return the sorted list of merged enabled profile ids.

    Reuses :func:`eawf.config.layered.merge_config` against the workspace
    root so the fixture-friendly cases (no config files) still produce a
    stable empty list. Any unexpected exception collapses to ``[]`` so
    drift detection sees "no profile change" rather than crashing.
    """
    try:
        from eawf.config.layered import merge_config
    except Exception as exc:  # pragma: no cover - defensive only
        logger.debug(f"_current_profile_ids: import failed: {exc}")
        return []
    workspace_root = _workspace_root_for_state(state_path)
    try:
        merged, _sources = merge_config(repo=workspace_root, workspace=workspace_root)
    except Exception as exc:
        logger.debug(f"_current_profile_ids: merge_config raised: {exc}")
        return []
    profiles = merged.get("profiles") if isinstance(merged, dict) else None
    if not isinstance(profiles, dict):
        return []
    enabled = profiles.get("enabled") or []
    if not isinstance(enabled, list):
        return []
    return sorted(str(p) for p in enabled if isinstance(p, str))


def compute_drift(
    checkpoint: FlowCheckpointPayload,
    state_path: Path,
    *,
    args_per_step: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Compare a checkpoint's parent_* fields against the current workspace.

    Returns ``None`` when every dimension matches (no drift). Otherwise
    returns a dict whose keys identify each dimension that drifted; the
    values carry the ``checkpoint`` and ``current`` snapshots so the
    operator can see what changed.

    Drift dimensions:

    - ``state_json``: ``sha256:<hex>`` of ``state.json`` bytes.
    - ``git_head``: 40-char ``git rev-parse HEAD`` (``None`` when not a
      git repo on either side; both sides ``None`` is treated as a match).
    - ``profile_ids``: sorted list of merged enabled profile ids.
    - ``args_per_step``: ``sha256:<hex>`` of canonical-form
      ``args_per_step`` dict.
    """
    drift: dict[str, Any] = {}

    current_state = _state_hash(state_path)
    if current_state != checkpoint.parent_state_hash:
        drift["state_json"] = {
            "checkpoint": checkpoint.parent_state_hash,
            "current": current_state,
        }

    workspace_root = _workspace_root_for_state(state_path)
    current_head = _current_git_head(workspace_root)
    if (current_head, checkpoint.parent_git_head) != (None, None) and (
        current_head != checkpoint.parent_git_head
    ):
        drift["git_head"] = {
            "checkpoint": checkpoint.parent_git_head,
            "current": current_head,
        }

    current_profiles = _current_profile_ids(state_path)
    if current_profiles != checkpoint.parent_profile_ids:
        drift["profile_ids"] = {
            "checkpoint": checkpoint.parent_profile_ids,
            "current": current_profiles,
        }

    current_step_args = _resolve_step_args(args_per_step, checkpoint.step_name)
    current_args_hash = _canonical_args_per_step_hash(current_step_args)
    if current_args_hash != checkpoint.args_per_step_hash:
        drift["args_per_step"] = {
            "checkpoint": checkpoint.args_per_step_hash,
            "current": current_args_hash,
        }

    return drift or None


# ---- Safe-step predicate ---------------------------------------------------


def is_safe_step_boundary(step_status: str, step_name: SkillName) -> bool:
    """Return True iff a step boundary is "safe" per the spec §4.2 predicate.

    Safe = the step's terminal envelope status is ``ok`` or ``partial``.
    The conservative ``/ship`` rule (the boundary becomes safe only after
    the terminal ok envelope is fully appended) reduces to the same
    condition in the runner — by the time we read ``step_envelope``,
    the envelope IS fully appended.

    ``failed``, ``blocked``, and ``needs_user`` are never safe — those
    statuses already exit the flow per the W03 short-circuit.
    """
    # ``step_name`` reserved for future per-skill conservatism (e.g.
    # marking ``/ship`` unsafe until a ``ship.pr_pushed`` event lands).
    # v0.1 just folds the ``/ship`` rule into the general ok-or-partial
    # predicate.
    del step_name
    return step_status in {"ok", "partial"}


# ---- Checkpoint emission ---------------------------------------------------


def _flow_jsonl_path(state_path: Path) -> Path:
    """Return ``<state>/store/flow.jsonl``."""
    return store_path(state_path, StoreKind.FLOW)


def _new_envelope_id(prefix: str = "EV") -> str:
    """Mint a fresh ``<prefix>-<uuid12>`` envelope id."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _emit_flow_record(
    *,
    state_path: Path,
    scope_id: str,
    flow_id: str,
    goal: str,
    policy: dict[str, Any],
    status: FlowStatus,
    last_safe_checkpoint: str | None,
    next_action: str | None,
) -> str:
    """Append a :class:`FlowPayload` (``flow_record``) envelope.

    Returns the freshly minted envelope id so the caller can fold it
    into ``persisted_store_records``.
    """
    payload = FlowPayload(
        flow_id=flow_id,
        goal=goal,
        policy=policy,
        last_safe_checkpoint=last_safe_checkpoint,
        next_action=next_action,
        status=status,
    )
    envelope_id = _new_envelope_id()
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id=scope_id,
        created_at=datetime.now(UTC),
        updated_at=None,
        summary=f"flow: {flow_id} status={status.value}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(_flow_jsonl_path(state_path), envelope)
    return envelope_id


def _emit_checkpoint(
    *,
    state_path: Path,
    scope_id: str,
    flow_id: str,
    step_index: int,
    step_name: SkillName,
    started_at: datetime,
    completed_at: datetime,
    last_safe: bool,
    payload_hash: str,
    parent_state_hash: str,
    parent_git_head: str | None,
    parent_profile_ids: list[str],
    args_per_step_hash: str,
) -> str:
    """Append a :class:`FlowCheckpointPayload` envelope.

    Returns the freshly minted envelope id (``EV-...``). The append
    routes through :func:`eawf.store.append.append_envelope`, so the
    line is fsynced before this function returns.
    """
    payload = FlowCheckpointPayload(
        flow_id=flow_id,
        step_index=step_index,
        step_name=step_name,
        started_at=started_at,
        completed_at=completed_at,
        last_safe=last_safe,
        payload_hash=payload_hash,
        parent_state_hash=parent_state_hash,
        parent_git_head=parent_git_head,
        parent_profile_ids=parent_profile_ids,
        args_per_step_hash=args_per_step_hash,
    )
    envelope_id = _new_envelope_id()
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id=scope_id,
        created_at=completed_at,
        updated_at=None,
        summary=f"flow: {flow_id} checkpoint step_index={step_index} {step_name}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(_flow_jsonl_path(state_path), envelope)
    return envelope_id


# ---- flow.jsonl readers (read-only) ----------------------------------------


def load_flow_records(state_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Stream-parse ``flow.jsonl`` and return ``(envelope_id, payload)`` pairs.

    Records are returned in append order (oldest first). Malformed lines
    (orjson decode failure or non-flow kind) are skipped with a debug
    log so a partially-corrupted file still surfaces the parseable
    suffix.
    """
    path = _flow_jsonl_path(state_path)
    if not path.exists():
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    raw = path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            decoded = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            logger.debug(f"load_flow_records: skipping malformed line: {exc}")
            continue
        if not isinstance(decoded, dict):
            continue
        envelope_id = decoded.get("id")
        payload = decoded.get("payload")
        if not isinstance(envelope_id, str) or not isinstance(payload, dict):
            continue
        out.append((envelope_id, payload))
    return out


def load_latest_records_per_flow(state_path: Path) -> dict[str, FlowPayload]:
    """Return a ``{flow_id: latest FlowPayload}`` mapping.

    Discriminator-aware: only ``kind == "flow_record"`` lines contribute
    to the mapping. A flow with no ``flow_record`` lines (only
    checkpoints) does not appear in the mapping.
    """
    out: dict[str, FlowPayload] = {}
    for _envelope_id, payload in load_flow_records(state_path):
        if payload.get("kind") != "flow_record":
            continue
        try:
            record = FlowPayload.model_validate(payload)
        except Exception as exc:
            logger.debug(f"load_latest_records_per_flow: validation failed: {exc}")
            continue
        out[record.flow_id] = record
    return out


def load_latest_safe_checkpoint(
    state_path: Path,
    flow_id: str,
) -> tuple[str, FlowCheckpointPayload] | None:
    """Return the latest ``(envelope_id, payload)`` with ``last_safe=True``.

    Returns ``None`` when the flow has no safe checkpoints — the runner
    treats that as "nothing to resume to" and refuses with an
    ``INTEGRITY_VIOLATION`` exit code.
    """
    safe: tuple[str, FlowCheckpointPayload] | None = None
    for envelope_id, payload in load_flow_records(state_path):
        if payload.get("kind") != "flow_checkpoint":
            continue
        if payload.get("flow_id") != flow_id:
            continue
        try:
            ckpt = FlowCheckpointPayload.model_validate(payload)
        except Exception as exc:
            logger.debug(f"load_latest_safe_checkpoint: validation failed: {exc}")
            continue
        if ckpt.last_safe:
            safe = (envelope_id, ckpt)
    return safe


def in_progress_flow_ids(state_path: Path) -> list[str]:
    """Return the ids of flows whose latest record status is ``in_progress``."""
    latest = load_latest_records_per_flow(state_path)
    return [fid for fid, record in latest.items() if record.status == FlowStatus.IN_PROGRESS]


# ---- Skill registration ----------------------------------------------------


@register
class FlowSkill(Skill):
    """Concrete ``/flow`` skill (Phase 4 W03 + Phase 5 W02 resume).

    Runs the six core skills sequentially. Short-circuits on the first
    non-``ok`` envelope status, propagating the failing step's
    ``repair_commands`` to the flow's own footer.

    Phase 5 W02 adds:
    - Per-step ``flow_checkpoint`` envelope appended to ``flow.jsonl``.
    - Optional ``resume_from`` ctx arg replays from a safe checkpoint.
    - ``flow_record`` envelopes (start, terminal) so ``status`` /
      ``--resume`` can locate the active run.
    """

    name: SkillName = "/flow"

    # Canonical step order — exposed so tests can read the sequence
    # without re-importing the module-private tuple.
    flow_order: tuple[tuple[SkillName, type[Skill]], ...] = _CORE_FLOW_ORDER

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)
        topic = args.get("topic")
        stop_after = _resolve_stop_after(args.get("stop_after"))
        args_per_step_raw = args.get("args_per_step") or {}
        if not isinstance(args_per_step_raw, dict):
            args_per_step_raw = {}
        args_per_step: dict[str, Any] = args_per_step_raw

        resume_from_raw = args.get("resume_from")
        resume_from: FlowCheckpointPayload | None = None
        resume_from_id: str | None = None
        if isinstance(resume_from_raw, dict):
            resume_envelope_id = resume_from_raw.get("__envelope_id__")
            ckpt_dict = {k: v for k, v in resume_from_raw.items() if k != "__envelope_id__"}
            try:
                resume_from = FlowCheckpointPayload.model_validate(ckpt_dict)
            except ValidationError as exc:
                raise cli_errors.InvalidInput(
                    f"resume_from checkpoint failed validation: {exc.errors()[0].get('msg', exc)}"
                ) from exc
            if isinstance(resume_envelope_id, str):
                resume_from_id = resume_envelope_id

        flow_id_raw = args.get("flow_id")
        if isinstance(flow_id_raw, str) and flow_id_raw:
            flow_id = flow_id_raw
        else:
            flow_id = f"FL-{uuid.uuid4().hex[:12]}"

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        evidence_refs: list[str] = []

        # Determine the start step index for resume.
        start_index = 0 if resume_from is None else resume_from.step_index + 1
        # Re-emit canonical events so observers can distinguish a fresh
        # run from a resume.
        if resume_from is None:
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="flow.start",
                summary=f"flow: start topic={topic!r} flow_id={flow_id}",
                payload={
                    "topic": topic,
                    "stop_after": stop_after,
                    "step_count": len(self.flow_order),
                    "flow_id": flow_id,
                },
            )
            persisted_records.append(evt_id)
            # Record the in-progress run summary so a kill leaves a trail
            # for ``--resume`` to find.
            policy: dict[str, Any] = {}
            if stop_after is not None:
                policy["stop_after"] = stop_after
            record_id = _emit_flow_record(
                state_path=state_path,
                scope_id=scope_id,
                flow_id=flow_id,
                goal=str(topic) if topic is not None else "",
                policy=policy,
                status=FlowStatus.IN_PROGRESS,
                last_safe_checkpoint=None,
                next_action="eawf flow status",
            )
            persisted_records.append(record_id)
        else:
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="flow.resume_start",
                summary=(
                    f"flow: resume_start flow_id={flow_id} from step_index={resume_from.step_index}"
                ),
                payload={
                    "flow_id": flow_id,
                    "resume_from_step_index": resume_from.step_index,
                    "resume_from_step_name": resume_from.step_name,
                    "resume_from_checkpoint_id": resume_from_id,
                },
            )
            persisted_records.append(evt_id)

        steps: list[dict[str, Any]] = []
        repair_commands: list[str] | None = None
        next_actions: list[str] = ["eawf flow status", "eawf audit"]
        last_safe_checkpoint_id: str | None = resume_from_id if resume_from_id is not None else None

        # Snapshot drift sentinels once at the top of the loop body and
        # re-snapshot before every step so the checkpoint records the
        # values at step START, not at runner start.
        for idx, (skill_name, skill_cls) in enumerate(self.flow_order):
            if idx < start_index:
                # Skipped step (resume prefix). Project a minimal
                # placeholder envelope dict so ``body.steps`` still
                # describes the canonical six-step shape.
                continue

            short = _stop_after_short_name(skill_name)
            # Build a per-step context using explicit-key precedence so an
            # empty-dict args entry isn't silently dropped to the
            # short-name fallback (the ``or`` chain it replaced did so
            # because empty dicts are falsy).
            step_args = _resolve_step_args(args_per_step, skill_name)

            step_ctx = SkillContext(
                scope=ctx.scope,
                session=ctx.session,
                instrument_probe=dict(ctx.instrument_probe),
                args=step_args,
                failure_repair_commands=ctx.failure_repair_commands,
            )

            # Snapshot drift sentinels BEFORE running the step. Hash the
            # per-step args dict (not the whole multi-step mapping) so
            # the checkpoint records which args this step actually
            # consumed; ``compute_drift`` mirrors this on resume.
            workspace_root = _workspace_root_for_state(state_path)
            parent_state_hash = _state_hash(state_path)
            parent_git_head = _current_git_head(workspace_root)
            parent_profile_ids = _current_profile_ids(state_path)
            args_per_step_hash = _canonical_args_per_step_hash(step_args)

            started_at = datetime.now(UTC)

            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="flow.step_start",
                summary=f"flow: step {skill_name} starting",
                payload={"skill": skill_name, "flow_id": flow_id, "step_index": idx},
            )
            persisted_records.append(evt_id)

            step_envelope: OutputEnvelope = run_skill(skill_cls(), step_ctx)
            step_status = step_envelope.header.status
            steps.append(step_envelope.model_dump(mode="json"))
            persisted_records.extend(step_envelope.footer.persisted_store_records)
            state_mutations.extend(step_envelope.footer.state_mutations)
            evidence_refs.extend(step_envelope.footer.evidence_refs)

            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="flow.step_end",
                summary=f"flow: step {skill_name} -> {step_status}",
                payload={
                    "skill": skill_name,
                    "status": step_status,
                    "flow_id": flow_id,
                    "step_index": idx,
                },
            )
            persisted_records.append(evt_id)

            # Append the per-step checkpoint.
            completed_at = datetime.now(UTC)
            last_safe = is_safe_step_boundary(step_status, skill_name)
            checkpoint_id = _emit_checkpoint(
                state_path=state_path,
                scope_id=scope_id,
                flow_id=flow_id,
                step_index=idx,
                step_name=skill_name,
                started_at=started_at,
                completed_at=completed_at,
                last_safe=last_safe,
                payload_hash=_payload_hash(step_envelope.body),
                parent_state_hash=parent_state_hash,
                parent_git_head=parent_git_head,
                parent_profile_ids=parent_profile_ids,
                args_per_step_hash=args_per_step_hash,
            )
            persisted_records.append(checkpoint_id)
            if last_safe:
                last_safe_checkpoint_id = checkpoint_id

            # Short-circuit on first non-ok status.
            if step_status != "ok":
                repair_commands = list(step_envelope.footer.repair_commands or [])
                evt_id = emit_event(
                    state_path=state_path,
                    scope_id=scope_id,
                    event_type="flow.short_circuit",
                    summary=f"flow: short-circuit on {skill_name} ({step_status})",
                    payload={
                        "skill": skill_name,
                        "status": step_status,
                        "flow_id": flow_id,
                    },
                )
                persisted_records.append(evt_id)
                break

            # ``stop_after`` honours the §14 flag.
            if stop_after is not None and stop_after == short:
                evt_id = emit_event(
                    state_path=state_path,
                    scope_id=scope_id,
                    event_type="flow.stop_after",
                    summary=f"flow: stop-after {short}",
                    payload={"stop_after": stop_after, "flow_id": flow_id},
                )
                persisted_records.append(evt_id)
                break

        terminal_status = short_circuit_terminal_status([s["header"]["status"] for s in steps])

        # Map terminal flow status onto a FlowStatus enum value for the
        # final flow_record summary.
        if terminal_status in {"ok", "partial"}:
            terminal_flow_status = FlowStatus.DONE
        elif terminal_status == "blocked":
            terminal_flow_status = FlowStatus.BLOCKED
        elif terminal_status == "needs_user":
            terminal_flow_status = FlowStatus.PAUSED
        else:
            # ``failed`` and any unrecognised status fall through to the
            # FlowStatus.BLOCKED summary so the operator still sees a
            # non-success terminal record.
            terminal_flow_status = FlowStatus.BLOCKED

        evt_type = "flow.resume_end" if resume_from is not None else "flow.end"
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type=evt_type,
            summary=f"flow: end terminal_status={terminal_status} flow_id={flow_id}",
            payload={
                "terminal_status": terminal_status,
                "steps_run": len(steps),
                "flow_id": flow_id,
            },
        )
        persisted_records.append(evt_id)

        terminal_record_id = _emit_flow_record(
            state_path=state_path,
            scope_id=scope_id,
            flow_id=flow_id,
            goal=str(topic) if topic is not None else "",
            policy={"stop_after": stop_after} if stop_after is not None else {},
            status=terminal_flow_status,
            last_safe_checkpoint=last_safe_checkpoint_id,
            next_action=None,
        )
        persisted_records.append(terminal_record_id)

        body = FlowBody(
            topic=str(topic) if topic is not None else None,
            steps=steps,
            terminal_status=terminal_status,
            user_question=None,
            resume_from_checkpoint_id=resume_from_id,
            drift=None,
        )

        return SkillResult(
            status=terminal_status,  # type: ignore[arg-type]
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            state_mutations=state_mutations,
            evidence_refs=evidence_refs,
            next_valid_actions=next_actions,
            repair_commands=repair_commands,
        )


__all__ = [
    "FlowSkill",
    "compute_drift",
    "in_progress_flow_ids",
    "is_safe_step_boundary",
    "load_flow_records",
    "load_latest_records_per_flow",
    "load_latest_safe_checkpoint",
    "short_circuit_terminal_status",
]
