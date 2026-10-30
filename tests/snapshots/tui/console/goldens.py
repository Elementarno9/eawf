"""Where the tracked console golden contract lives.

One module owns the paths so the replay tests and the styled tier never re-derive them;
this package can move inside ``tests/`` without a path edit because the root is found by
walking up to the ``tests`` directory rather than by counting parents.
"""

from __future__ import annotations

import os
from pathlib import Path

from eawf.surfaces.tui.console.harness import Contract, GoldenLayout


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

#: The routes whose frames a native binding must leave exactly where it found them, at
#: every width.
ACCEPTANCE_FRAME_ROUTES: tuple[str, ...] = ("milestone", "release", "receipt", "export")

#: Whether this process is a pytest-xdist worker. The variable is set before collection,
#: so every worker parametrises the same cases.
SHARDED: bool = "PYTEST_XDIST_WORKER" in os.environ


def replayed_frame_ids(contract: Contract, *, sharded: bool) -> tuple[str, ...]:
    """Return the frame ids one replay covers, in the order the pack files carry them.

    A single-process run replays every frame. Under xdist every worker that draws a
    replay case replays on its own, so a sharded run replays a fixed subset instead: each
    frame at 80 columns, each frame with no width, and the acceptance routes at every
    width, which keeps every frame kind and every route and still reaches 120 and 160
    columns. The console-replay workflow replays the whole contract in one process.

    Args:
        contract: The tracked contract.
        sharded: Whether the replay runs inside an xdist worker.
    """
    ids = tuple(state.id for state in contract.states)
    if not sharded:
        return ids
    return tuple(
        frame_id
        for frame_id in ids
        if not frame_id.endswith(("@120", "@160"))
        or any(route in frame_id for route in ACCEPTANCE_FRAME_ROUTES)
    )
