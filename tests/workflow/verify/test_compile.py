"""Tests for :func:`eawf.workflow.verify.compile.compile_gate` (P28-I01-W08).

Pins the W08 compile-gate contract:

* deterministic ``command_exit_zero`` gates compile to a CheckSpec that
  carries the gate's ``argv`` + the canonical ``criterion`` metadata
  shape that :func:`eawf.workflow.skills.audit.build_criterion_specs`
  already produces — same CheckSpec audit emits, no duplication;
* W15-added kwargs on the typed gate (``timeout_class``, ``scope``,
  ``wave_id``, ``wave_file_scopes``) thread through onto the compiled
  CheckSpec.args so the runner picks them up;
* ``evidence_kind`` other than ``"deterministic"`` returns ``None`` —
  jury + attested compile-paths land in v0.4.1+;
* a ``command_exit_zero`` gate with no usable ``argv`` returns ``None``
  (compile-gate stays a pure shape transform; the runner does not see
  a malformed spec);
* non-``command_exit_zero`` deterministic kinds pass through verbatim
  with ``name=gate.id`` so future registered kinds (e.g.
  ``regex_match``, ``schema_validate``) compile without per-kind
  branches here.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec, QualityDimension
from eawf.platform.profiles.models import FloorCheck
from eawf.workflow.audit_dsl.models import CheckSpec
from eawf.workflow.verify import compile_gate
from eawf.workflow.verify.compile import (
    _COMMAND_EXIT_ZERO_PASSTHROUGH_KEYS,
    FloorPackCompileError,
    compile_floor_pack,
)


def _criterion(
    cid: str = "CRIT-1",
    *,
    evidence_kind: str = "deterministic",
    gate_ids: list[str] | None = None,
) -> CriterionSpec:
    return CriterionSpec(
        id=cid,
        text=f"criterion {cid}",
        kind="behavior",
        acceptance_style="binary",
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        gate_ids=list(gate_ids or []),
        required=True,
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the compiled floor pack scores this criterion deterministically",
    )


def _command_exit_zero_gate(
    gid: str = "GATE-1",
    *,
    criterion_id: str = "CRIT-1",
    argv: list[str] | None = None,
    extra_args: dict[str, object] | None = None,
) -> GateSpec:
    args: dict[str, object] = {"argv": list(argv or ["uv", "run", "pytest", "-q"])}
    if extra_args:
        args.update(extra_args)
    return GateSpec(
        id=gid,
        criterion_id=criterion_id,
        kind="command_exit_zero",
        args=args,
        policy="block",
        cadence="every-wave",
        required=True,
    )


# ---- evidence_kind gating ---------------------------------------------------


def test_compile_gate_deterministic_returns_check_spec() -> None:
    """Deterministic criterion + command_exit_zero gate -> CheckSpec."""
    gate = _command_exit_zero_gate()
    criterion = _criterion(evidence_kind="deterministic")

    result = compile_gate(gate, criterion=criterion)

    assert isinstance(result, CheckSpec)
    assert result.kind == "command_exit_zero"
    assert result.name == "GATE-1"
    # Shape comes from build_criterion_specs: argv + criterion metadata.
    assert result.args["argv"] == ["uv", "run", "pytest", "-q"]
    assert result.args["criterion"] == "CRIT-1"


def test_compile_gate_jury_returns_none() -> None:
    """``evidence_kind='jury'`` defers to v0.4.1+ -> None."""
    gate = _command_exit_zero_gate()
    criterion = _criterion(evidence_kind="jury")

    assert compile_gate(gate, criterion=criterion) is None


def test_compile_gate_attested_returns_none() -> None:
    """``evidence_kind='attested'`` defers to v0.4.1+ -> None."""
    gate = _command_exit_zero_gate()
    criterion = _criterion(evidence_kind="attested")

    assert compile_gate(gate, criterion=criterion) is None


# ---- W15 kwargs passthrough -------------------------------------------------


def test_compile_gate_threads_timeout_class() -> None:
    """``timeout_class`` on the gate lands on the compiled CheckSpec.args."""
    gate = _command_exit_zero_gate(extra_args={"timeout_class": "quick"})
    criterion = _criterion()

    result = compile_gate(gate, criterion=criterion)

    assert result is not None
    assert result.args["timeout_class"] == "quick"


def test_compile_gate_threads_scope() -> None:
    """``scope`` on the gate lands on the compiled CheckSpec.args."""
    gate = _command_exit_zero_gate(extra_args={"scope": "touched"})
    criterion = _criterion()

    result = compile_gate(gate, criterion=criterion)

    assert result is not None
    assert result.args["scope"] == "touched"


def test_compile_gate_threads_all_w15_kwargs() -> None:
    """Every W15-added kwarg threads through compile_gate.

    Pins the full set documented in
    :data:`_COMMAND_EXIT_ZERO_PASSTHROUGH_KEYS` so a future addition
    to the runner's kwargs schema is caught here when the constant
    + the runner's args validator drift apart.
    """
    extra = {
        "timeout_class": "slow",
        "scope": "all",
        "wave_id": "P01-I01-W01",
        "wave_file_scopes": ["src/eawf/"],
    }
    gate = _command_exit_zero_gate(extra_args=extra)
    criterion = _criterion()

    result = compile_gate(gate, criterion=criterion)

    assert result is not None
    for key, value in extra.items():
        assert result.args[key] == value, f"key={key!r} did not thread through"
    # Constant matches the keys we just threaded — kept in sync.
    assert set(_COMMAND_EXIT_ZERO_PASSTHROUGH_KEYS) == set(extra)


def test_compile_gate_omitted_kwargs_default_via_runner() -> None:
    """Kwargs not on the gate stay off the compiled args (runner defaults)."""
    gate = _command_exit_zero_gate(extra_args=None)
    criterion = _criterion()

    result = compile_gate(gate, criterion=criterion)

    assert result is not None
    for key in _COMMAND_EXIT_ZERO_PASSTHROUGH_KEYS:
        assert key not in result.args


# ---- malformed / edge cases -------------------------------------------------


def test_compile_gate_command_exit_zero_without_argv_returns_none() -> None:
    """``command_exit_zero`` requires a usable argv -> None when missing.

    Construction-time validation would already reject this gate via
    :meth:`GateSpec._argv_passes_l0_policy`, so we cannot build the
    GateSpec with an empty argv. Bypass via ``model_construct`` so the
    test exercises compile_gate's own defensive None-return without
    fighting the construction validator. The defense in compile_gate
    matters because future gate kinds may carry argv-shaped args
    without the L0 policy check (e.g. internal-only ``script_run``
    kinds the planner might emit).
    """
    gate = GateSpec.model_construct(
        id="GATE-noargv",
        criterion_id="CRIT-1",
        kind="command_exit_zero",
        args={},  # no argv
        policy="block",
        cadence="every-wave",
        required=True,
        timeout_s=None,
    )
    criterion = _criterion()

    assert compile_gate(gate, criterion=criterion) is None


def test_compile_gate_passthrough_for_non_command_exit_zero_kind() -> None:
    """Other registered kinds pass through with ``name=gate.id``.

    ``criterion_in_diff`` is registered in the audit-DSL runner and
    carries a different args schema; compile_gate copies the args
    verbatim so the runner picks them up. The L0 argv-policy validator
    on :class:`GateSpec` only fires for argv-bearing kinds, so this
    construction succeeds without bypass.
    """
    gate = GateSpec(
        id="GATE-cid",
        criterion_id="CRIT-1",
        kind="criterion_in_diff",
        args={
            "criterion": "some criterion text",
            "pattern": "expected-substring",
            "file_scopes": ["src/eawf/foo.py"],
        },
        policy="block",
        cadence="every-wave",
        required=True,
    )
    criterion = _criterion()

    result = compile_gate(gate, criterion=criterion)

    assert isinstance(result, CheckSpec)
    assert result.kind == "criterion_in_diff"
    assert result.name == "GATE-cid"
    assert result.args == {
        "criterion": "some criterion text",
        "pattern": "expected-substring",
        "file_scopes": ["src/eawf/foo.py"],
    }


def test_compile_gate_passthrough_other_kind_jury_still_none() -> None:
    """The jury / attested skip happens before the kind branch."""
    gate = GateSpec(
        id="GATE-cid-jury",
        criterion_id="CRIT-jury",
        kind="criterion_in_diff",
        args={
            "criterion": "x",
            "pattern": "y",
            "file_scopes": ["src/eawf/foo.py"],
        },
        policy="block",
        cadence="every-wave",
        required=True,
    )
    criterion = _criterion(cid="CRIT-jury", evidence_kind="jury")

    assert compile_gate(gate, criterion=criterion) is None


# ---- reuse contract — same shape as audit build_criterion_specs -------------


def test_compile_gate_reuses_build_criterion_specs_shape() -> None:
    """The compiled CheckSpec carries the canonical audit-side shape.

    Cross-pin to :func:`eawf.workflow.skills.audit.build_criterion_specs`:
    a directive with ``argv`` + ``criterion`` produces the exact args
    shape compile_gate must emit. Drift here means audit and verify
    no longer hand the runner identical CheckSpec shapes — a
    rule 1 + rule 4 hygiene break that this test guards against.
    """
    from eawf.workflow.skills.audit import build_criterion_specs

    directive = {"criterion": "CRIT-1", "argv": ["uv", "run", "pytest", "-q"]}
    audit_specs = build_criterion_specs(criterion_checks=[directive], wave=None)
    assert len(audit_specs) == 1
    audit_spec = audit_specs[0]

    gate = _command_exit_zero_gate()
    verify_spec = compile_gate(gate, criterion=_criterion())

    assert verify_spec is not None
    # Same kind, same argv, same criterion metadata. The only
    # intentional delta is the spec name (compile_gate uses gate.id
    # so per-gate evidence + waivers still address it by typed
    # identity).
    assert verify_spec.kind == audit_spec.kind
    assert verify_spec.args["argv"] == audit_spec.args["argv"]
    assert verify_spec.args["criterion"] == audit_spec.args["criterion"]


# ---- boundary: validation -------------------------------------------------


def test_compile_gate_rejects_invalid_gate_type() -> None:
    """Passing a non-GateSpec raises at attribute access (defensive)."""
    with pytest.raises(AttributeError):
        compile_gate(object(), criterion=_criterion())  # type: ignore[arg-type]


# ---- W10 compile_floor_pack -------------------------------------------------


def _floor_check(
    name: str = "fc-1",
    *,
    cmd: list[str] | None = None,
    scope: str = "touched",
    cadence: str = "every-wave",
    policy: str = "warn",
    timeout_class: str = "standard",
) -> FloorCheck:
    return FloorCheck(
        name=name,
        cmd=list(cmd or ["uv", "run", "pytest", "-q"]),
        scope=scope,  # type: ignore[arg-type]
        cadence=cadence,  # type: ignore[arg-type]
        policy=policy,  # type: ignore[arg-type]
        timeout_class=timeout_class,  # type: ignore[arg-type]
    )


def test_compile_floor_pack_empty_list_yields_empty_pack() -> None:
    """An empty floor-check list compiles to an empty CheckSpec list."""
    assert compile_floor_pack([], allowlist=[]) == []


def test_compile_floor_pack_one_check_yields_one_check_spec() -> None:
    """One floor check -> one ``command_exit_zero`` CheckSpec."""
    pack = compile_floor_pack(
        [_floor_check("pytest", cmd=["uv", "run", "pytest", "-q"])],
        allowlist=["pytest"],
    )
    assert len(pack) == 1
    spec = pack[0]
    assert isinstance(spec, CheckSpec)
    assert spec.kind == "command_exit_zero"
    assert spec.name == "pytest"
    assert spec.args["argv"] == ["uv", "run", "pytest", "-q"]
    assert spec.args["criterion"] == "pytest"
    assert spec.args["scope"] == "touched"
    assert spec.args["timeout_class"] == "standard"


def test_compile_floor_pack_threads_scope_and_timeout() -> None:
    """``scope`` and ``timeout_class`` thread onto the compiled CheckSpec.args."""
    pack = compile_floor_pack(
        [_floor_check("c", cmd=["uv", "run", "mypy"], scope="all", timeout_class="slow")],
        allowlist=["mypy"],
    )
    assert pack[0].args["scope"] == "all"
    assert pack[0].args["timeout_class"] == "slow"


def test_compile_floor_pack_rejects_shell_at_compile_time() -> None:
    """A floor check whose argv head is in the shell-deny floor fails at compile time."""
    bad = _floor_check("evil", cmd=["sh", "-c", "echo pwned"])
    with pytest.raises(FloorPackCompileError, match="evil"):
        compile_floor_pack([bad], allowlist=["sh"])


def test_compile_floor_pack_rejects_with_argv_policy_detail() -> None:
    """The reject message names the offending floor-check + the L0 policy reason."""
    bad = _floor_check("path-qualified", cmd=["/usr/bin/python", "-c", "1"])
    with pytest.raises(FloorPackCompileError) as exc:
        compile_floor_pack([bad], allowlist=["python"])
    msg = str(exc.value)
    assert "path-qualified" in msg
    assert "argv" in msg.lower() or "policy" in msg.lower()


def test_compile_floor_pack_unions_default_allowlist() -> None:
    """The kernel-spec default allowlist is unioned with the profile-fed one.

    The default allowlist (:data:`DEFAULT_GATE_ARGV_ALLOWLIST`) carries
    ``uv``, ``run``, ``pytest``, ``mypy``, ``pre-commit`` etc; even
    when the profile passes an empty per-floor-check allowlist, the
    kernel-spec floor of dev-loop wrappers still validates.
    """
    pack = compile_floor_pack(
        [_floor_check("pc", cmd=["uv", "run", "pre-commit", "run", "--all-files"])],
        allowlist=[],  # No profile-fed entries.
    )
    assert len(pack) == 1
    assert pack[0].args["argv"][0] == "uv"


def test_compile_floor_pack_first_failure_stops_compilation() -> None:
    """A single bad check stops compilation; later checks are not reached."""
    good = _floor_check("good", cmd=["uv", "run", "pytest"])
    bad = _floor_check("bad", cmd=["sh", "-c", "evil"])
    # Order matters — the bad one is second so the test pins
    # "good compiles; bad raises" rather than just "raises".
    with pytest.raises(FloorPackCompileError, match="bad"):
        compile_floor_pack([good, bad], allowlist=["sh", "pytest"])


def test_compile_floor_pack_preserves_check_order() -> None:
    """Compiled CheckSpec rows match the input floor-check ordering."""
    pack = compile_floor_pack(
        [
            _floor_check("a", cmd=["uv", "run", "pre-commit"]),
            _floor_check("b", cmd=["uv", "run", "mypy"]),
            _floor_check("c", cmd=["uv", "run", "pytest"]),
        ],
        allowlist=[],
    )
    assert [spec.name for spec in pack] == ["a", "b", "c"]
