"""``eawf doctor`` Typer command.

Surface contract:

- ``eawf doctor`` runs the W01 check set (tools, state presence, config
  merge), prints a Rich-formatted table, and exits ``0`` when every check
  is ``ok``/``warn`` and ``6`` when a hard probe fails (via
  :class:`eawf.surfaces.cli.errors.UserError` with ``kind="InstrumentMissing"``).
- ``eawf doctor --reprobe`` deletes the on-disk probe cache before re-running
  the probes (forces a fresh ``shutil.which`` round-trip per requirement).
- ``eawf doctor --user-scope`` additionally probes ``uv tool list`` for a
  user-scope eawf install and appends a ``user_scope`` check to the
  envelope. The probe never crashes when ``uv`` is absent — it degrades to
  a ``warn``-status note instead. The flag is additive: when absent, the
  probe does *not* run.
- ``eawf --json doctor`` switches to the canonical JSON envelope:

  .. code-block:: json

      {
        "ok": true,
        "status": "ok",
        "checks": [
          {"name": "tools_available", "status": "ok", "detail": "3 probes ok"},
          ...
        ]
      }

Exit codes:

- ``0`` — every check passed (warnings allowed).
- ``6`` — at least one ``hard`` requirement is missing (probe raised
  :class:`eawf.surfaces.cli.errors.UserError` with ``kind="InstrumentMissing"``).
- ``1`` — any other ``fail`` status (forward-compat; the W01 surface only
  has the probe path that maps to ``6``).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import typer

import eawf
from eawf.platform.install.instrument_probe import resolve_cache_path
from eawf.surfaces.cli.errors import UserError, ValidationError, emit_error
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.workflow.lifecycle.wave_sha import Drift, detect_git_state_drift

if TYPE_CHECKING:
    # Annotation-only; the runtime capability/probe values are imported
    # lazily inside _run_runtime_drift_check so importing this module for
    # completion does not pull eawf.runtime.runtimes.capabilities (and its yaml
    # transitive dep).
    from eawf.runtime.runtimes.capabilities import DriftRow

logger = logging.getLogger(__name__)

# ``uv tool list`` formats each tool as a header line followed by zero or
# more bullet lines:
#
#     eawf v0.1.0
#     - eawf
#     - ea
#
# The header line is the only one we parse — ``^(\S+)\s+v(\S+)$`` captures
# the tool name and its installed version. Bullet lines start with ``- `` and
# are skipped.
_UV_TOOL_LIST_HEADER_RE: re.Pattern[str] = re.compile(r"^(\S+)\s+v(\S+)$")


UserScopeStatus = Literal["ok", "warn", "info"]


@dataclass(frozen=True)
class _UserScopeResult:
    """Stand-alone result for the user-scope probe.

    ``eawf.observability.doctor.checks.CheckResult`` only supports ``ok|warn|fail`` — the
    user-scope probe wants an ``info`` outcome (no eawf installed via uv
    tool, which is not a problem, just an observation). We keep the existing
    ``CheckResult`` shape untouched and emit this local dataclass instead so
    the probe stays additive.
    """

    name: str
    status: UserScopeStatus
    detail: str

    def as_payload(self) -> dict[str, str]:
        """Return a JSON-friendly dict matching the doctor envelope shape."""
        return {"name": self.name, "status": self.status, "detail": self.detail}

    def as_text(self) -> str:
        """Return a one-line ``"<STATUS>  <name>  <detail>"`` rendering."""
        return f"{self.status.upper():<4}  {self.name:<24}  {self.detail}"


def _parse_uv_tool_list(stdout: str) -> dict[str, str]:
    """Return ``{tool_name: version}`` parsed from ``uv tool list`` stdout.

    Lines that do not match the header regex (e.g., the bullet entry lines
    that follow each tool) are ignored. An empty stdout yields ``{}``.
    """
    out: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _UV_TOOL_LIST_HEADER_RE.match(line)
        if match is None:
            continue
        name, version = match.group(1), match.group(2)
        out[name] = version
    return out


def _which_uv() -> str | None:
    """Module-local wrapper around :func:`shutil.which` for ``uv``.

    Wrapping the lookup in a named helper lets tests monkeypatch a single
    function (``eawf.surfaces.cli.commands.doctor._which_uv``) without leaking the
    stub to other call sites that share the ``shutil`` module.
    """
    return shutil.which("uv")


def _run_uv_tool_list() -> subprocess.CompletedProcess[str]:
    """Module-local wrapper around ``uv tool list`` subprocess invocation.

    The wrapper exists for the same reason as :func:`_which_uv`: tests can
    monkeypatch ``eawf.surfaces.cli.commands.doctor._run_uv_tool_list`` to a fake
    that returns a stub :class:`subprocess.CompletedProcess`, without
    intercepting the instrument probe's own ``subprocess.run`` calls.
    """
    return subprocess.run(
        ["uv", "tool", "list"],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_user_scope_check() -> _UserScopeResult:
    """Probe ``uv tool list`` for a user-scope eawf install.

    The function never raises: missing ``uv`` on PATH and subprocess
    failures collapse to a ``warn``-status :class:`_UserScopeResult` so the
    doctor surface stays useful regardless of the operator's setup.

    Outcomes:

    - ``ok`` — ``uv tool list`` reports an ``eawf`` entry whose version
      matches :data:`eawf.__version__`.
    - ``warn`` — ``eawf`` is installed but its version differs from
      :data:`eawf.__version__` (suggests ``uv tool upgrade eawf``); or
      ``uv`` is not on PATH; or ``uv tool list`` failed.
    - ``info`` — ``uv tool list`` ran cleanly but no ``eawf`` entry
      appears (the operator has not installed eawf via ``uv tool`` —
      points them at ``uv tool install --from . eawf``).
    """
    uv_path = _which_uv()
    if uv_path is None:
        return _UserScopeResult(
            name="user_scope",
            status="warn",
            detail="uv not on PATH; cannot probe user-scope install",
        )

    try:
        completed = _run_uv_tool_list()
    except (subprocess.CalledProcessError, OSError) as exc:
        # ``check=False`` already prevents CalledProcessError on non-zero
        # exit; keep it in the except clause for defence in depth in case a
        # future caller flips the flag. ``OSError`` covers permission /
        # ENOENT / EACCES races where ``uv`` disappears between the
        # ``shutil.which`` call and the subprocess spawn.
        return _UserScopeResult(
            name="user_scope",
            status="warn",
            detail=f"uv tool list failed: {exc}",
        )

    if completed.returncode != 0:
        stderr_summary = (completed.stderr or "").strip().splitlines()
        head = stderr_summary[0] if stderr_summary else f"exit {completed.returncode}"
        return _UserScopeResult(
            name="user_scope",
            status="warn",
            detail=f"uv tool list failed: {head}",
        )

    tools = _parse_uv_tool_list(completed.stdout)
    installed_version = tools.get("eawf")
    current_version = eawf.__version__

    if installed_version is None:
        return _UserScopeResult(
            name="user_scope",
            status="info",
            detail=("eawf not installed via uv tool; install with `uv tool install --from . eawf`"),
        )

    if installed_version == current_version:
        return _UserScopeResult(
            name="user_scope",
            status="ok",
            detail=f"user-scope eawf v{installed_version} matches current",
        )

    return _UserScopeResult(
        name="user_scope",
        status="warn",
        detail=(
            f"user-scope eawf v{installed_version} differs from current "
            f"v{current_version} — consider `uv tool upgrade eawf`"
        ),
    )


@dataclass(frozen=True)
class _DriftReconcilerCheck:
    """Stand-alone result for the git/state drift reconciler.

    Surfaced as an additive doctor row so the existing W08 check set
    stays untouched. The shape mirrors
    :class:`_UserScopeResult` (``name`` / ``status`` / ``detail``) plus
    a structured ``drifts`` payload so the JSON consumer can iterate
    the per-wave rows without re-parsing the text detail.
    """

    name: str
    status: Literal["ok", "warn"]
    detail: str
    drifts: list[Drift]

    def as_payload(self) -> dict[str, Any]:
        """Return the JSON-friendly envelope row."""
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "drifts": [
                {
                    "wave_id": d.wave_id,
                    "kind": d.kind,
                    "state_commit": d.state_commit,
                    "git_commit": d.git_commit,
                }
                for d in self.drifts
            ],
        }

    def as_text(self) -> str:
        """Return a single-line summary suitable for the doctor table."""
        return f"{self.status.upper():<4}  {self.name:<24}  {self.detail}"


@dataclass(frozen=True)
class _CrossScopeDupCheck:
    """Stand-alone result for the plugin cross-scope duplication detector.

    Surfaces region_ids that appear in the manifest under both
    ``"project"`` and ``"user"`` scope — the same plugin file emitted
    by both ``eawf plugin install codex`` (project) and ``eawf plugin
    install codex --scope user``, for example. The downstream runtime
    will see two grants with undefined precedence; the doctor warns so
    the operator can pick one and uninstall the other.
    """

    name: str
    status: Literal["ok", "warn"]
    detail: str
    duplicates: list[str]

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "duplicates": list(self.duplicates),
        }

    def as_text(self) -> str:
        return f"{self.status.upper():<4}  {self.name:<24}  {self.detail}"


@dataclass(frozen=True)
class _ProjectRecordCheck:
    """Stand-alone result for the repo project-record presence check."""

    name: str
    status: Literal["ok", "warn"]
    detail: str

    def as_payload(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}

    def as_text(self) -> str:
        return f"{self.status.upper():<4}  {self.name:<24}  {self.detail}"


def _run_git_state_drift_check(workspace: Path | None) -> _DriftReconcilerCheck:
    """Reconcile every CLOSED wave's recorded commit against git history.

    Returns ``ok`` when every closed wave reconciles cleanly; ``warn``
    when at least one ``Wave.commit`` disagrees with what ``git log
    --grep <prefix>`` would surface today. The check degrades to ``ok``
    with an explanatory note when ``state.json`` is absent or
    unparseable — those failure modes are surfaced by other doctor
    rows (``state_present`` / a future ``state_valid`` check) and we
    avoid double-flipping the overall status.
    """
    import json as _json

    from pydantic import ValidationError as _PydanticValidationError

    from eawf.kernel.state.models import State
    from eawf.kernel.state.resolve import resolve_with_reason
    from eawf.workflow.lifecycle.wave_sha import load_drift_acks

    name = "git_state_drift"
    try:
        state_path, _reason = resolve_with_reason(workspace=workspace)
    except FileNotFoundError, ValueError:
        return _DriftReconcilerCheck(name=name, status="ok", detail="no state.json", drifts=[])
    if not state_path.exists():
        return _DriftReconcilerCheck(name=name, status="ok", detail="no state.json", drifts=[])
    try:
        raw = _json.loads(state_path.read_text(encoding="utf-8"))
        state = State.model_validate(raw)
    except _json.JSONDecodeError, _PydanticValidationError:
        return _DriftReconcilerCheck(
            name=name, status="ok", detail="state.json unparseable", drifts=[]
        )

    repo_root = state_path.parent.parent
    acked = load_drift_acks(repo_root)
    drifts = detect_git_state_drift(state, repo_root=repo_root, acked_wave_ids=acked)
    if not drifts:
        closed = sum(1 for w in state.waves.values() if w.status.value == "closed")
        ack_note = f"; {len(acked)} acked" if acked else ""
        return _DriftReconcilerCheck(
            name=name,
            status="ok",
            detail=f"{closed} closed wave(s) reconcile with git{ack_note}",
            drifts=[],
        )
    # Cap surfaced rows in the detail line so the doctor table stays
    # readable; the full list is recoverable via ``--json``.
    summaries = [f"{d.wave_id}={d.kind}" for d in drifts[:5]]
    tail = f" (+{len(drifts) - 5} more)" if len(drifts) > 5 else ""
    return _DriftReconcilerCheck(
        name=name,
        status="warn",
        detail=f"{len(drifts)} drift(s): {', '.join(summaries)}{tail}",
        drifts=drifts,
    )


def _publish_git_state_drift_event(*, workspace: Path | None, drifts: list[Drift]) -> None:
    """Publish a ``git_state_drift_detected`` event to the event store.

    Best-effort: a missing state.json, an absent store path, or a
    locked event log degrades to a debug log line so the doctor surface
    stays useful even when the event store is unwritable. The publish
    routes through :func:`eawf.workflow.skills._common.emit_event`
    (the same writer the skills surface uses), which appends a single
    ``StoreKind.EVENT`` envelope under the same scope as the state
    file. The event_kind is the closed
    :class:`~eawf.kernel.store.kinds.event.EventKind` literal
    ``git_state_drift_detected``; downstream consumers (telemetry
    projector, observability dashboards) discriminate on the kind.
    """
    from eawf.kernel.state.resolve import resolve_with_reason

    try:
        state_path, _reason = resolve_with_reason(workspace=workspace)
    except FileNotFoundError, ValueError:
        logger.debug("_publish_git_state_drift_event status=no-state-path")
        return
    if not state_path.exists():
        logger.debug("_publish_git_state_drift_event status=no-state-file")
        return
    try:
        from eawf.workflow.skills._common import emit_event

        # Build a small structured payload — the per-drift detail rides
        # in ``extras`` since the EventPayload schema does not have a
        # typed slot for drift rows. Keep extras small (≤25 rows + a
        # summary count) so the event row stays bounded.
        sample = ",".join(f"{d.wave_id}:{d.kind}" for d in drifts[:25])
        scope_id = f"urn:eawf:v1:state:{state_path.parent.parent.name}"
        emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="git_state_drift_detected",
            summary=f"{len(drifts)} wave(s) with git/state commit drift",
            payload={
                "event_kind": "git_state_drift_detected",
                "extras": {
                    "drift_count": len(drifts),
                    "sample": sample,
                },
            },
        )
    except Exception as exc:
        logger.debug(f"_publish_git_state_drift_event status=publish-failed error={exc!r}")


def _run_plugin_cross_scope_check(workspace: Path | None) -> _CrossScopeDupCheck:
    """Flag region_ids the manifest has installed under more than one scope.

    Walks ``<workspace>/.ea/indexes/generated.json`` and groups
    :class:`~eawf.surfaces.render.manifest.ManifestEntry` rows by
    ``region_id``. Any region_id present under both ``"project"`` and
    ``"user"`` scope is surfaced as a duplicate — the runtime will see
    two grants with undefined precedence and the operator needs to
    pick one.
    """
    from eawf.surfaces.render.manifest import load as load_manifest

    name = "plugin_cross_scope_dup"
    if workspace is None:
        return _CrossScopeDupCheck(
            name=name,
            status="ok",
            detail="no workspace anchor; nothing to verify",
            duplicates=[],
        )
    manifest_path = Path(workspace) / ".ea" / "indexes" / "generated.json"
    if not manifest_path.exists():
        return _CrossScopeDupCheck(
            name=name,
            status="ok",
            detail="no manifest; nothing to verify",
            duplicates=[],
        )
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        return _CrossScopeDupCheck(
            name=name,
            status="warn",
            detail=f"manifest unreadable: {exc}",
            duplicates=[],
        )

    scopes_by_region: dict[str, set[str]] = {}
    for entry in manifest.generated.values():
        if entry.scope is None:
            continue
        scopes_by_region.setdefault(entry.region_id, set()).add(entry.scope)
    duplicates = sorted(rid for rid, scopes in scopes_by_region.items() if len(scopes) > 1)
    if not duplicates:
        n_scoped = sum(1 for e in manifest.generated.values() if e.scope is not None)
        return _CrossScopeDupCheck(
            name=name,
            status="ok",
            detail=f"{n_scoped} scoped region(s); no cross-scope dup",
            duplicates=[],
        )
    sample = ", ".join(duplicates[:5])
    tail = f" (+{len(duplicates) - 5} more)" if len(duplicates) > 5 else ""
    return _CrossScopeDupCheck(
        name=name,
        status="warn",
        detail=f"{len(duplicates)} region(s) in both project+user: {sample}{tail}",
        duplicates=duplicates,
    )


def _run_project_record_check(workspace: Path | None) -> _ProjectRecordCheck:
    """Verify repo-scoped state carries a materialised ``project`` record."""
    import json as _json

    from pydantic import ValidationError as _PydanticValidationError

    from eawf.kernel.state.models import State
    from eawf.kernel.state.resolve import resolve_with_reason

    name = "project_record_present"
    try:
        state_path, _reason = resolve_with_reason(workspace=workspace)
    except FileNotFoundError, ValueError:
        return _ProjectRecordCheck(name=name, status="ok", detail="no state.json")
    if not state_path.exists():
        return _ProjectRecordCheck(name=name, status="ok", detail="no state.json")
    try:
        raw = _json.loads(state_path.read_text(encoding="utf-8"))
        state = State.model_validate(raw)
    except _json.JSONDecodeError, _PydanticValidationError:
        return _ProjectRecordCheck(name=name, status="ok", detail="state.json unparseable")
    if state.scope_kind.value != "repo":
        return _ProjectRecordCheck(name=name, status="ok", detail="non-repo state")
    if state.project is None:
        return _ProjectRecordCheck(
            name=name,
            status="warn",
            detail="repo state has no project record; run `eawf project init --upgrade`",
        )
    return _ProjectRecordCheck(
        name=name,
        status="ok",
        detail=f"project {state.project.code} present",
    )


def _append_user_scope_check(payload: dict[str, Any], text: str) -> tuple[dict[str, Any], str]:
    """Append the ``user_scope`` row to the doctor envelope.

    Extracted from the doctor callback to keep its cyclomatic complexity
    under the project lint ceiling. The function mutates *payload* in
    place (the checks list is grown by one) and returns it alongside the
    text body with a single-line tail appended.
    """
    user_scope_result = _run_user_scope_check()
    payload["checks"].append(user_scope_result.as_payload())
    # ``info`` does not change ``ok`` / ``status``: a missing user-scope
    # install is informational, not a failure.
    if user_scope_result.status == "warn" and payload["status"] == "ok":
        payload["ok"] = False
        payload["status"] = "warn"
    return payload, f"{text}\n{user_scope_result.as_text()}"


def _append_drift_checks(
    payload: dict[str, Any], text: str, *, workspace: Path | None
) -> tuple[dict[str, Any], str]:
    """Append the git/state drift + plugin cross-scope dup rows.

    Wraps both P28-I02-W01 checks in a single call so the doctor
    callback stays within its cyclomatic budget. When the drift
    reconciler surfaces at least one row, also publishes a
    ``git_state_drift_detected`` event via the closed
    :class:`~eawf.kernel.store.kinds.event.EventKind` literal.
    """
    drift_check = _run_git_state_drift_check(workspace)
    payload["checks"].append(drift_check.as_payload())
    if drift_check.status == "warn" and payload["status"] == "ok":
        payload["ok"] = False
        payload["status"] = "warn"
    text = f"{text}\n{drift_check.as_text()}"
    if drift_check.drifts:
        _publish_git_state_drift_event(workspace=workspace, drifts=drift_check.drifts)

    cross_scope_check = _run_plugin_cross_scope_check(workspace)
    payload["checks"].append(cross_scope_check.as_payload())
    if cross_scope_check.status == "warn" and payload["status"] == "ok":
        payload["ok"] = False
        payload["status"] = "warn"
    text = f"{text}\n{cross_scope_check.as_text()}"
    return payload, text


def _append_project_record_check(
    payload: dict[str, Any], text: str, *, workspace: Path | None
) -> tuple[dict[str, Any], str]:
    """Append the repo project-record check row."""
    check = _run_project_record_check(workspace)
    payload["checks"].append(check.as_payload())
    if check.status == "warn" and payload["status"] == "ok":
        payload["ok"] = False
        payload["status"] = "warn"
    return payload, f"{text}\n{check.as_text()}"


doctor_app = typer.Typer(
    name="doctor",
    help="Run install-readiness checks (tools, state, config).",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _maybe_clear_cache(workspace: Path | None) -> None:
    """Delete the probe cache if it exists, honouring ``EA_INSTRUMENT_PROBE``.

    Targets the per-USER cache home (``~/.eawf/cache/instrument-probe.json``)
    -- the same location :func:`eawf.observability.doctor.checks.run_all`
    probes into -- so ``--reprobe`` clears the cache the next probe will read,
    not a stray copy in an anchor's ``.ea/``.
    """
    from eawf.observability.doctor.checks import (
        _resolve_probe_cache_path as _probe_cache,
    )
    from eawf.observability.doctor.checks import resolve_anchor as _resolve_anchor

    anchor = _resolve_anchor(workspace)
    target = resolve_cache_path(_probe_cache(anchor))
    if target.exists():
        try:
            target.unlink()
            logger.info(f"_maybe_clear_cache removed={target}")
        except OSError as exc:
            logger.warning(f"_maybe_clear_cache cannot-remove path={target} err={exc!r}")


def _drift_row_to_payload(row: DriftRow) -> dict[str, str]:
    """JSON-friendly mapping for a single :class:`DriftRow`."""
    return {
        "capability": row.capability,
        "declared": row.declared,
        "status": row.status,
        "detail": row.detail,
    }


def _run_runtime_drift_check(runtime_id: str) -> tuple[dict[str, Any], str]:
    """Run the capability-matrix drift detector for one runtime.

    Args:
        runtime_id: Canonical runtime id (``claude-code`` / ``codex`` /
            ``opencode``).

    Returns:
        ``(payload, text)`` tuple. ``payload`` is the JSON envelope shape
        emitted via :func:`emit_json_or_text`; ``text`` is the
        column-aligned rendered table the TTY branch prints.

    Raises:
        ValidationError: ``runtime_id`` is not one of the three
            canonical v0.3-v0.5 runtimes.
    """
    from eawf.runtime.runtimes.capabilities import (
        RUNTIME_IDS,
        ProbeResult,
        detect_drift,
        render_drift_table,
    )
    from eawf.runtime.runtimes.probes.sdk_baseline import probe_all

    if runtime_id not in RUNTIME_IDS:
        raise ValidationError(
            f"unknown runtime: {runtime_id!r} (expected one of {list(RUNTIME_IDS)!r})"
        )

    snapshot = probe_all()
    probe_row = next(
        (row for row in snapshot.runtimes if row.runtime_id == runtime_id),
        None,
    )
    if probe_row is None:
        # Snapshot always returns rows for every probed runtime, so this
        # branch is defence-in-depth; the loader-side schema check would
        # catch a missing row at startup.
        raise ValidationError(f"probe snapshot missing row for runtime: {runtime_id!r}")

    probe = ProbeResult(
        runtime_id=probe_row.runtime_id,
        installed=probe_row.installed,
        observed_flags=probe_row.advertised_sdk_flags,
    )
    rows = detect_drift(runtime_id, probe)

    payload: dict[str, Any] = {
        "runtime": runtime_id,
        "installed": probe.installed,
        "drift_rows": [_drift_row_to_payload(row) for row in rows],
        "ok": all(row.status in {"OK", "UNKNOWN", "MISSING"} for row in rows),
    }
    text = render_drift_table(runtime_id, rows)
    logger.info(
        f"_run_runtime_drift_check runtime={runtime_id!r} "
        f"installed={probe.installed} rows={len(rows)}"
    )
    return payload, text


def _repair_plan_text(plan: Any) -> str:
    """Render one concise repair preview shared by prompt and output."""
    from eawf.observability.doctor.repair import render_repair_plan

    return render_repair_plan(plan)


def _append_repair_guidance(
    payload: dict[str, Any],
    text: str,
    *,
    workspace: Path | None,
) -> tuple[dict[str, Any], str]:
    """Add read-only repair availability without prompting or mutating."""
    if workspace is None:
        return payload, text
    from eawf.observability.doctor.repair import build_repair_plan

    try:
        plan = build_repair_plan(workspace)
    except Exception as exc:
        logger.debug(f"_append_repair_guidance status=unavailable err={exc!s}")
        return payload, text
    payload["repair"] = {
        "status": plan.status,
        "action_count": len(plan.actions),
        "unresolved_findings": plan.unresolved_findings,
        "preview_command": plan.rerun_command.removesuffix(" --yes"),
    }
    if not plan.actions:
        return payload, text
    preview_command = plan.rerun_command.removesuffix(" --yes")
    return (
        payload,
        f"{text}\nrepair: {len(plan.actions)} action(s) available\nrun: {preview_command}",
    )


def _run_doctor_repair(flags: GlobalFlags) -> None:
    """Preview or apply the shared Doctor repair plan."""
    from eawf.observability.doctor.checks import resolve_anchor
    from eawf.observability.doctor.repair import build_repair_plan
    from eawf.surfaces.doctor_repair import apply_repair_plan

    anchor = resolve_anchor(flags.workspace)
    if anchor is None:
        emit_error(
            ValidationError("doctor --fix requires an EAWF-managed workspace"),
            flags=flags,
        )
        return
    plan = build_repair_plan(anchor)
    preview_text = _repair_plan_text(plan)
    if not plan.actions:
        emit_json_or_text(
            plan.model_dump(mode="json"),
            preview_text,
            flags=flags,
        )
        return
    if not flags.no_input and not flags.json_output:
        typer.echo(preview_text)
        if not typer.confirm("Apply this repair plan?", default=False):
            typer.echo(f"not applied; rerun: {plan.rerun_command}")
            return
    else:
        plan.status = "needs_user"
        emit_json_or_text(
            plan.model_dump(mode="json"),
            f"{preview_text}\nrerun: {plan.rerun_command}",
            flags=flags,
        )
        return
    try:
        result = apply_repair_plan(plan)
    except Exception as exc:
        emit_error(
            ValidationError(f"doctor repair failed: {exc}"),
            flags=flags,
        )
        return
    emit_json_or_text(
        result,
        f"doctor repair applied: {result['applied_count']} action(s)",
        flags=flags,
    )


def _run_doctor_repair_yes(flags: GlobalFlags) -> None:
    """Apply one Doctor plan without prompting."""
    from eawf.observability.doctor.checks import resolve_anchor
    from eawf.observability.doctor.repair import build_repair_plan
    from eawf.surfaces.doctor_repair import apply_repair_plan

    anchor = resolve_anchor(flags.workspace)
    if anchor is None:
        emit_error(
            ValidationError("doctor --fix requires an EAWF-managed workspace"),
            flags=flags,
        )
        return
    plan = build_repair_plan(anchor)
    if not plan.actions:
        emit_json_or_text(
            plan.model_dump(mode="json"),
            _repair_plan_text(plan),
            flags=flags,
        )
        return
    try:
        result = apply_repair_plan(plan)
    except Exception as exc:
        emit_error(
            ValidationError(f"doctor repair failed: {exc}"),
            flags=flags,
        )
        return
    emit_json_or_text(
        result,
        f"doctor repair applied: {result['applied_count']} action(s)",
        flags=flags,
    )


@doctor_app.callback(invoke_without_command=True)
def doctor(
    ctx: typer.Context,
    reprobe: Annotated[
        bool,
        typer.Option(
            "--reprobe",
            help="Invalidate the cached probe results and re-run every check.",
        ),
    ] = False,
    user_scope: Annotated[
        bool,
        typer.Option(
            "--user-scope",
            help="Probe `uv tool list` for a user-scope eawf install.",
        ),
    ] = False,
    runtime: Annotated[
        str | None,
        typer.Option(
            "--runtime",
            help=(
                "Report capability-matrix drift for one runtime "
                "(claude-code | codex | opencode); bypasses the "
                "install-readiness check set."
            ),
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a machine-readable JSON envelope (overrides root --json).",
        ),
    ] = False,
    fix: Annotated[
        bool,
        typer.Option(
            "--fix",
            help="Preview digest-guarded repairs, then apply after one confirmation.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Apply the Doctor repair preview without an interactive prompt.",
        ),
    ] = False,
) -> None:
    """Run install-readiness checks."""
    from eawf.observability.doctor import checks as doctor_checks
    from eawf.observability.doctor.report import overall_status, to_payload, to_text

    flags: GlobalFlags = ctx.obj
    effective_flags = GlobalFlags(
        json_output=flags.json_output or json_output,
        plain_output=flags.plain_output,
        no_input=flags.no_input,
        workspace=flags.workspace,
    )

    if yes and not fix:
        emit_error(
            ValidationError("--yes requires --fix"),
            flags=effective_flags,
        )
        return

    if fix:
        if yes:
            _run_doctor_repair_yes(effective_flags)
        else:
            _run_doctor_repair(effective_flags)
        return

    if runtime is not None:
        try:
            payload, text = _run_runtime_drift_check(runtime)
        except ValidationError as exc:
            emit_error(exc, flags=effective_flags)
        emit_json_or_text(payload, text, flags=effective_flags)
        return

    if reprobe:
        _maybe_clear_cache(effective_flags.workspace)

    try:
        reserved_config_result = doctor_checks.check_reserved_config_keys(
            workspace=effective_flags.workspace
        )
        results = doctor_checks.run_all(
            workspace=effective_flags.workspace,
            reprobe=reprobe,
            reserved_config_result=reserved_config_result,
        )
    except UserError as exc:
        emit_error(exc, flags=effective_flags)

    # Resolve the ``.ea/`` anchor pwd-upward so a plain ``eawf doctor`` (no
    # ``-w``) verifies THIS repo: the appended drift / cross-scope / project
    # checks key off this anchor instead of the raw ``None`` flag.
    anchor = doctor_checks.resolve_anchor(effective_flags.workspace)

    payload = to_payload(results)
    text = to_text(results, plain=effective_flags.plain_output)

    if user_scope:
        payload, text = _append_user_scope_check(payload, text)

    # P28-I02-W12: init should materialise repo Project; flag legacy state.
    payload, text = _append_project_record_check(payload, text, workspace=anchor)

    # P28-I02-W01: drift reconciler — git/state + plugin cross-scope dup.
    payload, text = _append_drift_checks(payload, text, workspace=anchor)
    payload, text = _append_repair_guidance(payload, text, workspace=anchor)

    emit_json_or_text(payload, text, flags=effective_flags)
    if overall_status(results) == "fail":
        # Defence in depth: ``run_all`` already converts hard probe failures
        # into UserError (kind="InstrumentMissing"). A residual ``fail`` here
        # means a future check produced one without raising — we still want a
        # non-zero exit.
        raise typer.Exit(code=1)
