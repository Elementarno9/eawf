"""``criterion_in_diff`` audit-DSL kind.

Verifies a single wave success-criterion against the actual source by
greping the criterion's verification ``pattern`` across the post-change
content of the criterion's ``file_scopes``. This is the criterion-vs-diff
half of the ``/audit`` gate: it answers "does the code that shipped
actually reflect what the criterion claims?" without re-running the
behavioural surface (which the ``command_exit_zero`` kind covers).

Why source-content rather than the raw unified diff: a criterion can be
satisfied by code that was already present before the wave (e.g. a guard
the wave was supposed to keep), so matching against the current ``HEAD``
content of the named scopes is the conservative read of "the criterion
holds now". The wave's ``file_scopes`` bound the search so an unrelated
match elsewhere in the tree cannot mask a regression in the scope the
criterion governs.

Args (read from ``spec.args``):

* ``criterion`` — the success-criterion text. Surfaced verbatim in the
  result ``details`` so a failing audit names the offending criterion.
* ``pattern`` — a regex that must match somewhere in at least one
  ``file_scopes`` entry for the criterion to pass.
* ``file_scopes`` — non-empty ``list[str]`` of repo-relative paths the
  criterion governs. A scope that is a directory is walked recursively
  for files; a scope that is a file is read directly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)


def _require_str(args: dict[str, Any], key: str, *, name: str, kind: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"check {name!r} kind={kind}: missing or empty str arg {key!r}")
    return value


def _require_str_list(args: dict[str, Any], key: str, *, name: str, kind: str) -> list[str]:
    value = args.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(f"check {name!r} kind={kind}: arg {key!r} must be a non-empty list[str]")
    return list(value)


def _iter_scope_files(scope: Path) -> list[Path]:
    """Return the readable files a scope entry resolves to.

    A file scope yields itself; a directory scope yields its recursive
    file membership. A scope that does not exist yields an empty list so
    a deleted / renamed path surfaces as "criterion not found" rather
    than crashing the check.
    """
    if scope.is_file():
        return [scope]
    if scope.is_dir():
        return [p for p in sorted(scope.rglob("*")) if p.is_file()]
    return []


def check_criterion_in_diff(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Grep a criterion's verification pattern across its file scopes.

    Args:
        spec: The check spec. ``spec.args`` must carry ``criterion``,
            ``pattern``, and ``file_scopes``.
        cwd: Directory the relative ``file_scopes`` resolve against.

    Returns:
        :class:`CheckResult` — ``passed=True`` when ``pattern`` matches
        in at least one ``file_scopes`` entry; ``passed=False`` with the
        offending criterion in ``details`` otherwise.

    Raises:
        ValueError: When ``criterion`` / ``pattern`` are missing or
            empty, ``file_scopes`` is not a non-empty ``list[str]``, or
            ``pattern`` is not a valid regex.
    """
    criterion = _require_str(spec.args, "criterion", name=spec.name, kind=spec.kind)
    pattern = _require_str(spec.args, "pattern", name=spec.name, kind=spec.kind)
    file_scopes = _require_str_list(spec.args, "file_scopes", name=spec.name, kind=spec.kind)
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(
            f"check {spec.name!r} kind={spec.kind}: arg 'pattern' is not a valid regex: {exc}"
        ) from exc

    searched = 0
    for raw_scope in file_scopes:
        scope_path = (
            (cwd / raw_scope).resolve() if not Path(raw_scope).is_absolute() else Path(raw_scope)
        )
        for path in _iter_scope_files(scope_path):
            searched += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug(f"check_criterion_in_diff skip path={path} reason={exc!r}")
                continue
            if compiled.search(text):
                return CheckResult(
                    name=spec.name,
                    kind=spec.kind,
                    passed=True,
                    details=f"criterion={criterion!r} pattern matched in {raw_scope}",
                )

    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        details=(
            f"unmet criterion: {criterion!r} pattern={pattern!r} "
            f"not found across {len(file_scopes)} scope(s) ({searched} file(s) searched)"
        ),
    )


__all__ = ["check_criterion_in_diff"]
