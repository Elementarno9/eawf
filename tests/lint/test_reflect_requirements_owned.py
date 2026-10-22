"""The /reflect requirement rows are owned by the Task that builds them, with tests.

SKILL-031, SKILL-032, SKILL-034 and SKILL-035 were imported owned by an epoch-1 wave
with no test behind them, so the census called them owned while nothing proved them.
Each row must now name EAWF-0486 among its owners and at least one test module that
exists in the tree. A companion test feeds the check the rows as they were imported
and proves it reds.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = _ROOT / ".ea" / "requirements.json"
_OWNER = "EAWF-0486"
_IDS = ("SKILL-031", "SKILL-032", "SKILL-034", "SKILL-035")


def _ownership_problems(rows: Iterable[Mapping[str, Any]], root: Path) -> list[str]:
    """Return why each reflect row is not owned by the Task with an existing test."""
    by_id = {row["id"]: row for row in rows}
    problems = []
    for req_id in _IDS:
        row = by_id.get(req_id)
        if row is None:
            problems.append(f"{req_id} is not in the catalog")
            continue
        if _OWNER not in row.get("owners", ()):
            problems.append(f"{req_id} is not owned by {_OWNER}")
        if not any((root / path).is_file() for path in row.get("tests", ())):
            problems.append(f"{req_id} lists no test that exists")
    return problems


def test_reflect_requirements_are_owned_by_eawf_0486() -> None:
    """Every reflect row names the Task as an owner and an existing test module."""
    catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
    assert _ownership_problems(catalog["requirements"], _ROOT) == []


def test_the_rows_as_imported_are_refused() -> None:
    """Fire proof: the imported shape, a wave owner and no test, reds on every id."""
    imported = [{"id": req_id, "owners": ["P35-I03-W02"], "tests": []} for req_id in _IDS]
    assert len(_ownership_problems(imported, _ROOT)) == 2 * len(_IDS)


def test_a_listed_test_that_does_not_exist_is_refused() -> None:
    """Error path: a test path the tree does not hold proves nothing."""
    rows = [
        {"id": req_id, "owners": [_OWNER], "tests": ["tests/no/such_test.py"]} for req_id in _IDS
    ]
    assert _ownership_problems(rows, _ROOT) == [
        f"{req_id} lists no test that exists" for req_id in _IDS
    ]


def test_a_missing_row_is_refused() -> None:
    """Boundary: an empty catalog names every id as missing."""
    assert _ownership_problems([], _ROOT) == [f"{req_id} is not in the catalog" for req_id in _IDS]
