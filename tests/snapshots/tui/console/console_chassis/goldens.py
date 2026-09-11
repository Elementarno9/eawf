"""Where the tracked console golden contract lives.

One module owns the paths so the chassis, the harness and the replay tests never
re-derive them; the package can move inside ``tests/`` without a path edit because the
root is found by walking up to the ``tests`` directory rather than by counting parents.
"""

from __future__ import annotations

from pathlib import Path


def _tests_root() -> Path:
    """Return the repository's ``tests`` directory.

    Raises:
        RuntimeError: this package was imported from outside ``tests/``, so the
            tracked golden contract cannot be located.
    """
    for parent in Path(__file__).resolve().parents:
        if parent.name == "tests":
            return parent
    raise RuntimeError("console chassis imported from outside tests/; no golden root")


#: Root of the tracked contract: the extracted prototype fixture, the pack's frame and
#: journey sequences, and the CON-147 pack-to-port normalisation map.
GOLDEN_ROOT: Path = _tests_root() / "fixtures" / "console" / "golden"

#: The four prototype fixture registers the renderers read.
FIXTURE_DIR: Path = GOLDEN_ROOT / "fixture"

#: The pack's 261 frame states and 25 journeys.
SEQUENCES_DIR: Path = GOLDEN_ROOT / "sequences"

#: The frozen pack-to-port normalisation map.
NORMALISATION_MAP: Path = GOLDEN_ROOT / "normalisation-map.json"
