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

from eawf.platform.lint._conditional import changed_files
from eawf.workflow.audit_dsl.kinds.criterion_in_diff import check_criterion_in_diff
from eawf.workflow.audit_dsl.kinds.verify_implements import check_verify_implements
from eawf.workflow.audit_dsl.models import (
    CheckResult,
    CheckSpec,
    CommandExitZeroArgs,
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


CHECK_REGISTRY: dict[str, CheckFn] = {
    "file_exists": _check_file_exists,
    "path_glob_nonempty": _check_path_glob_nonempty,
    "regex_in_file": _check_regex_in_file,
    "state_field_equals": _check_state_field_equals,
    "command_exit_zero": _check_command_exit_zero,
    "verify_implements": check_verify_implements,
    "criterion_in_diff": check_criterion_in_diff,
}


__all__ = ["CHECK_REGISTRY", "CheckFn"]
