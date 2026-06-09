"""Deterministic idle-contract gate for the band-scoped spec-jury QC gate.

This repo has a history of building a verifier and then leaving it IDLE
forever -- a dead gate that never runs (tracked as the B091 idle-verifier
regression). The spec-jury close gate is the latest such verifier: W05/W06
wired the producer
(:func:`eawf.workflow.dispatch.spec_jury.produce_spec_jury_verdict`) and the
band-conditional resolver
(:func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`), and the
shipped ``quality`` profile turns it on for a non-empty UI/UX band. This gate
makes "wired + band-scoped, not idle, not global" a CHECKED invariant rather
than a hope.

Three gates run from :func:`main`, in precedence order, and any failing
exits non-zero:

- :func:`check_idle_contract` -- the original single B091 spec-jury contract
  described below.
- :func:`check_skill_body_binding` -- a *binding-proof* probe that drives a
  deliberately drifted dict body through ``run_skill`` and asserts the emit
  path RAISES ``pydantic.ValidationError``. The drift is an extra-forbid key
  against a registered body model, so a working binding rejects it before the
  envelope is built. If a later refactor lets the body-validation-at-emit
  binding regress to idle, the drifted body would emit silently and this probe
  stops raising -- failing the gate. This is the meta-binding that keeps the
  emit-validation binding from silently going dead.
- :func:`detect_idle_contracts` -- a *meta-gate* that reads a git diff and
  flags any newly-defined contract (a ``check_*`` / ``*_gate`` / ``*_lint``
  function, a ``CheckKind`` runner registration, an ``OracleTier`` dispatch
  arm, or an ``eawf0##_*.py`` lint module) that ships idle: no call-site
  outside its own module AND no asserting test references it. The meta-gate
  generalizes the B091 lesson from one hardcoded contract to *every* future
  contract a diff introduces, so a fresh dead verifier is caught the same
  commit it lands.

Three independent contracts are asserted, in precedence order:

- **not-idle** -- the producer is importable AND at least one shipped profile
  enables it via a non-empty :attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands`
  with :attr:`~eawf.platform.profiles.models.VerifyBlock.enforce` true. A
  producer that no profile wires on is idle-forever; this contract fails on
  exactly that.
- **band-scoped (not global)** -- for that band-enabling profile,
  :func:`resolve_wave_verify_block` resolves to ``enforce=True`` for a UI-scope
  probe wave (``file_scopes`` under ``src/eawf/surfaces/tui/`` or
  ``.../render/``) AND to ``enforce=False`` for a non-UI probe wave (e.g.
  ``src/eawf/kernel/...``). A profile that flips enforcement on fleet-wide
  fails this contract because it would gate every wave, not just the band.
- **emit-validation not-idle** -- a drifted dict body (an extra-forbid key on a
  registered body model) driven through ``run_skill`` RAISES
  ``pydantic.ValidationError`` before the envelope is built. A regression that
  drops the emit-time body-validation chokepoint would emit the drift silently;
  this contract (:func:`check_skill_body_binding`) fails on exactly that.

Both band-probe waves and the body-binding probe are pure in-process objects --
the gate never mutates state, never writes a file, never runs a mutating
``eawf`` command.

The checks are injectable: :func:`check_idle_contract` takes the candidate
profile list and the resolver as parameters, and
:func:`check_skill_body_binding` takes the ``run_skill`` callable (each
defaulting to the live production value) so the failure modes are testable
without editing shipped profiles or the engine. Each returns a typed
:class:`GateResult` and the thin :func:`main` CLI maps them onto an exit code.

Invocation:

    python3 tools/idle_contract_gate.py

Exit codes:
- ``0`` -- the producer is importable + wired on for a non-empty band that
  resolves band-scoped (not global), AND the emit-time body validation rejects
  a drifted body, AND no newly-defined contract in the staged diff ships idle.
- ``1`` -- a contract failed (the failure is named on stderr).
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import pydantic

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import Wave
from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.platform.profiles.models import ProfileBody, VerifyBlock
from eawf.surfaces.render.envelope import OutputEnvelope
from eawf.workflow.dispatch.spec_jury import produce_spec_jury_verdict  # noqa: F401
from eawf.workflow.skills.engine import (
    ProbeOutcome,
    Skill,
    SkillContext,
    SkillResult,
)
from eawf.workflow.skills.engine import run_skill as _run_skill
from eawf.workflow.verify.readiness import resolve_wave_verify_block

#: A resolver with the shape of
#: :func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`. Injected so
#: the global-flip failure mode is testable with a stub resolver that returns
#: an always-enforcing block.
type ResolveFn = Callable[[VerifyBlock | None, Wave], VerifyBlock | None]

#: Probe file scope that IS UI surface (per
#: :func:`eawf.kernel.spec.heuristics.is_ui_scope`). A band profile MUST resolve
#: to ``enforce=True`` for a wave touching this scope.
_UI_SCOPE = "src/eawf/surfaces/tui/app.py"

#: Probe file scope that is NOT UI surface. A band-scoped (not global) profile
#: MUST resolve to ``enforce=False`` for a wave touching this scope.
_NON_UI_SCOPE = "src/eawf/kernel/state/models.py"

_PROBE_OPENED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class GateFailure(StrEnum):
    """The mutually exclusive ways the idle-contract gate can fail.

    The order encodes precedence: an absent producer wiring (idle) is reported
    before a band/global resolution defect, so a single run names the more
    fundamental problem first.
    """

    PRODUCER_IDLE = "producer_idle"
    BAND_ENFORCES_GLOBALLY = "band_enforces_globally"
    BODY_VALIDATION_IDLE = "body_validation_idle"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Typed outcome of one idle-contract check.

    Attributes:
        passed: Whether both the not-idle and band-scoped contracts held.
        failure: The failure kind when ``passed`` is ``False``; ``None`` on a
            pass.
        message: A human-readable line; on failure it names the violated
            contract and the offending profile.
    """

    passed: bool
    failure: GateFailure | None
    message: str


def _make_probe_wave(*, scope: str) -> Wave:
    """Build a pure in-process probe :class:`Wave` whose only varying axis is *scope*.

    The wave is never persisted and never mutated; it exists only so
    :func:`resolve_wave_verify_block` can be asked how it bands a given file
    scope. The id / title are deliberately neutral (no ``uiux_bands`` token
    substring) so band membership is decided by the structural ``file_scopes``
    arm alone -- the gate can then assert the UI / non-UI split unambiguously.

    Args:
        scope: The single repo-relative file scope the probe wave declares.

    Returns:
        A validated :class:`Wave` with ``file_scopes=[scope]``.
    """
    return Wave(
        id="P00-I01-W01",
        iter_id="P00-I01",
        title="idle-contract probe wave",
        status=WaveStatus.PENDING,
        file_scopes=[scope],
        opened_at=_PROBE_OPENED_AT,
    )


def _band_enabling_profiles(profiles: Sequence[ProfileBody]) -> list[ProfileBody]:
    """Return the profiles that wire the spec-jury producer on for a real band.

    A profile wires the producer on when its ``verify`` block declares a
    non-empty :attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands`
    AND :attr:`~eawf.platform.profiles.models.VerifyBlock.enforce` is true: the
    band list is the structural opt-in and ``enforce`` is the gating bit the
    resolver narrows per wave. A profile with an empty band list (or
    ``enforce=False``, or no verify block at all) leaves the producer idle.

    Args:
        profiles: The candidate profile bodies to scan.

    Returns:
        The subset of *profiles* whose verify block enables a non-empty band
        with enforcement on.
    """
    return [
        profile
        for profile in profiles
        if profile.verify is not None and profile.verify.enforce and profile.verify.uiux_bands
    ]


def _load_shipped_profiles() -> list[ProfileBody]:
    """Load every shipped (built-in) profile body.

    Returns:
        The validated :class:`ProfileBody` for each id from
        :func:`eawf.platform.profiles.loader.list_profiles`, in id order.
    """
    return [load_profile(profile_id) for profile_id in list_profiles()]


def check_idle_contract(
    *,
    profiles: Sequence[ProfileBody] | None = None,
    resolve_fn: ResolveFn = resolve_wave_verify_block,
) -> GateResult:
    """Assert the spec-jury producer is wired on for a band and resolves band-scoped.

    The two contracts are checked in precedence order:

    1. **not-idle** -- at least one of *profiles* enables the producer via a
       non-empty ``uiux_bands`` with ``enforce=True`` (see
       :func:`_band_enabling_profiles`). The producer importability is proven
       by this module importing
       :func:`eawf.workflow.dispatch.spec_jury.produce_spec_jury_verdict` at
       module load. When no profile wires it on, the producer is idle-forever
       and the gate fails :attr:`GateFailure.PRODUCER_IDLE`.
    2. **band-scoped** -- for a band-enabling profile, *resolve_fn* resolves to
       ``enforce=True`` for a UI-scope probe wave AND ``enforce=False`` for a
       non-UI probe wave. A profile that resolves to ``enforce=True`` for the
       non-UI probe enforces fleet-wide (it would gate every wave) and the gate
       fails :attr:`GateFailure.BAND_ENFORCES_GLOBALLY`.

    Args:
        profiles: Candidate profile bodies. ``None`` loads the shipped
            built-in profiles via :func:`_load_shipped_profiles`. Tests inject
            a synthetic list to exercise the idle / global failure modes
            without editing shipped profiles.
        resolve_fn: The band-conditional resolver under test. Defaults to
            :func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`;
            tests inject a stub to force the global-flip path.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the
        producer is wired on for a non-empty band AND that band profile
        resolves band-scoped (UI enforces, non-UI does not); otherwise
        ``failure`` names the first violated contract.
    """
    candidate = list(profiles) if profiles is not None else _load_shipped_profiles()

    band_profiles = _band_enabling_profiles(candidate)
    if not band_profiles:
        return GateResult(
            passed=False,
            failure=GateFailure.PRODUCER_IDLE,
            message=(
                "spec-jury producer is idle: no shipped profile enables a verify band "
                "(a non-empty 'uiux_bands' with 'enforce: true'); the producer "
                "'produce_spec_jury_verdict' is importable but never wired on"
            ),
        )

    ui_wave = _make_probe_wave(scope=_UI_SCOPE)
    non_ui_wave = _make_probe_wave(scope=_NON_UI_SCOPE)
    for profile in band_profiles:
        ui_resolved = resolve_fn(profile.verify, ui_wave)
        non_ui_resolved = resolve_fn(profile.verify, non_ui_wave)
        ui_enforces = ui_resolved is not None and ui_resolved.enforce
        non_ui_enforces = non_ui_resolved is not None and non_ui_resolved.enforce
        if not ui_enforces or non_ui_enforces:
            return GateResult(
                passed=False,
                failure=GateFailure.BAND_ENFORCES_GLOBALLY,
                message=(
                    f"band profile {profile.name!r} enforces globally, not band-scoped: "
                    f"ui_scope enforce={ui_enforces} (expected True), "
                    f"non_ui_scope enforce={non_ui_enforces} (expected False); "
                    "a band profile must gate only UI/UX waves, never the whole fleet"
                ),
            )

    band_names = ", ".join(sorted(profile.name for profile in band_profiles))
    return GateResult(
        passed=True,
        failure=None,
        message=(
            f"idle-contract gate: ok (spec-jury producer wired on by [{band_names}]; "
            "band resolves enforce=True for UI, enforce=False for non-UI)"
        ),
    )


# =========================================================================== #
# Binding-proof probe: emit-time body validation must reject a drifted body.
# =========================================================================== #

#: A ``run_skill`` with the shape of
#: :func:`eawf.workflow.skills.engine.run_skill`. Injected so the
#: body-validation-idle failure mode is testable: a stub that returns an
#: envelope without validating the body makes the probe stop raising.
type RunSkillFn = Callable[[Skill, SkillContext], OutputEnvelope]

#: The registered skill the binding probe drifts. ``/audit`` resolves to
#: :class:`~eawf.workflow.skills.bodies.audit.AuditBody`, whose only required
#: fields are ``scope_id`` and ``kind`` -- a minimal valid body that an extra
#: key drifts unambiguously.
_PROBE_SKILL_NAME = "/audit"

#: The extra-forbid key the probe injects to drift the body. It is not a field
#: on any registered body model, so an ``extra="forbid"`` model rejects it.
_DRIFT_KEY = "__idle_contract_probe_drift__"

#: Pure in-process probe context. The scope / session are well-formed URN-shaped
#: strings; the probe never reads ``.ea/`` or persists anything, so they only
#: need to satisfy the envelope header's type, not resolve to real state.
_PROBE_SCOPE = "urn:eawf:v1:state:QR/P00"
_PROBE_SESSION = "urn:eawf:v1:store:QR/sessions/SES-PROBE"


class _DriftedBodySkill(Skill):
    """Pure in-process probe skill that emits a deliberately drifted dict body.

    The skill's :meth:`probe` always succeeds with a synthetic instrument map
    (it never shells out, so the binding probe stays hermetic), and its
    :meth:`action` returns a :class:`SkillResult` whose ``body`` is a dict that
    is valid for :class:`~eawf.workflow.skills.bodies.audit.AuditBody` except
    for one extra-forbid key. Driving this skill through ``run_skill`` exercises
    exactly the emit-time body-validation chokepoint: a live binding rejects the
    drift with :class:`pydantic.ValidationError` before the envelope is built.
    """

    name = _PROBE_SKILL_NAME

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        """Return an always-ok probe with a synthetic instrument map (no shell)."""
        return ProbeOutcome(ok=True, instrument_probe={"git": "ok"})

    def action(self, ctx: SkillContext) -> SkillResult:
        """Return an ``ok`` result whose dict body carries one extra-forbid key."""
        body: dict[str, object] = {
            "scope_id": _PROBE_SCOPE,
            "kind": "evaluation",
            _DRIFT_KEY: "this key is not a field on AuditBody",
        }
        return SkillResult(status="ok", body=body)


def check_skill_body_binding(
    *,
    run_skill_fn: RunSkillFn = _run_skill,
) -> GateResult:
    """Assert the emit-time body-validation binding rejects a drifted dict body.

    Builds a pure in-process :class:`_DriftedBodySkill` whose action returns a
    dict body that is valid for the registered ``/audit`` body model except for
    one :data:`_DRIFT_KEY` extra-forbid key, then drives it through
    *run_skill_fn*. A live emit-time binding (the
    :func:`~eawf.workflow.skills.engine._validate_body` chokepoint that
    ``run_skill`` invokes before building the envelope) raises
    :class:`pydantic.ValidationError` on the drift -- proving the binding is
    not idle.

    The probe is the meta-binding for the W02/W03/W05 emit-validation work: if a
    later refactor drops the ``_validate_body`` call (or neuters it to a no-op),
    *run_skill_fn* returns an envelope WITHOUT raising, this check fails
    :attr:`GateFailure.BODY_VALIDATION_IDLE`, and the gate exits non-zero. The
    failure-mode injection seam is *run_skill_fn*: a test passes a stub that
    skips validation to prove the gate bites when the binding is removed.

    The probe mutates nothing -- it never writes a file, never touches
    ``.ea/``, and never runs a mutating ``eawf`` command. The synthetic scope /
    session strings exist only to satisfy the envelope header's type.

    Args:
        run_skill_fn: The skill engine under test. Defaults to
            :func:`eawf.workflow.skills.engine.run_skill`; a test injects a
            no-validation stub to exercise the idle failure mode.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when
        *run_skill_fn* raised :class:`pydantic.ValidationError` on the drifted
        body; otherwise ``failure`` is :attr:`GateFailure.BODY_VALIDATION_IDLE`.
    """
    skill = _DriftedBodySkill()
    ctx = SkillContext(scope=_PROBE_SCOPE, session=_PROBE_SESSION)
    try:
        run_skill_fn(skill, ctx)
    except pydantic.ValidationError:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (emit-time body validation rejected a "
                f"drifted {_PROBE_SKILL_NAME} body with pydantic.ValidationError)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.BODY_VALIDATION_IDLE,
        message=(
            "emit-time body validation is idle: a drifted dict body (an "
            f"extra-forbid key on the {_PROBE_SKILL_NAME} body model) was emitted "
            "through run_skill WITHOUT raising pydantic.ValidationError; the "
            "body-validation-at-emit binding regressed to a no-op"
        ),
    )


# =========================================================================== #
# Meta-gate: detect a newly-defined contract that ships idle in a diff.
# =========================================================================== #

#: Repo root, derived once from this file's location (``tools/`` is a sibling
#: of ``src/`` and ``tests/``). The tree-scan default reads files relative to
#: this root so the gate works from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Contract-family detectors over a single added source line. The meta-gate is
#: scoped tightly to these families so a plain internal helper (e.g.
#: ``def _coerce_row(...)``) is never flagged. Each pattern captures the
#: contract symbol name in group ``sym`` so the finding can name the orphan.
#:
#: - ``check_*`` / ``*_gate`` / ``*_lint`` function defs are the gate / lint /
#:   validator family.
#: - a ``@register(...)`` / ``@register_check(...)`` decorator whose argument is
#:   a ``CheckKind`` string token registers a check runner; the decorated name
#:   is the contract.
#: - a new ``OracleTier.T<n>_*`` dispatch arm is an oracle-tier branch.
_CONTRACT_DEF_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*def\s+(?P<sym>check_[A-Za-z0-9_]+)\s*\("),
    re.compile(r"^\s*def\s+(?P<sym>[A-Za-z0-9_]+_gate)\s*\("),
    re.compile(r"^\s*def\s+(?P<sym>[A-Za-z0-9_]+_lint)\s*\("),
)

#: A ``CheckKind``-runner registration: a ``@register(...)`` /
#: ``@register_check(...)`` decorator line that names a ``CheckKind`` token. The
#: decorated def on the following added line carries the contract symbol.
_CHECKKIND_DECORATOR_RE = re.compile(r"^\s*@register(?:_check)?\s*\(.*CheckKind.*\)\s*$")

#: A decorated def line (used to recover the symbol a ``CheckKind`` decorator
#: registers).
_DECORATED_DEF_RE = re.compile(r"^\s*def\s+(?P<sym>[A-Za-z0-9_]+)\s*\(")

#: A new oracle-tier dispatch arm: a reference to an ``OracleTier.T<n>_<NAME>``
#: member in an added line. The member token is the contract symbol.
_ORACLE_TIER_RE = re.compile(r"OracleTier\.(?P<sym>T\d+_[A-Z0-9_]+)")

#: A new ``eawf0##_*.py`` lint rule module under the lint package. The file's
#: rule id (``eawf0##``) is the contract symbol.
_LINT_MODULE_RE = re.compile(r"^src/eawf/platform/lint/(?P<sym>eawf0\d\d)_[A-Za-z0-9_]+\.py$")

#: A unified-diff hunk-header carrying the file path of the *added* side.
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(?P<path>.+)$")

#: The unified-diff ``--- /dev/null`` old-side marker, present only when the
#: file is genuinely added (a modified file's old side is ``--- a/<path>``).
#: The lint-module path detector keys on this so a mere edit of an existing
#: ``eawf0##_*.py`` module is not mistaken for a new lint contract -- only a
#: brand-new module file registers the rule-id contract. ``git diff`` also
#: emits a ``new file mode`` line for an addition, but ``--- /dev/null`` is the
#: portable signal both real git and the test fixtures carry.
_NEW_FILE_OLD_SIDE_RE = re.compile(r"^--- /dev/null$")

#: Repo-relative path prefixes a contract may legitimately be DEFINED under.
#: A contract is defined in shipped source (``src/``) or a gate script
#: (``tools/``); a ``tests/`` file that constructs a contract-shaped token is a
#: test fixture (or the discharge itself), never a new contract, so the parser
#: ignores added lines in test files to avoid flagging its own fixtures.
_SOURCE_PREFIXES: tuple[str, ...] = ("src/", "tools/")

#: An added line in a unified diff (``+`` prefix, but not the ``+++`` header).
_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+ )(?P<body>.*)$")


class MissingDischarge(StrEnum):
    """Which idle-contract discharge a finding reports as missing.

    A contract is discharged only when it is both *called* (a call-site outside
    its defining module proves it runs in production) AND *asserted* (a test
    references it so a regression is caught). The order is informational only.
    """

    NO_CALL_SITE = "no_call_site"
    NO_ASSERTING_TEST = "no_asserting_test"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class IdleContractFinding:
    """A newly-defined contract that ships idle (an orphan).

    Attributes:
        symbol: The contract symbol name (function / tier member / rule id).
        module: The repo-relative path of the file defining the contract.
        missing: Which discharge is absent -- no call-site, no asserting test,
            or both.
    """

    symbol: str
    module: str
    missing: MissingDischarge


@dataclass(frozen=True, slots=True)
class _ContractDef:
    """An added contract definition parsed out of a diff (internal).

    Attributes:
        symbol: The contract symbol name to chase for discharges.
        module: The repo-relative path of the defining file.
    """

    symbol: str
    module: str


#: A diff source: returns the unified-diff text for a rev range. Injected so
#: tests feed a synthetic diff without a git repo (mirrors how
#: :func:`check_idle_contract` injects ``profiles`` / ``resolve_fn``).
type DiffFn = Callable[[str], str]

#: A tree-scan source: returns the repo-relative paths of every source / test
#: file in the working tree. Injected alongside :data:`ReadFn` so tests feed a
#: synthetic tree without touching the real one.
type TreeFn = Callable[[], Sequence[str]]

#: A file-read source: returns the text of a repo-relative path. Injected so
#: the discharge scan reads the synthetic tree the test built.
type ReadFn = Callable[[str], str]


def _default_diff(diff_range: str) -> str:
    """Return the unified diff for *diff_range* via git.

    Args:
        diff_range: A git rev range (``HEAD~1..HEAD``) or a flag the diff
            subcommand accepts (``--cached``).

    Returns:
        The unified-diff text. Empty when the range has no changes.
    """
    proc = subprocess.run(
        ["git", "diff", "--unified=0", diff_range],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _default_tree() -> list[str]:
    """Return the repo-relative paths of tracked Python sources and tests.

    Returns:
        Every ``.py`` path under ``src/`` and ``tests/`` plus the ``tools/``
        gate scripts, repo-relative and sorted.
    """
    paths: list[str] = []
    for top in ("src", "tests", "tools"):
        root = _REPO_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            paths.append(str(path.relative_to(_REPO_ROOT)))
    return sorted(paths)


def _default_read(path: str) -> str:
    """Return the text of repo-relative *path*, or empty if it is unreadable.

    Args:
        path: A repo-relative file path.

    Returns:
        The file text, or ``""`` when the file is absent (e.g. a path that was
        renamed away after the diff was taken).
    """
    target = _REPO_ROOT / path
    if not target.is_file():
        return ""
    return target.read_text(encoding="utf-8", errors="replace")


def _family_symbols_on_line(body: str) -> list[str]:
    """Return the contract symbols a single added source line defines.

    Recognizes a :data:`_CONTRACT_DEF_PATTERNS` ``def`` family (``check_*`` /
    ``*_gate`` / ``*_lint``) and an :data:`_ORACLE_TIER_RE` tier arm. A plain
    helper line matches nothing, so it yields an empty list.

    Args:
        body: The added line's text (the ``+`` prefix already stripped).

    Returns:
        The contract symbol names found on the line, in detection order.
    """
    symbols: list[str] = []
    for pattern in _CONTRACT_DEF_PATTERNS:
        family = pattern.match(body)
        if family is not None:
            symbols.append(family.group("sym"))
            break
    tier = _ORACLE_TIER_RE.search(body)
    if tier is not None:
        symbols.append(tier.group("sym"))
    return symbols


def _parse_added_contract_defs(diff_text: str) -> list[_ContractDef]:
    """Parse the contract-family symbols newly defined in *diff_text*.

    Only added lines (``+`` prefixed) under a :data:`_SOURCE_PREFIXES` path are
    considered, and only those that match a :data:`_CONTRACT_DEF_PATTERNS`
    family, a ``CheckKind``-runner decorator, an :data:`_ORACLE_TIER_RE` arm, or
    a new :data:`_LINT_MODULE_RE` module. Plain helpers never match, and added
    lines under ``tests/`` are skipped (a contract-shaped token in a test is a
    fixture, not a new contract), so neither becomes a finding.

    Args:
        diff_text: A unified diff (``git diff`` output).

    Returns:
        The de-duplicated contract definitions, in first-seen order.
    """
    parser = _DiffContractParser()
    for raw in diff_text.splitlines():
        parser.feed(raw)
    return parser.defs


@dataclass(slots=True)
class _DiffContractParser:
    """A line-at-a-time state machine that collects added contract defs.

    The diff walk is a small state machine: a ``--- /dev/null`` old-side marker
    flags the next file as a genuine addition, a ``+++ b/<path>`` header sets
    the file scope (and, when the file is newly added, may itself name a
    lint-module contract), then each added line in a source file is matched
    against the contract families. The ``CheckKind`` decorator/def pairing
    spans two lines, so the pending flag carries that one bit of state between
    :meth:`feed` calls.

    Attributes:
        defs: The de-duplicated contract definitions collected so far.
    """

    defs: list[_ContractDef] = field(default_factory=list)
    _seen: set[tuple[str, str]] = field(default_factory=set)
    _current_file: str = ""
    _pending_checkkind: bool = False
    _next_file_is_new: bool = False

    def feed(self, raw: str) -> None:
        """Advance the state machine by one raw diff line.

        Args:
            raw: A single line of unified-diff text.
        """
        if _NEW_FILE_OLD_SIDE_RE.match(raw) is not None:
            self._next_file_is_new = True
            return
        file_match = _DIFF_FILE_RE.match(raw)
        if file_match is not None:
            self._enter_file(file_match.group("path"))
            return

        # Only added lines in shipped source / gate scripts define a contract;
        # a contract-shaped token added under tests/ is a fixture, never a new
        # contract (this is what keeps the meta-gate from flagging itself).
        if not self._current_file.startswith(_SOURCE_PREFIXES):
            return
        added = _ADDED_LINE_RE.match(raw)
        if added is not None:
            self._feed_added(added.group("body"))

    def _enter_file(self, path: str) -> None:
        self._current_file = path
        self._pending_checkkind = False
        # A lint-module contract is keyed on the file path, so it is "new" only
        # when the module file itself is newly added -- editing an existing
        # eawf0##_*.py module adds no new rule-id contract.
        is_new = self._next_file_is_new
        self._next_file_is_new = False
        if not is_new:
            return
        lint_match = _LINT_MODULE_RE.match(path)
        if lint_match is not None:
            self._record(lint_match.group("sym"))

    def _feed_added(self, body: str) -> None:
        # A def on the line right after a CheckKind decorator is the registered
        # runner; a non-def line breaks the adjacency and falls through.
        if self._pending_checkkind:
            self._pending_checkkind = False
            decorated = _DECORATED_DEF_RE.match(body)
            if decorated is not None:
                self._record(decorated.group("sym"))
                return
        if _CHECKKIND_DECORATOR_RE.match(body) is not None:
            self._pending_checkkind = True
            return
        for symbol in _family_symbols_on_line(body):
            self._record(symbol)

    def _record(self, symbol: str) -> None:
        key = (symbol, self._current_file)
        if key not in self._seen:
            self._seen.add(key)
            self.defs.append(_ContractDef(symbol=symbol, module=self._current_file))


def _wired_through_own_main(symbol: str, defining_module: str, read_fn: ReadFn) -> bool:
    """Return whether a ``tools/`` gate script wires *symbol* through its own ``main``.

    A ``src/`` contract proves it runs via a production caller in another module,
    but a ``tools/`` gate script's production caller IS its own ``main`` (the
    pre-commit hook invokes ``python tools/<gate>.py``, which runs ``main``).
    A gate-check function referenced inside that module's ``main`` body is
    therefore genuinely wired on, not idle -- so this same-module wiring counts
    as a call-site for a ``tools/`` script (and only there; a ``src/`` module's
    ``main`` does not, since shipped contracts must run from production code).

    Args:
        symbol: The contract symbol to chase.
        defining_module: The repo-relative path of the file that defines it.
        read_fn: Reader for a repo-relative path.

    Returns:
        ``True`` when *defining_module* is a ``tools/`` script whose ``main``
        function body references *symbol*.
    """
    if not defining_module.startswith("tools/"):
        return False
    text = read_fn(defining_module)
    main_match = re.search(r"^def main\(", text, flags=re.MULTILINE)
    if main_match is None:
        return False
    # The main body runs to the next top-level def/class or the module guard.
    tail = text[main_match.start() :]
    end_match = re.search(r"\n(?:def |class |if __name__)", tail[1:])
    main_body = tail if end_match is None else tail[: end_match.start() + 1]
    needle = re.compile(rf"\b{re.escape(symbol)}\b")
    # The def line itself is not a call; only a reference in the body counts.
    body_after_signature = main_body.split("\n", 1)[1] if "\n" in main_body else ""
    return needle.search(body_after_signature) is not None


def _has_call_site(symbol: str, defining_module: str, tree: Iterable[str], read_fn: ReadFn) -> bool:
    """Return whether *symbol* is referenced in a non-test file other than its module.

    A call-site outside the defining module proves the contract runs in
    production (a same-module self-reference does not discharge a ``src/``
    contract, and a test reference is counted separately as the asserting-test
    discharge). The one same-module exception is a ``tools/`` gate script that
    wires the check through its own ``main`` -- that IS the production caller for
    a gate script (see :func:`_wired_through_own_main`).

    Args:
        symbol: The contract symbol to chase.
        defining_module: The repo-relative path of the file that defines it.
        tree: The repo-relative paths to scan.
        read_fn: Reader for a repo-relative path.

    Returns:
        ``True`` when some non-test, non-defining file references *symbol*, or
        when a ``tools/`` gate script wires it through its own ``main``.
    """
    if _wired_through_own_main(symbol, defining_module, read_fn):
        return True
    needle = re.compile(rf"\b{re.escape(symbol)}\b")
    for path in tree:
        if path == defining_module:
            continue
        if path.startswith("tests/") or "/tests/" in path:
            continue
        if needle.search(read_fn(path)):
            return True
    return False


def _has_asserting_test(symbol: str, tree: Iterable[str], read_fn: ReadFn) -> bool:
    """Return whether a test file references *symbol*.

    Pragmatically, a test under ``tests/`` that imports or references the
    symbol name discharges the asserting-test contract: the symbol is pulled
    into a test module's namespace, so a regression has somewhere to fail.

    Args:
        symbol: The contract symbol to chase.
        tree: The repo-relative paths to scan.
        read_fn: Reader for a repo-relative path.

    Returns:
        ``True`` when some ``tests/`` file references *symbol*.
    """
    needle = re.compile(rf"\b{re.escape(symbol)}\b")
    for path in tree:
        if not (path.startswith("tests/") or "/tests/" in path):
            continue
        if needle.search(read_fn(path)):
            return True
    return False


def detect_idle_contracts(
    diff_range: str = "--cached",
    *,
    diff_fn: DiffFn = _default_diff,
    tree_fn: TreeFn = _default_tree,
    read_fn: ReadFn = _default_read,
) -> list[IdleContractFinding]:
    """Flag every newly-defined contract in *diff_range* that ships idle.

    The meta-gate parses the contract-family symbols a diff adds (see
    :func:`_parse_added_contract_defs`), then for each chases two discharges in
    the resulting working tree: a call-site outside the defining module
    (proves it runs) and an asserting test that references it (proves a
    regression is caught). A contract missing either discharge is an orphan and
    yields one :class:`IdleContractFinding`; a contract with BOTH yields none.

    The diff, tree, and read sources are injectable so tests feed a synthetic
    diff + tree without a git repo, mirroring how :func:`check_idle_contract`
    injects its ``profiles`` / ``resolve_fn``. The function mutates nothing --
    it only reads.

    Args:
        diff_range: A git rev range (``HEAD~1..HEAD``) or the staged-diff flag
            (``--cached``, the default so the pre-commit hook needs no args).
        diff_fn: Diff source; defaults to ``git diff --unified=0 <range>``.
        tree_fn: Tree-scan source; defaults to the tracked ``src`` / ``tests``
            / ``tools`` Python files.
        read_fn: File reader; defaults to reading the working-tree file.

    Returns:
        One :class:`IdleContractFinding` per orphan contract, in the order the
        defs appear in the diff. Empty when every added contract is discharged
        (or the diff adds no contract).
    """
    contract_defs = _parse_added_contract_defs(diff_fn(diff_range))
    if not contract_defs:
        return []

    tree = list(tree_fn())
    findings: list[IdleContractFinding] = []
    for contract in contract_defs:
        has_call = _has_call_site(contract.symbol, contract.module, tree, read_fn)
        has_test = _has_asserting_test(contract.symbol, tree, read_fn)
        if has_call and has_test:
            continue
        if not has_call and not has_test:
            missing = MissingDischarge.BOTH
        elif not has_call:
            missing = MissingDischarge.NO_CALL_SITE
        else:
            missing = MissingDischarge.NO_ASSERTING_TEST
        findings.append(
            IdleContractFinding(
                symbol=contract.symbol,
                module=contract.module,
                missing=missing,
            )
        )
    return findings


def _render_findings(findings: Sequence[IdleContractFinding]) -> str:
    """Render *findings* as a human-readable multi-line failure message.

    Args:
        findings: The orphan contracts to describe.

    Returns:
        One line per finding, each naming the symbol, its module, and the
        missing discharge.
    """
    lines = [f"idle-contract meta-gate: {len(findings)} new contract(s) ship idle:"]
    for finding in findings:
        lines.append(
            f"  - {finding.symbol} (in {finding.module}) is missing {finding.missing.value}; "
            "a new contract needs a call-site outside its module AND an asserting test"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """Run all three idle-contract gates over the staged diff and current tree.

    The original B091 single-contract check (:func:`check_idle_contract`) runs
    first, then the emit-validation binding-proof probe
    (:func:`check_skill_body_binding`), then the meta-gate
    (:func:`detect_idle_contracts`) over the staged diff. All must pass; the
    exit code is non-zero when any fails.

    Args:
        argv: Process argv. ``argv[1]``, when present, overrides the default
            ``--cached`` diff range fed to the meta-gate (e.g. ``HEAD~1..HEAD``
            for a CI range check).

    Returns:
        ``0`` when all three gates pass; ``1`` when any fails.
    """
    diff_range = argv[1] if len(argv) > 1 else "--cached"

    failed = False
    result = check_idle_contract()
    if result.passed:
        print(result.message)
    else:
        print(result.message, file=sys.stderr)
        failed = True

    # Pass the module-level run_skill explicitly so a test (or a future caller)
    # can patch it via attribute assignment -- a default-bound parameter would
    # snapshot the unpatched function at def time (see the same pattern below
    # for the meta-gate's diff / tree / read sources).
    binding = check_skill_body_binding(run_skill_fn=_run_skill)
    if binding.passed:
        print(binding.message)
    else:
        print(binding.message, file=sys.stderr)
        failed = True

    # Pass the module-level default sources explicitly so a test (or a future
    # caller) can patch them via attribute assignment -- a default-bound
    # parameter would snapshot the unpatched function at def time.
    findings = detect_idle_contracts(
        diff_range,
        diff_fn=_default_diff,
        tree_fn=_default_tree,
        read_fn=_default_read,
    )
    if findings:
        print(_render_findings(findings), file=sys.stderr)
        failed = True
    else:
        print("idle-contract meta-gate: ok (no new contract ships idle)")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
