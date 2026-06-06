"""SVG visual-fidelity oracle stack tests (FS16, P29-I11-W05).

Covers the two oracle kinds added for the SVG fidelity layer:

* T2 ``svg_well_formed`` (``xmllint --noout``) -- fully testable on the
  authoring host (``xmllint`` ships with macOS / most Linux).
* T5 ``svg_pixel_diff`` (``resvg`` render + byte compare) -- guarded by
  :func:`shutil.which` so the assertions that need the renderer skip on
  a host without the pinned ``resvg`` binary; CI runs the real diff.

The CR-1 ordering invariant -- a T2 well-formedness fail aborts BEFORE
the T5 pixel diff -- is exercised by sorting both kinds' gate-kind
strings through the oracle tier map and asserting that an ascending,
short-circuit-on-pass escalation never reaches the T5 kind once the T2
kind fails.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from eawf.kernel.spec.common import OracleTier, _tier_for_gate_kind
from eawf.workflow.audit_dsl.kinds.svg_pixel_diff import check_svg_pixel_diff
from eawf.workflow.audit_dsl.kinds.svg_well_formed import check_svg_well_formed
from eawf.workflow.audit_dsl.models import CheckKind, CheckResult, CheckSpec
from eawf.workflow.audit_dsl.registry import CHECK_REGISTRY, CheckFn

_SVG_DIR = Path(__file__).parent
_FIXTURES = _SVG_DIR / "fixtures"
_FONTS = _SVG_DIR / "fonts"
_GOLDEN = _SVG_DIR / "golden"

_WELL_FORMED = "tests/snapshots/svg/fixtures/well_formed.svg"
_FENCED = "tests/snapshots/svg/fixtures/fenced_malformed.svg"
_GOLDEN_PNG = "tests/snapshots/svg/golden/well_formed.png"
_FONTS_REL = "tests/snapshots/svg/fonts"

_REPO_ROOT = _SVG_DIR.parents[2]

_HAS_XMLLINT = shutil.which("xmllint") is not None
_HAS_RESVG = shutil.which("resvg") is not None


def _golden_is_placeholder() -> bool:
    """True when the committed golden is the resvg-absent placeholder marker.

    The placeholder PNG carries a ``PLACEHOLDER golden`` tEXt chunk; until the
    real golden is seeded (``eawf vfl approve --kind svg`` on a resvg host), the
    render-match assertion would byte-compare against a 1x1 stub, so it skips.
    """
    png = _REPO_ROOT / _GOLDEN_PNG
    if not png.exists():
        return False
    return b"PLACEHOLDER golden" in png.read_bytes()


_GOLDEN_PLACEHOLDER = _golden_is_placeholder()


# --------------------------------------------------------------------------
# fixtures-on-disk sanity (these are committed assets the kinds read)
# --------------------------------------------------------------------------


def test_committed_fixtures_present() -> None:
    """The committed SVG / font / golden assets exist on disk."""
    assert (_FIXTURES / "well_formed.svg").is_file()
    assert (_FIXTURES / "fenced_malformed.svg").is_file()
    assert (_FONTS / "EawfTestMono-Regular.ttf").is_file()
    assert (_GOLDEN / "well_formed.png").is_file()


# --------------------------------------------------------------------------
# T2 svg_well_formed -- boundary + error path
# --------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_XMLLINT, reason="xmllint not installed")
def test_svg_well_formed_passes_on_well_formed_svg() -> None:
    """A real well-formed SVG file passes the T2 check."""
    spec = CheckSpec(kind="svg_well_formed", name="t2-ok", args={"path": _WELL_FORMED})
    result = check_svg_well_formed(spec, _REPO_ROOT)
    assert result.status == "pass"
    assert result.passed is True


@pytest.mark.skipif(not _HAS_XMLLINT, reason="xmllint not installed")
def test_svg_well_formed_passes_on_inline_svg() -> None:
    """An inline (string) well-formed SVG passes the T2 check."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
    spec = CheckSpec(kind="svg_well_formed", name="t2-inline", args={"svg": svg})
    result = check_svg_well_formed(spec, _REPO_ROOT)
    assert result.status == "pass"


@pytest.mark.skipif(not _HAS_XMLLINT, reason="xmllint not installed")
def test_svg_well_formed_fails_on_fenced_svg_with_parser_error() -> None:
    """A Markdown-fenced SVG is not well-formed XML -> T2 fail with line:col error.

    The worked example from the wave spec: a ```svg ... ``` block makes
    ``xmllint`` exit non-zero and emit a ``line:col`` parser error, which
    the kind surfaces in ``details``.
    """
    spec = CheckSpec(kind="svg_well_formed", name="t2-fenced", args={"path": _FENCED})
    result = check_svg_well_formed(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    # xmllint prints "<file>:<line>: parser error : ..."; the kind keeps
    # the line:col diagnostic so the failure is actionable.
    assert "parser error" in result.details
    assert ":1:" in result.details or "fenced_malformed.svg:1" in result.details


@pytest.mark.skipif(not _HAS_XMLLINT, reason="xmllint not installed")
def test_svg_well_formed_fails_on_inline_fenced_svg() -> None:
    """An inline fenced SVG string also fails T2 (no path needed)."""
    fenced = "```svg\n<svg></svg>\n```\n"
    spec = CheckSpec(kind="svg_well_formed", name="t2-inline-fenced", args={"svg": fenced})
    result = check_svg_well_formed(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "parser error" in result.details


# --------------------------------------------------------------------------
# T2 svg_well_formed -- malformed args degrade to fail, never raise
# --------------------------------------------------------------------------


def test_svg_well_formed_missing_args_fails_not_raises() -> None:
    """Neither path nor svg -> fail (never a raised exception)."""
    spec = CheckSpec(kind="svg_well_formed", name="t2-noargs", args={})
    result = check_svg_well_formed(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "exactly one" in result.details


def test_svg_well_formed_both_args_fails() -> None:
    """Both path and svg -> fail (mutually exclusive)."""
    spec = CheckSpec(
        kind="svg_well_formed",
        name="t2-both",
        args={"path": _WELL_FORMED, "svg": "<svg/>"},
    )
    result = check_svg_well_formed(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert "exactly one" in (result.details or "")


def test_svg_well_formed_nonstr_path_fails() -> None:
    """A non-str path arg -> fail (never raises)."""
    spec = CheckSpec(kind="svg_well_formed", name="t2-badtype", args={"path": 123})
    result = check_svg_well_formed(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert "must be a str" in (result.details or "")


def test_svg_well_formed_missing_file_fails() -> None:
    """A path to a non-existent file -> fail."""
    spec = CheckSpec(
        kind="svg_well_formed",
        name="t2-missing",
        args={"path": "tests/snapshots/svg/fixtures/does_not_exist.svg"},
    )
    result = check_svg_well_formed(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert "not found" in (result.details or "")


# --------------------------------------------------------------------------
# CR-1 ordering: T2 fail aborts before the T5 pixel diff
# --------------------------------------------------------------------------


def test_tier_ordering_t2_before_t5() -> None:
    """``svg_well_formed`` (T2) strictly precedes ``svg_pixel_diff`` (T5)."""
    t2 = _tier_for_gate_kind("svg_well_formed")
    t5 = _tier_for_gate_kind("svg_pixel_diff")
    assert t2 is OracleTier.T2_STRUCTURAL
    assert t5 is OracleTier.T5_GOLDEN
    assert int(t2) < int(t5)


@pytest.mark.skipif(not _HAS_XMLLINT, reason="xmllint not installed")
def test_fenced_svg_aborts_before_pixel_diff() -> None:
    """A fenced SVG fails T2 and the T5 pixel diff is never invoked.

    Mirrors the runner's ascending-tier, short-circuit-on-pass
    escalation: the gates are ordered by tier; the T2 check runs first;
    its fail status means the escalation does not produce a pass, and a
    spy on the T5 kind confirms it is never reached for the failed
    criterion.
    """
    gates: list[CheckKind] = ["svg_well_formed", "svg_pixel_diff"]
    ordered = sorted(gates, key=lambda kind: int(_tier_for_gate_kind(kind)))
    assert ordered == ["svg_well_formed", "svg_pixel_diff"]

    pixel_diff_called = False

    def spy_pixel_diff(spec: CheckSpec, cwd: Path) -> CheckResult:
        nonlocal pixel_diff_called
        pixel_diff_called = True
        return check_svg_pixel_diff(spec, cwd)

    dispatch: dict[CheckKind, CheckFn] = {
        "svg_well_formed": check_svg_well_formed,
        "svg_pixel_diff": spy_pixel_diff,
    }

    # Escalate in tier order; stop at the first fail (the T2 gate). The
    # T5 pixel diff must never be dispatched once T2 has falsified the SVG.
    aborted_at: CheckKind | None = None
    for kind in ordered:
        spec = CheckSpec(kind=kind, name=f"order-{kind}", args={"path": _FENCED})
        result = dispatch[kind](spec, _REPO_ROOT)
        if result.status != "pass":
            aborted_at = kind
            break

    assert aborted_at == "svg_well_formed"
    assert pixel_diff_called is False


# --------------------------------------------------------------------------
# registry wiring
# --------------------------------------------------------------------------


def test_both_kinds_registered() -> None:
    """Both new kinds are bound in the dispatch registry."""
    assert CHECK_REGISTRY["svg_well_formed"] is check_svg_well_formed
    assert CHECK_REGISTRY["svg_pixel_diff"] is check_svg_pixel_diff


# --------------------------------------------------------------------------
# T5 svg_pixel_diff -- skip-guarded on a host without resvg
# --------------------------------------------------------------------------


def test_svg_pixel_diff_blocked_without_resvg() -> None:
    """When ``resvg`` is absent the kind returns ``blocked`` (not fail/raise).

    Runs unconditionally: on a dev host without resvg this asserts the
    blocked path; on a CI host with resvg the branch is simply not taken
    and the assertion is skipped via the guard below.
    """
    if _HAS_RESVG:
        pytest.skip("resvg installed; blocked-path assertion is host-without-resvg only")
    spec = CheckSpec(
        kind="svg_pixel_diff",
        name="t5-blocked",
        args={"svg": _WELL_FORMED, "golden": _GOLDEN_PNG, "fonts_dir": _FONTS_REL},
    )
    result = check_svg_pixel_diff(spec, _REPO_ROOT)
    assert result.status == "blocked"
    assert result.details == "resvg not installed"


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg not installed")
@pytest.mark.skipif(
    _GOLDEN_PLACEHOLDER,
    reason="golden is placeholder; run eawf vfl approve --kind svg to seed the real golden",
)
def test_svg_pixel_diff_render_matches_golden() -> None:
    """A fixture SVG renders bit-identically to its committed golden (CR-2).

    Skips locally (resvg absent); CI has the pinned binary and renders +
    byte-compares the real golden under ``golden/well_formed.png``.
    """
    spec = CheckSpec(
        kind="svg_pixel_diff",
        name="t5-match",
        args={"svg": _WELL_FORMED, "golden": _GOLDEN_PNG, "fonts_dir": _FONTS_REL},
    )
    result = check_svg_pixel_diff(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


@pytest.mark.skipif(not _HAS_RESVG, reason="resvg not installed")
def test_svg_pixel_diff_missing_golden_fails() -> None:
    """A missing golden path -> fail (never raises)."""
    spec = CheckSpec(
        kind="svg_pixel_diff",
        name="t5-missing-golden",
        args={
            "svg": _WELL_FORMED,
            "golden": "tests/snapshots/svg/golden/nope.png",
            "fonts_dir": _FONTS_REL,
        },
    )
    result = check_svg_pixel_diff(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert "not found" in (result.details or "")


def test_svg_pixel_diff_malformed_args_fails_or_blocks() -> None:
    """Malformed args degrade to blocked (no resvg) or fail (resvg present).

    On a host without resvg the which-guard short-circuits to
    ``blocked`` before args are inspected; with resvg present a missing
    ``svg`` arg degrades to ``fail``. Either way the kind never raises.
    """
    spec = CheckSpec(kind="svg_pixel_diff", name="t5-noargs", args={})
    result = check_svg_pixel_diff(spec, _REPO_ROOT)
    assert result.status in {"blocked", "fail"}
    assert result.passed is False
