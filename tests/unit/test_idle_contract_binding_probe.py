"""Unit tests for the binding-proof probe in ``tools/idle_contract_gate.py``.

The probe :func:`check_skill_body_binding` is the meta-binding that keeps the
emit-time body-validation chokepoint (``run_skill`` -> ``_validate_body``) from
silently regressing to idle. It drives a deliberately drifted dict body (an
extra-forbid key on the registered ``/audit`` body model) through ``run_skill``
and asserts the emit path RAISES :class:`pydantic.ValidationError`.

These tests pin both halves of the gate-that-guards-the-gate:

- with the live binding present, the probe raises on the drifted body, so
  :func:`check_skill_body_binding` PASSES and the full :func:`main` gate exits
  ``0``;
- a stub ``run_skill`` that skips body validation makes the probe stop raising,
  so the check FAILS with :attr:`GateFailure.BODY_VALIDATION_IDLE` -- proving
  the gate bites the moment the binding is removed.

Boundary: the probe is pure and hermetic -- it writes no file and never touches
``.ea/``; this is asserted by neutralizing the filesystem write paths and the
``.ea`` tree and confirming the probe still raises.

The gate module is loaded via :mod:`importlib` because ``tools/`` is excluded
from the package and so is not importable by name (mirrors
``tests/unit/test_idle_contract_gate.py``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.surfaces.render.envelope import (
    EnvelopeBody,
    EnvelopeFooter,
    EnvelopeHeader,
    OutputEnvelope,
)
from eawf.workflow.skills.engine import Skill, SkillContext

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "idle_contract_gate.py"
_TOOL_DIR = _GATE_PATH.parent


def _load_module() -> Any:
    """Load ``tools/idle_contract_gate.py`` as an importable module object."""
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("idle_contract_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["idle_contract_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod() -> Any:
    """Return the freshly loaded gate module."""
    return _load_module()


def _no_validation_run_skill(skill: Skill, ctx: SkillContext) -> OutputEnvelope:
    """A ``run_skill`` stand-in that emits the body WITHOUT validating it.

    Models the regression the probe must catch: the emit-time
    :func:`~eawf.workflow.skills.engine._validate_body` chokepoint has been
    dropped, so even a drifted dict body is folded into the envelope verbatim
    and no :class:`pydantic.ValidationError` is raised.

    Args:
        skill: The probe skill (its ``action`` supplies the drifted body).
        ctx: The probe context.

    Returns:
        A populated :class:`OutputEnvelope` carrying the unvalidated body.
    """
    from datetime import UTC, datetime

    result = skill.action(ctx)
    now = datetime.now(UTC)
    header = EnvelopeHeader(
        skill=skill.name,
        scope_id=ctx.scope,
        session=ctx.session,
        started_at=now,
        finished_at=now,
        status=result.status,
        instrument_probe={},
    )
    footer = EnvelopeFooter()
    body: EnvelopeBody = result.body
    return OutputEnvelope(header=header, body=body, footer=footer)


def test_check_skill_body_binding_passes_with_live_binding(mod: Any) -> None:
    """With the W02 binding present the probe raises on a drifted body -> pass."""
    result = mod.check_skill_body_binding()
    assert result.passed is True
    assert result.failure is None
    assert "rejected" in result.message


def test_check_skill_body_binding_idle_when_validation_skipped(mod: Any) -> None:
    """A no-validation ``run_skill`` stub makes the probe stop raising -> fail."""
    result = mod.check_skill_body_binding(run_skill_fn=_no_validation_run_skill)
    assert result.passed is False
    assert result.failure is mod.GateFailure.BODY_VALIDATION_IDLE
    assert "idle" in result.message


def test_main_exits_zero_with_live_binding(mod: Any) -> None:
    """The full gate exits ``0`` on the live tree (every contract discharged)."""
    code = mod.main(["idle_contract_gate.py"])
    assert code == 0


def test_main_exits_nonzero_when_binding_idle(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralizing the emit-validation binding makes the whole gate exit non-zero.

    Patching the engine's ``run_skill`` import target on the gate module to the
    no-validation stub reproduces the regression end-to-end: the probe stops
    raising, :func:`check_skill_body_binding` reports the idle contract, and
    :func:`main` returns a non-zero code.
    """
    monkeypatch.setattr(mod, "_run_skill", _no_validation_run_skill)
    code = mod.main(["idle_contract_gate.py"])
    assert code != 0


def test_probe_writes_no_file_and_skips_dot_ea(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: the probe never writes a file and never reads/writes ``.ea/``.

    Filesystem write paths are replaced with raisers and any ``.ea`` path access
    is forbidden; the probe must still drive the drifted body through the live
    binding and raise -- proving it is pure and hermetic.
    """

    def _forbid_write(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("probe attempted a filesystem write")

    def _forbid_dot_ea(self: Path, *args: Any, **kwargs: Any) -> Any:
        if ".ea" in self.parts:
            raise AssertionError(f"probe touched .ea/ via {self}")
        return _orig_read_text(self, *args, **kwargs)

    _orig_read_text = Path.read_text
    monkeypatch.setattr(Path, "write_text", _forbid_write)
    monkeypatch.setattr(Path, "write_bytes", _forbid_write)
    monkeypatch.setattr(Path, "mkdir", _forbid_write)
    monkeypatch.setattr(Path, "read_text", _forbid_dot_ea)

    result = mod.check_skill_body_binding()
    assert result.passed is True
    assert result.failure is None


# --------------------------------------------------------------------------- #
# CR-02: dynamic-exercise leg -- a bound contract must produce runtime output.
# --------------------------------------------------------------------------- #


def _fixed_store(rows_by_stem: dict[str, list[dict[str, Any]]]) -> Any:
    """Return an injectable ``store_rows_fn`` over a synthetic store map.

    Maps a store stem to its parsed records; an unknown stem reads honest-empty
    (mirroring an absent ``.ea/store/<stem>.jsonl`` file).
    """

    def store_rows_fn(stem: str) -> list[dict[str, Any]]:
        return rows_by_stem.get(stem, [])

    return store_rows_fn


def test_check_contract_exercised_flags_zero_output_contract(mod: Any) -> None:
    """Every bound contract with an empty store yields one NO_RUNTIME_OUTPUT finding."""
    findings = mod.check_contract_exercised(
        store_rows_fn=_fixed_store({}),
        jury_convened_fn=lambda: False,
    )
    assert {f.symbol for f in findings} == {"verdict_producer", "eu_capture", "calibration"}
    assert all(f.missing is mod.MissingDischarge.NO_RUNTIME_OUTPUT for f in findings)


def test_check_contract_exercised_passes_once_rows_exist(mod: Any) -> None:
    """One exercising row per bound contract discharges the dynamic leg."""
    store = _fixed_store(
        {
            "auditor_report": [{"report_id": "AR-1"}],
            "actual": [{"elapsed_eu": 1.5}],
            "gold_label": [{"wave_id": "P30-I22-W01", "label": "pass"}],
        }
    )
    findings = mod.check_contract_exercised(store_rows_fn=store, jury_convened_fn=lambda: False)
    assert findings == []


def test_check_contract_exercised_ignores_zero_elapsed_eu_row(mod: Any) -> None:
    """Boundary: an actuals row that measured nothing (elapsed_eu == 0) does not count."""
    store = _fixed_store(
        {
            "auditor_report": [{"report_id": "AR-1"}],
            "actual": [{"elapsed_eu": 0.0}],
            "gold_label": [{"wave_id": "W", "label": "pass"}],
        }
    )
    findings = mod.check_contract_exercised(store_rows_fn=store, jury_convened_fn=lambda: False)
    assert [f.symbol for f in findings] == ["eu_capture"]
    assert findings[0].missing is mod.MissingDischarge.NO_RUNTIME_OUTPUT


def test_check_contract_exercised_skips_ballot_until_jury_convenes(mod: Any) -> None:
    """The ballot contract is skipped while the jury-convened predicate is False.

    A contract nothing exercises yet must not wedge the phase close, so while the
    jury has never convened the ballot is skipped even though its store is empty.
    Once the jury convenes, an empty ballot store flags NO_RUNTIME_OUTPUT.
    """
    empty = _fixed_store({})
    skipped = mod.check_contract_exercised(
        store_rows_fn=empty,
        jury_convened_fn=lambda: False,
        contracts=[mod._BALLOT_CONTRACT],
    )
    assert skipped == []

    flagged = mod.check_contract_exercised(
        store_rows_fn=empty,
        jury_convened_fn=lambda: True,
        contracts=[mod._BALLOT_CONTRACT],
    )
    assert [f.symbol for f in flagged] == ["juror_ballot"]
    assert flagged[0].missing is mod.MissingDischarge.NO_RUNTIME_OUTPUT


def test_i22_bound_contracts_exclude_the_ballot(mod: Any) -> None:
    """The enforced I22 set is the three bound contracts; the ballot is excluded."""
    stems = {c.store_stem for c in mod._I22_BOUND_CONTRACTS}
    assert stems == {"auditor_report", "actual", "gold_label"}
    assert "ballot" not in stems
    assert mod._BALLOT_CONTRACT.gated_by_jury is True


def test_default_store_rows_absent_file_is_honest_empty(mod: Any) -> None:
    """A store no wave has written yet reads honest-empty rather than raising."""
    assert mod._default_store_rows("no_such_store_stem_xyz") == []


def test_default_store_rows_unwraps_enveloped_payload(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enveloped record is unwrapped to its payload; a plain record is returned as-is."""
    store_dir = tmp_path / ".ea" / "store"
    store_dir.mkdir(parents=True)
    (store_dir / "actual.jsonl").write_text(
        '{"kind": "actual", "payload": {"elapsed_eu": 2.0}}\n'
        "\n"
        "not-json-skip-me\n"
        '{"wave_id": "W", "label": "pass"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_REPO_ROOT", tmp_path)
    rows = mod._default_store_rows("actual")
    assert rows == [{"elapsed_eu": 2.0}, {"wave_id": "W", "label": "pass"}]


def test_main_phase_close_reds_on_zero_output_contract(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --phase-close a bound contract with no runtime output reds main().

    The meta-gate + diff sources are neutralized (empty diff) so only the dynamic
    leg decides the outcome.
    """
    monkeypatch.setattr(mod, "_default_diff", lambda _r: "")
    monkeypatch.setattr(mod, "_default_tree", lambda: [])
    monkeypatch.setattr(mod, "_default_read", lambda _p: "")
    monkeypatch.setattr(mod, "_default_store_rows", lambda _stem: [])
    monkeypatch.setattr(mod, "_default_jury_convened", lambda: False)
    code = mod.main(["idle_contract_gate.py", "--phase-close"])
    assert code != 0


def test_main_phase_close_passes_when_every_contract_exercised(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --phase-close with an exercising row for each bound contract, main() exits 0."""
    monkeypatch.setattr(mod, "_default_diff", lambda _r: "")
    monkeypatch.setattr(mod, "_default_tree", lambda: [])
    monkeypatch.setattr(mod, "_default_read", lambda _p: "")
    rows = {
        "auditor_report": [{"report_id": "AR-1"}],
        "actual": [{"elapsed_eu": 3.0}],
        "gold_label": [{"wave_id": "W", "label": "pass"}],
    }
    monkeypatch.setattr(mod, "_default_store_rows", lambda stem: rows.get(stem, []))
    monkeypatch.setattr(mod, "_default_jury_convened", lambda: False)
    code = mod.main(["idle_contract_gate.py", "--phase-close"])
    assert code == 0


def test_main_without_phase_close_flag_skips_dynamic_leg(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --phase-close the dynamic leg never runs, so an empty store keeps main() green.

    This is what keeps the ordinary per-commit pre-commit run passing while the
    bound stores are still empty (runtime output accrues per phase, not per commit).
    """
    monkeypatch.setattr(mod, "_default_diff", lambda _r: "")
    monkeypatch.setattr(mod, "_default_tree", lambda: [])
    monkeypatch.setattr(mod, "_default_read", lambda _p: "")
    monkeypatch.setattr(mod, "_default_store_rows", lambda _stem: [])
    code = mod.main(["idle_contract_gate.py"])
    assert code == 0
