"""Unit tests for ``tools/idle_contract_gate.py``.

Covers the deterministic idle-contract gate that guards the band-scoped
spec-jury QC gate against the B091 idle-verifier regression:

- the gate PASSES on the current tree (the producer is importable + the shipped
  ``quality`` profile wires it on for a non-empty UI/UX band that resolves
  band-scoped);
- it FAILS with :attr:`GateFailure.PRODUCER_IDLE` when fed a profile set in
  which no profile enables a verify band (empty ``uiux_bands`` everywhere) --
  the idle-forever detection;
- it FAILS with :attr:`GateFailure.BAND_ENFORCES_GLOBALLY` when the resolver
  is stubbed to enforce for a non-UI wave -- the fleet-wide-flip detection;
- the happy path (producer wired + band present + band-scoped resolver) passes
  explicitly.

It also covers the :func:`detect_idle_contracts` meta-gate that generalizes
the B091 lesson: parse the contract-family symbols a diff adds, then flag any
that ships idle (no call-site outside its module AND/OR no asserting test). The
diff / tree / read sources are injectable so these cases feed a synthetic diff
+ tree without a git repo.

The gate module is loaded via :mod:`importlib` because ``tools/`` is excluded
from the package and so is not importable by name. The checks are injectable
(``profiles`` / ``resolve_fn`` / ``diff_fn`` / ``tree_fn`` / ``read_fn``) so
the failure cases never touch shipped profiles or the real tree.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.models import Wave
from eawf.platform.profiles.models import ProfileBody, VerifyBlock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "idle_contract_gate.py"
_TOOL_DIR = _GATE_PATH.parent


def _load_module():
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("idle_contract_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["idle_contract_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


# --------------------------------------------------------------------------- #
# Synthetic-profile + resolver builders (no shipped-profile edits).
# --------------------------------------------------------------------------- #


def _profile(*, name: str, verify: VerifyBlock | None) -> ProfileBody:
    """Build a minimal :class:`ProfileBody` carrying *verify* and nothing else."""
    return ProfileBody(name=name, verify=verify)


def _band_verify() -> VerifyBlock:
    """A band-scoped enforce block: non-empty band + enforcement on."""
    return VerifyBlock(enforce=True, uiux_bands=["tui", "render"])


def _idle_verify() -> VerifyBlock:
    """An idle block: enforce on but NO band, so the producer is never wired on."""
    return VerifyBlock(enforce=True, uiux_bands=[])


def _global_resolver(verify: VerifyBlock | None, wave: Wave) -> VerifyBlock | None:
    """A resolver that enforces for EVERY wave -- the fleet-wide-flip defect."""
    del wave  # global flip ignores the per-wave band
    return verify


# --------------------------------------------------------------------------- #
# Pass path -- the real shipped tree.
# --------------------------------------------------------------------------- #


def test_gate_passes_on_current_tree(mod) -> None:
    # Default args load the shipped profiles + the real resolver: the producer
    # is wired on (quality profile) and resolves band-scoped.
    result = mod.check_idle_contract()
    assert result.passed is True
    assert result.failure is None


def test_gate_passes_with_band_profile_and_real_resolver(mod) -> None:
    # Boundary / happy path asserted explicitly: a producer-present +
    # band-present + band-scoped tree passes. Inject a one-profile list so the
    # assertion does not depend on which shipped profiles exist.
    profiles = [_profile(name="quality", verify=_band_verify())]
    result = mod.check_idle_contract(profiles=profiles)
    assert result.passed is True
    assert result.failure is None
    assert "quality" in result.message


# --------------------------------------------------------------------------- #
# Failure path -- idle producer (no band enables enforcement).
# --------------------------------------------------------------------------- #


def test_gate_fails_when_no_profile_enables_a_band(mod) -> None:
    # Every profile leaves the band empty, so the producer is idle-forever.
    profiles = [
        _profile(name="core", verify=None),
        _profile(name="quality", verify=_idle_verify()),
    ]
    result = mod.check_idle_contract(profiles=profiles)
    assert result.passed is False
    assert result.failure is mod.GateFailure.PRODUCER_IDLE
    assert "idle" in result.message


def test_gate_fails_when_profile_list_is_empty(mod) -> None:
    # No profiles at all is the degenerate idle case.
    result = mod.check_idle_contract(profiles=[])
    assert result.passed is False
    assert result.failure is mod.GateFailure.PRODUCER_IDLE


# --------------------------------------------------------------------------- #
# Failure path -- band enforces globally (resolver returns enforce for non-UI).
# --------------------------------------------------------------------------- #


def test_gate_fails_when_band_enforces_globally(mod) -> None:
    # The profile declares a band, but the (stubbed) resolver enforces for the
    # non-UI probe too -- a fleet-wide flip that would gate every wave.
    profiles = [_profile(name="quality", verify=_band_verify())]
    result = mod.check_idle_contract(profiles=profiles, resolve_fn=_global_resolver)
    assert result.passed is False
    assert result.failure is mod.GateFailure.BAND_ENFORCES_GLOBALLY
    assert "globally" in result.message
    assert "quality" in result.message


def test_gate_reports_idle_before_global(mod) -> None:
    # Precedence: a profile set that is BOTH idle (empty band) and paired with a
    # global resolver names the more fundamental idle failure first.
    profiles = [_profile(name="quality", verify=_idle_verify())]
    result = mod.check_idle_contract(profiles=profiles, resolve_fn=_global_resolver)
    assert result.failure is mod.GateFailure.PRODUCER_IDLE


# --------------------------------------------------------------------------- #
# Probe-wave construction -- UI vs non-UI band membership.
# --------------------------------------------------------------------------- #


def test_probe_waves_split_on_ui_scope(mod) -> None:
    # The gate's UI / non-UI probe scopes must actually straddle the band line
    # so the band-scoped assertion is meaningful: with the REAL resolver the UI
    # probe enforces and the non-UI probe does not.
    band = _band_verify()
    ui_wave = mod._make_probe_wave(scope=mod._UI_SCOPE)
    non_ui_wave = mod._make_probe_wave(scope=mod._NON_UI_SCOPE)

    from eawf.workflow.verify.readiness import resolve_wave_verify_block

    ui_resolved = resolve_wave_verify_block(band, ui_wave)
    non_ui_resolved = resolve_wave_verify_block(band, non_ui_wave)
    assert ui_resolved is not None and ui_resolved.enforce is True
    assert non_ui_resolved is not None and non_ui_resolved.enforce is False


def test_probe_wave_id_title_carry_no_band_token(mod) -> None:
    # Band membership must be decided by file_scopes alone -- the neutral id /
    # title must not accidentally contain a 'tui' / 'render' substring.
    wave = mod._make_probe_wave(scope=mod._NON_UI_SCOPE)
    corpus = f"{wave.id}\n{wave.title}".lower()
    assert "tui" not in corpus
    assert "render" not in corpus


# --------------------------------------------------------------------------- #
# CLI wrapper.
# --------------------------------------------------------------------------- #


def test_cli_returns_zero_on_pass(mod) -> None:
    code = mod.main(["idle_contract_gate.py"])
    assert code == 0


def test_cli_returns_one_on_failure(mod, monkeypatch) -> None:
    # Force the idle failure by making the shipped-profile loader return a set
    # with no band, then confirm the CLI maps the failed result onto exit 1.
    monkeypatch.setattr(
        mod,
        "_load_shipped_profiles",
        lambda: [
            ProfileBody(
                name="quality",
                verify=VerifyBlock(enforce=True, uiux_bands=[]),
            )
        ],
    )
    code = mod.main(["idle_contract_gate.py"])
    assert code == 1


def test_make_probe_wave_is_validated(mod) -> None:
    # The probe wave is a real validated Wave (not a loose dict) so the gate
    # exercises the same model the resolver consumes in production.
    wave = mod._make_probe_wave(scope=mod._UI_SCOPE)
    assert isinstance(wave, Wave)
    assert wave.file_scopes == [mod._UI_SCOPE]
    assert isinstance(wave.opened_at, datetime)
    assert wave.opened_at.tzinfo is UTC


# --------------------------------------------------------------------------- #
# Meta-gate: detect a newly-defined contract that ships idle in a diff.
# --------------------------------------------------------------------------- #


def _diff_adding(path: str, lines: list[str]) -> str:
    """Build a minimal unified diff that ADDS *lines* to *path*.

    The header is the ``+++ b/<path>`` marker the parser keys file scope on;
    each body line is a ``+`` added line. ``--unified=0`` style (no context).
    """
    body = "\n".join(f"+{line}" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"


def _tree_from(files: dict[str, str]):
    """Return injectable ``tree_fn`` / ``read_fn`` over a synthetic *files* map.

    *files* maps a repo-relative path to its full text. ``tree_fn`` lists the
    keys; ``read_fn`` returns the text (empty for an unknown path, mirroring an
    absent working-tree file).
    """

    def tree_fn() -> list[str]:
        return sorted(files)

    def read_fn(path: str) -> str:
        return files.get(path, "")

    return tree_fn, read_fn


def test_detect_idle_contracts_flags_orphan_def(mod) -> None:
    # A new check_* contract with NO call-site and NO asserting test is an
    # orphan: exactly one finding naming the symbol + both discharges missing.
    defining = "src/eawf/platform/lint/foo.py"
    diff = _diff_adding(defining, ["def check_widget_parity(node):", "    return True"])
    tree_fn, read_fn = _tree_from({defining: "def check_widget_parity(node):\n    return True\n"})
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: diff,
        tree_fn=tree_fn,
        read_fn=read_fn,
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.symbol == "check_widget_parity"
    assert finding.module == defining
    assert finding.missing is mod.MissingDischarge.BOTH


def test_detect_idle_contracts_clean_with_call_and_test(mod) -> None:
    # The SAME def WITH a call-site outside its module AND an asserting test
    # yields zero findings -- the contract is fully discharged.
    defining = "src/eawf/platform/lint/foo.py"
    diff = _diff_adding(defining, ["def check_widget_parity(node):", "    return True"])
    tree_fn, read_fn = _tree_from(
        {
            defining: "def check_widget_parity(node):\n    return True\n",
            "src/eawf/platform/lint/runner.py": (
                "from .foo import check_widget_parity\n"
                "def run(node):\n    return check_widget_parity(node)\n"
            ),
            "tests/unit/test_foo.py": (
                "from eawf.platform.lint.foo import check_widget_parity\n"
                "def test_it():\n    assert check_widget_parity(None) is True\n"
            ),
        }
    )
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: diff,
        tree_fn=tree_fn,
        read_fn=read_fn,
    )
    assert findings == []


def test_detect_idle_contracts_caller_but_no_test_still_fires(mod) -> None:
    # A call-site alone does NOT discharge the contract: with a caller but no
    # asserting test, exactly one finding flags the missing test discharge.
    defining = "src/eawf/platform/lint/foo.py"
    diff = _diff_adding(defining, ["def check_widget_parity(node):", "    return True"])
    tree_fn, read_fn = _tree_from(
        {
            defining: "def check_widget_parity(node):\n    return True\n",
            "src/eawf/platform/lint/runner.py": (
                "from .foo import check_widget_parity\n"
                "def run(node):\n    return check_widget_parity(node)\n"
            ),
        }
    )
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: diff,
        tree_fn=tree_fn,
        read_fn=read_fn,
    )
    assert len(findings) == 1
    assert findings[0].missing is mod.MissingDischarge.NO_ASSERTING_TEST


def test_detect_idle_contracts_ignores_internal_helper(mod) -> None:
    # A plain internal helper is NOT a contract family, so even an orphan
    # ``_coerce_row`` yields zero findings -- the detector is tightly scoped.
    defining = "src/eawf/platform/lint/foo.py"
    diff = _diff_adding(defining, ["def _coerce_row(row):", "    return dict(row)"])
    tree_fn, read_fn = _tree_from({defining: "def _coerce_row(row):\n    return dict(row)\n"})
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: diff,
        tree_fn=tree_fn,
        read_fn=read_fn,
    )
    assert findings == []


def test_detect_idle_contracts_recognizes_gate_and_lint_families(mod) -> None:
    # The ``*_gate`` and ``*_lint`` families are recognized alongside
    # ``check_*``; each orphan yields its own finding.
    defining = "tools/widget_gate.py"
    diff = _diff_adding(
        defining,
        ["def widget_gate(diff):", "    return []", "def widget_lint(node):", "    return []"],
    )
    tree_fn, read_fn = _tree_from({defining: ""})
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: diff,
        tree_fn=tree_fn,
        read_fn=read_fn,
    )
    symbols = {f.symbol for f in findings}
    assert symbols == {"widget_gate", "widget_lint"}


def test_detect_idle_contracts_recognizes_checkkind_runner(mod) -> None:
    # A ``@register(...CheckKind...)`` decorated def registers a CheckKind
    # runner; the decorated name is the contract and an orphan one fires.
    defining = "src/eawf/workflow/audit_dsl/kinds/widget.py"
    diff = _diff_adding(
        defining,
        [
            '@register_check("widget_parity", kind=CheckKind)',
            "def run_widget(spec):",
            "    return []",
        ],
    )
    tree_fn, read_fn = _tree_from({defining: ""})
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: diff,
        tree_fn=tree_fn,
        read_fn=read_fn,
    )
    assert len(findings) == 1
    assert findings[0].symbol == "run_widget"


def test_detect_idle_contracts_recognizes_oracle_tier_arm(mod) -> None:
    # A new ``OracleTier.T<n>_<NAME>`` dispatch arm is an oracle-tier branch;
    # an orphan tier member fires.
    defining = "src/eawf/workflow/verify/oracle.py"
    diff = _diff_adding(
        defining,
        ["    if tier == OracleTier.T8_WIDGET:", "        return _resolve_widget()"],
    )
    tree_fn, read_fn = _tree_from({defining: ""})
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: diff,
        tree_fn=tree_fn,
        read_fn=read_fn,
    )
    assert len(findings) == 1
    assert findings[0].symbol == "T8_WIDGET"


def test_detect_idle_contracts_recognizes_lint_rule_module(mod) -> None:
    # A new ``eawf0##_*.py`` module under the lint package is a contract family
    # keyed on the file path; the rule id is the symbol.
    defining = "src/eawf/platform/lint/eawf099_widget.py"
    diff = _diff_adding(defining, ["def run(node):", "    return []"])
    tree_fn, read_fn = _tree_from({defining: ""})
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: diff,
        tree_fn=tree_fn,
        read_fn=read_fn,
    )
    assert len(findings) == 1
    assert findings[0].symbol == "eawf099"


def test_detect_idle_contracts_ignores_contract_token_in_test_file(mod) -> None:
    # A contract-shaped token added under tests/ is a fixture, not a new
    # contract, so the parser skips it -- this is what keeps the meta-gate
    # from flagging its own synthetic fixtures.
    test_path = "tests/unit/test_widget.py"
    diff = _diff_adding(
        test_path,
        ["def check_widget_parity(node):", "    return True"],
    )
    tree_fn, read_fn = _tree_from({test_path: "def check_widget_parity(node):\n    return True\n"})
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: diff,
        tree_fn=tree_fn,
        read_fn=read_fn,
    )
    assert findings == []


def test_detect_idle_contracts_empty_diff_yields_nothing(mod) -> None:
    # A diff that adds no contract symbol yields zero findings (and never even
    # scans the tree).
    findings = mod.detect_idle_contracts(
        "HEAD~1..HEAD",
        diff_fn=lambda _r: "",
        tree_fn=lambda: [],
        read_fn=lambda _p: "",
    )
    assert findings == []


def test_main_returns_nonzero_on_orphan_contract(mod, monkeypatch) -> None:
    # End-to-end binding proof: an orphan contract in the staged diff makes the
    # meta-gate fire and main() exit non-zero (the B091 check still passes).
    # Patch the injectable default sources so main()'s real detect path runs
    # against a synthetic diff + tree (no git, no real tree).
    defining = "src/eawf/platform/lint/foo.py"
    diff = _diff_adding(defining, ["def check_widget_parity(node):", "    return True"])
    tree_fn, read_fn = _tree_from({defining: "def check_widget_parity(node):\n    return True\n"})
    monkeypatch.setattr(mod, "_default_diff", lambda _r: diff)
    monkeypatch.setattr(mod, "_default_tree", tree_fn)
    monkeypatch.setattr(mod, "_default_read", read_fn)
    code = mod.main(["idle_contract_gate.py"])
    assert code == 1


def test_main_returns_zero_when_no_orphan_contract(mod, monkeypatch) -> None:
    # The negative path end-to-end: an empty meta-gate result + a passing B091
    # check makes main() exit 0. An empty diff adds no contract symbol.
    monkeypatch.setattr(mod, "_default_diff", lambda _r: "")
    monkeypatch.setattr(mod, "_default_tree", lambda: [])
    monkeypatch.setattr(mod, "_default_read", lambda _p: "")
    code = mod.main(["idle_contract_gate.py"])
    assert code == 0
