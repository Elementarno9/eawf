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
disallowed argv. Tracked as backlog item B044 for v0.3 hardening.
See ``docs/architecture/audit-checks.md`` for the full boundary
discussion.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eawf.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)

CheckFn = Callable[[CheckSpec, Path], CheckResult]


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


def _check_command_exit_zero(spec: CheckSpec, cwd: Path) -> CheckResult:
    argv = spec.args.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError(
            f"check {spec.name!r} kind=command_exit_zero: arg 'argv' must be a non-empty list[str]"
        )
    # Sandbox-policy enforcement is the caller's responsibility in v0.2;
    # see docs/architecture/audit-checks.md (B044 follow-up).
    try:
        completed = subprocess.run(
            argv,
            check=False,
            cwd=str(cwd),
            capture_output=True,
            text=True,
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
}


__all__ = ["CHECK_REGISTRY", "CheckFn"]
