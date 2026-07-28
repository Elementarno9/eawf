"""Loader + dispatcher for the audit-check DSL (B019).

Public surface
--------------

* :func:`load_spec` — read a yaml file, validate it against
  :class:`~eawf.workflow.audit_dsl.models.CheckFile`, return the typed
  ``checks`` list. Bad input is reported as
  :class:`~eawf.surfaces.cli.errors.UserError` with ``kind="InvalidInput"``.
* :func:`run_checks` — iterate the spec list, dispatch each via
  :data:`~eawf.workflow.audit_dsl.registry.CHECK_REGISTRY`, return the list
  of :class:`~eawf.workflow.audit_dsl.models.CheckResult` values.

Notes:
    Unknown ``kind`` values cannot reach :func:`run_checks` because
    :class:`CheckKind` is a Literal — Pydantic rejects at
    :func:`load_spec` time.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from eawf.surfaces.cli.errors import UserError
from eawf.workflow.audit_dsl.models import CheckFile, CheckResult, CheckSpec
from eawf.workflow.audit_dsl.registry import (
    BeforeGateExecute,
    execute_check,
)

logger = logging.getLogger(__name__)


def _invocation_key(spec: CheckSpec) -> str:
    """Return canonical execution identity for one typed check spec."""
    payload = json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_spec(path: Path) -> list[CheckSpec]:
    """Load and validate a DSL yaml document.

    Args:
        path: Path to the yaml spec.

    Returns:
        The validated ``checks`` list.

    Raises:
        UserError: When the file is missing, yaml-malformed, or
            fails Pydantic validation (``kind="InvalidInput"``).
    """
    if not path.is_file():
        raise UserError(f"audit-check spec {path} not found", kind="InvalidInput")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise UserError(
            f"audit-check spec {path} not valid yaml: {exc}", kind="InvalidInput"
        ) from exc
    if raw is None:
        raise UserError(f"audit-check spec {path} is empty", kind="InvalidInput")
    try:
        doc = CheckFile.model_validate(raw)
    except ValidationError as exc:
        raise UserError(
            f"audit-check spec {path} schema mismatch: {exc}", kind="InvalidInput"
        ) from exc
    return list(doc.checks)


def run_checks(
    specs: list[CheckSpec],
    *,
    cwd: Path | None = None,
    before_execute: BeforeGateExecute | None = None,
) -> list[CheckResult]:
    """Dispatch every spec via :data:`CHECK_REGISTRY` and collect results.

    Args:
        specs: Already-validated check specs (e.g. from :func:`load_spec`).
        cwd: Directory the checks run against. Defaults to
            :func:`Path.cwd` so glob/file lookups resolve against the
            caller's working tree.
        before_execute: Optional freshness-key callback invoked immediately
            before any deterministic check. Returning a result suppresses
            execution and reuses that terminal or fail-closed result.

    Returns:
        One :class:`CheckResult` per input spec, in declaration order.

    Raises:
        ValueError: When a kind-specific argument check fails (e.g.
            missing ``path`` for ``file_exists``). Propagated as-is so
            the caller can surface a clean error envelope.
    """
    base = (cwd or Path.cwd()).resolve()
    out: list[CheckResult] = []
    executed: dict[str, CheckResult] = {}
    for spec in specs:
        invocation_key = _invocation_key(spec)
        previous = executed.get(invocation_key)
        if previous is not None:
            logger.debug(
                f"run_checks status=reuse name={spec.name!r} kind={spec.kind!r} "
                f"invocation_key={invocation_key!r}"
            )
            out.append(previous)
            continue
        logger.debug(f"run_checks dispatching name={spec.name} kind={spec.kind}")
        result = execute_check(
            spec,
            base,
            before_execute=before_execute,
        )
        executed[invocation_key] = result
        out.append(result)
    return out


__all__ = ["load_spec", "run_checks"]
