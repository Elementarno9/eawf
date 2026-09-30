"""The trust scorecard is advisory: only its named surface reads it.

No dispatch, admission, integration or acceptance gate may consume the
scorecard until a calibration threshold is ratified against a labelled
dataset. The census below reads every source module's imports, so a gate
that starts importing the scorecard reds this test instead of shipping.
"""

from __future__ import annotations

import ast
from pathlib import Path

import eawf
from eawf.workflow.estimation.trust_scorecard import SCORECARD_CONSUMER, TrustScorecard

_SOURCE_ROOT = Path(eawf.__file__).parent
_SCORECARD_MODULE = "eawf.workflow.estimation.trust_scorecard"
_SCORECARD_FILE = Path("workflow/estimation/trust_scorecard.py")

#: The modules allowed to import the scorecard, each with its role.
_READERS = {Path("surfaces/cli/commands/why.py"): "the eawf why surface"}


def _importers(root: Path) -> set[Path]:
    """Return every module under *root* that imports the scorecard module."""
    found: set[Path] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(name == _SCORECARD_MODULE for name in names):
                found.add(path.relative_to(root))
    return found


def test_meas_027_only_the_named_surface_imports_the_scorecard() -> None:
    importers = _importers(_SOURCE_ROOT) - {_SCORECARD_FILE}

    assert importers == set(_READERS)


def test_meas_027_the_census_sees_a_gate_that_imports_the_scorecard(tmp_path: Path) -> None:
    """The census is not vacuous: a planted gate import is found."""
    gate = tmp_path / "workflow" / "verify" / "gate.py"
    gate.parent.mkdir(parents=True)
    gate.write_text(
        "from eawf.workflow.estimation.trust_scorecard import compute_trust_scorecard\n",
        encoding="utf-8",
    )

    assert _importers(tmp_path) == {Path("workflow/verify/gate.py")}


def test_meas_027_the_scorecard_states_it_is_advisory_and_names_its_consumer() -> None:
    fields = TrustScorecard.model_fields

    assert fields["advisory"].default is True
    assert fields["consumer"].default == SCORECARD_CONSUMER == "eawf why"
