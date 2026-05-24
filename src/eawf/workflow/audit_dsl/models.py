"""Pydantic v2 models for the audit-check DSL (B019).

The DSL is yaml-driven (``schema_version: "1.0"``) and resolves to a
typed list of :class:`CheckSpec` values that the registry-based runner
dispatches into per-kind callables. Each callable returns a
:class:`CheckResult` recording pass/fail plus a one-line ``details``
note.

The check kinds frozen for v0.3:

* ``file_exists`` — ``args = {path: str}``.
* ``path_glob_nonempty`` — ``args = {pattern: str}``.
* ``regex_in_file`` — ``args = {path: str, pattern: str}``.
* ``state_field_equals`` — ``args = {field: str, value: Any,
  state_path: str = ".ea/state.json"}``.
* ``command_exit_zero`` — ``args = {argv: list[str]}``.
* ``verify_implements`` — ``args = {phase_id: str, diff_base: str,
  cadence: str, current_trigger: str}``. Walks closed WaveSpecs
  under ``.ea/specs/<phase_id>/`` and greps the diff against
  ``diff_base`` for verdict-id markers under each wave's
  ``file_scopes``; short-circuits when ``cadence`` does not match
  ``current_trigger``.
* ``criterion_in_diff`` — ``args = {criterion: str, pattern: str,
  file_scopes: list[str]}``. Greps a single wave success-criterion's
  verification ``pattern`` across the current content of its
  ``file_scopes``; fails with the offending criterion text in
  ``details`` when the pattern is absent. Drives the criterion-vs-diff
  half of the ``/audit`` gate.

See ``docs/architecture/audit-checks.md`` for grammar + the
sandbox-policy boundary that ``command_exit_zero`` leaves to the
caller (tracked in backlog item B044).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

CheckKind = Literal[
    "file_exists",
    "path_glob_nonempty",
    "regex_in_file",
    "state_field_equals",
    "command_exit_zero",
    "verify_implements",
    "criterion_in_diff",
]


class CheckSpec(BaseModel):
    """One DSL-declared check.

    ``kind`` is constrained to :data:`CheckKind` so Pydantic rejects
    unknown values at load time — the runner never receives a
    surprise.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CheckKind
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class CheckResult(BaseModel):
    """Outcome of a single check.

    ``details`` is a one-line note suitable for the audit record.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str
    passed: bool
    details: str | None = None


class CheckFile(BaseModel):
    """Top-level yaml document validated by :func:`load_spec`."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    checks: list[CheckSpec]


__all__ = ["CheckFile", "CheckKind", "CheckResult", "CheckSpec"]
