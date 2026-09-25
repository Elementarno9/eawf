"""No console module outside the golden harness embeds a prototype literal.

The prototype's revision (``41,208``), its Milestone ``MLS-0004``, its scope name and its
Run ids are data the golden contract replays. A renderer that spells one out draws it on
every console, live or not, so the only module allowed to hold them is the harness's
:mod:`~eawf.surfaces.tui.console.prototype`, which a console built from the packaged
chrome never reaches. The scan reads every module's text, comments included, and a
literal seeded into a copy of a renderer proves it reds.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from eawf.surfaces.tui.console import prototype
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture

CONSOLE = Path(prototype.__file__).parent
FIXTURE_DIR = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"

#: The golden-harness modules allowed to hold prototype literals, relative to the package.
HARNESS: frozenset[str] = frozenset({"prototype.py"})

#: Any Run id in the prototype's shape, whether or not the registers carry it.
_RUN_ID = re.compile(r"\bRUN-[0-9a-f]{8}\b")


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the tracked prototype registers."""
    return load_fixture(FIXTURE_DIR)


@pytest.fixture(scope="module")
def literals(fixture: Fixture) -> frozenset[str]:
    """Return the prototype literals no console module may spell out."""
    proto = fixture.proto
    runs = {row.run for row in proto.fleet} | {k for k in fixture.detail if k.startswith("RUN-")}
    return frozenset({*runs, "MLS-0004", proto.scope, f"{proto.revision:,}", str(proto.revision)})


def scan(root: Path, literals: frozenset[str]) -> list[str]:
    """Return ``path:line: literal`` for every prototype literal outside the harness.

    Args:
        root: The console package directory to scan.
        literals: The exact strings no module may contain.

    Raises:
        FileNotFoundError: ``root`` is not a directory.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in HARNESS:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits = {literal for literal in literals if literal in line}
            hits |= set(_RUN_ID.findall(line))
            found.extend(f"{rel}:{number}: {hit}" for hit in sorted(hits))
    return found


@pytest.fixture
def console_copy(tmp_path: Path) -> Path:
    """Return a scratch copy of the console package the seeding tests may edit."""
    copy = tmp_path / "console"
    shutil.copytree(CONSOLE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    return copy


def test_scan_finds_no_prototype_literal_in_the_console(literals: frozenset[str]) -> None:
    assert scan(CONSOLE, literals) == []


def test_scan_covers_every_renderer_and_drawer(literals: frozenset[str]) -> None:
    scanned = {p.relative_to(CONSOLE).as_posix() for p in CONSOLE.rglob("*.py")} - HARNESS
    assert "drawers.py" in scanned
    assert "reads.py" in scanned
    assert {p.name for p in (CONSOLE / "renderers").glob("*.py")} <= {
        Path(rel).name for rel in scanned if rel.startswith("renderers/")
    }


def test_the_literal_set_names_the_prototype_revision_and_milestone(
    literals: frozenset[str],
) -> None:
    assert {"41,208", "MLS-0004", "eawf-core"} <= literals
    assert len([x for x in literals if x.startswith("RUN-")]) >= 26


@pytest.mark.parametrize(
    ("module", "seed"),
    [
        ("renderers/history.py", 'SEEDED = "MLS-0004 accepted"\n'),
        ("reads.py", 'SEEDED = "revision 41,208"\n'),
        ("drawers.py", "# drawn from RUN-538453eb\n"),
        ("renderers/activity.py", 'SEEDED = "Eä ▸ eawf-core"\n'),
        ("overlays/pause.py", 'SEEDED = "RUN-0000beef"\n'),
    ],
)
def test_scan_reds_on_a_seeded_literal(
    console_copy: Path, literals: frozenset[str], module: str, seed: str
) -> None:
    target = console_copy / module
    target.write_text(target.read_text(encoding="utf-8") + seed, encoding="utf-8")
    found = scan(console_copy, literals)
    assert len(found) == 1
    assert found[0].startswith(f"{module}:")


def test_scan_allows_a_literal_inside_the_harness(
    console_copy: Path, literals: frozenset[str]
) -> None:
    target = console_copy / "prototype.py"
    target.write_text(target.read_text(encoding="utf-8") + 'X = "MLS-0004"\n', encoding="utf-8")
    assert scan(console_copy, literals) == []


def test_scan_of_an_empty_package_finds_nothing(tmp_path: Path, literals: frozenset[str]) -> None:
    assert scan(tmp_path, literals) == []


def test_scan_refuses_a_missing_package(tmp_path: Path, literals: frozenset[str]) -> None:
    with pytest.raises(FileNotFoundError, match="is not a directory"):
        scan(tmp_path / "absent", literals)
