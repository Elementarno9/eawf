"""No expected-minutes figure is derived from anything but the effort-mapping policy record.

AUTH-041 retired the five-point ladder, and MEAS-019 (the effort-unit mapping is a
policy record with a revision and a digest) moved the one effort constant that replaced
it into :class:`~eawf.workflow.estimation.mapping.EffortMapping`: 0.8 EU, 24 minutes,
revision 2. ``expected_minutes`` derives from that record alone. This census walks every
module under ``src/eawf`` and refuses three shapes that would bring a code constant back:

* a retired identifier (the ladder's raw-minutes default, a label multiplier, the per-label
  EU table or the helpers that multiplied by them; the module constants the record
  replaced; a pessimistic multiplier, since MEAS-017 makes the pessimistic value a
  declared upper bound);
* arithmetic assigned to ``expected_minutes`` that does not read the mapping record;
* arithmetic over a table indexed by a wave's ``effort_bucket`` label.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

SRC: Final = Path(__file__).resolve().parents[2] / "src" / "eawf"

#: The identifiers the retired ladder derived its minutes from.
RETIRED: Final[frozenset[str]] = frozenset(
    {
        "DEFAULT_RAW_MINUTES",
        "DEFAULT_CENTRAL_MULTIPLIER",
        "DEFAULT_PESSIMISTIC_MULTIPLIER",
        "BUCKET_EU",
        "calc_expected_eu",
        "calc_pessimistic_eu",
        "EFFORT_EU",
        "EFFORT_MINUTES",
        "EU_MINUTES",
        "EFFORT_DISPERSION_MINUTES",
        "pessimistic_multiplier",
    }
)

#: The only names an expected-minutes derivation may rest on: the mapping record's fields.
CONSTANTS: Final[frozenset[str]] = frozenset({"effort_eu", "effort_minutes"})

#: The live derivation the ``estimate set`` verb carried before the epoch-1 command surface
#: was deleted, quoted from that source so the census is proven on a real defect.
RETIRED_ESTIMATE_SET: Final = """
expected = quantize(
    calc_expected_eu(DEFAULT_RAW_MINUTES, DEFAULT_CENTRAL_MULTIPLIER, DEFAULT_EU_MINUTES),
    DEFAULT_EU_QUANTUM,
)
expected_minutes = expected * DEFAULT_EU_MINUTES
"""


def _names(node: ast.AST) -> set[str]:
    """Return every bare name and attribute name *node* mentions."""
    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            out.add(child.id)
        elif isinstance(child, ast.Attribute):
            out.add(child.attr)
    return out


def _is_arithmetic(node: ast.AST) -> bool:
    return any(isinstance(child, ast.BinOp) for child in ast.walk(node))


def _expected_minutes_values(tree: ast.AST) -> list[ast.expr]:
    """Return every value assigned or passed to ``expected_minutes``."""
    values: list[ast.expr] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.keyword):
            targets, value = [ast.Name(id=node.arg or "")], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets, value = [node.target], node.value
        if value is not None and any("expected_minutes" in _names(t) for t in targets):
            values.append(value)
    return values


def ladder_findings(source: str, *, where: str) -> list[str]:
    """Return one finding per ladder-derived minutes shape in *source*.

    Args:
        source: Python source text.
        where: The label findings are reported under.

    Returns:
        ``<where>:<line>: <reason>`` per finding, in walk order.
    """
    tree = ast.parse(source)
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in RETIRED:
                findings.append(f"{where}:{node.lineno}: retired identifier {name}")
        if isinstance(node, ast.BinOp):
            for operand in (node.left, node.right):
                if isinstance(operand, ast.Subscript) and "effort_bucket" in _names(operand.slice):
                    findings.append(f"{where}:{node.lineno}: arithmetic over a size-label table")
    for value in _expected_minutes_values(tree):
        if _is_arithmetic(value) and not (_names(value) & CONSTANTS):
            findings.append(
                f"{where}:{value.lineno}: expected_minutes derived from something "
                "other than the effort mapping record"
            )
    return findings


def test_auth_041_census_catches_the_retired_estimate_set_derivation() -> None:
    findings = ladder_findings(RETIRED_ESTIMATE_SET, where="estimation.py")

    assert any("retired identifier DEFAULT_RAW_MINUTES" in f for f in findings)
    assert any("retired identifier DEFAULT_CENTRAL_MULTIPLIER" in f for f in findings)
    assert any("expected_minutes derived" in f for f in findings)


@pytest.mark.parametrize(
    ("source", "caught"),
    [
        ("minutes = BUCKET_EU[wave.effort_bucket] * 30\n", True),
        ("eu = table[wave.effort_bucket] * EU_MINUTES\n", True),
        ("Estimate(expected_minutes=raw * 0.5)\n", True),
        ("expected_minutes = EFFORT_EU * EU_MINUTES\n", True),
        ("pessimistic = expected * pessimistic_multiplier\n", True),
        ("expected_minutes = mapping.effort_eu * mapping.eu_minutes\n", False),
        ("Estimate(expected_minutes=CURRENT_EFFORT_MAPPING.effort_minutes)\n", False),
        ("spec = Spec(expected_minutes=row.expected_minutes if row else None)\n", False),
        ("", False),
    ],
)
def test_auth_041_census_boundaries(source: str, caught: bool) -> None:
    assert bool(ladder_findings(source, where="probe.py")) is caught


def test_auth_041_census_rejects_unparseable_source() -> None:
    with pytest.raises(SyntaxError):
        ladder_findings("expected_minutes = (", where="broken.py")


def test_auth_041_no_expected_minutes_derivation_outside_the_constant_in_src() -> None:
    modules = sorted(SRC.rglob("*.py"))
    assert modules, f"no Python source found under {SRC}"
    findings = [
        finding
        for module in modules
        for finding in ladder_findings(
            module.read_text(encoding="utf-8"), where=module.relative_to(SRC).as_posix()
        )
    ]
    assert findings == []
