"""Typed builders that emit one epoch-1 snapshot tree per rehearsal shape.

The rehearsal fixtures are corpora, not JSON blobs, and the difference
matters. A hand-edited document drifts the moment a row contract gains a
required field: the file still parses, the census still reads it, and the
fixture quietly stops covering what it was written to cover. A builder
cannot drift that way, because every row is a model whose fields are the
contract -- add a required field to the epoch-1 shape and the builder
stops constructing, which is the failure an author can act on.

The document the builder emits is total over the epoch-1 top-level keys
by construction: :data:`NULL_COLLECTIONS` and :data:`EMPTY_COLLECTIONS`
enumerate the keys that carried no rows at the pinned revision, and every
other key is filled from a typed collection. Totality is what the census
refuses on, so a builder that emitted 37 of the 38 keys would produce a
corpus no fixture could use.

One escape hatch exists and is deliberate. :attr:`CorpusPlan.extensions`
adds top-level keys the disposition table does not declare, which is the
only way to build the unknown-extension-field corpus: an undeclared key
is exactly what that fixture has to carry.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


#: The epoch-1 schema version every built corpus declares. The row
#: contracts were measured at this revision, so a corpus that claimed a
#: different one would be asserting a shape nobody checked.
SOURCE_SCHEMA_VERSION: Final = "1.19"

#: The one timestamp every built row carries. Fixed rather than generated
#: because a corpus whose bytes move between two runs cannot have a
#: manifest digest recorded as a golden.
FIXED_TIMESTAMP: Final = "2026-01-01T00:00:00Z"

#: The top-level keys that serialized as JSON ``null`` at the pinned
#: revision. Null is not empty here: the row contracts declare these
#: nullable, and writing ``{}`` instead would assert a container the
#: source never held.
NULL_COLLECTIONS: Final[tuple[str, ...]] = (
    "claims",
    "fleet_run",
    "health",
    "hypotheses",
    "mcp_grants",
    "mcp_servers",
    "open_questions",
    "outcomes",
    "tracks",
)

#: The top-level keys that held an empty container at the pinned revision
#: and that no builder collection fills.
EMPTY_COLLECTIONS: Final[tuple[str, ...]] = (
    "indexes",
    "plugins",
    "wave_dependency_barriers",
)

#: The document's own metadata keys, which carry no rows at all.
SCALAR_FIELDS: Final[tuple[str, ...]] = (
    "dispatch_paused",
    "schema_version",
    "scope_kind",
    "updated_at",
    "urn",
)

#: The snapshot layout the read barrier walks. Named here so the builder
#: and the barrier cannot disagree about where a surface lives.
DOCUMENT_FILENAME: Final = "document.json"
REGISTRY_FILENAME: Final = "registry.json"
TELEMETRY_FILENAME: Final = "telemetry.json"
STORE_DIRNAME: Final = "store"
CONFIG_DIRNAME: Final = "config"
AUDIT_LEDGER_FILENAME: Final = "audit.jsonl"
DECISION_LEDGER_FILENAME: Final = "decision.jsonl"
CONFIG_FILENAME: Final = "base.yaml"

#: The layered-config bytes every built corpus carries. The config
#: surface is digested and never decoded, so one shared body is enough
#: for the barrier to have something to pin.
CONFIG_BODY: Final = "schema_version: '1.0'\nlayer: 0\nprofiles:\n  - python\n"

#: The telemetry metadata every built corpus carries, for the same
#: reason.
TELEMETRY_BODY: Final[dict[str, Any]] = {
    "row_counts": {"events": 0},
    "schema_version": "1.0",
    "tables": ["events", "spans"],
}


class _Row(BaseModel):
    """Base for every typed epoch-1 row a builder can emit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def row(self) -> dict[str, Any]:
        """Return the row as the epoch-1 document carried it.

        Returns:
            The model's fields with every ``None`` dropped, because an
            epoch-1 row omitted a field it never recorded rather than
            writing a null into it.
        """
        return {
            key: value for key, value in self.model_dump(mode="json").items() if value is not None
        }


NonBlank = Annotated[str, Field(min_length=1)]


class PhaseRow(_Row):
    """One epoch-1 ``phases`` row.

    Attributes:
        id: The phase id, which is also its key.
        scope_id: The scope the phase names.
        status: The source status, which must be inside the closed phase
            status map.
        title: The phase title.
        opened_at: When it opened.
        iter_ids: The iters it requires, which convert to canonical Batch
            references and therefore have to exist in the same corpus.
        audit_id: The audit that closed it, when one did.
    """

    id: NonBlank
    scope_id: NonBlank
    status: Literal["planned", "active", "closed", "archived"]
    title: NonBlank
    opened_at: str = FIXED_TIMESTAMP
    iter_ids: tuple[str, ...] | None = None
    audit_id: str | None = None


class IterRow(_Row):
    """One epoch-1 ``iters`` row.

    Attributes:
        id: The iter id, which is also its key.
        phase_id: The phase it belongs to, which converts to a canonical
            Milestone reference.
        status: The source status, inside the closed iter status map.
        title: The iter title.
        opened_at: When it opened.
        wave_ids: The waves it holds, which convert to canonical Task
            references.
        audit_id: The audit that closed it, when one did.
    """

    id: NonBlank
    phase_id: NonBlank
    status: Literal["planned", "active", "closed", "abandoned"]
    title: NonBlank
    opened_at: str = FIXED_TIMESTAMP
    wave_ids: tuple[str, ...] | None = None
    audit_id: str | None = None


class WaveRow(_Row):
    """One epoch-1 ``waves`` row.

    Attributes:
        id: The wave id, which is also its key.
        iter_id: The iter it belongs to, which converts to a canonical
            Batch reference. A value naming no iter in the same corpus is
            how the corrupt-reference corpus is built.
        status: The source status, inside the closed wave status map.
        title: The wave title, which is also what a null intent defaults
            from.
        opened_at: When it opened.
        claim_session_id: The session that claimed it, carried as a
            legacy string rather than a canonical edge.
    """

    id: NonBlank
    iter_id: NonBlank
    status: Literal["pending", "claimed", "in_progress", "closed", "abandoned", "failed"]
    title: NonBlank
    opened_at: str = FIXED_TIMESTAMP
    claim_session_id: str | None = None


class BacklogRow(_Row):
    """One epoch-1 ``backlog`` row.

    Attributes:
        id: The row id, which is also its key.
        scope_id: The scope it was filed under.
        status: The source status, inside the closed backlog status map.
        title: The row title.
        created_at: When it was filed.
        resolution: The prose a closed row was closed with. A closed row
            that carries neither this nor a commit reaches no classifier
            arm, which the importer refuses rather than dropping the row
            unexplained.
        commit: The commit that closed it, when the source recorded one.
    """

    id: NonBlank
    scope_id: NonBlank
    status: Literal["open", "closed"]
    title: NonBlank
    created_at: str = FIXED_TIMESTAMP
    resolution: str | None = None
    commit: str | None = None


class SessionRow(_Row):
    """One epoch-1 ``agent_sessions`` row.

    Attributes:
        id: The session id, which is also its key.
        role: The agent role the session ran under.
        runtime: The runtime it ran on.
        scope_id: The scope it worked in.
        status: Whether it finished.
        started_at: When it started.
    """

    id: NonBlank
    role: NonBlank
    runtime: NonBlank
    scope_id: NonBlank
    status: NonBlank
    started_at: str = FIXED_TIMESTAMP


class WorktreeRow(_Row):
    """One epoch-1 ``worktrees`` row.

    Attributes:
        id: The worktree id, which is also its key.
        wave_id: The wave it was cut for.
        branch: The worktree branch.
        base_branch: The branch it was cut from.
        path: The repo-relative worktree path. Never an absolute path: a
            committed corpus that carried one would be a leak.
        status: The lease state the source recorded.
        created_at: When it was cut.
    """

    id: NonBlank
    wave_id: NonBlank
    branch: NonBlank
    base_branch: NonBlank
    path: NonBlank
    status: NonBlank
    created_at: str = FIXED_TIMESTAMP


class EstimateRow(_Row):
    """One epoch-1 ``estimates`` row, keyed by the scope it measures.

    Attributes:
        id: The estimate's own identifier, which is never the key.
        scope_id: The scope it measures, which is the collection key.
        display: The bucket the estimate was expressed in.
        confidence: The quality marker, carried verbatim.
        reference_class: The class the estimate was drawn from.
        updated_at: When it was last written.
    """

    id: NonBlank
    scope_id: NonBlank
    display: NonBlank
    confidence: NonBlank
    reference_class: NonBlank
    updated_at: str = FIXED_TIMESTAMP


class ActualRow(_Row):
    """One epoch-1 ``actuals`` row, keyed by the scope it measures.

    Attributes:
        id: The measurement's own identifier, which is never the key.
        scope_id: The scope it measures, which is the collection key.
        status: The quality marker, carried verbatim.
        elapsed_eu: The measured effort, when the source recorded one.
        calibration_excluded: Whether the row must not calibrate.
        updated_at: When it was last written.
    """

    id: NonBlank
    scope_id: NonBlank
    status: NonBlank
    elapsed_eu: float | None = None
    calibration_excluded: bool | None = None
    updated_at: str = FIXED_TIMESTAMP


class AuditRow(_Row):
    """One epoch-1 ``audits`` row.

    Attributes:
        id: The audit id, which is also its key.
        scope_id: What it audited.
        kind: Which audit kind ran.
        status: Whether it finished.
        verdict: What it concluded, when it concluded anything.
        created_at: When it ran.
    """

    id: NonBlank
    scope_id: NonBlank
    kind: NonBlank
    status: NonBlank
    verdict: str | None = None
    created_at: str = FIXED_TIMESTAMP


class DecisionRow(_Row):
    """One epoch-1 ``decisions`` row.

    Attributes:
        id: The decision id, which is also its key.
        scope_id: The scope it was taken in.
        status: Whether it stands.
        title: The decision title.
        rationale: Why it was taken.
        created_at: When it was taken.
    """

    id: NonBlank
    scope_id: NonBlank
    status: NonBlank
    title: NonBlank
    rationale: NonBlank
    created_at: str = FIXED_TIMESTAMP


class IncidentRow(_Row):
    """One epoch-1 ``incidents`` row.

    Attributes:
        id: The incident id, which is also its key.
        scope_id: Where it happened.
        status: Whether it is closed.
        title: The incident title.
        severity: How bad it was.
        cause: What caused it.
        opened_at: When it opened.
    """

    id: NonBlank
    scope_id: NonBlank
    status: NonBlank
    title: NonBlank
    severity: NonBlank
    cause: NonBlank
    opened_at: str = FIXED_TIMESTAMP


class ArtifactRow(_Row):
    """One epoch-1 ``artifacts`` row.

    Attributes:
        id: The artifact id, which is also its key.
        kind: Which artifact kind it is.
        uri: Where it lives, repo-relative.
        urn: Its epoch-1 URN.
        created_at: When it was written.
    """

    id: NonBlank
    kind: NonBlank
    uri: NonBlank
    urn: NonBlank
    created_at: str = FIXED_TIMESTAMP


class MemoryRow(_Row):
    """One epoch-1 ``memory_index`` row.

    Attributes:
        id: The memory id, which is also its key.
        scope_id: What it remembers about.
        status: Whether it is still live.
        tier: Which memory tier holds it.
        summary: The one-line hook.
        confidence: How much it is trusted.
    """

    id: NonBlank
    scope_id: NonBlank
    status: NonBlank
    tier: NonBlank
    summary: NonBlank
    confidence: NonBlank


class GoalRow(_Row):
    """One epoch-1 ``goals`` row.

    Attributes:
        id: The goal id, which is also its key.
        scope_id: The scope it belongs to.
        status: Whether it is live.
        title: The goal title.
        summary: What it aims at.
        created_at: When it was set.
    """

    id: NonBlank
    scope_id: NonBlank
    status: NonBlank
    title: NonBlank
    summary: NonBlank
    created_at: str = FIXED_TIMESTAMP


class SandboxPolicyRow(_Row):
    """One epoch-1 ``sandbox_policies`` row.

    Attributes:
        id: The policy id, which is also its key.
        scope_id: The scope it grants over.
        scope_kind: What kind of scope that is.
        granted_at: When it was granted.
    """

    id: NonBlank
    scope_id: NonBlank
    scope_kind: NonBlank
    granted_at: str = FIXED_TIMESTAMP


class CloseAttemptRow(_Row):
    """One epoch-1 ``close_attempts`` row.

    Attributes:
        id: The attempt id, which is also its key.
        wave_id: The wave whose close was attempted.
        status: How far the attempt got. A non-terminal value is what
            makes the interrupted-close corpus interrupted.
        outcome: What the attempt concluded, verbatim.
        requested_at: When it was requested.
    """

    id: NonBlank
    wave_id: NonBlank
    status: NonBlank
    outcome: NonBlank
    requested_at: str = FIXED_TIMESTAMP


class WaveIntegrationRow(_Row):
    """One epoch-1 ``wave_integrations`` row.

    Attributes:
        id: The integration id, which is also its key.
        wave_id: The wave it integrated.
        kind: How it was integrated.
        status: Whether it landed.
        created_at: When it was recorded.
    """

    id: NonBlank
    wave_id: NonBlank
    kind: NonBlank
    status: NonBlank
    created_at: str = FIXED_TIMESTAMP


class DependencyBindingRow(_Row):
    """One epoch-1 ``wave_dependency_bindings`` row.

    The collection is keyed by a composite the row does not carry as a
    single field, so the key is built from the pair rather than read off
    the row.

    Attributes:
        wave_id: The dependent wave.
        dep_wave_id: The wave it waits on.
        bound_at: When the edge was recorded.
    """

    wave_id: NonBlank
    dep_wave_id: NonBlank
    bound_at: str = FIXED_TIMESTAMP

    def key(self) -> str:
        """Return the composite key the collection is keyed by."""
        return f"{self.wave_id}:{self.dep_wave_id}"


class ProjectBlock(_Row):
    """The epoch-1 ``project`` mapping.

    Attributes:
        code: The project code, which is the only addressing slot epoch 1
            recorded.
        title: The project title.
        created_at: When the project was initialised.
    """

    code: NonBlank
    title: NonBlank
    created_at: str = FIXED_TIMESTAMP


class CurrentBlock(_Row):
    """The epoch-1 ``current`` pointer block.

    Attributes:
        phase_id: The phase in flight, or ``None``.
        iter_id: The iter in flight, or ``None``.
        wave_id: The wave in flight, or ``None``.
    """

    phase_id: str | None = None
    iter_id: str | None = None
    wave_id: str | None = None

    def block(self) -> dict[str, Any]:
        """Return the pointer block with its nulls kept.

        Returns:
            All three pointers, nulls included. Unlike a row, the pointer
            block recorded "nothing in flight" as an explicit null, so
            dropping the key would lose that statement.
        """
        return self.model_dump(mode="json")


class WorkspaceBlock(_Row):
    """The epoch-1 ``workspace`` mapping.

    Attributes:
        key: The workspace key the repository belongs to.
        roots: The repo-relative roots the workspace spans, which is what
            makes a corpus multi-root.
        member_project_codes: The projects the workspace holds.
    """

    key: NonBlank
    roots: tuple[str, ...]
    member_project_codes: tuple[str, ...]


class RegistryWorkspace(_Row):
    """One workspace row of the registry an apply resolves against.

    Attributes:
        key: The workspace key.
        title: What it is for.
        home_project_code: The project the workspace is rooted on.
        member_project_codes: Every project it holds.
    """

    key: NonBlank
    title: NonBlank
    home_project_code: NonBlank
    member_project_codes: tuple[str, ...]

    def row(self) -> dict[str, Any]:
        """Return the workspace row in the registry's own shape."""
        return {
            "home_project_code": self.home_project_code,
            "key": self.key,
            "member_project_codes": list(self.member_project_codes),
            "revision": 1,
            "title": self.title,
            "updated_at": FIXED_TIMESTAMP,
        }


class RegistryDocument(_Row):
    """The workspace registry a built corpus ships beside its snapshot.

    Attributes:
        workspaces: Every registered workspace, in declaration order.
    """

    workspaces: tuple[RegistryWorkspace, ...]

    def document(self) -> dict[str, Any]:
        """Return the registry document the apply reads."""
        return {
            "active_code": None,
            "repos": {},
            "updated_at": FIXED_TIMESTAMP,
            "version": "1",
            "workspaces": {row.key: row.row() for row in self.workspaces},
        }


class CorpusPlan(BaseModel):
    """One epoch-1 corpus, declared as typed rows rather than as JSON.

    Attributes:
        name: The fixture name, used as the directory name and as the
            parametrised test id.
        summary: What shape the corpus exercises, in one line.
        project: The project block.
        current: The pointer block.
        registry: The workspace registry the apply resolves against.
        workspace: The workspace mapping, when the corpus recorded one.
        phases: The ``phases`` rows.
        iters: The ``iters`` rows.
        waves: The ``waves`` rows.
        backlog: The ``backlog`` rows.
        sessions: The ``agent_sessions`` rows.
        worktrees: The ``worktrees`` rows.
        estimates: The ``estimates`` rows.
        actuals: The ``actuals`` rows.
        audits: The ``audits`` rows.
        decisions: The ``decisions`` rows.
        incidents: The ``incidents`` rows.
        artifacts: The ``artifacts`` rows.
        memory_index: The ``memory_index`` rows.
        goals: The ``goals`` rows.
        sandbox_policies: The ``sandbox_policies`` rows.
        close_attempts: The ``close_attempts`` rows.
        wave_integrations: The ``wave_integrations`` rows.
        dependency_bindings: The ``wave_dependency_bindings`` rows.
        audit_ledger_ids: Audit ids the store ledger holds and the
            document does not, which is what makes the audit population a
            union rather than a copy.
        extensions: Top-level keys the disposition table does not
            declare. Only the unknown-extension-field corpus carries any.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonBlank
    summary: NonBlank
    project: ProjectBlock
    current: CurrentBlock
    registry: RegistryDocument
    workspace: WorkspaceBlock | None = None
    phases: tuple[PhaseRow, ...] = ()
    iters: tuple[IterRow, ...] = ()
    waves: tuple[WaveRow, ...] = ()
    backlog: tuple[BacklogRow, ...] = ()
    sessions: tuple[SessionRow, ...] = ()
    worktrees: tuple[WorktreeRow, ...] = ()
    estimates: tuple[EstimateRow, ...] = ()
    actuals: tuple[ActualRow, ...] = ()
    audits: tuple[AuditRow, ...] = ()
    decisions: tuple[DecisionRow, ...] = ()
    incidents: tuple[IncidentRow, ...] = ()
    artifacts: tuple[ArtifactRow, ...] = ()
    memory_index: tuple[MemoryRow, ...] = ()
    goals: tuple[GoalRow, ...] = ()
    sandbox_policies: tuple[SandboxPolicyRow, ...] = ()
    close_attempts: tuple[CloseAttemptRow, ...] = ()
    wave_integrations: tuple[WaveIntegrationRow, ...] = ()
    dependency_bindings: tuple[DependencyBindingRow, ...] = ()
    audit_ledger_ids: tuple[str, ...] = ()
    extensions: dict[str, Any] = Field(default_factory=dict)

    def keyed_collections(self) -> dict[str, dict[str, Any]]:
        """Return every keyed-row collection, keyed as the source keyed it.

        Returns:
            One entry per collection, with the rows in the order the
            builder declared them. ``estimates`` and ``actuals`` key by
            the scope they measure rather than by their own id, and the
            dependency bindings key by a composite, so those three cannot
            be keyed generically.
        """
        return {
            "phases": {row.id: row.row() for row in self.phases},
            "iters": {row.id: row.row() for row in self.iters},
            "waves": {row.id: row.row() for row in self.waves},
            "backlog": {row.id: row.row() for row in self.backlog},
            "agent_sessions": {row.id: row.row() for row in self.sessions},
            "worktrees": {row.id: row.row() for row in self.worktrees},
            "estimates": {row.scope_id: row.row() for row in self.estimates},
            "actuals": {row.scope_id: row.row() for row in self.actuals},
            "audits": {row.id: row.row() for row in self.audits},
            "decisions": {row.id: row.row() for row in self.decisions},
            "incidents": {row.id: row.row() for row in self.incidents},
            "artifacts": {row.id: row.row() for row in self.artifacts},
            "memory_index": {row.id: row.row() for row in self.memory_index},
            "goals": {row.id: row.row() for row in self.goals},
            "sandbox_policies": {row.id: row.row() for row in self.sandbox_policies},
            "close_attempts": {row.id: row.row() for row in self.close_attempts},
            "wave_integrations": {row.id: row.row() for row in self.wave_integrations},
            "wave_dependency_bindings": {row.key(): row.row() for row in self.dependency_bindings},
        }

    def document(self) -> dict[str, Any]:
        """Return the epoch-1 state document this corpus stands for.

        Returns:
            A document total over the declared epoch-1 top-level keys,
            plus whatever :attr:`extensions` adds.
        """
        document: dict[str, Any] = dict(self.keyed_collections())
        document.update(dict.fromkeys(NULL_COLLECTIONS))
        document.update({key: {} for key in EMPTY_COLLECTIONS})
        document["current"] = self.current.block()
        document["project"] = self.project.row()
        document["workspace"] = self.workspace.row() if self.workspace is not None else None
        document["dispatch_paused"] = False
        document["schema_version"] = SOURCE_SCHEMA_VERSION
        document["scope_kind"] = "project"
        document["updated_at"] = FIXED_TIMESTAMP
        document["urn"] = f"urn:eawf:project:{self.project.code}"
        document.update(self.extensions)
        return document

    def audit_ledger(self) -> tuple[dict[str, Any], ...]:
        """Return the audit ledger rows the store holds.

        Returns:
            One row per document audit plus one per store-only id, in
            that order. The store-only rows are what make the audit
            population a union: a row only the ledger holds still counts.
        """
        rows = [
            {
                "created_at": row.created_at,
                "id": row.id,
                "kind": "audit",
                "schema_version": "1.0",
                "scope_id": row.scope_id,
                "summary": f"audit {row.id} {row.kind}",
                "updated_at": row.created_at,
            }
            for row in self.audits
        ]
        rows.extend(
            {
                "created_at": FIXED_TIMESTAMP,
                "id": audit_id,
                "kind": "audit",
                "schema_version": "1.0",
                "scope_id": self.project.code,
                "summary": f"audit {audit_id} recorded only in the store",
                "updated_at": FIXED_TIMESTAMP,
            }
            for audit_id in self.audit_ledger_ids
        )
        return tuple(rows)

    def decision_ledger(self) -> tuple[dict[str, Any], ...]:
        """Return the decision ledger rows the store holds."""
        return tuple(
            {
                "created_at": row.created_at,
                "id": row.id,
                "kind": "decision",
                "schema_version": "1.0",
                "scope_id": row.scope_id,
                "summary": f"decision {row.id}",
                "updated_at": row.created_at,
            }
            for row in self.decisions
        )

    def write(self, root: Path) -> Path:
        """Write the whole snapshot tree under ``root`` and return it.

        Args:
            root: The directory to build the snapshot in. Created when
                absent.

        Returns:
            ``root``, now holding a corpus the read barrier can pin.

        Raises:
            OSError: When a surface cannot be written.
        """
        (root / STORE_DIRNAME).mkdir(parents=True, exist_ok=True)
        (root / CONFIG_DIRNAME).mkdir(parents=True, exist_ok=True)
        _write_json(root / DOCUMENT_FILENAME, self.document())
        _write_json(root / REGISTRY_FILENAME, self.registry.document())
        _write_json(root / TELEMETRY_FILENAME, TELEMETRY_BODY)
        _write_jsonl(root / STORE_DIRNAME / AUDIT_LEDGER_FILENAME, self.audit_ledger())
        _write_jsonl(root / STORE_DIRNAME / DECISION_LEDGER_FILENAME, self.decision_ledger())
        (root / CONFIG_DIRNAME / CONFIG_FILENAME).write_text(CONFIG_BODY, encoding="utf-8")
        return root


def _write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` as sorted, newline-terminated JSON."""
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: tuple[dict[str, Any], ...]) -> None:
    """Write ``rows`` as one sorted JSON object per line."""
    body = "".join(f"{json.dumps(row, sort_keys=True)}\n" for row in rows)
    path.write_text(body, encoding="utf-8")


__all__ = [
    "AUDIT_LEDGER_FILENAME",
    "CONFIG_BODY",
    "DOCUMENT_FILENAME",
    "EMPTY_COLLECTIONS",
    "FIXED_TIMESTAMP",
    "NULL_COLLECTIONS",
    "REGISTRY_FILENAME",
    "SCALAR_FIELDS",
    "SOURCE_SCHEMA_VERSION",
    "STORE_DIRNAME",
    "TELEMETRY_FILENAME",
    "ActualRow",
    "ArtifactRow",
    "AuditRow",
    "BacklogRow",
    "CloseAttemptRow",
    "CorpusPlan",
    "CurrentBlock",
    "DecisionRow",
    "DependencyBindingRow",
    "EstimateRow",
    "GoalRow",
    "IncidentRow",
    "IterRow",
    "MemoryRow",
    "PhaseRow",
    "ProjectBlock",
    "RegistryDocument",
    "RegistryWorkspace",
    "SandboxPolicyRow",
    "SessionRow",
    "WaveIntegrationRow",
    "WaveRow",
    "WorkspaceBlock",
    "WorktreeRow",
]
