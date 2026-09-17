"""Where the tracked console golden contract lives.

One module owns the paths so the replay tests and the styled tier never re-derive them;
this package can move inside ``tests/`` without a path edit because the root is found by
walking up to the ``tests`` directory rather than by counting parents.
"""

from __future__ import annotations

from pathlib import Path

from eawf.surfaces.tui.console.harness import GoldenLayout


def _tests_root() -> Path:
    """Return the repository's ``tests`` directory.

    Raises:
        RuntimeError: this package was imported from outside ``tests/``, so the tracked
            golden contract cannot be located.
    """
    for parent in Path(__file__).resolve().parents:
        if parent.name == "tests":
            return parent
    raise RuntimeError("console goldens imported from outside tests/; no golden root")


#: Root of the tracked contract: the extracted prototype fixture, the pack's frame and
#: journey sequences, the pack-to-port normalisation map and the styled-capture tier.
GOLDEN_ROOT: Path = _tests_root() / "fixtures" / "console" / "golden"

#: Where the console's own golden harness finds each part of the contract.
LAYOUT: GoldenLayout = GoldenLayout.under(GOLDEN_ROOT)
