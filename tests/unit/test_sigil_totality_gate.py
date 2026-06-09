"""Unit tests for the sigil-totality gate.

Covers the structural gate that proves every TUI-render status enum value (and
every lifecycle FSM terminal) resolves to a REAL glyph through the single
resolver :func:`eawf.surfaces.tui.widgets.sigils.status_sigil` -- never a bare
``.value`` word, never a ``?`` fallthrough:

- the gate PASSES on the REAL resolver: every covered status value resolves to
  a real glyph in both the unicode and ascii columns;
- the coverage union spans ``list(EnumCls)`` for every render enum AND the FSM
  terminal keys (so a status that exists only as an FSM state is covered);
- the NEGATIVE control: a stub resolver that returns a bare ``.value`` for one
  deliberately-unmapped value makes the gate FAIL and CITE the offending member
  -- proving the gate is not idle;
- a resolver that RAISES ``KeyError`` for one value (a true unmapped member)
  also makes the gate FAIL;
- the thin :func:`tools.sigil_totality_gate.main` CLI maps a pass onto exit
  ``0``.

The gate logic lives in :mod:`eawf.platform.lint.sigil_totality` (a packaged,
importable-by-name surface with a production call-site at the ``eawf hook
sigil-totality`` command). The standalone ``main`` CLI shim is loaded via
:mod:`importlib` because ``tools/`` is excluded from the package and so is not
importable by name. The resolver is injectable (``resolve_fn``) so the
negative-control never touches the real resolver.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AuditVerdict,
    OpenQuestionStatus,
    WaveStatus,
)
from eawf.platform.lint.sigil_totality import (
    check_sigil_totality,
    covered_members,
)
from eawf.surfaces.tui.widgets.sigils import ResolvedSigil, status_sigil

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "sigil_totality_gate.py"
_TOOL_DIR = _GATE_PATH.parent


def _load_cli_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("sigil_totality_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sigil_totality_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_gate_passes_on_the_real_resolver() -> None:
    # The real resolver resolves every covered status to a real glyph.
    result = check_sigil_totality()
    assert result.passed is True
    assert result.misses == ()
    assert result.checked > 0


def test_coverage_spans_every_render_enum_and_fsm_terminal() -> None:
    # The coverage union includes every render-enum member AND the FSM terminal
    # keys; the four ABANDONED/ARCHIVED extended states are present.
    members = set(covered_members())
    assert WaveStatus.ABANDONED in members
    assert AgentReportVerdict.PASS_WITH_FOLLOWUPS in members
    assert AuditVerdict.MINOR in members
    # The FSM terminals (e.g. WaveStatus.CLOSED / FAILED) are covered too.
    assert WaveStatus.CLOSED in members
    assert WaveStatus.FAILED in members


def test_negative_control_bare_value_makes_the_gate_fire() -> None:
    # A resolver that rubber-stamps a bare .value word for one value must make
    # the gate FAIL and cite the offending member -- proving it is not idle.
    def broken_resolve(member: object) -> ResolvedSigil:
        if member is WaveStatus.ABANDONED:
            return ResolvedSigil(
                glyph_unicode=WaveStatus.ABANDONED.value,
                glyph_ascii=WaveStatus.ABANDONED.value,
                tint_hex=None,
            )
        return status_sigil(member)

    result = check_sigil_totality(resolve_fn=broken_resolve)
    assert result.passed is False
    assert any("WaveStatus.ABANDONED" in miss for miss in result.misses)
    assert any("bare .value" in miss for miss in result.misses)


def test_negative_control_keyerror_makes_the_gate_fire() -> None:
    # A resolver that raises KeyError for one value (a true unmapped member)
    # must also make the gate FAIL and name the unmapped member.
    def raising_resolve(member: object) -> ResolvedSigil:
        if member is OpenQuestionStatus.BLOCKED:
            raise KeyError(member)
        return status_sigil(member)

    result = check_sigil_totality(resolve_fn=raising_resolve)
    assert result.passed is False
    assert any(
        "OpenQuestionStatus.BLOCKED" in miss and "unmapped" in miss for miss in result.misses
    )


def test_negative_control_question_mark_fallthrough_fires() -> None:
    # A resolver that emits the literal '?' fallthrough for one value fails.
    def fallthrough_resolve(member: object) -> ResolvedSigil:
        if member is AuditVerdict.MINOR:
            return ResolvedSigil(glyph_unicode="?", glyph_ascii="?", tint_hex=None)
        return status_sigil(member)

    result = check_sigil_totality(resolve_fn=fallthrough_resolve)
    assert result.passed is False
    assert any("AuditVerdict.MINOR" in miss and "fallthrough" in miss for miss in result.misses)


def test_main_returns_zero_on_pass() -> None:
    mod = _load_cli_module()
    assert mod.main([]) == 0
