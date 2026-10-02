"""The declared exceptions to the verb contract's closed groups and anchors.

The verb contract closes the root at its entity groups; every other root
entry, and every state-changing verb that takes no compare-and-swap anchor
and retry key, is named here with the reason it is allowed to stand. Each
list is a typed, reviewable claim: a root entry or an unanchored mutating
verb that is on neither the contract nor these lists fails the census that
walks the live command tree.

- :data:`ROOT_ENTRY_EXCEPTIONS` names every root entry outside
  :data:`~eawf.surfaces.cli.verb_contract.ENTITY_GROUPS`. The
  ``cross_cutting`` rows are exactly the contract's cross-cutting set; the
  ``tooling`` rows are surfaces still to be regrouped or retired by
  amendment.
- :data:`ANCHOR_EXEMPTIONS` names every verb the effect table classifies as
  writing that does not require both ``--expected-revision`` and
  ``--idempotency-key``. A partial kind still takes the one anchor
  :data:`PARTIAL_ANCHOR` names.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Why a root entry outside the entity groups may stand.
#:
#: - ``cross_cutting``: a cross-cutting group the verb contract declares.
#: - ``tooling``: a surface the contract has not placed yet -- a root verb
#:   another contract row names, native verbs of an entity with no group,
#:   a path installed hooks or plugin manifests call, epoch-1 upkeep, or
#:   operator diagnostics and setup. Each is regrouped or retired by
#:   amendment.
RootEntryKind = Literal["cross_cutting", "tooling"]

#: Why a writing verb may take less than both anchors.
#:
#: - ``revision_only``: the route keeps no replay ledger; the anchor alone
#:   makes a retry safe, so the verb requires ``--expected-revision``.
#: - ``derived_key``: the daemon derives the replay identity from the
#:   request's content; the verb requires ``--expected-revision``.
#: - ``create_keyed``: mints a record that has no revision yet; the verb
#:   requires ``--idempotency-key``.
#: - ``optional_key``: writes rows that keep no revision; the verb accepts
#:   ``--idempotency-key`` and passes it to the route when given.
#: - ``spec_anchored``: the retry key travels inside the ``--from-spec``
#:   document the route validates.
#: - ``epoch1_state``: writes epoch-1 state or moves an imported epoch-1 row,
#:   neither of which carries a row revision.
#: - ``unversioned_store``: writes a store that keeps no revision to compare.
#: - ``host_event``: records a host hook event as it fires; the host names
#:   neither a revision nor a retry key.
#: - ``local_setup``: creates or reconfigures local files, host settings or
#:   processes, never a revisioned record.
#: - ``wire_pending``: the route takes no anchor yet; adding it daemon-side
#:   is open work, and the row leaves this list when it lands.
AnchorExemptionKind = Literal[
    "revision_only",
    "derived_key",
    "create_keyed",
    "optional_key",
    "spec_anchored",
    "epoch1_state",
    "unversioned_store",
    "host_event",
    "local_setup",
    "wire_pending",
]

#: The one anchor each partial exemption kind still takes, and whether the
#: verb requires it.
PARTIAL_ANCHOR: Final[dict[AnchorExemptionKind, tuple[str, bool]]] = {
    "revision_only": ("--expected-revision", True),
    "derived_key": ("--expected-revision", True),
    "create_keyed": ("--idempotency-key", True),
    "optional_key": ("--idempotency-key", False),
}


class RootEntryException(BaseModel):
    """One root entry outside the entity groups, and why it stands.

    Attributes:
        name: The root entry, ``eawf <name>``.
        kind: Which reason a root entry may rest on.
        reason: The specific reason, in one line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    kind: RootEntryKind
    reason: str = Field(min_length=1)


class AnchorExemption(BaseModel):
    """One writing verb that takes less than both anchors, and why.

    Attributes:
        verb: The command path after ``eawf``.
        kind: Which reason an exemption may rest on.
        reason: The specific reason, in one line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verb: str = Field(min_length=1)
    kind: AnchorExemptionKind
    reason: str = Field(min_length=1)


def _entry(name: str, kind: RootEntryKind, reason: str) -> RootEntryException:
    return RootEntryException(name=name, kind=kind, reason=reason)


def _exempt(verb: str, kind: AnchorExemptionKind, reason: str) -> AnchorExemption:
    return AnchorExemption(verb=verb, kind=kind, reason=reason)


ROOT_ENTRY_EXCEPTIONS: Final[tuple[RootEntryException, ...]] = (
    _entry("workspace", "cross_cutting", "workspaces span repositories, so no entity owns them"),
    _entry("config", "cross_cutting", "the layered configuration configures every entity"),
    _entry("daemon", "cross_cutting", "the daemon serves every entity's routes"),
    _entry("memory", "cross_cutting", "memory rows annotate the project, not one entity"),
    _entry("ui", "cross_cutting", "the console renders every entity"),
    _entry("migrate", "cross_cutting", "the cutover moves every entity of a tree at once"),
    _entry("reflect", "cross_cutting", "reports over measurement collections, not an entity"),
    _entry("follow", "tooling", "streams any submitted operation from a cursor"),
    _entry("verbs", "tooling", "emits the introspectable verb catalog"),
    _entry("plan", "tooling", "plan revisions are Milestone verbs not yet regrouped"),
    _entry("record", "tooling", "ledger records belong to no entity group yet"),
    _entry("repository", "tooling", "the Repository entity has no contract group yet"),
    _entry("hook", "tooling", "installed git and host hooks call these paths"),
    _entry("plugin", "tooling", "installs and checks the host plugin bundles"),
    _entry("cc", "tooling", "the Claude Code status line calls this path"),
    _entry("skill", "tooling", "rendered skill bodies call these paths"),
    _entry("mcp", "tooling", "host MCP configuration launches these paths"),
    _entry("completion", "tooling", "shell profiles source the completion script"),
    _entry("session", "tooling", "a live epoch-1 session blocks the cutover"),
    _entry("worktree", "tooling", "live epoch-1 worktrees block the cutover"),
    _entry("wal", "tooling", "inspects the write-ahead log the cutover must drain"),
    _entry("store", "tooling", "compacts the epoch-1 JSONL stores"),
    _entry("init", "tooling", "creates a tree, before any entity exists"),
    _entry("clone-repo", "tooling", "clones a repository and initialises it"),
    _entry("repo", "tooling", "registers repositories in the machine registry"),
    _entry("status", "tooling", "summarises the tree for the operator"),
    _entry("validate", "tooling", "validates a state or envelope document"),
    _entry("doctor", "tooling", "diagnoses the installation"),
    _entry("doc", "tooling", "checks documentation drift"),
    _entry("schema", "tooling", "dumps the JSON Schemas of the canonical models"),
    _entry("sync", "tooling", "re-renders the managed instruction assets"),
    _entry("rules", "tooling", "reads and migrates the rendered rule set"),
    _entry("profile", "tooling", "scaffolds and validates rule profiles"),
    _entry("coauthor", "tooling", "resolves the commit trailer policy"),
    _entry("pr", "tooling", "renders a pull request body"),
    _entry("wiki", "tooling", "renders the wiki pages"),
    _entry("render-output", "tooling", "converts an output envelope between forms"),
    _entry("impact", "tooling", "renders the file impact graph"),
    _entry("metrics", "tooling", "reports rolling workflow metrics"),
    _entry("why", "tooling", "explains an entity's trust tier"),
    _entry("jury", "tooling", "writes calibration gold labels"),
    _entry("bench", "tooling", "runs the performance bench"),
    _entry("telemetry", "tooling", "checks the telemetry pricing tables"),
    _entry("snapshot", "tooling", "regenerates golden fixtures"),
    _entry("vfl", "tooling", "approves visual fidelity goldens"),
    _entry("backup", "tooling", "snapshots and restores the tree's stores"),
    _entry("help", "tooling", "prints prose help topics"),
    _entry("version", "tooling", "prints the installed version"),
    _entry("scope-debug", "tooling", "prints the resolved global flags, hidden"),
)


ANCHOR_EXEMPTIONS: Final[tuple[AnchorExemption, ...]] = (
    _exempt(
        "action decide-permission",
        "revision_only",
        "the permission route keeps no replay ledger; its revision guards a retry",
    ),
    _exempt(
        "question reply",
        "revision_only",
        "the question route keeps no replay ledger; the question revision guards a retry",
    ),
    _exempt(
        "action notice",
        "revision_only",
        "the notice ledger keeps no replay ledger; the notice revision guards a retry",
    ),
    *(
        _exempt(
            f"run {control}",
            "create_keyed",
            "mints a control request fact, which has no revision; its reference is the key",
        )
        for control in ("interrupt", "cancel", "reconcile")
    ),
    *(
        _exempt(
            f"run {verb}-dispatch",
            "create_keyed",
            "mints a dispatch control fact, which has no revision; its reference is the key",
        )
        for verb in ("pause", "drain", "resume")
    ),
    _exempt(
        "milestone open-approval",
        "derived_key",
        "a question already standing on the same bundle is returned, not filed twice",
    ),
    _exempt("batch integrate", "derived_key", "assembling the same plan names the same replay key"),
    _exempt(
        "release adopt",
        "revision_only",
        "the release record keeps no replay ledger; its revision guards a retry",
    ),
    _exempt(
        "release cancel",
        "revision_only",
        "the release record keeps no replay ledger; its revision guards a retry",
    ),
    _exempt(
        "campaign cancel",
        "revision_only",
        "the close route keeps no replay ledger; the Campaign revision guards it",
    ),
    _exempt(
        "plan submit",
        "create_keyed",
        "submits a new plan revision, which has no revision to anchor yet",
    ),
    _exempt(
        "question open-decision",
        "spec_anchored",
        "the document carries the idempotency key the route replays on",
    ),
    _exempt(
        "milestone close-legacy",
        "epoch1_state",
        "moves an imported epoch-1 row, which has no revision",
    ),
    _exempt(
        "milestone cancel-legacy",
        "epoch1_state",
        "moves an imported epoch-1 row, which has no revision",
    ),
    _exempt(
        "batch close-legacy", "epoch1_state", "moves an imported epoch-1 row, which has no revision"
    ),
    _exempt(
        "task advance-legacy",
        "epoch1_state",
        "moves an imported epoch-1 row, which has no revision",
    ),
    _exempt(
        "record append", "epoch1_state", "files a record for imported work, which has no revision"
    ),
    _exempt("session close", "epoch1_state", "writes the epoch-1 session row the cutover waits on"),
    _exempt(
        "session recover", "epoch1_state", "writes the epoch-1 session row the cutover waits on"
    ),
    _exempt(
        "worktree merge-back",
        "epoch1_state",
        "writes the epoch-1 worktree row the cutover waits on",
    ),
    _exempt(
        "worktree cleanup", "epoch1_state", "writes the epoch-1 worktree row the cutover waits on"
    ),
    _exempt(
        "worktree reconcile",
        "epoch1_state",
        "writes the epoch-1 worktree rows the cutover waits on",
    ),
    _exempt(
        "store compact",
        "epoch1_state",
        "rewrites the epoch-1 JSONL stores, which carry no revision",
    ),
    _exempt("mcp add", "create_keyed", "registers a server, which has no revision to anchor yet"),
    _exempt("mcp grant", "create_keyed", "files a grant, which has no revision to anchor yet"),
    _exempt("memory add", "wire_pending", "the memory routes take no anchor yet"),
    _exempt("memory promote", "wire_pending", "the memory routes take no anchor yet"),
    _exempt(
        "memory compact", "unversioned_store", "compacts the memory file, which keeps no revision"
    ),
    _exempt("memory prune", "wire_pending", "the memory routes take no anchor yet"),
    _exempt("memory gc", "wire_pending", "the memory routes take no anchor yet"),
    _exempt("memory tier", "wire_pending", "the memory routes take no anchor yet"),
    _exempt("config set", "unversioned_store", "layered configuration files keep no revision"),
    _exempt("config unset", "unversioned_store", "layered configuration files keep no revision"),
    _exempt("config menu", "unversioned_store", "layered configuration files keep no revision"),
    _exempt(
        "config profile enable", "unversioned_store", "layered configuration files keep no revision"
    ),
    _exempt(
        "repo add",
        "optional_key",
        "registry rows keep no revision; the retry key is passed when given",
    ),
    _exempt(
        "repo register",
        "optional_key",
        "registry rows keep no revision; the retry key is passed when given",
    ),
    _exempt(
        "repo remove",
        "optional_key",
        "registry rows keep no revision; the retry key is passed when given",
    ),
    _exempt(
        "repo prune",
        "optional_key",
        "registry rows keep no revision; the retry key is passed when given",
    ),
    _exempt("jury label", "wire_pending", "jury.label takes neither anchor yet"),
    _exempt("hook run", "host_event", "runs the hooks of one host event as it fires"),
    _exempt("hook dispatch", "host_event", "seeds the verdict of one host agent_end event"),
    _exempt("hook agent-output", "host_event", "records one host agent output chunk"),
    _exempt("init", "local_setup", "creates a tree, before any record exists"),
    _exempt("clone-repo", "local_setup", "clones and initialises a repository"),
    _exempt("repo init", "local_setup", "creates a repository tree, before any record exists"),
    _exempt(
        "workspace select", "local_setup", "picks the session's workspace and writes no record"
    ),
    _exempt("profile new", "local_setup", "scaffolds a rule profile file"),
    _exempt("rules rollback", "local_setup", "restores the rule set files"),
    _exempt("plugin install", "local_setup", "writes the host plugin bundle"),
    _exempt("plugin update", "local_setup", "rewrites the host plugin bundle"),
    _exempt("plugin sync", "local_setup", "rewrites the host plugin bundle"),
    _exempt("completion install", "local_setup", "writes the shell completion script"),
    _exempt("skill run", "local_setup", "runs a skill in a local session"),
    _exempt(
        "backup restore",
        "local_setup",
        "restores a file snapshot below the record layer, only while no daemon runs",
    ),
    _exempt("daemon run", "local_setup", "runs the daemon process"),
    _exempt("daemon start", "local_setup", "starts the daemon process"),
    _exempt("daemon restart", "local_setup", "restarts the daemon process"),
    _exempt("daemon stop", "local_setup", "stops the daemon process"),
    _exempt("daemon replay-wal", "local_setup", "replays the daemon's own write-ahead log"),
    _exempt("daemon reclaim", "local_setup", "trims the daemon's backups and write-ahead log"),
    _exempt("daemon service-enable", "local_setup", "installs the daemon service unit"),
    _exempt("daemon service-disable", "local_setup", "removes the daemon service unit"),
    _exempt(
        "metrics",
        "optional_key",
        "refit files a mapping revision under --idempotency-key or the day; rebuild "
        "re-projects the derived telemetry database",
    ),
    _exempt("doctor", "local_setup", "--fix repairs the local install, below the record layer"),
    _exempt("migrate", "epoch1_state", "rewrites an epoch-1 state.json across schema versions"),
    _exempt("migrate epoch2", "epoch1_state", "cuts an epoch-1 tree over before any record exists"),
    _exempt("cc statusline install", "local_setup", "writes the host statusline configuration"),
    _exempt("schema dump", "local_setup", "rewrites the committed generated schema files"),
    _exempt("sync", "local_setup", "re-renders the managed instruction files"),
    _exempt("snapshot update", "local_setup", "rewrites committed golden fixtures"),
    _exempt("vfl approve", "local_setup", "rewrites committed visual goldens"),
    _exempt("bench fixture seed", "local_setup", "writes the deterministic bench corpus files"),
    _exempt("mcp run-config", "local_setup", "writes a host MCP configuration file"),
    _exempt("workspace add", "wire_pending", "the registry create route takes no retry key yet"),
    _exempt(
        "workspace member add",
        "wire_pending",
        "the local fallback cannot check the optional revision yet",
    ),
    _exempt(
        "workspace member remove",
        "wire_pending",
        "the local fallback cannot check the optional revision yet",
    ),
    _exempt("release create", "wire_pending", "release.create takes neither anchor yet"),
    _exempt("release approve", "wire_pending", "release.approve takes neither anchor yet"),
    _exempt("release candidate", "wire_pending", "release.candidate takes neither anchor yet"),
    _exempt("release pipeline", "wire_pending", "drives release routes that take no anchor yet"),
    _exempt("release bind-regime", "wire_pending", "runtime.regime.bind takes neither anchor yet"),
    _exempt(
        "release discharge-debt",
        "wire_pending",
        "runtime.regime.discharge_debt takes neither anchor yet",
    ),
    _exempt("release tag", "wire_pending", "the tag step takes neither anchor yet"),
    _exempt(
        "release receipts", "wire_pending", "release.produce_receipts takes neither anchor yet"
    ),
    _exempt("release advance", "wire_pending", "release.advance_train takes neither anchor yet"),
    _exempt("campaign new", "wire_pending", "runtime.campaign.start takes neither anchor yet"),
    _exempt("campaign run", "wire_pending", "runtime.campaign.run takes neither anchor yet"),
)


__all__ = [
    "ANCHOR_EXEMPTIONS",
    "PARTIAL_ANCHOR",
    "ROOT_ENTRY_EXCEPTIONS",
    "AnchorExemption",
    "AnchorExemptionKind",
    "RootEntryException",
    "RootEntryKind",
]
