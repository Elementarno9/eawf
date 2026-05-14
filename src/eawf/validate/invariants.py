"""Cross-entity invariants for eawf state.

Each invariant is a pure function ``State -> Iterable[Violation]``. Schema-level
checks (``extra="forbid"``, type/enum/regex) live on the Pydantic models in
:mod:`eawf.state.models`; this module covers the rules from
``docs/architecture/state-model.md`` that span multiple
entities.

Codes follow the ``INV.<CATEGORY>.<SPECIFIC>`` convention; they are part of
the CLI/test contract and must not be renamed without updating fixtures.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from eawf.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    BacklogStatus,
    GoalStatus,
    IncidentStatus,
    IterStatus,
    OutcomeStatus,
    PhaseStatus,
    WaveStatus,
)
from eawf.state.ids import parents_of
from eawf.state.models import State
from eawf.state.urn import parse as parse_urn
from eawf.store.envelope import Envelope
from eawf.store.kinds.agent_report import (
    AgentReportPayload,
    AuditorReportBody,
    ExecutorReportBody,
    OperatorReportBody,
    ReviewerReportBody,
    store_kind_for_role,
)

logger = logging.getLogger(__name__)

_OPEN_PHASE_STATUSES: frozenset[str] = frozenset(
    {PhaseStatus.PLANNED.value, PhaseStatus.ACTIVE.value}
)
_OPEN_ITER_STATUSES: frozenset[str] = frozenset({IterStatus.PLANNED.value, IterStatus.ACTIVE.value})
_ACTIVE_WAVE_STATUSES: frozenset[str] = frozenset(
    {WaveStatus.CLAIMED.value, WaveStatus.IN_PROGRESS.value}
)
_OPEN_WAVE_STATUSES: frozenset[str] = frozenset(
    {WaveStatus.PENDING.value, WaveStatus.CLAIMED.value, WaveStatus.IN_PROGRESS.value}
)
_TERMINAL_PHASE_STATUSES: frozenset[str] = frozenset(
    {PhaseStatus.CLOSED.value, PhaseStatus.ARCHIVED.value}
)
_TERMINAL_ITER_STATUSES: frozenset[str] = frozenset(
    {IterStatus.CLOSED.value, IterStatus.ABANDONED.value}
)
_TERMINAL_WAVE_STATUSES: frozenset[str] = frozenset(
    {WaveStatus.CLOSED.value, WaveStatus.FAILED.value, WaveStatus.ABANDONED.value}
)
_TERMINAL_GOAL_STATUSES: frozenset[str] = frozenset(
    {GoalStatus.ACHIEVED.value, GoalStatus.ABANDONED.value}
)
_TERMINAL_BACKLOG_STATUSES: frozenset[str] = frozenset({BacklogStatus.CLOSED.value})
_TERMINAL_INCIDENT_STATUSES: frozenset[str] = frozenset(
    {IncidentStatus.RESOLVED.value, IncidentStatus.WONT_FIX.value}
)
_TERMINAL_SESSION_STATUSES: frozenset[str] = frozenset(
    {
        AgentSessionStatus.CLOSED.value,
        AgentSessionStatus.STALE.value,
        AgentSessionStatus.FAILED.value,
    }
)


@dataclass(frozen=True)
class Violation:
    """A single invariant violation."""

    code: str
    path: str
    message: str


Invariant = Callable[[State], Iterable[Violation]]


def check_parent_ids(state: State) -> Iterable[Violation]:
    """Iter/wave parentage rules (``INV.PARENT.*``).

    - ``iters[*].phase_id`` must exist in ``phases``.
    - ``waves[*].iter_id`` must exist in ``iters``.
    - The encoded parents from the entity ID must match the recorded
      ``phase_id`` / ``iter_id`` fields.
    """
    for iter_id, it in state.iters.items():
        # Encoded phase parent must match recorded phase_id.
        encoded = parents_of(it.id)
        encoded_phase = encoded[0] if encoded else None
        if encoded_phase is not None and encoded_phase != it.phase_id:
            yield Violation(
                code="INV.PARENT.ITER_ID_MISMATCH",
                path=f"/iters/{iter_id}/phase_id",
                message=(
                    f"iter {it.id!r} encodes phase {encoded_phase!r} "
                    f"but phase_id is {it.phase_id!r}"
                ),
            )
        if it.phase_id not in state.phases:
            yield Violation(
                code="INV.PARENT.ITER_PHASE_MISSING",
                path=f"/iters/{iter_id}/phase_id",
                message=(f"iter {it.id!r} references missing phase {it.phase_id!r}"),
            )

    for wave_id, w in state.waves.items():
        encoded = parents_of(w.id)
        encoded_iter = encoded[1] if len(encoded) >= 2 else None
        if encoded_iter is not None and encoded_iter != w.iter_id:
            yield Violation(
                code="INV.PARENT.WAVE_ID_MISMATCH",
                path=f"/waves/{wave_id}/iter_id",
                message=(
                    f"wave {w.id!r} encodes iter {encoded_iter!r} but iter_id is {w.iter_id!r}"
                ),
            )
        if w.iter_id not in state.iters:
            yield Violation(
                code="INV.PARENT.WAVE_ITER_MISSING",
                path=f"/waves/{wave_id}/iter_id",
                message=(f"wave {w.id!r} references missing iter {w.iter_id!r}"),
            )


def check_current_pointers(state: State) -> Iterable[Violation]:
    """``current.*`` pointers must reference open lifecycle entries.

    - ``current.phase_id`` (if non-null) must name a phase whose status is
      ``planned`` or ``active``.
    - ``current.iter_id`` (if non-null) must name an iter whose status is
      ``planned`` or ``active``.
    - ``current.active_wave_ids`` must reference waves whose status is
      ``claimed`` or ``in_progress``.
    """
    cp = state.current

    if cp.phase_id is not None:
        phase = state.phases.get(cp.phase_id)
        if phase is None:
            yield Violation(
                code="INV.CURRENT.PHASE_MISSING",
                path="/current/phase_id",
                message=f"current.phase_id {cp.phase_id!r} not in phases",
            )
        elif phase.status not in _OPEN_PHASE_STATUSES:
            yield Violation(
                code="INV.CURRENT.PHASE_NOT_OPEN",
                path="/current/phase_id",
                message=(
                    f"current.phase_id {cp.phase_id!r} has status "
                    f"{phase.status.value!r}; expected planned or active"
                ),
            )

    if cp.iter_id is not None:
        it = state.iters.get(cp.iter_id)
        if it is None:
            yield Violation(
                code="INV.CURRENT.ITER_MISSING",
                path="/current/iter_id",
                message=f"current.iter_id {cp.iter_id!r} not in iters",
            )
        elif it.status not in _OPEN_ITER_STATUSES:
            yield Violation(
                code="INV.CURRENT.ITER_NOT_OPEN",
                path="/current/iter_id",
                message=(
                    f"current.iter_id {cp.iter_id!r} has status "
                    f"{it.status.value!r}; expected planned or active"
                ),
            )

    if cp.phase_id is not None and cp.iter_id is not None and cp.iter_id in state.iters:
        iter_phase = state.iters[cp.iter_id].phase_id
        if iter_phase != cp.phase_id:
            yield Violation(
                code="INV.CURRENT.ITER_PHASE_MISMATCH",
                path="/current/iter_id",
                message=(
                    f"current.iter_id {cp.iter_id!r} belongs to phase "
                    f"{iter_phase!r}, not current.phase_id {cp.phase_id!r}"
                ),
            )

    for wave_id in cp.active_wave_ids:
        w = state.waves.get(wave_id)
        if w is None:
            yield Violation(
                code="INV.CURRENT.WAVE_MISSING",
                path="/current/active_wave_ids",
                message=f"active wave {wave_id!r} not in waves",
            )
        elif w.status not in _ACTIVE_WAVE_STATUSES:
            yield Violation(
                code="INV.CURRENT.WAVE_NOT_ACTIVE",
                path="/current/active_wave_ids",
                message=(
                    f"active wave {wave_id!r} has status "
                    f"{w.status.value!r}; expected claimed or in_progress"
                ),
            )


def check_closure_rules(state: State) -> Iterable[Violation]:
    """Closed parents must not have open children (``INV.CLOSURE.*``).

    - A ``closed`` phase must not have iter children whose status is in
      ``{planned, active}``.
    - A ``closed`` iter must not have wave children whose status is in
      ``{pending, claimed, in_progress}``.
    """
    for phase_id, phase in state.phases.items():
        if phase.status != PhaseStatus.CLOSED.value:
            continue
        for iter_id, it in state.iters.items():
            if it.phase_id != phase_id:
                continue
            if it.status in _OPEN_ITER_STATUSES:
                yield Violation(
                    code="INV.CLOSURE.PHASE_HAS_OPEN_ITER",
                    path=f"/phases/{phase_id}",
                    message=(
                        f"closed phase {phase_id!r} has open iter "
                        f"{iter_id!r} (status {it.status.value!r})"
                    ),
                )

    for iter_id, it in state.iters.items():
        if it.status != IterStatus.CLOSED.value:
            continue
        for wave_id, w in state.waves.items():
            if w.iter_id != iter_id:
                continue
            if w.status in _OPEN_WAVE_STATUSES:
                yield Violation(
                    code="INV.CLOSURE.ITER_HAS_OPEN_WAVE",
                    path=f"/iters/{iter_id}",
                    message=(
                        f"closed iter {iter_id!r} has open wave "
                        f"{wave_id!r} (status {w.status.value!r})"
                    ),
                )


def check_audit_evidence(state: State) -> Iterable[Violation]:
    """Outcomes/hypotheses with verdicts require an ``audit_id`` (``INV.AUDIT.*``).

    - Outcomes whose status is ``met`` or ``missed`` must have ``audit_id``
      non-null.
    - Hypotheses whose verdict is non-null (i.e. resolved) must have
      ``audit_id`` non-null.
    """
    if state.outcomes is not None:
        for oid, outcome in state.outcomes.items():
            if outcome.status not in {
                OutcomeStatus.MET.value,
                OutcomeStatus.MISSED.value,
            }:
                continue
            if outcome.audit_id is None:
                yield Violation(
                    code="INV.AUDIT.OUTCOME_MISSING_AUDIT",
                    path=f"/outcomes/{oid}/audit_id",
                    message=(
                        f"outcome {oid!r} has status {outcome.status.value!r} but no audit_id"
                    ),
                )

    if state.hypotheses is not None:
        for hid, hyp in state.hypotheses.items():
            if hyp.verdict is None:
                continue
            # Verdict is non-null (one of confirmed/rejected/inconclusive).
            if hyp.audit_id is None:
                yield Violation(
                    code="INV.AUDIT.HYPOTHESIS_MISSING_AUDIT",
                    path=f"/hypotheses/{hid}/audit_id",
                    message=(
                        f"hypothesis {hid!r} has verdict {hyp.verdict.value!r} but no audit_id"
                    ),
                )


def check_mcp_plugin_owners(state: State) -> Iterable[Violation]:
    """All ``mcp_servers`` entries must declare ``owner == "eawf"``.

    Phase 1 enforces a single managed-owner policy: any non-``"eawf"`` owner is
    flagged as ``INV.OWNER.MCP_NON_EAWF``. The :class:`McpServer` model already
    requires an ``owner`` field, so no presence check is needed.
    """
    if state.mcp_servers is None:
        return
    for mid, server in state.mcp_servers.items():
        if server.owner != "eawf":
            yield Violation(
                code="INV.OWNER.MCP_NON_EAWF",
                path=f"/mcp_servers/{mid}/owner",
                message=(f"mcp_servers[{mid!r}].owner is {server.owner!r}; expected 'eawf'"),
            )


def check_mcp_grant_server_ref(state: State) -> Iterable[Violation]:
    """Every ``mcp_grants`` row must reference a known MCP server.

    Each :class:`~eawf.state.models.McpGrant` carries a ``server_id`` whose
    value must appear as a key in :attr:`State.mcp_servers`; otherwise the
    dispatcher's allowed-tools projection (P10 W03/W04) would emit a
    ``mcp__<unknown>__*`` glob the runtime can never match. A dangling
    reference is flagged as ``INV.REF.MCP_GRANT_SERVER_MISSING`` against the
    ``server_id`` field so callers can pinpoint the offending grant.
    """
    if state.mcp_grants is None:
        return
    servers = state.mcp_servers or {}
    for gid, grant in state.mcp_grants.items():
        if grant.server_id not in servers:
            yield Violation(
                code="INV.REF.MCP_GRANT_SERVER_MISSING",
                path=f"/mcp_grants/{gid}/server_id",
                message=(f"mcp_grants[{gid!r}].server_id {grant.server_id!r} not in mcp_servers"),
            )


def check_sandbox_policy_scope_ref(state: State) -> Iterable[Violation]:
    """Every ``sandbox_policies`` row must reference an existing scope.

    A :class:`~eawf.sandbox.policy.SandboxPolicy` with ``scope_kind="wave"``
    must reference a wave id present in :attr:`State.waves`; the
    ``"profile"`` and ``"global"`` shapes are free-form strings in v0.2
    (profile composition is config-side; ``"global"`` is the literal scope).
    Dangling wave references are flagged as
    ``INV.REF.SANDBOX_POLICY_SCOPE_MISSING``.
    """
    if state.sandbox_policies is None:
        return
    waves = state.waves or {}
    for pid, policy in state.sandbox_policies.items():
        if policy.scope_kind != "wave":
            continue
        if policy.scope_id not in waves:
            yield Violation(
                code="INV.REF.SANDBOX_POLICY_SCOPE_MISSING",
                path=f"/sandbox_policies/{pid}/scope_id",
                message=(
                    f"sandbox_policies[{pid!r}].scope_id {policy.scope_id!r} "
                    f"not in waves (scope_kind=wave)"
                ),
            )


def check_plugin_runtimes(state: State) -> Iterable[Violation]:
    """All ``plugins`` entries must declare ``runtime == "claude"`` in v0.1.

    The ``runtime`` field is a free string at the schema layer because future
    profiles may add more runtimes; for v0.1 the spec restricts it to
    ``"claude"`` and any other value is flagged as
    ``INV.OWNER.PLUGIN_NON_CLAUDE``.
    """
    if state.plugins is None:
        return
    for pid, plugin in state.plugins.items():
        if plugin.runtime != "claude":
            yield Violation(
                code="INV.OWNER.PLUGIN_NON_CLAUDE",
                path=f"/plugins/{pid}/runtime",
                message=(f"plugins[{pid!r}].runtime is {plugin.runtime!r}; expected 'claude'"),
            )


def check_scope_consistency(state: State) -> Iterable[Violation]:
    """``scope_kind`` must match the presence of ``project`` / ``workspace``.

    - ``scope_kind == "repo"`` requires ``state.project`` to be non-null.
    - ``scope_kind == "workspace"`` requires ``state.workspace`` to be non-null
      AND must NOT embed ``state.project`` (workspace states reference repos
      via ``WorkspaceIndex``, not by embedding a project).
    """
    sk = state.scope_kind.value if hasattr(state.scope_kind, "value") else str(state.scope_kind)
    if sk == "repo":
        if state.project is None:
            yield Violation(
                code="INV.SCOPE.REPO_REQUIRES_PROJECT",
                path="/project",
                message="scope_kind 'repo' requires project to be non-null",
            )
    elif sk == "workspace":
        if state.workspace is None:
            yield Violation(
                code="INV.SCOPE.WORKSPACE_REQUIRES_INDEX",
                path="/workspace",
                message="scope_kind 'workspace' requires workspace index to be non-null",
            )
        if state.project is not None:
            yield Violation(
                code="INV.SCOPE.WORKSPACE_NO_PROJECT",
                path="/project",
                message="scope_kind 'workspace' must not embed project",
            )


def check_plugin_owners(state: State) -> Iterable[Violation]:
    """All ``plugins`` entries must declare ``owner == "eawf"`` (Phase 1)."""
    if state.plugins is None:
        return
    for pid, plugin in state.plugins.items():
        if plugin.owner != "eawf":
            yield Violation(
                code="INV.OWNER.PLUGIN_NON_EAWF",
                path=f"/plugins/{pid}/owner",
                message=(f"plugins[{pid!r}].owner is {plugin.owner!r}; expected 'eawf'"),
            )


def check_wave_blocks_invariant(state: State) -> Iterable[Violation]:
    """Wave ``deps`` / ``blocks`` must be mutually consistent (``INV.GRAPH.*``).

    The ``Wave.blocks`` field is the reverse index of ``Wave.deps``: if wave
    *X* lists *Y* in ``X.deps``, then *Y* must list *X* in ``Y.blocks`` (and
    vice versa). When a referenced peer is absent from ``state.waves`` the
    pair is silently skipped — that failure is reported by
    :func:`check_parent_ids` already. Repair drift with
    ``eawf wave blocks-rebuild``.
    """
    waves = state.waves
    for wid, w in waves.items():
        for dep_id in w.deps:
            peer = waves.get(dep_id)
            if peer is None:
                continue
            if wid not in peer.blocks:
                yield Violation(
                    code="INV.GRAPH.BLOCKS_MISSING_REVERSE",
                    path=f"/waves/{dep_id}/blocks",
                    message=(
                        f"wave {wid!r} declares dep {dep_id!r} but "
                        f"{dep_id!r}.blocks is missing {wid!r}"
                    ),
                )
        for block_id in w.blocks:
            peer = waves.get(block_id)
            if peer is None:
                continue
            if wid not in peer.deps:
                yield Violation(
                    code="INV.GRAPH.DEPS_MISSING_REVERSE",
                    path=f"/waves/{block_id}/deps",
                    message=(
                        f"wave {wid!r} declares block {block_id!r} but "
                        f"{block_id!r}.deps is missing {wid!r}"
                    ),
                )


def check_artifact_urns(state: State) -> Iterable[Violation]:
    """Artifact URNs must be canonical artifact URNs."""
    for artifact_id, artifact in state.artifacts.items():
        try:
            parsed = parse_urn(artifact.urn)
        except ValueError as exc:
            yield Violation(
                code="INV.URN.ARTIFACT_INVALID",
                path=f"/artifacts/{artifact_id}/urn",
                message=str(exc),
            )
            continue
        if parsed.kind != "artifact" or parsed.id != artifact.id:
            yield Violation(
                code="INV.URN.ARTIFACT_MISMATCH",
                path=f"/artifacts/{artifact_id}/urn",
                message=(f"artifact {artifact_id!r} urn targets {parsed.kind!r}/{parsed.id!r}"),
            )


def _report_path(report_id: str, field: str) -> str:
    return f"/agent_reports/{report_id}/{field}"


def _scope_exists(state: State, scope_id: str) -> bool:
    if scope_id in state.phases or scope_id in state.iters or scope_id in state.waves:
        return True
    return state.project is not None and scope_id == state.project.code


def _wave_phase_id(state: State, wave_id: str) -> str | None:
    wave = state.waves.get(wave_id)
    if wave is None:
        return None
    iter_record = state.iters.get(wave.iter_id)
    if iter_record is None:
        return None
    return iter_record.phase_id


def check_agent_report_invariants(state: State, reports: Iterable[Envelope]) -> Iterable[Violation]:
    """Validate typed agent-report store envelopes against state context.

    This helper is separate from :data:`ALL_INVARIANTS` because report rows
    live in JSONL stores, not inside ``state.json``.
    """
    attempts: dict[tuple[AgentSessionRole, str], list[tuple[int, str]]] = {}
    for envelope in reports:
        payload = AgentReportPayload.model_validate(envelope.payload)
        header = payload.header
        body = payload.body
        expected_kind = store_kind_for_role(header.role)

        if envelope.kind != expected_kind:
            yield Violation(
                code="INV.AGENT_REPORT.STORE_KIND_MISMATCH",
                path=_report_path(header.report_id, "kind"),
                message=(
                    f"agent report {header.report_id!r} has store kind "
                    f"{envelope.kind.value!r}; expected {expected_kind.value!r}"
                ),
            )
        if envelope.scope_id != header.scope_id:
            yield Violation(
                code="INV.AGENT_REPORT.ENVELOPE_SCOPE_MISMATCH",
                path=_report_path(header.report_id, "scope_id"),
                message=(
                    f"agent report {header.report_id!r} envelope scope "
                    f"{envelope.scope_id!r} does not match header scope {header.scope_id!r}"
                ),
            )

        session = state.agent_sessions.get(header.session_id)
        if session is None:
            yield Violation(
                code="INV.AGENT_REPORT.SESSION_MISSING",
                path=_report_path(header.report_id, "header/session_id"),
                message=(
                    f"agent report {header.report_id!r} references missing "
                    f"session {header.session_id!r}"
                ),
            )
        else:
            if session.role != header.role:
                yield Violation(
                    code="INV.AGENT_REPORT.SESSION_ROLE_MISMATCH",
                    path=_report_path(header.report_id, "header/session_id"),
                    message=(
                        f"agent report {header.report_id!r} role {header.role.value!r} "
                        f"does not match session {header.session_id!r} role "
                        f"{session.role.value!r}"
                    ),
                )
            if session.scope_id != header.scope_id:
                yield Violation(
                    code="INV.AGENT_REPORT.SESSION_SCOPE_MISMATCH",
                    path=_report_path(header.report_id, "header/scope_id"),
                    message=(
                        f"agent report {header.report_id!r} scope {header.scope_id!r} "
                        f"does not match session {header.session_id!r} scope "
                        f"{session.scope_id!r}"
                    ),
                )

        if not _scope_exists(state, header.scope_id):
            yield Violation(
                code="INV.AGENT_REPORT.SCOPE_MISSING",
                path=_report_path(header.report_id, "header/scope_id"),
                message=(
                    f"agent report {header.report_id!r} references missing "
                    f"scope {header.scope_id!r}"
                ),
            )

        attempts.setdefault((header.role, header.base_id), []).append(
            (header.attempt, header.report_id)
        )

        if isinstance(body, ExecutorReportBody):
            if body.commit_sha is None:
                yield Violation(
                    code="INV.AGENT_REPORT.EXECUTOR_COMMIT_MISSING",
                    path=_report_path(header.report_id, "body/commit_sha"),
                    message=f"executor report {header.report_id!r} has no commit_sha",
                )
            elif body.wave_id not in state.waves:
                yield Violation(
                    code="INV.AGENT_REPORT.EXECUTOR_WAVE_MISSING",
                    path=_report_path(header.report_id, "body/wave_id"),
                    message=(
                        f"executor report {header.report_id!r} references "
                        f"missing wave {body.wave_id!r}"
                    ),
                )

        if isinstance(body, ReviewerReportBody) and not body.coverage_refs:
            yield Violation(
                code="INV.AGENT_REPORT.REVIEWER_COVERAGE_MISSING",
                path=_report_path(header.report_id, "body/coverage_refs"),
                message=f"reviewer report {header.report_id!r} has no coverage_refs",
            )

        if isinstance(body, AuditorReportBody) and not body.criteria:
            yield Violation(
                code="INV.AGENT_REPORT.AUDITOR_CRITERIA_MISSING",
                path=_report_path(header.report_id, "body/criteria"),
                message=f"auditor report {header.report_id!r} has no criteria",
            )

        if isinstance(body, OperatorReportBody):
            if body.phase_id not in state.phases:
                yield Violation(
                    code="INV.AGENT_REPORT.OPERATOR_PHASE_MISSING",
                    path=_report_path(header.report_id, "body/phase_id"),
                    message=(
                        f"operator report {header.report_id!r} references "
                        f"missing phase {body.phase_id!r}"
                    ),
                )
            for wave_id in body.completed_wave_ids:
                wave_phase_id = _wave_phase_id(state, wave_id)
                if wave_phase_id is None:
                    yield Violation(
                        code="INV.AGENT_REPORT.OPERATOR_WAVE_MISSING",
                        path=_report_path(header.report_id, "body/completed_wave_ids"),
                        message=(
                            f"operator report {header.report_id!r} references "
                            f"missing completed wave {wave_id!r}"
                        ),
                    )
                elif wave_phase_id != body.phase_id:
                    yield Violation(
                        code="INV.AGENT_REPORT.OPERATOR_WAVE_PHASE_MISMATCH",
                        path=_report_path(header.report_id, "body/completed_wave_ids"),
                        message=(
                            f"operator report {header.report_id!r} lists wave "
                            f"{wave_id!r} from phase {wave_phase_id!r}, not "
                            f"{body.phase_id!r}"
                        ),
                    )

    for (role, base_id), rows in attempts.items():
        seen: set[int] = set()
        duplicates: set[int] = set()
        for attempt, _report_id in rows:
            if attempt in seen:
                duplicates.add(attempt)
            seen.add(attempt)
        if duplicates:
            report_ids = [report_id for attempt, report_id in rows if attempt in duplicates]
            yield Violation(
                code="INV.AGENT_REPORT.ATTEMPT_DUPLICATE",
                path="/agent_reports",
                message=(
                    f"agent report attempts for role={role.value!r} "
                    f"base_id={base_id!r} duplicate attempts {sorted(duplicates)!r} "
                    f"in reports {report_ids!r}"
                ),
            )
            continue
        expected = list(range(1, len(rows) + 1))
        actual = sorted(seen)
        if actual != expected:
            report_ids = [report_id for _attempt, report_id in rows]
            yield Violation(
                code="INV.AGENT_REPORT.ATTEMPT_GAP",
                path="/agent_reports",
                message=(
                    f"agent report attempts for role={role.value!r} base_id={base_id!r} "
                    f"are {actual!r}; expected {expected!r} in reports {report_ids!r}"
                ),
            )


def check_closure_timestamps(state: State) -> Iterable[Violation]:
    """Terminal-status entries must carry their closure timestamp (``INV.CLOSURE.*_NO_TIMESTAMP``).

    - ``Phase`` in ``{closed, archived}`` requires ``closed_at`` non-null.
    - ``Iter`` in ``{closed, abandoned}`` requires ``closed_at`` non-null.
    - ``Wave`` in ``{closed, failed, abandoned}`` requires ``closed_at`` non-null.
    - ``Goal`` in ``{achieved, abandoned}`` requires ``closed_at`` non-null.
    - ``BacklogItem`` in ``{closed}`` requires ``closed_at`` non-null.
    - ``Incident`` in ``{resolved, wont-fix}`` requires ``closed_at`` non-null.
    - ``AgentSession`` in ``{closed, stale, failed}`` requires ``ended_at`` non-null.
    """
    for phase_id, phase in state.phases.items():
        if phase.status in _TERMINAL_PHASE_STATUSES and phase.closed_at is None:
            yield Violation(
                code="INV.CLOSURE.PHASE_NO_CLOSED_AT",
                path=f"/phases/{phase_id}/closed_at",
                message=(
                    f"phase {phase_id!r} has terminal status "
                    f"{phase.status.value!r} but closed_at is null"
                ),
            )

    for iter_id, it in state.iters.items():
        if it.status in _TERMINAL_ITER_STATUSES and it.closed_at is None:
            yield Violation(
                code="INV.CLOSURE.ITER_NO_CLOSED_AT",
                path=f"/iters/{iter_id}/closed_at",
                message=(
                    f"iter {iter_id!r} has terminal status "
                    f"{it.status.value!r} but closed_at is null"
                ),
            )

    for wave_id, w in state.waves.items():
        if w.status in _TERMINAL_WAVE_STATUSES and w.closed_at is None:
            yield Violation(
                code="INV.CLOSURE.WAVE_NO_CLOSED_AT",
                path=f"/waves/{wave_id}/closed_at",
                message=(
                    f"wave {wave_id!r} has terminal status {w.status.value!r} but closed_at is null"
                ),
            )

    if state.goals is not None:
        for goal_id, goal in state.goals.items():
            if goal.status in _TERMINAL_GOAL_STATUSES and goal.closed_at is None:
                yield Violation(
                    code="INV.CLOSURE.GOAL_NO_CLOSED_AT",
                    path=f"/goals/{goal_id}/closed_at",
                    message=(
                        f"goal {goal_id!r} has terminal status "
                        f"{goal.status.value!r} but closed_at is null"
                    ),
                )

    if state.backlog is not None:
        for bid, item in state.backlog.items():
            if item.status in _TERMINAL_BACKLOG_STATUSES and item.closed_at is None:
                yield Violation(
                    code="INV.CLOSURE.BACKLOG_NO_CLOSED_AT",
                    path=f"/backlog/{bid}/closed_at",
                    message=(
                        f"backlog item {bid!r} has terminal status "
                        f"{item.status.value!r} but closed_at is null"
                    ),
                )

    if state.incidents is not None:
        for iid, inc in state.incidents.items():
            if inc.status in _TERMINAL_INCIDENT_STATUSES and inc.closed_at is None:
                yield Violation(
                    code="INV.CLOSURE.INCIDENT_NO_CLOSED_AT",
                    path=f"/incidents/{iid}/closed_at",
                    message=(
                        f"incident {iid!r} has terminal status "
                        f"{inc.status.value!r} but closed_at is null"
                    ),
                )

    for sid, session in state.agent_sessions.items():
        if session.status in _TERMINAL_SESSION_STATUSES and session.ended_at is None:
            yield Violation(
                code="INV.CLOSURE.SESSION_NO_ENDED_AT",
                path=f"/agent_sessions/{sid}/ended_at",
                message=(
                    f"agent session {sid!r} has terminal status "
                    f"{session.status.value!r} but ended_at is null"
                ),
            )


ALL_INVARIANTS: tuple[Invariant, ...] = (
    check_parent_ids,
    check_current_pointers,
    check_closure_rules,
    check_closure_timestamps,
    check_audit_evidence,
    check_mcp_plugin_owners,
    check_mcp_grant_server_ref,
    check_sandbox_policy_scope_ref,
    check_plugin_runtimes,
    check_scope_consistency,
    check_plugin_owners,
    check_wave_blocks_invariant,
    check_artifact_urns,
)
