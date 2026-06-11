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

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
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
from eawf.workflow.audit_dsl.kinds.schema_validate import check_schema_validate
from eawf.workflow.audit_dsl.kinds.svg_pixel_diff import check_svg_pixel_diff
from eawf.workflow.audit_dsl.kinds.svg_well_formed import check_svg_well_formed
from eawf.workflow.audit_dsl.kinds.transition_coverage import check_transition_coverage
from eawf.workflow.audit_dsl.kinds.tui_flow import check_tui_flow
from eawf.workflow.audit_dsl.kinds.verify_implements import check_verify_implements
from eawf.workflow.audit_dsl.models import (
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


def _check_command_exit_zero(spec: CheckSpec, cwd: Path) -> CheckResult:
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
    seconds = _TIMEOUT_CLASS_SECONDS[timeout_class]

    diff_base = derive_diff_base(args.wave_id, repo_root=cwd)
    selected_files = _resolve_scope_files(
        scope=args.scope,
        wave_file_scopes=args.wave_file_scopes,
        diff_base=diff_base,
        cwd=cwd,
    )
    child_env = {**os.environ, _GATE_FILES_ENV: _GATE_FILES_SEPARATOR.join(selected_files)}

    logger.debug(
        f"_check_command_exit_zero gate_id={spec.name!r} timeout_class={timeout_class!r} "
        f"scope={args.scope!r} diff_base={diff_base!r} files={len(selected_files)}"
    )

    # Sandbox-policy enforcement is the caller's responsibility in v0.2;
    # see docs/architecture/audit-checks.md (B074 follow-up).
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
        detail = f"timeout after {exc.timeout}s (class={timeout_class!r})"
        logger.info(
            f"_check_command_exit_zero gate_id={spec.name!r} status=blocked "
            f"timeout_class={timeout_class!r}"
        )
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="blocked",
            details=detail,
        )
    except FileNotFoundError as exc:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            details=f"argv={argv} not executable: {exc.strerror}",
        )
    passed = completed.returncode == 0
    details = f"argv={argv} returncode={completed.returncode}"
    return CheckResult(name=spec.name, kind=spec.kind, passed=passed, details=details)


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


def _check_mockup_golden_diff(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Capture a TUI screen via Pilot and compare it to a mockup golden."""
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
}


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
CLOSE_GATE_KINDS: frozenset[str] = frozenset({BACKLOG_RESOLUTION_KIND})


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
    "CheckFn",
    "registered_audit_dsl_kinds",
]
