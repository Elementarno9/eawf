"""Pydantic v2 models for the audit-check DSL (B019).

The DSL is yaml-driven (``schema_version: "1.0"``) and resolves to a
typed list of :class:`CheckSpec` values that the registry-based runner
dispatches into per-kind callables. Each callable returns a
:class:`CheckResult` recording pass/fail/blocked plus a one-line
``details`` note.

The check kinds frozen for v0.3:

* ``file_exists`` — ``args = {path: str}``.
* ``path_glob_nonempty`` — ``args = {pattern: str}``.
* ``regex_in_file`` — ``args = {path: str, pattern: str}``.
* ``state_field_equals`` — ``args = {field: str, value: Any,
  state_path: str = ".ea/state.json"}``.
* ``command_exit_zero`` — ``args`` validated by
  :class:`CommandExitZeroArgs`: ``{argv: list[str], timeout_class:
  TimeoutClass = "standard", scope: Scope = "changed", wave_id:
  str | None = None, wave_file_scopes: list[str] = []}``.
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
* ``citation_resolves`` — ``args`` validated by
  :class:`CitationResolvesArgs`: ``{path: str}`` or ``{text: str}``,
  plus optional typed ``references`` rows. Verifies dense ``[N]`` prose
  citations resolve to portable citation rows.
* ``schema_validate`` — ``args = {model: str, target: dict | str}``.
  Imports the dotted Pydantic model path in ``model`` and runs
  ``Model.model_validate`` over ``target`` (an inline dict or a
  repo-relative JSON file path resolved against ``cwd``); a
  :class:`pydantic.ValidationError` fails the check rather than
  raising.
* ``affordance_parity`` — ``args = {mode: str, state_path: str, size:
  [int, int]}``. Mounts the operator TUI, switches to the named mode,
  and drives each advertised footer-hint key through the real
  key->Binding path; fails (naming each offending key) when an
  advertised key does not resolve to a binding, else passes. A
  malformed ``args`` degrades to ``status="fail"`` rather than raising.
* ``transition_coverage`` — ``args = {table: str, covered_edges:
  list[list[str]] = <auto>}``. Compares the full edge set of a
  lifecycle FSM table (``wave`` / ``phase`` / ``iter`` / ``spec`` from
  :mod:`eawf.workflow.lifecycle.spec`) against the edge set a
  Hypothesis ``RuleBasedStateMachine`` exploration actually exercised.
  Passes iff every table edge was covered; fails naming the uncovered
  edges. When ``covered_edges`` is omitted the kind runs the machine
  in-process to collect coverage; an explicit list (e.g. one missing a
  known edge) drives the deterministic error path. A malformed ``args``
  degrades to ``status="fail"`` rather than raising.

See ``docs/architecture/audit-checks.md`` for grammar + the
sandbox-policy boundary that ``command_exit_zero`` leaves to the
caller (tracked in backlog item B074).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.platform.artifacts.references import Citation

logger = logging.getLogger(__name__)

CheckKind = Literal[
    "file_exists",
    "path_glob_nonempty",
    "regex_in_file",
    "state_field_equals",
    "command_exit_zero",
    "verify_implements",
    "criterion_in_diff",
    "citation_resolves",
    "schema_validate",
    "affordance_parity",
    "transition_coverage",
]


#: Closed status literal for one check execution.
#:
#: Mirrors :data:`eawf.workflow.verify.models.GateStatus` so the audit
#: DSL runner can surface ``blocked`` (e.g. a ``subprocess.TimeoutExpired``)
#: as a first-class outcome rather than collapsing it into ``fail``. The
#: pre-W15 ``passed: bool`` field is preserved so legacy callers keep
#: working — ``status="blocked"`` implies ``passed=False``.
CheckStatus = Literal["pass", "fail", "blocked"]


#: Timeout-class budget literal for ``command_exit_zero`` gates.
#:
#: The runner derives the concrete ``subprocess.run(..., timeout=...)``
#: seconds from this literal via :data:`registry._TIMEOUT_CLASS_SECONDS`
#: so YAML authors pick a budget by *intent* (a quick fmt-check vs. a
#: long-running pytest sweep) rather than guessing a number that drifts
#: across hosts.
TimeoutClass = Literal["quick", "standard", "slow", "very_slow"]


#: File-set scope literal for ``command_exit_zero`` gates.
#:
#: * ``changed`` — files in ``git diff <diff_base>...HEAD``.
#: * ``touched`` — ``changed`` union ``wave.file_scopes``.
#: * ``all`` — every tracked file (no filter; the gate receives an
#:   empty ``EAWF_GATE_FILES`` env var and is expected to glob the tree
#:   on its own).
Scope = Literal["changed", "touched", "all"]


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

    The model carries both the legacy ``passed: bool`` signal (kept for
    back-compat with pre-W15 callers) and the granular
    :data:`CheckStatus` ``status`` field. The two are kept consistent
    by a model validator: ``status="blocked"`` forces ``passed=False``;
    when ``status`` is omitted it defaults to ``"pass"`` if ``passed``
    is true and ``"fail"`` if ``passed`` is false.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str
    passed: bool
    status: CheckStatus | None = None
    details: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _synthesise_status(cls, data: Any) -> Any:
        """Synthesise ``status`` from ``passed`` when omitted + enforce consistency.

        Frozen-model-friendly: runs on the raw input ``dict`` before
        field assignment so the synthesised ``status`` lands at
        construction time rather than fighting ``frozen=True``.

        Raises:
            ValueError: when an explicit ``status="pass"`` contradicts
                ``passed=False``, or ``status`` is ``"fail"``/``"blocked"``
                with ``passed=True``.
        """
        if not isinstance(data, dict):
            return data
        status = data.get("status")
        passed = data.get("passed")
        if status is None:
            if isinstance(passed, bool):
                data = {**data, "status": "pass" if passed else "fail"}
            return data
        if status == "pass" and passed is False:
            name = data.get("name")
            raise ValueError(
                f"check result inconsistent: status='pass' but passed=False (name={name!r})"
            )
        if status in {"fail", "blocked"} and passed is True:
            name = data.get("name")
            raise ValueError(
                f"check result inconsistent: status={status!r} but passed=True (name={name!r})"
            )
        return data


class CommandExitZeroArgs(BaseModel):
    """Strict args schema for the ``command_exit_zero`` check kind (W15).

    Validates the typed kwargs the W15 runner-hardening pass added:
    timeout-class budget, file-set scope, and the optional wave-context
    hints the runner uses to derive ``diff_base`` and the ``touched``
    file union.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: list[str] = Field(min_length=1)
    timeout_class: TimeoutClass = "standard"
    scope: Scope = "changed"
    #: Optional wave id; resolved into ``diff_base = derive_wave_sha(wave_id) + "~1"``
    #: when set, with a ``git merge-base HEAD main`` fallback when the
    #: wave's SHA cannot be derived.
    wave_id: str | None = None
    #: Optional wave ``file_scopes`` list; unioned into the ``touched``
    #: scope's file set. Callers pass the wave's own ``file_scopes`` so
    #: the runner does not need to load ``state.json``.
    wave_file_scopes: list[str] = Field(default_factory=list)
    #: Audit-skill metadata. The ``/audit`` skill stashes the originating
    #: success-criterion text inside the spec's ``args`` so it can pull
    #: it back out when rendering findings (see
    #: :func:`eawf.workflow.skills.audit._build_criterion_specs`). The
    #: runner ignores it; this field exists only to satisfy
    #: ``extra="forbid"`` on the args schema.
    criterion: str | None = None

    @model_validator(mode="after")
    def _argv_all_str(self) -> CommandExitZeroArgs:
        """Enforce ``argv`` entries are strings.

        Raises:
            ValueError: when any ``argv`` entry is not a ``str``.
        """
        if not all(isinstance(a, str) for a in self.argv):
            raise ValueError("argv entries must be strings")
        return self


class CitationResolvesArgs(BaseModel):
    """Strict args schema for the ``citation_resolves`` check kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str | None = None
    text: str | None = None
    references: list[Citation] | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> CitationResolvesArgs:
        """Require exactly one citation source.

        Raises:
            ValueError: when both or neither of ``path`` and ``text`` are set.
        """
        has_path = self.path is not None
        has_text = self.text is not None
        if has_path == has_text:
            raise ValueError("exactly one of path or text is required")
        return self


class CheckFile(BaseModel):
    """Top-level yaml document validated by :func:`load_spec`."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    checks: list[CheckSpec]


__all__ = [
    "CheckFile",
    "CheckKind",
    "CheckResult",
    "CheckSpec",
    "CheckStatus",
    "CitationResolvesArgs",
    "CommandExitZeroArgs",
    "Scope",
    "TimeoutClass",
]
