"""``schema_validate`` audit-DSL kind (Fidelity Spine FS04).

Resolves a dotted Pydantic model path and runs ``model_validate`` over
a target payload, turning a successful validation into a ``pass`` and a
:class:`pydantic.ValidationError` into a ``fail`` (never a raised
exception). The kind is the deterministic gate behind schema-shaped
criteria: a criterion that asserts "this JSON fixture is a valid
:class:`GateSpec`" compiles to a ``schema_validate`` CheckSpec whose
``args`` name the model and the fixture.

Args (read from ``spec.args``)
------------------------------

* ``model`` — dotted import path to a Pydantic ``BaseModel`` subclass
  (e.g. ``"eawf.kernel.spec.common.GateSpec"``).
* ``target`` — either an inline mapping to validate directly, or a
  repo-relative path to a JSON file resolved against ``cwd``. A string
  is treated as a path; anything else is validated as-is.

A malformed ``args`` (missing key, unimportable path, non-model object,
unreadable / non-JSON file) yields ``status="fail"`` with a ``details``
note rather than propagating an exception, so a single bad criterion
cannot abort the whole audit run.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)


def _resolve_model(dotted: str) -> type[BaseModel]:
    """Import ``dotted`` and return the resolved Pydantic model class.

    Raises:
        ValueError: When the path has no module/attribute split, the
            module is unimportable, the attribute is absent, or the
            resolved object is not a :class:`pydantic.BaseModel`
            subclass.
    """
    module_path, _, attr = dotted.rpartition(".")
    if not module_path:
        raise ValueError(f"model path is not dotted: {dotted!r}")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ValueError(f"cannot import model module {module_path!r}: {exc}") from exc
    try:
        resolved = getattr(module, attr)
    except AttributeError as exc:
        raise ValueError(f"model attribute {attr!r} not found in {module_path!r}") from exc
    if not (isinstance(resolved, type) and issubclass(resolved, BaseModel)):
        raise ValueError(f"resolved object {dotted!r} is not a pydantic BaseModel")
    return resolved


def _resolve_target(target: Any, cwd: Path) -> Any:
    """Resolve the validation payload.

    A ``str`` target is a repo-relative JSON file path resolved against
    ``cwd``; any other value (typically a mapping) is returned verbatim.

    Raises:
        ValueError: When a path target is missing, unreadable, or not
            valid JSON.
    """
    if not isinstance(target, str):
        return target
    path = cwd / target
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read target file {target!r}: {exc.strerror}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"target file {target!r} is not valid json: {exc}") from exc


def check_schema_validate(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Validate ``spec.args['target']`` against the model named in ``spec.args['model']``.

    Args (read from ``spec.args``):
        model: Dotted import path to a Pydantic ``BaseModel`` subclass.
        target: Inline mapping to validate, or a repo-relative JSON
            file path resolved against ``cwd``.

    Returns:
        :class:`CheckResult` with ``status="pass"`` when
        ``Model.model_validate(target)`` succeeds; ``status="fail"``
        (with a ``details`` note naming the offending model / target /
        validation error) when the args are malformed or validation
        fails. Never raises -- a bad criterion degrades to a failed
        check, not an aborted run.
    """
    model_path = spec.args.get("model")
    if not isinstance(model_path, str) or not model_path:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details="missing or non-str arg 'model'",
        )
    if "target" not in spec.args:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details="missing arg 'target'",
        )

    try:
        model = _resolve_model(model_path)
        target = _resolve_target(spec.args["target"], cwd)
    except ValueError as exc:
        logger.debug(f"check_schema_validate setup-fail name={spec.name!r} reason={exc!s}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=str(exc),
        )

    try:
        model.model_validate(target)
    except ValidationError as exc:
        logger.debug(
            f"check_schema_validate invalid name={spec.name!r} model={model_path!r} "
            f"errors={exc.error_count()}"
        )
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"validation against {model_path} failed: {exc.error_count()} error(s)",
        )

    logger.debug(f"check_schema_validate ok name={spec.name!r} model={model_path!r}")
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=True,
        status="pass",
        details=f"valid against {model_path}",
    )


__all__ = ["check_schema_validate"]
