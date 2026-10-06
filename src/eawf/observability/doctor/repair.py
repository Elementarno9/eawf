"""Typed, digest-guarded repair planning for ``eawf doctor --fix``."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import tempfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

import orjson
import yaml
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.layered import get_dotted, global_config_path
from eawf.kernel.config.migration import migrate_config_payload
from eawf.kernel.config.registry.leaf_catalog import DEPRECATED_LEAF_KEYS, LEAF_KEY_REGISTRY
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.enums import AuditKind, IterStatus, StoreKind
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.models import State
from eawf.kernel.store.compaction import LegacySealPlan, plan_legacy_seal
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import GateReceipt, LegacyGateReceipt
from eawf.kernel.store.ledger import (
    LedgerRecord,
    content_digest,
    line_digest,
    split_ledger_lines,
)
from eawf.kernel.store.paths import ledger_path, store_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.artifact_digests import (
    ARTIFACT_DIGEST_ACTION,
    artifact_digest_preview,
    plan_artifact_digests,
    selected_artifact_ledger,
)
from eawf.observability.doctor.checks import (
    check_launchd_agent,
    check_manifest_in_sync,
)
from eawf.observability.doctor.daemon_strays import (
    StrayDaemon,
    describe_stray,
    find_stray_daemons,
)
from eawf.observability.doctor.receipt_rekey import (
    RECEIPT_REKEY_ACTION,
    plan_receipt_rekeys,
    receipt_rekey_preview,
    selected_receipt_ledger,
)
from eawf.platform.lint.ledger_history import (
    UNREACHABLE_KEY,
    LedgerHistoryError,
    UnreachableFinding,
    UnreachableSha,
    scan_ledger_history,
)
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.workflow.lifecycle._audit_acceptance import (
    ITER_CLOSE_AUDIT_CHECK_ORDER,
    assess_close_audit,
)
from eawf.workflow.lifecycle.legacy_audit import (
    disposition_matches,
    load_legacy_audit_dispositions,
)
from eawf.workflow.lifecycle.wave_sha import (
    CommitPinIssue,
    commit_identity_digest,
    scan_commit_pins,
)

DoctorRepairMutationClass = Literal[
    "local_diagnostics",
    "committed_config",
    "committed_state",
    "committed_store",
    "managed_rules",
    "user_service",
    "user_process",
]
DoctorRepairStatus = Literal[
    "planned",
    "applied",
    "noop",
    "skipped",
    "conflict",
    "failed",
]


class DoctorRepairAction(BaseModel):
    """One immutable repair preview row."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    preview_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    mutation_class: DoctorRepairMutationClass
    record_count: int = Field(ge=0)
    restart_required: bool = False
    status: DoctorRepairStatus = "planned"
    detail: str = Field(min_length=1)
    target: str | None = None


class DoctorRepairPlan(BaseModel):
    """Shared CLI/TUI repair plan."""

    model_config = ConfigDict(extra="forbid")

    workspace: str
    preview_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actions: list[DoctorRepairAction] = Field(default_factory=list)
    unresolved_findings: list[str] = Field(default_factory=list)
    status: Literal["ready", "healthy", "needs_user"] = "ready"
    rerun_command: str = "eawf doctor --fix --yes"


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_json(value: object) -> str:
    return _digest_bytes(orjson.dumps(value, option=orjson.OPT_SORT_KEYS))


def digest_repair_actions(actions: list[DoctorRepairAction]) -> str:
    """Return the canonical aggregate digest for ordered repair actions."""
    return _digest_json([action.model_dump(mode="json") for action in actions])


def _current_branch(workspace: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            **no_window_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    branch = result.stdout.strip()
    return branch or None


def _config_paths(workspace: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = [
        ("user", global_config_path()),
        ("workspace", workspace / ".ea" / "config.yaml"),
        ("local", workspace / ".ea" / "local" / "config.yaml"),
    ]
    branch = _current_branch(workspace)
    if branch:
        candidates.append(("branch", workspace / ".ea" / "branches" / f"{branch}.yaml"))
    candidates.extend(
        ("branch", path) for path in sorted((workspace / ".ea" / "branches").glob("**/*.yaml"))
    )
    seen: set[Path] = set()
    result: list[tuple[str, Path]] = []
    for scope, path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        result.append((scope, resolved))
    return result


def _stated_retired_keys(layer: dict[str, object]) -> list[str]:
    """Return the retired keys *layer* still states, sorted, so the repair names them."""
    stated: list[str] = []
    for key in sorted(DEPRECATED_LEAF_KEYS - LEAF_KEY_REGISTRY.keys()):
        try:
            get_dotted(layer, key)
        except KeyError:
            continue
        stated.append(key)
    return stated


def _config_actions(workspace: Path) -> list[DoctorRepairAction]:
    actions: list[DoctorRepairAction] = []
    for index, (scope, path) in enumerate(_config_paths(workspace), start=1):
        raw = path.read_bytes()
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            continue
        _upgraded, changed = migrate_config_payload(parsed)
        if not changed:
            continue
        stragglers = ", ".join(_stated_retired_keys(parsed))
        actions.append(
            DoctorRepairAction(
                action_id=f"config.normalize.{index}",
                scope=scope,
                preview_digest=_digest_bytes(raw),
                mutation_class="committed_config",
                record_count=1,
                detail=(
                    f"normalize deprecated configuration leaves in {scope} layer"
                    + (f": {stragglers}" if stragglers else "")
                ),
                target=str(path),
            )
        )
    return actions


def _receipt_action(workspace: Path, state_path: Path) -> DoctorRepairAction | None:
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    legacy_markers = (
        b'"argv"',
        b'"command"',
        b'"details"',
        b'"stdout_tail"',
        b'"stderr_tail"',
        b'"full_log_ref"',
    )
    if not any(marker in raw for marker in legacy_markers):
        return None
    records = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            envelope = Envelope.model_validate(orjson.loads(line))
        except ValueError:
            continue
        try:
            GateReceipt.model_validate(envelope.payload)
            continue
        except ValueError:
            pass
        try:
            LegacyGateReceipt.model_validate(envelope.payload)
        except ValueError:
            continue
        records += 1
    return DoctorRepairAction(
        action_id="receipts.scrub",
        scope="repo",
        preview_digest=_digest_bytes(raw),
        mutation_class="committed_store",
        record_count=records,
        detail="move raw gate diagnostics local and rewrite minimal receipts",
        target=str(path),
    )


def _invalid_audit_count(state_path: Path, state: State) -> int:
    dispositions = load_legacy_audit_dispositions(state_path)
    invalid = 0
    for iter_id, iter_row in state.iters.items():
        if iter_row.status is not IterStatus.CLOSED:
            continue
        assessment = assess_close_audit(
            state,
            audit_id=iter_row.audit_id,
            allowed_scope_ids=frozenset({iter_id}),
            required_kind=AuditKind.EVALUATION,
            check_order=ITER_CLOSE_AUDIT_CHECK_ORDER,
            require_passing_check=True,
        )
        if assessment.issue is None:
            continue
        if disposition_matches(
            dispositions,
            iter_id=iter_id,
            audit_id=iter_row.audit_id,
            issue=assessment.issue.value,
        ):
            continue
        invalid += 1
    return invalid


def _audit_action(state_path: Path, state: State) -> DoctorRepairAction | None:
    count = _invalid_audit_count(state_path, state)
    if not count:
        return None
    disposition_path = store_path(state_path, StoreKind.LEGACY_AUDIT_DISPOSITION)
    input_bytes = state_path.read_bytes()
    if disposition_path.is_file():
        input_bytes += disposition_path.read_bytes()
    return DoctorRepairAction(
        action_id="audits.acknowledge-legacy",
        scope="repo",
        preview_digest=_digest_bytes(input_bytes),
        mutation_class="committed_store",
        record_count=count,
        detail="append unverified legacy dispositions without changing audit verdicts",
        target=str(disposition_path),
    )


def _pin_action(
    workspace: Path,
    state_path: Path,
    issues: list[CommitPinIssue],
) -> DoctorRepairAction | None:
    repairable = [issue for issue in issues if issue.repairable]
    if not repairable:
        return None
    preview = [
        {
            "wave_id": issue.wave_id,
            "old": issue.state_commit,
            "new": issue.git_commit,
            "identity": issue.git_identity_digest,
            "basis": issue.repair_basis,
        }
        for issue in repairable
    ]
    basis_counts: dict[str, int] = {}
    for issue in repairable:
        basis = issue.repair_basis or "unknown"
        basis_counts[basis] = basis_counts.get(basis, 0) + 1
    basis_summary = ", ".join(f"{basis}={count}" for basis, count in sorted(basis_counts.items()))
    return DoctorRepairAction(
        action_id="commits.repin",
        scope="repo",
        preview_digest=_digest_json(
            {"state": _digest_bytes(state_path.read_bytes()), "repairs": preview}
        ),
        mutation_class="committed_state",
        record_count=len(repairable),
        detail=(f"re-pin uniquely resolved first-parent successors; {basis_summary}"),
        target=str(state_path),
    )


#: The action that supersedes ledger rows binding commits no live branch reaches.
LEDGER_SUPERSEDE_ACTION: Final = "ledger.supersede-unreachable"

#: The branch a ledger binding must descend from; the history gate's CI range starts here.
LEDGER_BASE_REF: Final = "origin/main"

#: Where the repair preserves a commit that no tag keeps alive.
ARCHIVE_REF_PREFIX: Final = "refs/eawf/archive/"


class LedgerSupersession(BaseModel):
    """One superseding row planned for one effective ledger line.

    Attributes:
        path: The ledger file, repo-relative.
        line: The 1-based line the row supersedes.
        supersedes: The digest of that line.
        rebinds: Each unreachable SHA mapped to the one commit on the base
            branch that shares its tree or patch id.
        unreachable: The SHAs with no such commit, declared with the ref
            that preserves each.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    supersedes: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rebinds: dict[str, str] = Field(default_factory=dict)
    unreachable: tuple[UnreachableSha, ...] = ()


def _git_text(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    """Run one read-only git command in *repo* and return its stdout.

    Raises:
        LedgerHistoryError: git exited non-zero.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        check=False,
        **no_window_kwargs(),
    )
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise LedgerHistoryError(f"git {' '.join(args)} failed: {message}")
    return proc.stdout.decode("utf-8", "replace")


def _patch_ids(repo: Path, diff: str) -> dict[str, str]:
    """Return the stable patch id of each commit in *diff*, keyed by commit."""
    out = _git_text(repo, "patch-id", "--stable", stdin=diff.encode("utf-8"))
    return {commit: patch for patch, commit in (row.split() for row in out.splitlines())}


def _is_ancestor(repo: Path, commit: str, of: str) -> bool:
    """Whether *commit* is an ancestor of *of*."""
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, of],
        cwd=repo,
        capture_output=True,
        check=False,
        **no_window_kwargs(),
    )
    return proc.returncode == 0


def _rebind_target(
    repo: Path, sha: str, base: str, trees: dict[str, list[str]]
) -> tuple[str | None, str]:
    """Find the one commit on *base* that landed *sha* under another SHA.

    A squash or an amend keeps the tree, a cherry-pick keeps the patch; only
    commits *base* gained after *sha* forked are candidates. Patch ids are
    computed only for candidates touching exactly the files *sha* touches,
    since hashing every diff of a long history costs tens of seconds.

    Args:
        repo: The repository's working directory.
        sha: The unreachable commit.
        base: The base branch's commit.
        trees: Every commit on *base*, keyed by its tree.

    Returns:
        ``(target, reason)``: the matching commit, or ``None`` with why no
        single commit matched.
    """
    tree = _git_text(repo, "rev-parse", f"{sha}^{{tree}}").strip()
    same_tree = [c for c in trees.get(tree, []) if not _is_ancestor(repo, c, sha)]
    if len(same_tree) == 1:
        return same_tree[0], "same tree"
    if same_tree:
        return None, f"{len(same_tree)} commits on {LEDGER_BASE_REF} share its tree"
    files = set(
        _git_text(repo, "diff-tree", "--root", "-r", "--name-only", "--no-commit-id", sha).split()
    )
    patch = _patch_ids(repo, _git_text(repo, "diff-tree", "--root", "-p", sha)).get(sha)
    if not files or patch is None:
        return None, f"no commit on {LEDGER_BASE_REF} shares its tree and it carries no patch"
    listing = _git_text(
        repo, "log", "--no-merges", "--full-diff", "--name-only", "--format=%x00%H",
        base, f"^{sha}", "--", *sorted(files),
    )  # fmt: skip
    candidates = []
    for block in listing.split("\0")[1:]:
        commit, *touched = block.split()
        if set(touched) == files:
            candidates.append(commit)
    same_patch: list[str] = []
    if candidates:
        diff = _git_text(repo, "log", "--no-walk=unsorted", "-p", "--format=commit %H", *candidates)
        same_patch = [commit for commit, pid in _patch_ids(repo, diff).items() if pid == patch]
    if len(same_patch) == 1:
        return same_patch[0], "same patch id"
    if same_patch:
        return None, f"{len(same_patch)} commits on {LEDGER_BASE_REF} share its patch id"
    return None, f"no commit on {LEDGER_BASE_REF} shares its tree or patch id"


def _archive_ref(repo: Path, sha: str) -> str:
    """Return the ref preserving *sha*: a containing tag, an archive ref, or one to mint."""
    refs = _git_text(
        repo,
        "for-each-ref",
        "--contains",
        sha,
        "--format=%(refname)",
        "refs/tags/",
        ARCHIVE_REF_PREFIX,
    ).split()
    # A tag travels with a push; an archive ref is local to this clone.
    refs.sort(key=lambda ref: (not ref.startswith("refs/tags/"), ref))
    return refs[0] if refs else f"{ARCHIVE_REF_PREFIX}{sha}"


def _grouped(
    findings: Iterable[UnreachableFinding],
) -> tuple[dict[tuple[str, int], list[str]], int]:
    """Group repairable findings by ledger line; count those in non-record files."""
    grouped: dict[tuple[str, int], list[str]] = {}
    unrepairable = 0
    for finding in findings:
        # Only the ledger/ files hold LedgerRecord lines a row can supersede.
        if Path(finding.path).parent.name != "ledger":
            unrepairable += 1
            continue
        grouped.setdefault((finding.path, finding.line), []).append(finding.sha)
    return grouped, unrepairable


def plan_ledger_supersessions(repo: Path) -> tuple[tuple[LedgerSupersession, ...], int]:
    """Plan one superseding row per effective ledger line binding an unreachable commit.

    The working tree's ledgers are scanned against :data:`LEDGER_BASE_REF`
    and ``HEAD``, the same reachability the ledger history gate applies.

    Args:
        repo: The repository's working directory.

    Returns:
        The planned rows in file order, and how many findings sit in files
        whose lines are not ledger records and so cannot be superseded here.

    Raises:
        LedgerHistoryError: The base ref does not resolve or git failed.
    """
    report = scan_ledger_history(repo, LEDGER_BASE_REF)
    grouped, unrepairable = _grouped(report.unreachable)
    trees: dict[str, list[str]] = {}
    if grouped:
        for row in _git_text(repo, "log", "--format=%H %T", report.base).splitlines():
            commit, tree = row.split()
            trees.setdefault(tree, []).append(commit)
    lines_of: dict[str, tuple[str, ...]] = {}
    rows: list[LedgerSupersession] = []
    for (path, line), shas in sorted(grouped.items()):
        if path not in lines_of:
            lines_of[path] = split_ledger_lines((repo / path).read_bytes())
        rebinds: dict[str, str] = {}
        unreachable: list[UnreachableSha] = []
        for sha in shas:
            target, reason = _rebind_target(repo, sha, report.base, trees)
            if target is not None:
                rebinds[sha] = target
            else:
                unreachable.append(
                    UnreachableSha(sha=sha, archive_ref=_archive_ref(repo, sha), reason=reason)
                )
        rows.append(
            LedgerSupersession(
                path=path,
                line=line,
                supersedes=line_digest(lines_of[path][line - 1]),
                rebinds=rebinds,
                unreachable=tuple(unreachable),
            )
        )
    return tuple(rows), unrepairable


def ledger_supersession_digest(repo: Path, rows: Iterable[LedgerSupersession]) -> str:
    """Return the preview digest of *rows* over the ledger bytes they were planned from."""
    planned = list(rows)
    ledgers = {
        path: content_digest((repo / path).read_bytes())
        for path in sorted({r.path for r in planned})
    }
    return _digest_json(
        {"rows": [row.model_dump(mode="json") for row in planned], "ledgers": ledgers}
    )


def _rebound(value: Any, rebinds: dict[str, str], repo: Path) -> Any:
    """Return *value* with every rebound SHA replaced, recursively.

    A record whose ``commit`` moved also gets the identity digest of its new
    commit, since that digest describes the commit the field names.
    """
    if isinstance(value, str):
        for old, new in rebinds.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_rebound(item, rebinds, repo) for item in value]
    if isinstance(value, dict):
        out = {key: _rebound(item, rebinds, repo) for key, item in value.items()}
        commit = out.get("commit")
        if (
            isinstance(commit, str)
            and commit != value.get("commit")
            and value.get("commit_identity_digest") is not None
        ):
            out["commit_identity_digest"] = commit_identity_digest(commit, repo_root=repo)
        return out
    return value


def superseding_record(
    repo: Path, row: LedgerSupersession, *, recorded_at: datetime
) -> LedgerRecord:
    """Build the record that supersedes *row*'s line.

    Args:
        repo: The repository's working directory.
        row: The planned supersession.
        recorded_at: The UTC instant the new row records.

    Returns:
        The superseded record with each rebound SHA replaced and each
        unreachable SHA declared under ``unreachable_shas`` in its payload.

    Raises:
        ValidationError: The line is not a ledger record.
    """
    text = split_ledger_lines((repo / row.path).read_bytes())[row.line - 1]
    record = LedgerRecord.model_validate_json(text)
    payload = _rebound(record.payload, row.rebinds, repo)
    if row.unreachable:
        declared = payload.get(UNREACHABLE_KEY)
        payload[UNREACHABLE_KEY] = [
            *(declared if isinstance(declared, list) else []),
            *(entry.model_dump(mode="json") for entry in row.unreachable),
        ]
    return LedgerRecord(
        collection=record.collection,
        record_key=record.record_key,
        status=record.status,
        recorded_at=recorded_at,
        supersedes=row.supersedes,
        payload=payload,
    )


def _ledger_action(root: Path) -> tuple[DoctorRepairAction | None, int]:
    """Preview the ledger supersession, and count the findings it cannot repair."""
    if not (root / ".ea" / "generations").is_dir():
        return None, 0
    try:
        rows, unrepairable = plan_ledger_supersessions(root)
    except LedgerHistoryError:
        # No base branch to measure reachability against (a fresh or
        # remote-less clone): the gate cannot judge, so nothing is planned.
        return None, 0
    if not rows:
        return None, unrepairable
    rebound = sum(bool(row.rebinds) for row in rows)
    declared = sum(bool(row.unreachable) for row in rows)
    return (
        DoctorRepairAction(
            action_id=LEDGER_SUPERSEDE_ACTION,
            scope="repo",
            preview_digest=ledger_supersession_digest(root, rows),
            mutation_class="committed_store",
            record_count=len(rows),
            detail=(
                f"append {len(rows)} superseding ledger row(s): {rebound} rebound to "
                f"{LEDGER_BASE_REF}, {declared} declaring unreachable_shas"
            ),
            target=str(root),
        ),
        unrepairable,
    )


def _artifact_digest_action(root: Path) -> tuple[DoctorRepairAction | None, int]:
    """Preview the artifact digest rows, and count the cited files absent here."""
    ledger = selected_artifact_ledger(root)
    if ledger is None:
        return None, 0
    plan = plan_artifact_digests(root, ledger)
    if not plan.rows:
        return None, len(plan.missing)
    return (
        DoctorRepairAction(
            action_id=ARTIFACT_DIGEST_ACTION,
            scope="repo",
            preview_digest=artifact_digest_preview(ledger, plan),
            mutation_class="committed_store",
            record_count=len(plan.rows),
            detail=(
                f"append {len(plan.rows)} superseding artifact row(s) carrying the sha256 of "
                f"the cited local file; {len(plan.missing)} cited file(s) absent here"
            ),
            target=str(root),
        ),
        len(plan.missing),
    )


def _receipt_rekey_action(root: Path) -> DoctorRepairAction | None:
    """Preview the re-keyed rows for filed proofs whose stored freshness key is stale."""
    ledger = selected_receipt_ledger(root)
    rows = () if ledger is None else plan_receipt_rekeys(ledger)
    if ledger is None or not rows:
        return None
    return DoctorRepairAction(
        action_id=RECEIPT_REKEY_ACTION,
        scope="repo",
        preview_digest=receipt_rekey_preview(ledger, rows),
        mutation_class="committed_store",
        record_count=len(rows),
        detail=(
            f"append {len(rows)} superseding receipt row(s) carrying the freshness key "
            "their recorded inputs produce"
        ),
        target=str(root),
    )


#: The action that seals the Task ledger's legacy import into a hashed snapshot.
LEGACY_SEAL_ACTION: Final = "ledger.compact-legacy-import"


def legacy_seal_preview(plan: LegacySealPlan) -> str:
    """Return the preview digest of a legacy seal; it covers the sealed bytes' digest."""
    return _digest_json(plan.model_dump(mode="json"))


def _legacy_seal_action(root: Path) -> DoctorRepairAction | None:
    """Preview sealing the selected generation's Task ledger import, if it has one."""
    authority = resolve_authority(root / ".ea")
    if authority.epoch != 2 or authority.target is None or authority.generation_id is None:
        return None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    plan = plan_legacy_seal(ledger_path(document, Epoch2Collection.TASK))
    if plan is None:
        return None
    return DoctorRepairAction(
        action_id=LEGACY_SEAL_ACTION,
        scope="repo",
        preview_digest=legacy_seal_preview(plan),
        mutation_class="committed_store",
        record_count=plan.sealed_lines,
        detail=(
            f"seal the Task ledger's first {plan.sealed_lines} line(s), through its last "
            f"legacy-origin line, into ledger/snapshots/ named by sha256; "
            f"{plan.live_lines} line(s) stay in the live file"
        ),
        target=str(root),
    )


def _ledger_actions(root: Path) -> tuple[list[DoctorRepairAction], list[str]]:
    """Return the ledger repair actions and the findings they leave unresolved."""
    actions: list[DoctorRepairAction] = []
    unresolved: list[str] = []
    for (action, count), finding in (
        (_ledger_action(root), "ledger.unreachable_unrepairable"),
        (_artifact_digest_action(root), "ledger.artifact_file_absent"),
    ):
        if action is not None:
            actions.append(action)
        if count:
            unresolved.append(f"{finding}={count}")
    rekey = _receipt_rekey_action(root)
    if rekey is not None:
        actions.append(rekey)
    # Last, so a row the actions above append is planned before the seal measures
    # its cut; a seal applied first would move the lines they were planned from.
    seal = _legacy_seal_action(root)
    if seal is not None:
        actions.append(seal)
    return actions, unresolved


def _desired_sync_drift(workspace: Path) -> bool:
    """Return whether current config/profile composition would change rules."""
    from eawf.platform.profiles.compose import compose
    from eawf.platform.profiles.loader import load_profile
    from eawf.platform.profiles.selection import resolve_enabled_profiles
    from eawf.platform.rules.render import (
        plan_rule_projections,
        projection_drift,
        rule_source_present,
    )
    from eawf.surfaces.render.agents_md import render_agents_md
    from eawf.surfaces.render.claude_shim import render_claude_md
    from eawf.surfaces.render.manifest import load, save_atomic

    if rule_source_present(workspace):
        return bool(projection_drift(workspace, plan_rule_projections(workspace)))
    enabled = resolve_enabled_profiles(workspace)
    composed = compose([load_profile(profile_id, workspace=workspace) for profile_id in enabled])
    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp)
        for relative in ("AGENTS.md", "CLAUDE.md", ".ea/indexes/generated.json"):
            source = workspace / relative
            if source.is_file():
                target = shadow / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        manifest_path = shadow / ".ea" / "indexes" / "generated.json"
        before = load(manifest_path)
        _result, after = render_agents_md(
            composed,
            shadow / "AGENTS.md",
            before,
            generator="eawf-doctor-preview",
        )
        save_atomic(manifest_path, after)
        render_claude_md(shadow / "CLAUDE.md")
        for relative in ("AGENTS.md", "CLAUDE.md"):
            source = workspace / relative
            target = shadow / relative
            if not source.is_file() or not target.is_file():
                return source.is_file() != target.is_file()
            if source.read_bytes() != target.read_bytes():
                return True
    return False


def _sync_action(workspace: Path) -> DoctorRepairAction | None:
    check = check_manifest_in_sync(workspace=workspace)
    if check.status == "ok" and not _desired_sync_drift(workspace):
        return None
    inputs: list[tuple[str, str | None]] = []
    for relative in (
        "AGENTS.md",
        "CLAUDE.md",
        ".ea/config.yaml",
        ".ea/indexes/generated.json",
        ".ea/state.json",
        ".ea/store/memory.jsonl",
    ):
        path = workspace / relative
        inputs.append((relative, _digest_bytes(path.read_bytes()) if path.is_file() else None))
    for scope, path in _config_paths(workspace):
        inputs.append(
            (
                f"config:{scope}:{path.name}",
                _digest_bytes(path.read_bytes()),
            )
        )
    from eawf.platform.profiles.discovery import (
        user_profiles_dir,
        workspace_profiles_dir,
    )

    for scope, root in (
        ("profile:user", user_profiles_dir()),
        ("profile:workspace", workspace_profiles_dir(workspace)),
    ):
        for path in sorted(root.glob("*.yaml")):
            inputs.append((f"{scope}:{path.name}", _digest_bytes(path.read_bytes())))
    return DoctorRepairAction(
        action_id="lifecycle.sync",
        scope="repo",
        preview_digest=_digest_json(inputs),
        mutation_class="managed_rules",
        record_count=1,
        detail="re-render managed lifecycle blocks and profile overlays",
        target=str(workspace),
    )


def _service_action() -> DoctorRepairAction | None:
    check = check_launchd_agent()
    if check.status == "ok":
        return None
    return DoctorRepairAction(
        action_id="daemon.rerender-restart",
        scope="user",
        preview_digest=_digest_json(
            {"name": check.name, "status": check.status, "detail": check.detail}
        ),
        mutation_class="user_service",
        record_count=1,
        restart_required=True,
        detail="re-render and restart stale daemon service definition",
    )


def digest_strays(strays: list[StrayDaemon]) -> str:
    """Return the preview digest of a stray-daemon listing."""
    return _digest_json([stray.model_dump(mode="json") for stray in strays])


def _stray_action() -> DoctorRepairAction | None:
    strays = find_stray_daemons()
    if not strays:
        return None
    return DoctorRepairAction(
        action_id="daemon.stop-strays",
        scope="user",
        preview_digest=digest_strays(strays),
        mutation_class="user_process",
        record_count=len(strays),
        detail="stop eawfd processes no client can reach: "
        + "; ".join(describe_stray(stray) for stray in strays),
    )


def build_repair_plan(workspace: Path) -> DoctorRepairPlan:
    """Build the shared read-only repair preview for one managed workspace."""
    root = workspace.resolve()
    state_path = root / ".ea" / "state.json"
    actions = _config_actions(root)
    unresolved: list[str] = []
    if state_path.is_file():
        state = State.model_validate_json(state_path.read_bytes())
        # An epoch-2 tree froze state.json at the cutover, so its wave
        # pins can no longer drift and re-pinning them would rewrite a
        # frozen file.
        pin_issues = (
            []
            if resolve_authority(state_path.parent).epoch == 2
            else scan_commit_pins(state, repo_root=root)
        )
        unresolved_pins = [issue for issue in pin_issues if not issue.repairable]
        if unresolved_pins:
            unresolved.append(f"commits.unresolved={len(unresolved_pins)}")
        for action in (
            _receipt_action(root, state_path),
            _audit_action(state_path, state),
            _pin_action(root, state_path, pin_issues),
        ):
            if action is not None:
                actions.append(action)
    ledger_actions, ledger_findings = _ledger_actions(root)
    actions.extend(ledger_actions)
    unresolved.extend(ledger_findings)
    for action in (_sync_action(root), _service_action(), _stray_action()):
        if action is not None:
            actions.append(action)
    digest = digest_repair_actions(actions)
    status: Literal["ready", "healthy", "needs_user"]
    if actions:
        status = "ready"
    else:
        from eawf.observability.doctor.checks import run_all
        from eawf.observability.doctor.report import overall_status

        status = (
            "healthy"
            if not unresolved and overall_status(run_all(workspace=root)) == "ok"
            else "needs_user"
        )
    return DoctorRepairPlan(
        workspace=str(root),
        preview_digest=digest,
        actions=actions,
        unresolved_findings=unresolved,
        status=status,
        rerun_command=(f"eawf --workspace {shlex.quote(str(root))} doctor --fix --yes"),
    )


def render_repair_plan(plan: DoctorRepairPlan) -> str:
    """Render the shared CLI/TUI repair preview."""
    if not plan.actions:
        if plan.status == "healthy":
            return "doctor repair: no actions needed; Doctor is healthy"
        detail = ", ".join(plan.unresolved_findings) or "manual diagnosis required"
        return f"doctor repair: no automatic actions available; {detail}"
    lines = [f"doctor repair preview: {len(plan.actions)} action(s)"]
    for action in plan.actions:
        restart = " · restart required" if action.restart_required else ""
        lines.append(
            f"{action.action_id} · {action.scope} · "
            f"{action.mutation_class} · {action.record_count} record(s){restart}"
        )
        lines.append(f"  {action.detail}")
    return "\n".join(lines)


__all__ = [
    "ARCHIVE_REF_PREFIX",
    "LEDGER_BASE_REF",
    "LEDGER_SUPERSEDE_ACTION",
    "LEGACY_SEAL_ACTION",
    "DoctorRepairAction",
    "DoctorRepairMutationClass",
    "DoctorRepairPlan",
    "DoctorRepairStatus",
    "LedgerSupersession",
    "build_repair_plan",
    "digest_repair_actions",
    "digest_strays",
    "ledger_supersession_digest",
    "legacy_seal_preview",
    "plan_ledger_supersessions",
    "render_repair_plan",
    "superseding_record",
]
