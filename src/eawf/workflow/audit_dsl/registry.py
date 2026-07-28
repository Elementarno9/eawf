"""Check-kind registry for the audit-check DSL (B019).

Each check kind is a small pure callable ``(spec, cwd) -> CheckResult``.
The runner dispatches via :data:`CHECK_REGISTRY`; Pydantic guards the
input so an unknown kind cannot reach the dispatch table.

Sandbox-policy boundary
-----------------------

:func:`_check_command_exit_zero` shells out via :func:`subprocess.run`.
The DSL runner does NOT enforce the sandbox/permission policy table
in v0.2 — callers (the ``audit run`` command, CI driver, etc.) are
responsible for invoking ``eawf wave policy show`` and refusing
disallowed argv. Tracked as backlog item B074 for v0.4 hardening
via :func:`eawf.runtime.sandbox.argv_policy.validate_gate_argv`. See
``docs/architecture/audit-checks.md`` for the full boundary
discussion.

W15 hardening
-------------

:func:`_check_command_exit_zero` learns three new behaviours so the
W08 compile-gate + W10 profile-floor packs can lean on the runner:

* A ``timeout_class`` kwarg maps to a concrete
  ``subprocess.run(..., timeout=...)`` budget via
  :data:`_TIMEOUT_CLASS_SECONDS`. A :class:`subprocess.TimeoutExpired`
  becomes ``CheckResult(status="blocked", passed=False)`` rather than
  an uncaught exception.
* A ``scope`` kwarg picks ``changed`` / ``touched`` / ``all`` and
  resolves the file set via
  :func:`eawf.platform.lint._conditional.changed_files` +
  ``wave.file_scopes``. The resolved set is exported to the child
  process through the ``EAWF_GATE_FILES`` env var, newline-separated
  (POSIX env vars cannot carry NUL bytes — the original W15 spec said
  "NUL-separated" but ``os.execve(2)`` rejects NUL in env-value bytes,
  so newline is the canonical fallback that still survives every path
  the repo actually contains). The var is set even when the resolved
  set is empty so the child can distinguish "no scope evaluation"
  from "scope evaluated to empty".
* The ``diff_base`` for ``changed``/``touched`` is derived from
  ``derive_diff_base(wave_id)`` so wave-anchored gates compare against
  the wave's own delta; the merge-base-with-main fallback keeps the
  fresh-clone / CI path fail-open.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eawf.platform.artifacts.references import (
    Citation,
    CitationValidationError,
    citation_numbers_in_text,
    validate_dense_citation_refs,
)
from eawf.platform.lint._conditional import changed_files
from eawf.workflow.audit_dsl.kinds.affordance_parity import check_affordance_parity
from eawf.workflow.audit_dsl.kinds.backlog_resolution import BACKLOG_RESOLUTION_KIND
from eawf.workflow.audit_dsl.kinds.criterion_in_diff import check_criterion_in_diff
from eawf.workflow.audit_dsl.kinds.journal_chain import check_journal_chain
from eawf.workflow.audit_dsl.kinds.jury_calibrated import JURY_CALIBRATED_KIND
from eawf.workflow.audit_dsl.kinds.schema_validate import check_schema_validate
from eawf.workflow.audit_dsl.kinds.svg_pixel_diff import check_svg_pixel_diff
from eawf.workflow.audit_dsl.kinds.svg_well_formed import check_svg_well_formed
from eawf.workflow.audit_dsl.kinds.transition_coverage import check_transition_coverage
from eawf.workflow.audit_dsl.kinds.tui_flow import check_tui_flow
from eawf.workflow.audit_dsl.kinds.verify_implements import check_verify_implements
from eawf.workflow.audit_dsl.models import (
    OUTPUT_TAIL_MAX_CHARS,
    CheckResult,
    CheckSpec,
    CitationResolvesArgs,
    CommandExitZeroArgs,
    MockupGoldenDiffArgs,
    Scope,
    TimeoutClass,
)
from eawf.workflow.lifecycle.wave_sha import derive_diff_base

logger = logging.getLogger(__name__)

CheckFn = Callable[[CheckSpec, Path], CheckResult]
BeforeGateExecute = Callable[[CheckSpec, str], CheckResult | None]
# Backward-compatible import name for callers compiled before all deterministic
# kinds gained the same durable pre-execution claim boundary.
BeforeCommandExecute = BeforeGateExecute


#: Mapping from :data:`~eawf.workflow.audit_dsl.models.TimeoutClass`
#: literal to concrete ``subprocess.run(..., timeout=...)`` budget in
#: seconds. The class names match the W08 compile-gate vocabulary so
#: YAML authors pick a budget by intent (a fast linter run vs. a
#: long-running pytest sweep) rather than guessing a number.
_TIMEOUT_CLASS_SECONDS: dict[TimeoutClass, int] = {
    "quick": 60,
    "standard": 300,
    "slow": 900,
    "very_slow": 3600,
}

#: Maximum characters retained from each subprocess output stream. Full
#: output is represented by its digest and, when a caller supplies one, a
#: durable log reference.
_OUTPUT_TAIL_CHARS: int = OUTPUT_TAIL_MAX_CHARS

#: Env-var name the runner sets on the child process to publish the
#: resolved file set. Newline-separated (POSIX env vars cannot carry
#: NUL bytes — execve(2) rejects them — and every other audit-DSL test
#: path is whitespace-free, so newline is the safe canonical separator).
#: Set even when empty so callers can tell "scope=all (no filter)" from
#: "scope=changed, nothing changed".
_GATE_FILES_ENV: str = "EAWF_GATE_FILES"

#: Joiner used inside :data:`_GATE_FILES_ENV`. Newline-not-NUL — see
#: :data:`_GATE_FILES_ENV` for the OS-level constraint.
_GATE_FILES_SEPARATOR: str = "\n"

_SECTION_HEADING_RE = re.compile(r"^## (?P<title>[^\n#]+)\s*$", re.MULTILINE)
_REFERENCE_ROW_RE = re.compile(r"^\[(?P<n>[1-9][0-9]*)\]\s+(?P<ref>\S+)")


def _require_str(args: dict[str, Any], key: str, *, name: str, kind: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ValueError(f"check {name!r} kind={kind}: missing or non-str arg {key!r}")
    return value


def _resolve_dotpath(payload: Any, dotpath: str) -> Any:
    """Resolve a dot-separated path against a parsed JSON-ish payload.

    Raises:
        KeyError: When a segment is missing from a mapping.
        TypeError: When traversal hits a non-mapping intermediate value.
    """
    cur: Any = payload
    for seg in dotpath.split("."):
        if not isinstance(cur, dict):
            raise TypeError(f"dot-path segment {seg!r} hit non-dict node ({type(cur).__name__})")
        if seg not in cur:
            raise KeyError(seg)
        cur = cur[seg]
    return cur


def _check_file_exists(spec: CheckSpec, cwd: Path) -> CheckResult:
    path_arg = _require_str(spec.args, "path", name=spec.name, kind=spec.kind)
    target = (cwd / path_arg).resolve() if not Path(path_arg).is_absolute() else Path(path_arg)
    passed = target.is_file()
    details = f"path={path_arg} exists={passed}"
    return CheckResult(name=spec.name, kind=spec.kind, passed=passed, details=details)


def _check_path_glob_nonempty(spec: CheckSpec, cwd: Path) -> CheckResult:
    pattern = _require_str(spec.args, "pattern", name=spec.name, kind=spec.kind)
    matches = list(cwd.glob(pattern))
    passed = len(matches) >= 1
    details = f"pattern={pattern} matches={len(matches)}"
    return CheckResult(name=spec.name, kind=spec.kind, passed=passed, details=details)


def _check_regex_in_file(spec: CheckSpec, cwd: Path) -> CheckResult:
    path_arg = _require_str(spec.args, "path", name=spec.name, kind=spec.kind)
    pattern = _require_str(spec.args, "pattern", name=spec.name, kind=spec.kind)
    target = (cwd / path_arg).resolve() if not Path(path_arg).is_absolute() else Path(path_arg)
    if not target.is_file():
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details=f"path={path_arg} not found",
        )
    body = target.read_text(encoding="utf-8")
    match = re.search(pattern, body)
    passed = match is not None
    details = f"path={path_arg} pattern={pattern} match={passed}"
    return CheckResult(name=spec.name, kind=spec.kind, passed=passed, details=details)


def _check_state_field_equals(spec: CheckSpec, cwd: Path) -> CheckResult:
    field = _require_str(spec.args, "field", name=spec.name, kind=spec.kind)
    if "value" not in spec.args:
        raise ValueError(
            f"check {spec.name!r} kind=state_field_equals: missing required arg 'value'"
        )
    expected = spec.args["value"]
    state_path = spec.args.get("state_path", ".ea/state.json")
    if not isinstance(state_path, str):
        raise ValueError(f"check {spec.name!r} kind=state_field_equals: non-str arg 'state_path'")
    target = (
        (cwd / state_path).resolve() if not Path(state_path).is_absolute() else Path(state_path)
    )
    if not target.is_file():
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details=f"state_path={state_path} not found",
        )
    try:
        parsed = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details=f"state_path={state_path} not valid JSON: {exc.msg}",
        )
    try:
        actual = _resolve_dotpath(parsed, field)
    except (KeyError, TypeError) as exc:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details=f"field={field} unreachable ({exc.__class__.__name__})",
        )
    passed = actual == expected
    details = f"field={field} expected={expected!r} actual={actual!r}"
    return CheckResult(name=spec.name, kind=spec.kind, passed=passed, details=details)


def _prose_without_references_section(text: str) -> str:
    matches = list(_SECTION_HEADING_RE.finditer(text))
    if not matches:
        return text
    chunks: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        heading = f"## {match.group('title').strip()}"
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if heading == "## References":
            chunks.append(text[cursor:start])
            cursor = end
    chunks.append(text[cursor:])
    return "".join(chunks)


def _citation_rows_from_markdown(text: str) -> list[Citation]:
    rows: list[Citation] = []
    in_references = False
    for line in text.splitlines():
        heading = _SECTION_HEADING_RE.match(line.strip())
        if heading is not None:
            in_references = heading.group("title").strip() == "References"
            continue
        if not in_references:
            continue
        match = _REFERENCE_ROW_RE.match(line.strip())
        if match is None:
            continue
        rows.append(Citation.from_legacy_source(int(match.group("n")), match.group("ref")))
    return rows


def _check_citation_resolves(spec: CheckSpec, cwd: Path) -> CheckResult:
    try:
        args = CitationResolvesArgs.model_validate(spec.args)
    except ValidationError as exc:
        raise ValueError(
            f"check {spec.name!r} kind=citation_resolves: invalid args: {exc}"
        ) from exc

    path_arg = args.path
    if path_arg is not None:
        target = (cwd / path_arg).resolve() if not Path(path_arg).is_absolute() else Path(path_arg)
        if not target.is_file():
            return CheckResult(
                name=spec.name,
                kind=spec.kind,
                passed=False,
                details=f"path={path_arg} not found",
            )
        text = target.read_text(encoding="utf-8")
        source = f"path={path_arg}"
    else:
        text = args.text or ""
        source = "text=<inline>"

    try:
        references = (
            args.references if args.references is not None else _citation_rows_from_markdown(text)
        )
        validate_dense_citation_refs(_prose_without_references_section(text), references)
    except (CitationValidationError, ValueError) as exc:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details=f"{source} {exc}",
        )

    used = citation_numbers_in_text(_prose_without_references_section(text))
    details = f"{source} citations={len(set(used))} references={len(references)}"
    return CheckResult(name=spec.name, kind=spec.kind, passed=True, details=details)


def _resolve_scope_files(
    *,
    scope: Scope,
    wave_file_scopes: list[str],
    diff_base: str,
    cwd: Path,
) -> list[str]:
    """Resolve the scope literal to a sorted list of repo-relative paths.

    * ``all`` — empty list (the runner publishes ``EAWF_GATE_FILES=""``
      and the child gate is expected to walk the tree itself).
    * ``changed`` — :func:`changed_files` on *diff_base*.
    * ``touched`` — :func:`changed_files` union *wave_file_scopes*.
    """
    if scope == "all":
        return []
    changed = changed_files(diff_base, cwd=cwd)
    if scope == "changed":
        return sorted(set(changed))
    # touched
    return sorted(set(changed) | set(wave_file_scopes))


def _output_text(value: str | bytes | None) -> str:
    """Normalise subprocess output into text without dropping undecodable bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _sha256_text(value: str) -> str:
    """Return a lowercase SHA-256 digest for UTF-8 text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_output(value: str | bytes | None) -> str:
    """Digest exact captured output bytes when subprocess supplies them."""
    if value is None:
        raw = b""
    elif isinstance(value, bytes):
        raw = value
    else:
        raw = value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _bounded_tail(value: str) -> str:
    """Return at most the final :data:`_OUTPUT_TAIL_CHARS` characters."""
    return value[-_OUTPUT_TAIL_CHARS:]


def _selected_file_digest(selected_files: list[str]) -> str:
    """Digest the canonical sorted selected-file manifest."""
    manifest = "\n".join(sorted(selected_files))
    return _sha256_text(manifest)


def _declared_manifest_digest(
    cwd: Path,
    value: str | None,
    *,
    expected_digest: str | None,
    arg_name: str,
) -> tuple[str | None, str | None]:
    """Digest one declared artifact and compare it to its explicit baseline."""
    if value is None:
        return None, None
    target = (cwd / value).resolve()
    try:
        target.relative_to(cwd.resolve())
    except ValueError:
        return None, f"{arg_name}={value!r} escapes repository root"
    if not target.is_file():
        return None, f"{arg_name}={value!r} not found after gate execution"
    actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if expected_digest is not None and actual_digest != expected_digest:
        return (
            actual_digest,
            f"{arg_name} digest mismatch expected={expected_digest} actual={actual_digest}",
        )
    return actual_digest, None


def _runner_fingerprint() -> str | None:
    """Digest registry, dispatcher, and typed execution-contract sources."""
    try:
        root = Path(__file__).parent
        rows = [
            {
                "name": name,
                "digest": hashlib.sha256((root / name).read_bytes()).hexdigest(),
            }
            for name in ("models.py", "registry.py", "runner.py")
        ]
        return _sha256_text(json.dumps(rows, sort_keys=True, separators=(",", ":")))
    except OSError:
        return None


def _environment_fingerprint() -> str | None:
    """Digest non-identifying execution-environment facts.

    Hostnames, paths, environment variables, and user identity are
    intentionally excluded.
    """
    try:
        facts = {
            "machine": platform.machine(),
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "system": platform.system(),
            "system_release": platform.release(),
        }
        encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    except OSError:
        logger.exception("_environment_fingerprint status=unavailable")
        return None
    return _sha256_text(encoded)


def _freshness_key(
    spec: CheckSpec,
    *,
    resolved_timeout_seconds: int | None,
    selected_file_digest: str | None,
    runner_fingerprint: str | None,
    environment_fingerprint: str | None,
) -> str:
    """Digest complete execution identity plus caller-supplied freshness facts."""
    payload = {
        "environment_fingerprint": environment_fingerprint,
        "resolved_timeout_seconds": resolved_timeout_seconds,
        "runner_fingerprint": runner_fingerprint,
        "selected_file_digest": selected_file_digest,
        "spec": spec.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def _execution_fingerprints(spec: CheckSpec) -> tuple[str | None, str | None]:
    """Resolve runner and environment fingerprints for one typed check."""
    supplied = spec.freshness
    runner_fingerprint = (
        supplied.runner_fingerprint
        if supplied is not None and supplied.runner_fingerprint is not None
        else _runner_fingerprint()
    )
    environment_fingerprint = (
        supplied.environment_fingerprint
        if supplied is not None and supplied.environment_fingerprint is not None
        else _environment_fingerprint()
    )
    return runner_fingerprint, environment_fingerprint


def _check_with_durable_instrumentation(
    spec: CheckSpec,
    cwd: Path,
    *,
    before_execute: BeforeGateExecute,
) -> CheckResult:
    """Claim, execute, time, fingerprint, and prove any non-command check."""
    runner_fingerprint, environment_fingerprint = _execution_fingerprints(spec)
    freshness_key = _freshness_key(
        spec,
        resolved_timeout_seconds=None,
        selected_file_digest=None,
        runner_fingerprint=runner_fingerprint,
        environment_fingerprint=environment_fingerprint,
    )
    claimed_result = before_execute(spec, freshness_key)
    if claimed_result is not None:
        return claimed_result

    started_at = datetime.now(UTC)
    started_ns = time.perf_counter_ns()
    result = CHECK_REGISTRY[spec.kind](spec, cwd)
    ended_at = datetime.now(UTC)
    duration_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
    supplied = spec.freshness
    full_log_ref = (
        supplied.full_log_ref
        if supplied is not None and supplied.full_log_ref is not None
        else f".ea/local/gate-proofs/{freshness_key}.log"
    )
    enriched = result.model_copy(
        update={
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "runner_fingerprint": runner_fingerprint,
            "environment_fingerprint": environment_fingerprint,
            "full_log_ref": full_log_ref,
            "freshness_key": freshness_key,
            "freshness": supplied,
        }
    )
    _write_full_log(
        cwd=cwd,
        full_log_ref=full_log_ref,
        argv=[],
        stdout=enriched.details or "",
        stderr="",
    )
    return enriched


def _failure_details(
    prefix: str,
    *,
    stdout_tail: str,
    stderr_tail: str,
    full_log_ref: str | None,
) -> str:
    """Render bounded captured output and an honest full-log reference."""
    log_ref = full_log_ref if full_log_ref is not None else "unavailable"
    prefix_rendered = prefix if len(prefix) <= 400 else f"{prefix[:397]}..."

    def _tail_repr(value: str, *, limit: int) -> str:
        rendered = repr(value)
        if len(rendered) <= limit:
            return rendered
        return f"...{rendered[-(limit - 3) :]}"

    return (
        f"{prefix_rendered} stdout_tail={_tail_repr(stdout_tail, limit=512)} "
        f"stderr_tail={_tail_repr(stderr_tail, limit=512)} "
        f"full_log_ref={_tail_repr(log_ref, limit=502)}"
    )


def _command_result(
    spec: CheckSpec,
    *,
    argv: list[str],
    passed: bool,
    status: str,
    detail_prefix: str,
    started_at: datetime,
    started_ns: int,
    timeout_class: TimeoutClass,
    resolved_timeout_seconds: int,
    exit_status: int | None,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    selected_file_digest: str,
    runner_fingerprint: str | None,
    environment_fingerprint: str | None,
    freshness_key: str,
    collected_nodeid_digest: str | None = None,
    residual_manifest_digest: str | None = None,
) -> CheckResult:
    """Build one evidence-rich result from a completed process attempt."""
    ended_at = datetime.now(UTC)
    duration_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
    stdout_text = _output_text(stdout)
    stderr_text = _output_text(stderr)
    stdout_tail = _bounded_tail(stdout_text)
    stderr_tail = _bounded_tail(stderr_text)
    freshness = spec.freshness
    full_log_ref = freshness.full_log_ref if freshness is not None else None
    details = detail_prefix
    if not passed:
        details = _failure_details(
            detail_prefix,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            full_log_ref=full_log_ref,
        )
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=passed,
        status=status,  # type: ignore[arg-type]
        details=details,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        timeout_class=timeout_class,
        resolved_timeout_seconds=resolved_timeout_seconds,
        exit_status=exit_status,
        argv=argv,
        command=shlex.join(argv),
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        stdout_digest=_sha256_output(stdout),
        stderr_digest=_sha256_output(stderr),
        selected_file_digest=selected_file_digest,
        collected_nodeid_digest=(
            collected_nodeid_digest
            if collected_nodeid_digest is not None
            else (freshness.collected_nodeid_digest if freshness is not None else None)
        ),
        residual_manifest_digest=(
            residual_manifest_digest
            if residual_manifest_digest is not None
            else (freshness.residual_manifest_digest if freshness is not None else None)
        ),
        runner_fingerprint=runner_fingerprint,
        environment_fingerprint=environment_fingerprint,
        full_log_ref=full_log_ref,
        freshness_key=freshness_key,
        freshness=freshness,
    )


def _write_full_log(
    *,
    cwd: Path,
    full_log_ref: str | None,
    argv: list[str],
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> None:
    """Persist exact captured subprocess output when a safe sink is supplied."""
    if full_log_ref is None:
        return
    relative = Path(full_log_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"full_log_ref must be repo-relative: {full_log_ref!r}")
    target = (cwd / relative).resolve()
    try:
        target.relative_to(cwd.resolve())
    except ValueError as exc:
        raise ValueError(f"full_log_ref escapes repository root: {full_log_ref!r}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        f"argv={shlex.join(argv)}\n"
        f"--- stdout ---\n{_output_text(stdout)}\n"
        f"--- stderr ---\n{_output_text(stderr)}\n"
    )
    target.write_text(payload, encoding="utf-8")


def _check_command_exit_zero(
    spec: CheckSpec,
    cwd: Path,
    *,
    before_execute: BeforeGateExecute | None = None,
) -> CheckResult:
    """Run an argv and report exit-zero / non-zero / blocked.

    The W15 hardening pass validates kwargs via :class:`CommandExitZeroArgs`,
    derives the ``subprocess.run`` timeout from
    :data:`_TIMEOUT_CLASS_SECONDS`, resolves the requested file scope,
    publishes the resolved set through :data:`_GATE_FILES_ENV`, and
    maps :class:`subprocess.TimeoutExpired` to
    ``CheckResult(status="blocked", passed=False)``.
    """
    try:
        args = CommandExitZeroArgs.model_validate(spec.args)
    except ValidationError as exc:
        raise ValueError(
            f"check {spec.name!r} kind=command_exit_zero: invalid args: {exc}"
        ) from exc
    argv = list(args.argv)
    timeout_class = args.timeout_class
    seconds = (
        args.timeout_s if args.timeout_s is not None else _TIMEOUT_CLASS_SECONDS[timeout_class]
    )

    diff_base = "all" if args.scope == "all" else derive_diff_base(args.wave_id, repo_root=cwd)
    selected_files = _resolve_scope_files(
        scope=args.scope,
        wave_file_scopes=args.wave_file_scopes,
        diff_base=diff_base,
        cwd=cwd,
    )
    child_env = {**os.environ, _GATE_FILES_ENV: _GATE_FILES_SEPARATOR.join(selected_files)}
    selected_digest = _selected_file_digest(selected_files)
    supplied_freshness = spec.freshness
    runner_fingerprint, environment_fingerprint = _execution_fingerprints(spec)
    freshness_key = _freshness_key(
        spec,
        resolved_timeout_seconds=seconds,
        selected_file_digest=selected_digest,
        runner_fingerprint=runner_fingerprint,
        environment_fingerprint=environment_fingerprint,
    )
    if before_execute is not None:
        claimed_result = before_execute(spec, freshness_key)
        if claimed_result is not None:
            return claimed_result

    logger.debug(
        f"_check_command_exit_zero gate_id={spec.name!r} timeout_class={timeout_class!r} "
        f"timeout_s={seconds} scope={args.scope!r} diff_base={diff_base!r} "
        f"files={len(selected_files)}"
    )

    # Sandbox-policy enforcement is the caller's responsibility in v0.2;
    # see docs/architecture/audit-checks.md (B074 follow-up).
    started_at = datetime.now(UTC)
    started_ns = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            argv,
            timeout=seconds,
            check=False,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        _write_full_log(
            cwd=cwd,
            full_log_ref=(
                supplied_freshness.full_log_ref if supplied_freshness is not None else None
            ),
            argv=argv,
            stdout=exc.stdout,
            stderr=exc.stderr,
        )
        detail = f"timeout after {exc.timeout}s (class={timeout_class!r})"
        logger.info(
            f"_check_command_exit_zero gate_id={spec.name!r} status=blocked "
            f"timeout_class={timeout_class!r}"
        )
        return _command_result(
            spec,
            argv=argv,
            passed=False,
            status="blocked",
            detail_prefix=detail,
            started_at=started_at,
            started_ns=started_ns,
            timeout_class=timeout_class,
            resolved_timeout_seconds=seconds,
            exit_status=None,
            stdout=exc.stdout,
            stderr=exc.stderr,
            selected_file_digest=selected_digest,
            runner_fingerprint=runner_fingerprint,
            environment_fingerprint=environment_fingerprint,
            freshness_key=freshness_key,
        )
    except FileNotFoundError as exc:
        _write_full_log(
            cwd=cwd,
            full_log_ref=(
                supplied_freshness.full_log_ref if supplied_freshness is not None else None
            ),
            argv=argv,
            stdout="",
            stderr=str(exc),
        )
        return _command_result(
            spec,
            argv=argv,
            passed=False,
            status="fail",
            detail_prefix=f"argv={argv} not executable: {exc.strerror}",
            started_at=started_at,
            started_ns=started_ns,
            timeout_class=timeout_class,
            resolved_timeout_seconds=seconds,
            exit_status=None,
            stdout="",
            stderr=str(exc),
            selected_file_digest=selected_digest,
            runner_fingerprint=runner_fingerprint,
            environment_fingerprint=environment_fingerprint,
            freshness_key=freshness_key,
        )
    collected_digest, collected_error = _declared_manifest_digest(
        cwd,
        args.collected_nodeids_path,
        expected_digest=args.collected_nodeids_expected_digest,
        arg_name="collected_nodeids_path",
    )
    residual_digest, residual_error = _declared_manifest_digest(
        cwd,
        args.residual_manifest_path,
        expected_digest=args.residual_manifest_expected_digest,
        arg_name="residual_manifest_path",
    )
    manifest_errors = [error for error in (collected_error, residual_error) if error is not None]
    passed = completed.returncode == 0 and not manifest_errors
    _write_full_log(
        cwd=cwd,
        full_log_ref=(supplied_freshness.full_log_ref if supplied_freshness is not None else None),
        argv=argv,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    details = f"argv={argv} returncode={completed.returncode}"
    if manifest_errors:
        details = f"{details} manifest_errors={manifest_errors}"
    return _command_result(
        spec,
        argv=argv,
        passed=passed,
        status="pass" if passed else "fail",
        detail_prefix=details,
        started_at=started_at,
        started_ns=started_ns,
        timeout_class=timeout_class,
        resolved_timeout_seconds=seconds,
        exit_status=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        selected_file_digest=selected_digest,
        runner_fingerprint=runner_fingerprint,
        environment_fingerprint=environment_fingerprint,
        freshness_key=freshness_key,
        collected_nodeid_digest=collected_digest,
        residual_manifest_digest=residual_digest,
    )


def _resolve_optional_file(
    value: str | None,
    *,
    cwd: Path,
    arg_name: str,
) -> tuple[Path | None, str | None]:
    """Resolve an optional repo-relative file arg into a path or error."""
    if value is None:
        return None, None
    target = (cwd / value).resolve() if not Path(value).is_absolute() else Path(value)
    if not target.is_file():
        return None, f"{arg_name}={value} not found"
    return target, None


def _live_capture_tui_png(
    spec: CheckSpec,
    cwd: Path,
    args: MockupGoldenDiffArgs,
) -> tuple[bytes | None, CheckResult | None]:
    """Capture a live Pilot render as PNG bytes for image-mode live diffing.

    Mounts the surface named by the reused text-mode selectors (``scope`` /
    ``state_path`` / ``mode`` / ``key_sequence`` / ``size``) under Pilot,
    exports the screen SVG, and rasterises it through the pinned ``resvg``
    chain. Returns ``(png_bytes, None)`` on success, or ``(None, result)``
    with an early ``CheckResult`` -- ``status="blocked"`` when ``resvg`` is
    absent (CI portability) and ``status="fail"`` on a bad state path or a
    capture/render error. Never raises.
    """
    from eawf.surfaces.tui.snapshot import pilot_harness

    state_path_abs: Path | None = None
    if args.state_path is not None:
        resolved, state_err = _resolve_optional_file(
            args.state_path, cwd=cwd, arg_name="state_path"
        )
        if state_err is not None:
            return None, CheckResult(
                name=spec.name, kind=spec.kind, passed=False, status="fail", details=state_err
            )
        state_path_abs = resolved

    try:
        png = pilot_harness.capture_mockup_golden_screen_png_sync(
            scope=args.scope,
            state_path=state_path_abs,
            mode=args.mode,
            key_sequence=list(args.key_sequence),
            size=(args.size[0], args.size[1]),
        )
    except FileNotFoundError:
        logger.info(f"_live_capture_tui_png blocked name={spec.name!r} reason=no-resvg")
        return None, CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="blocked",
            details="resvg not installed",
        )
    except Exception as exc:  # capture/render failure degrades, never raises
        logger.debug(f"_live_capture_tui_png capture-fail name={spec.name!r} reason={exc!r}")
        return None, CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"live capture failed: {exc}",
        )
    return png, None


def _check_mockup_image_diff(
    spec: CheckSpec,
    cwd: Path,
    args: MockupGoldenDiffArgs,
) -> CheckResult:
    """VIS-1 image mode: layout-shape-weighted diff of mockup PNG vs TUI PNG.

    Decodes the committed reference mockup PNG and the TUI-render PNG to coarse
    ink grids and scores their divergence weighting layout shape (border-corner
    round-vs-square, body column count) above the broadened secondary
    falsifiers (right-edge alignment, selected-row contrast) and token
    fidelity. A layout-shape mismatch FAILS the gate; a faithful pair PASSES.
    When ``tui_png`` is the :data:`LIVE_CAPTURE_SENTINEL` the TUI side is a
    fresh Pilot render captured + rasterised on the spot instead of a committed
    PNG. Never raises -- a malformed fixture degrades to ``status="fail"``.
    """
    from eawf.workflow.audit_dsl.kinds.mockup_image_diff import (
        LIVE_CAPTURE_SENTINEL,
        compare_mockup_png_to_tui_png,
        layout_diff_fails,
    )

    assert args.mockup_png is not None  # narrowed by the caller's branch
    mockup_path, mockup_err = _resolve_optional_file(
        args.mockup_png, cwd=cwd, arg_name="mockup_png"
    )
    if mockup_err is not None or mockup_path is None:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=mockup_err or "mockup_png not found",
        )

    if args.tui_png == LIVE_CAPTURE_SENTINEL:
        # Live mode: capture a fresh Pilot render instead of a committed PNG.
        tui_bytes, early = _live_capture_tui_png(spec, cwd, args)
        if early is not None:
            return early
        assert tui_bytes is not None
        tui_label = LIVE_CAPTURE_SENTINEL
    else:
        # In image mode ``tui_png`` is the render side; the fixture-pair tests
        # pass it directly, falling back to ``golden_path`` as the reference TUI render.
        tui_arg = args.tui_png if args.tui_png is not None else args.golden_path
        tui_path, tui_err = _resolve_optional_file(tui_arg, cwd=cwd, arg_name="tui_png")
        if tui_err is not None or tui_path is None:
            return CheckResult(
                name=spec.name,
                kind=spec.kind,
                passed=False,
                status="fail",
                details=tui_err or "tui_png not found",
            )
        tui_bytes = tui_path.read_bytes()
        tui_label = str(tui_arg)

    try:
        diff = compare_mockup_png_to_tui_png(mockup_path.read_bytes(), tui_bytes)
    except ValueError as exc:
        logger.debug(f"_check_mockup_image_diff decode-fail name={spec.name!r} reason={exc!r}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"mockup image diff failed: {exc}",
        )

    failed = layout_diff_fails(diff)
    summary = (
        f"score={diff.score:.3f} border_shape_mismatch={diff.border_shape_mismatch} "
        f"column_count_mismatch={diff.column_count_mismatch} "
        f"alignment_mismatch={diff.alignment_mismatch} "
        f"contrast_regression={diff.contrast_regression} "
        f"token_divergence={diff.token_divergence:.4f} "
        f"mockup={args.mockup_png} tui={tui_label} :: {'; '.join(diff.reasons)}"
    )
    if failed:
        logger.debug(f"_check_mockup_image_diff fail name={spec.name!r} score={diff.score:.3f}")
        return CheckResult(
            name=spec.name, kind=spec.kind, passed=False, status="fail", details=summary
        )
    logger.debug(f"_check_mockup_image_diff ok name={spec.name!r} score={diff.score:.3f}")
    return CheckResult(name=spec.name, kind=spec.kind, passed=True, status="pass", details=summary)


def _check_mockup_golden_diff(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Capture a TUI screen via Pilot and compare it to a mockup golden.

    Default ASCII-text mode byte-compares the normalised live screen to a text
    golden. When ``mockup_png`` is set the kind dispatches to the VIS-1 image
    falsifier (:func:`_check_mockup_image_diff`), which weights layout shape
    above token fidelity.
    """
    try:
        args = MockupGoldenDiffArgs.model_validate(spec.args)
    except ValidationError as exc:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"invalid args: {exc.errors()[0]['msg']}",
        )

    if args.mockup_png is not None:
        return _check_mockup_image_diff(spec, cwd, args)

    golden_path, golden_error = _resolve_optional_file(
        args.golden_path,
        cwd=cwd,
        arg_name="golden_path",
    )
    if golden_error is not None or golden_path is None:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=golden_error or "golden_path not found",
        )
    state_path, state_error = _resolve_optional_file(
        args.state_path,
        cwd=cwd,
        arg_name="state_path",
    )
    if state_error is not None:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=state_error,
        )

    from eawf.surfaces.tui.snapshot import pilot_harness

    try:
        expected = golden_path.read_text(encoding="utf-8").rstrip("\n")
        captured = pilot_harness.capture_mockup_golden_screen_text_sync(
            scope=args.scope,
            state_path=state_path,
            mode=args.mode,
            key_sequence=list(args.key_sequence),
            size=(args.size[0], args.size[1]),
        )
    except Exception as exc:
        logger.debug(f"_check_mockup_golden_diff capture-fail name={spec.name!r} reason={exc!r}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"mockup golden capture failed: {exc}",
        )

    if captured == expected:
        byte_count = len(expected.encode("utf-8"))
        logger.debug(f"_check_mockup_golden_diff ok name={spec.name!r} golden={args.golden_path!r}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=True,
            status="pass",
            details=f"screen matches mockup golden path={args.golden_path} bytes={byte_count}",
        )

    details = pilot_harness.mockup_golden_diff_detail(golden_path, expected, captured)
    logger.debug(
        f"_check_mockup_golden_diff mismatch name={spec.name!r} golden={args.golden_path!r}"
    )
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        status="fail",
        details=details,
    )


CHECK_REGISTRY: dict[str, CheckFn] = {
    "file_exists": _check_file_exists,
    "path_glob_nonempty": _check_path_glob_nonempty,
    "regex_in_file": _check_regex_in_file,
    "state_field_equals": _check_state_field_equals,
    "command_exit_zero": _check_command_exit_zero,
    "verify_implements": check_verify_implements,
    "criterion_in_diff": check_criterion_in_diff,
    "citation_resolves": _check_citation_resolves,
    "schema_validate": check_schema_validate,
    "affordance_parity": check_affordance_parity,
    "transition_coverage": check_transition_coverage,
    "tui_flow": check_tui_flow,
    "svg_well_formed": check_svg_well_formed,
    "svg_pixel_diff": check_svg_pixel_diff,
    "mockup_golden_diff": _check_mockup_golden_diff,
    "journal_chain": check_journal_chain,
}


def execute_check(
    spec: CheckSpec,
    cwd: Path,
    *,
    before_execute: BeforeGateExecute | None = None,
) -> CheckResult:
    """Execute one check, applying durable instrumentation when requested.

    Legacy callers that omit ``before_execute`` retain direct registry
    dispatch. Durable-close callers supply the claim callback and receive the
    same claim/timing/fingerprint/proof contract for every deterministic kind.
    """
    if spec.kind == "command_exit_zero":
        return _check_command_exit_zero(
            spec,
            cwd,
            before_execute=before_execute,
        )
    if before_execute is not None:
        return _check_with_durable_instrumentation(
            spec,
            cwd,
            before_execute=before_execute,
        )
    return CHECK_REGISTRY[spec.kind](spec, cwd)


#: Close-gate kinds that score against the validated state model rather
#: than a checkout file set, so they do NOT take the ``(CheckSpec, Path)``
#: runner shape and are not dispatched through :func:`run_checks`. They
#: are still *registered* audit-DSL kinds: the BIND-1 wired-on sweep
#: (``tools/idle_contract_gate.py``) reads :func:`registered_audit_dsl_kinds`
#: -- the union of :data:`CHECK_REGISTRY` and this set -- so a close-gate
#: kind cannot ship registered-but-idle either.
#:
#: ``backlog_resolution`` reads the wave-linked backlog items off state
#: and is driven from the close-readiness compute when ``verify.enforce``
#: is active (see :func:`eawf.workflow.verify.readiness.compute`).
#:
#: ``jury_calibrated`` reads the I09 jury-validation metrics
#: (:class:`eawf.observability.eval.jury_validation.JuryValidationReport`)
#: plus the scope's Oracle-Determinism-Ratio and gates the cross-vendor
#: jury's blocking authority on them (see
#: :func:`eawf.workflow.audit_dsl.kinds.jury_calibrated.check_jury_calibrated`).
CLOSE_GATE_KINDS: frozenset[str] = frozenset({BACKLOG_RESOLUTION_KIND, JURY_CALIBRATED_KIND})


def registered_audit_dsl_kinds() -> frozenset[str]:
    """Return every registered audit-DSL kind string.

    The union of the file-set check kinds (:data:`CHECK_REGISTRY` keys)
    and the state-scoring close-gate kinds (:data:`CLOSE_GATE_KINDS`).
    This is the canonical kind population the BIND-1 idle-contract
    meta-gate sweeps: a kind that appears here MUST have a production
    caller (a tier mapping or a close-gate wiring) or the sweep reds.

    Returns:
        The frozenset of all registered kind strings.
    """
    return frozenset(CHECK_REGISTRY) | CLOSE_GATE_KINDS


__all__ = [
    "CHECK_REGISTRY",
    "CLOSE_GATE_KINDS",
    "BeforeCommandExecute",
    "BeforeGateExecute",
    "CheckFn",
    "execute_check",
    "registered_audit_dsl_kinds",
]
