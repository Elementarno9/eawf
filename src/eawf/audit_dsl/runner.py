"""Loader + dispatcher for the audit-check DSL (B019).

Public surface
--------------

* :func:`load_spec` — read a yaml file, validate it against
  :class:`~eawf.audit_dsl.models.CheckFile`, return the typed
  ``checks`` list. Raises :class:`~eawf.cli.errors.UserError`
  (``kind="InvalidInput"``) on missing file, bad yaml, or schema-mismatch.
* :func:`run_checks` — iterate the spec list, dispatch each via
  :data:`~eawf.audit_dsl.registry.CHECK_REGISTRY`, return the list
  of :class:`~eawf.audit_dsl.models.CheckResult` values.

Notes:
    Unknown ``kind`` values cannot reach :func:`run_checks` because
    :class:`CheckKind` is a Literal — Pydantic rejects at
    :func:`load_spec` time.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from eawf.audit_dsl.models import CheckFile, CheckResult, CheckSpec
from eawf.audit_dsl.registry import CHECK_REGISTRY
from eawf.cli.errors import UserError

logger = logging.getLogger(__name__)


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
) -> list[CheckResult]:
    """Dispatch every spec via :data:`CHECK_REGISTRY` and collect results.

    Args:
        specs: Already-validated check specs (e.g. from :func:`load_spec`).
        cwd: Directory the checks run against. Defaults to
            :func:`Path.cwd` so glob/file lookups resolve against the
            caller's working tree.

    Returns:
        One :class:`CheckResult` per input spec, in declaration order.

    Raises:
        ValueError: When a kind-specific argument check fails (e.g.
            missing ``path`` for ``file_exists``). Propagated as-is so
            the caller can surface a clean error envelope.
    """
    base = (cwd or Path.cwd()).resolve()
    out: list[CheckResult] = []
    for spec in specs:
        fn = CHECK_REGISTRY[spec.kind]
        logger.debug(f"run_checks dispatching name={spec.name} kind={spec.kind}")
        out.append(fn(spec, base))
    return out


__all__ = ["load_spec", "run_checks"]
