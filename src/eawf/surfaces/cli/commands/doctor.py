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
from eawf.install.instrument_probe import resolve_cache_path
from eawf.surfaces.cli.errors import UserError, ValidationError, emit_error
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

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

    ``eawf.doctor.checks.CheckResult`` only supports ``ok|warn|fail`` — the
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


doctor_app = typer.Typer(
    name="doctor",
    help="Run install-readiness checks (tools, state, config).",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _maybe_clear_cache(workspace: Path | None) -> None:
    """Delete the probe cache if it exists, honouring ``EA_INSTRUMENT_PROBE``."""
    anchor = workspace if workspace is not None else Path.cwd()
    candidate = anchor / ".ea" / "instrument-probe.json"
    target = resolve_cache_path(candidate)
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
) -> None:
    """Run install-readiness checks."""
    from eawf.doctor import checks as doctor_checks
    from eawf.doctor.report import overall_status, to_payload, to_text

    flags: GlobalFlags = ctx.obj
    effective_flags = GlobalFlags(
        json_output=flags.json_output or json_output,
        plain_output=flags.plain_output,
        no_input=flags.no_input,
        workspace=flags.workspace,
    )

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
        results = doctor_checks.run_all(
            workspace=effective_flags.workspace,
            reprobe=reprobe,
        )
    except UserError as exc:
        emit_error(exc, flags=effective_flags)

    payload = to_payload(results)
    text = to_text(results, plain=effective_flags.plain_output)

    if user_scope:
        user_scope_result = _run_user_scope_check()
        # Append to the JSON envelope (``checks`` list) so structured
        # consumers see the probe alongside the W08 check set.
        payload["checks"].append(user_scope_result.as_payload())
        # ``info`` does not change ``ok`` / ``status``: a missing user-scope
        # install is informational, not a failure.
        if user_scope_result.status == "warn" and payload["status"] == "ok":
            payload["ok"] = False
            payload["status"] = "warn"
        # Append a stable single-line tail to the text body so the operator
        # gets the same datum in TTY output.
        text = f"{text}\n{user_scope_result.as_text()}"

    emit_json_or_text(payload, text, flags=effective_flags)
    if overall_status(results) == "fail":
        # Defence in depth: ``run_all`` already converts hard probe failures
        # into UserError (kind="InstrumentMissing"). A residual ``fail`` here
        # means a future check produced one without raising — we still want a
        # non-zero exit.
        raise typer.Exit(code=1)
