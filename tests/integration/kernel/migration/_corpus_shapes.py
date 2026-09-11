"""The eight structural corpora the v0.7 rehearsal runs over.

Each entry answers one question about the cutover that the pinned
``epoch1-full`` corpus cannot answer on its own, because that corpus is a
single mid-life shape: it has open work and closed work, one project and
one workspace, and every reference it carries resolves. The shapes here
are the edges around it -- nothing at all, one live wave, nothing live,
a close that stopped half way, attention still open, more than one root,
a reference that names nothing, and a key nobody declared.

Six of the eight are expected to import. Two are expected to refuse, and
they refuse in different layers on purpose: an undeclared top-level key
is caught by the census before any row is read, while a reference that
names nothing survives the census and is caught by the validator, after
the whole corpus has been staged. A fixture set that only covered one of
those would leave the other layer unexercised.
"""

from __future__ import annotations

import logging
from typing import Final

from tests.integration.kernel.migration._corpus_builders import (
    ActualRow,
    ArtifactRow,
    AuditRow,
    BacklogRow,
    CloseAttemptRow,
    CorpusPlan,
    CurrentBlock,
    DecisionRow,
    DependencyBindingRow,
    EstimateRow,
    GoalRow,
    IncidentRow,
    IterRow,
    MemoryRow,
    PhaseRow,
    ProjectBlock,
    RegistryDocument,
    RegistryWorkspace,
    SandboxPolicyRow,
    SessionRow,
    WaveIntegrationRow,
    WaveRow,
    WorkspaceBlock,
    WorktreeRow,
)

logger = logging.getLogger(__name__)


#: The addressing slots every rehearsal plan is sealed under. Epoch 1
#: recorded only the project code, so the workspace and repository keys
#: are supplied by the cutover and have to agree with the registry each
#: corpus ships.
WORKSPACE_KEY: Final = "WSP-DEFAULT"
PROJECT_KEY: Final = "PRJ-DEMO"
REPOSITORY_KEY: Final = "REP-DEMO"

#: The second workspace only the multi-root corpus registers.
SECOND_WORKSPACE_KEY: Final = "WSP-SATELLITE"

#: The project code the built corpora carry in their own document. It is
#: the epoch-1 code, which is a different namespace from the epoch-2
#: project key above.
PROJECT_CODE: Final = "DEMO"

#: The iter the corrupt-reference corpus names and does not hold. The
#: value is shaped like a real iter id so the failure is a resolution
#: failure rather than a schema failure.
MISSING_ITER_ID: Final = "P09-I99"

#: The undeclared top-level key the unknown-extension-field corpus
#: carries. A reader of the refusal has to see this name, which is what
#: the negative test asserts on.
UNKNOWN_EXTENSION_FIELD: Final = "experimental_capabilities"

#: The session that was closing a wave when the close was interrupted.
INTERRUPTED_CLOSE_SESSION: Final = "SES-INTERRUPTED"

#: The close attempt that never reached a terminal state.
INTERRUPTED_CLOSE_ATTEMPT: Final = "CA-INTERRUPTED"


def default_registry() -> RegistryDocument:
    """Return the single-workspace registry every corpus resolves against.

    Returns:
        A registry holding only :data:`WORKSPACE_KEY`, which is the
        addressing workspace every rehearsal plan is sealed under. The
        frozen corpora ship this same registry, so a workspace check that
        started refusing would refuse for every fixture at once rather
        than for the built ones only.
    """
    return RegistryDocument(
        workspaces=(
            RegistryWorkspace(
                key=WORKSPACE_KEY,
                title="the workspace the rehearsal corpora mint under",
                home_project_code=PROJECT_KEY,
                member_project_codes=(PROJECT_KEY,),
            ),
        )
    )


def _registry(*workspaces: RegistryWorkspace) -> RegistryDocument:
    """Return a registry holding ``workspaces``, defaulting to the single one."""
    if workspaces:
        return RegistryDocument(workspaces=workspaces)
    return default_registry()


def _project() -> ProjectBlock:
    """Return the project block every built corpus carries."""
    return ProjectBlock(code=PROJECT_CODE, title="the rehearsal demo project")


EMPTY_REPOSITORY: Final = CorpusPlan(
    name="empty-repository",
    summary="a repository initialised and never worked in",
    project=_project(),
    current=CurrentBlock(),
    registry=_registry(),
)


MINIMAL_ACTIVE: Final = CorpusPlan(
    name="minimal-active",
    summary="one phase, one iter and one wave, all still in flight",
    project=_project(),
    current=CurrentBlock(phase_id="P01", iter_id="P01-I01", wave_id="P01-I01-W01"),
    registry=_registry(),
    phases=(
        PhaseRow(
            id="P01",
            scope_id="P01",
            status="active",
            title="Deliver the first phase",
            iter_ids=("P01-I01",),
        ),
    ),
    iters=(
        IterRow(
            id="P01-I01",
            phase_id="P01",
            status="active",
            title="Close the first iter",
            wave_ids=("P01-I01-W01",),
        ),
    ),
    waves=(
        WaveRow(
            id="P01-I01-W01",
            iter_id="P01-I01",
            status="in_progress",
            title="Land the first wave",
            claim_session_id="S001",
        ),
    ),
    sessions=(
        SessionRow(
            id="S001",
            role="executor",
            runtime="claude",
            scope_id="P01-I01-W01",
            status="open",
        ),
    ),
    estimates=(
        EstimateRow(
            id="EST-P01-I01-W01",
            scope_id="P01-I01-W01",
            display="M",
            confidence="medium",
            reference_class="wave",
        ),
    ),
    goals=(
        GoalRow(
            id="G01",
            scope_id=PROJECT_CODE,
            status="active",
            title="Keep the census total",
            summary="every row reaches exactly one arm",
        ),
    ),
)


ALL_TERMINAL: Final = CorpusPlan(
    name="all-terminal",
    summary="a repository whose every lifecycle row has stopped",
    project=_project(),
    current=CurrentBlock(),
    registry=_registry(),
    phases=(
        PhaseRow(
            id="P01",
            scope_id="P01",
            status="closed",
            title="Deliver the finished phase",
            iter_ids=("P01-I01", "P01-I02"),
            audit_id="A001",
        ),
        PhaseRow(id="P02", scope_id="P02", status="archived", title="Abandon the second phase"),
    ),
    iters=(
        IterRow(
            id="P01-I01",
            phase_id="P01",
            status="closed",
            title="Close the first iter",
            wave_ids=("P01-I01-W01", "P01-I01-W02"),
            audit_id="A001",
        ),
        IterRow(
            id="P01-I02",
            phase_id="P01",
            status="abandoned",
            title="Abandon the second iter",
        ),
    ),
    waves=(
        WaveRow(id="P01-I01-W01", iter_id="P01-I01", status="closed", title="Land the first wave"),
        WaveRow(id="P01-I01-W02", iter_id="P01-I01", status="failed", title="Fail the second wave"),
        WaveRow(
            id="P01-I02-W01",
            iter_id="P01-I02",
            status="abandoned",
            title="Abandon the third wave",
        ),
    ),
    backlog=(
        BacklogRow(
            id="B001",
            scope_id=PROJECT_CODE,
            status="closed",
            title="Retire the legacy exporter",
            resolution="delivered by P01-I01-W01",
        ),
    ),
    worktrees=(
        WorktreeRow(
            id="WT001",
            wave_id="P01-I01-W01",
            branch="feature/demo-p01-w01",
            base_branch="feature/demo",
            path=".ea/worktrees/WT001",
            status="abandoned",
        ),
    ),
    actuals=(
        ActualRow(
            id="ACT-P01-I01-W01",
            scope_id="P01-I01-W01",
            status="done",
            elapsed_eu=0.5,
            calibration_excluded=False,
        ),
    ),
    audits=(
        AuditRow(
            id="A001", scope_id="P01-I01", kind="evaluation", status="complete", verdict="pass"
        ),
    ),
    wave_integrations=(
        WaveIntegrationRow(
            id="WI01", wave_id="P01-I01-W01", kind="cherry_pick", status="integrated"
        ),
    ),
    dependency_bindings=(DependencyBindingRow(wave_id="P01-I01-W02", dep_wave_id="P01-I01-W01"),),
)


INTERRUPTED_CLOSE: Final = CorpusPlan(
    name="interrupted-close",
    summary="a wave whose close attempt stopped before it reached a verdict",
    project=_project(),
    current=CurrentBlock(phase_id="P01", iter_id="P01-I01", wave_id="P01-I01-W01"),
    registry=_registry(),
    phases=(
        PhaseRow(
            id="P01",
            scope_id="P01",
            status="active",
            title="Deliver the interrupted phase",
            iter_ids=("P01-I01",),
        ),
    ),
    iters=(
        IterRow(
            id="P01-I01",
            phase_id="P01",
            status="active",
            title="Close the interrupted iter",
            wave_ids=("P01-I01-W01",),
        ),
    ),
    waves=(
        WaveRow(
            id="P01-I01-W01",
            iter_id="P01-I01",
            status="claimed",
            title="Land the interrupted wave",
            claim_session_id=INTERRUPTED_CLOSE_SESSION,
        ),
    ),
    sessions=(
        SessionRow(
            id=INTERRUPTED_CLOSE_SESSION,
            role="executor",
            runtime="claude",
            scope_id="P01-I01-W01",
            status="open",
        ),
    ),
    close_attempts=(
        CloseAttemptRow(
            id=INTERRUPTED_CLOSE_ATTEMPT,
            wave_id="P01-I01-W01",
            status="in_progress",
            outcome="interrupted",
        ),
    ),
)


OPEN_ATTENTION: Final = CorpusPlan(
    name="open-attention",
    summary="a repository carrying open backlog, an open incident and a live memory",
    project=_project(),
    current=CurrentBlock(phase_id="P01", iter_id="P01-I01"),
    registry=_registry(),
    phases=(
        PhaseRow(
            id="P01",
            scope_id="P01",
            status="active",
            title="Deliver the attention phase",
            iter_ids=("P01-I01",),
        ),
    ),
    iters=(
        IterRow(
            id="P01-I01",
            phase_id="P01",
            status="active",
            title="Close the attention iter",
            wave_ids=("P01-I01-W01",),
        ),
    ),
    waves=(
        WaveRow(id="P01-I01-W01", iter_id="P01-I01", status="pending", title="Plan the next wave"),
    ),
    backlog=(
        BacklogRow(
            id="B001", scope_id=PROJECT_CODE, status="open", title="Widen the rehearsal fixture set"
        ),
        BacklogRow(
            id="B002", scope_id=PROJECT_CODE, status="open", title="Measure the residual document"
        ),
    ),
    incidents=(
        IncidentRow(
            id="INC01",
            scope_id=PROJECT_CODE,
            status="open",
            title="A gate scored nothing",
            severity="low",
            cause="the gate was wired to a criterion nobody produced",
        ),
    ),
    decisions=(
        DecisionRow(
            id="D01",
            scope_id=PROJECT_CODE,
            status="accepted",
            title="Rehearse before the flag day",
            rationale="recorded at the time of the call",
        ),
    ),
    artifacts=(
        ArtifactRow(
            id="ART-01",
            kind="research",
            uri=".ea/artifacts/research/ART-01.md",
            urn="urn:eawf:artifact:ART-01",
        ),
    ),
    memory_index=(
        MemoryRow(
            id="MEM01",
            scope_id=PROJECT_CODE,
            status="active",
            tier="project",
            summary="the rehearsal covers four legs per fixture",
            confidence="high",
        ),
    ),
    audits=(
        AuditRow(
            id="A001", scope_id="P01-I01", kind="review", status="complete", verdict="blocked"
        ),
    ),
    audit_ledger_ids=("A002",),
)


MULTI_ROOT_WORKSPACE: Final = CorpusPlan(
    name="multi-root-workspace",
    summary="one corpus addressed into a workspace that spans more than one root",
    project=_project(),
    current=CurrentBlock(phase_id="P01", iter_id="P01-I01"),
    registry=_registry(
        RegistryWorkspace(
            key=WORKSPACE_KEY,
            title="the workspace the rehearsal corpora mint under",
            home_project_code=PROJECT_KEY,
            member_project_codes=(PROJECT_KEY, "PRJ-SATELLITE"),
        ),
        RegistryWorkspace(
            key=SECOND_WORKSPACE_KEY,
            title="a second workspace the corpus is not addressed into",
            home_project_code="PRJ-SATELLITE",
            member_project_codes=("PRJ-SATELLITE",),
        ),
    ),
    workspace=WorkspaceBlock(
        key=WORKSPACE_KEY,
        roots=(".", "packages/satellite"),
        member_project_codes=(PROJECT_KEY, "PRJ-SATELLITE"),
    ),
    phases=(
        PhaseRow(
            id="P01",
            scope_id="P01",
            status="active",
            title="Deliver across two roots",
            iter_ids=("P01-I01",),
        ),
    ),
    iters=(
        IterRow(
            id="P01-I01",
            phase_id="P01",
            status="active",
            title="Close the multi-root iter",
            wave_ids=("P01-I01-W01",),
        ),
    ),
    waves=(
        WaveRow(
            id="P01-I01-W01", iter_id="P01-I01", status="closed", title="Land in the second root"
        ),
    ),
    worktrees=(
        WorktreeRow(
            id="WT001",
            wave_id="P01-I01-W01",
            branch="feature/satellite-p01-w01",
            base_branch="feature/satellite",
            path="packages/satellite/.ea/worktrees/WT001",
            status="released",
        ),
    ),
    sandbox_policies=(SandboxPolicyRow(id="SP01", scope_id=PROJECT_CODE, scope_kind="project"),),
)


CORRUPT_REFERENCE: Final = CorpusPlan(
    name="corrupt-reference",
    summary="a wave whose iter reference names an iter the corpus does not hold",
    project=_project(),
    current=CurrentBlock(phase_id="P01", iter_id="P01-I01"),
    registry=_registry(),
    phases=(
        PhaseRow(
            id="P01",
            scope_id="P01",
            status="active",
            title="Deliver the corrupt phase",
            iter_ids=("P01-I01",),
        ),
    ),
    iters=(
        IterRow(
            id="P01-I01",
            phase_id="P01",
            status="active",
            title="Close the corrupt iter",
            wave_ids=("P01-I01-W01",),
        ),
    ),
    waves=(
        WaveRow(id="P01-I01-W01", iter_id="P01-I01", status="closed", title="Land the good wave"),
        WaveRow(
            id="P01-I01-W02",
            iter_id=MISSING_ITER_ID,
            status="closed",
            title="Land the wave whose batch never existed",
        ),
    ),
)


UNKNOWN_EXTENSION_FIELD_CORPUS: Final = CorpusPlan(
    name="unknown-extension-field",
    summary="a document carrying a top-level key no disposition row declares",
    project=_project(),
    current=CurrentBlock(phase_id="P01", iter_id="P01-I01"),
    registry=_registry(),
    phases=(
        PhaseRow(
            id="P01",
            scope_id="P01",
            status="active",
            title="Deliver the extended phase",
            iter_ids=("P01-I01",),
        ),
    ),
    iters=(
        IterRow(
            id="P01-I01",
            phase_id="P01",
            status="active",
            title="Close the extended iter",
            wave_ids=("P01-I01-W01",),
        ),
    ),
    waves=(
        WaveRow(id="P01-I01-W01", iter_id="P01-I01", status="closed", title="Land the good wave"),
    ),
    extensions={
        UNKNOWN_EXTENSION_FIELD: {
            "CAP01": {"id": "CAP01", "enabled": True, "title": "a capability nobody declared"}
        }
    },
)


#: Every built corpus, in the order the rehearsal runs them.
STRUCTURAL_CORPORA: Final[tuple[CorpusPlan, ...]] = (
    EMPTY_REPOSITORY,
    MINIMAL_ACTIVE,
    ALL_TERMINAL,
    INTERRUPTED_CLOSE,
    OPEN_ATTENTION,
    MULTI_ROOT_WORKSPACE,
    CORRUPT_REFERENCE,
    UNKNOWN_EXTENSION_FIELD_CORPUS,
)


STRUCTURAL_CORPUS_INDEX: Final[dict[str, CorpusPlan]] = {
    corpus.name: corpus for corpus in STRUCTURAL_CORPORA
}


__all__ = [
    "ALL_TERMINAL",
    "CORRUPT_REFERENCE",
    "EMPTY_REPOSITORY",
    "INTERRUPTED_CLOSE",
    "INTERRUPTED_CLOSE_ATTEMPT",
    "INTERRUPTED_CLOSE_SESSION",
    "MINIMAL_ACTIVE",
    "MISSING_ITER_ID",
    "MULTI_ROOT_WORKSPACE",
    "OPEN_ATTENTION",
    "PROJECT_CODE",
    "PROJECT_KEY",
    "REPOSITORY_KEY",
    "SECOND_WORKSPACE_KEY",
    "STRUCTURAL_CORPORA",
    "STRUCTURAL_CORPUS_INDEX",
    "UNKNOWN_EXTENSION_FIELD",
    "UNKNOWN_EXTENSION_FIELD_CORPUS",
    "WORKSPACE_KEY",
    "default_registry",
]
