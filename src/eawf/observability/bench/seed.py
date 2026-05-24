"""Deterministic bench-corpus generation.

A bench corpus is a synthetic ``state.json``-shaped document plus a
parallel event stream sized to exercise a particular cost class
(per-call, collection, projection). The generator is **deterministic**:
a fixed size maps to a fixed RNG seed via
``sha256("bench-fixture-v1-<size>").digest()``, so re-seeding the same
size produces byte-identical output. The bench baselines depend on that
property — a non-deterministic corpus would make run-to-run wall-clock
comparison meaningless.

Determinism rules enforced here:

- The RNG is a freshly-seeded :class:`random.Random` per call; no module
  global state leaks between sizes.
- Every collection is emitted in a stable, sorted order.
- Timestamps are derived from a fixed epoch plus a deterministic offset,
  never ``datetime.now``.
- Serialisation uses ``orjson`` with ``OPT_SORT_KEYS`` so dict key order
  cannot perturb the bytes.

Public API:
    seed_corpus(size) -> BenchCorpus
    seed_fixture(size, output_dir) -> tuple[Path, Path]
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import orjson

logger = logging.getLogger(__name__)

FixtureSize = Literal["small", "medium", "large"]

# Ordered tuple of the corpus sizes this wave ships. ``jumbo`` is an
# opt-in scale-ceiling fuzz target deferred to a follow-up wave; only the
# three operator-facing sizes land here.
FIXTURE_SIZES: tuple[FixtureSize, ...] = ("small", "medium", "large")

# Wave / phase / event counts per size. The shape mirrors the C09 spec
# table (small=per-call, medium=collection, large=projection).
_SIZE_DIMS: dict[FixtureSize, tuple[int, int, int]] = {
    # size: (wave_count, phase_count, event_count)
    "small": (10, 1, 200),
    "medium": (50, 3, 2_000),
    "large": (200, 8, 20_000),
}

# Fixed epoch all synthetic timestamps derive from. Using a constant
# keeps the corpus byte-stable across machines and clocks.
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

# Versioned seed prefix. Bump only when the corpus shape changes in a way
# that should invalidate every committed fixture + baseline.
_SEED_PREFIX = "bench-fixture-v1"

_EVENT_KINDS: tuple[str, ...] = (
    "wave_claimed",
    "wave_closed",
    "phase_opened",
    "iter_opened",
    "decision_recorded",
)


@dataclass(frozen=True, slots=True)
class BenchCorpus:
    """One deterministic bench corpus.

    Attributes:
        size: The fixture size this corpus was generated for.
        state: The synthetic ``state.json``-shaped document.
        events: The parallel event stream — one dict per ``event.jsonl``
            line, in emission order.
    """

    size: FixtureSize
    state: dict[str, object]
    events: list[dict[str, object]]


def _rng_for(size: FixtureSize) -> random.Random:
    """Return a freshly-seeded RNG bound to *size*.

    The seed is the full sha256 digest of ``bench-fixture-v1-<size>``
    folded into an int, so the stream is reproducible across machines and
    Python builds (CPython's Mersenne Twister is portable for a fixed
    int seed).

    Args:
        size: The fixture size keying the seed.

    Returns:
        A :class:`random.Random` seeded deterministically for *size*.
    """
    digest = hashlib.sha256(f"{_SEED_PREFIX}-{size}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _ts(offset_minutes: int) -> str:
    """Return an ISO-8601 UTC timestamp at a fixed offset from the epoch."""
    return (_EPOCH + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _phase_id(index: int) -> str:
    return f"P{index:02d}"


def _wave_id(index: int) -> str:
    return f"W{index:02d}"


def _iter_id(phase_index: int) -> str:
    return f"{_phase_id(phase_index)}-I01"


def seed_corpus(size: FixtureSize) -> BenchCorpus:
    """Generate the deterministic corpus for *size*.

    Args:
        size: One of :data:`FIXTURE_SIZES`.

    Returns:
        A :class:`BenchCorpus` whose ``state`` + ``events`` are fully
        determined by *size* — calling twice yields equal objects.

    Raises:
        ValueError: When *size* is not one of :data:`FIXTURE_SIZES`.
    """
    if size not in _SIZE_DIMS:
        raise ValueError(f"unknown fixture size: {size!r} (want one of {list(FIXTURE_SIZES)})")

    rng = _rng_for(size)
    wave_count, phase_count, event_count = _SIZE_DIMS[size]
    logger.debug(
        f"seed_corpus size={size} waves={wave_count} phases={phase_count} events={event_count}"
    )

    phases: list[dict[str, object]] = []
    for p in range(1, phase_count + 1):
        phases.append(
            {
                "id": _phase_id(p),
                "iter_id": _iter_id(p),
                "title": f"Bench phase {p}",
                "status": "closed" if p < phase_count else "active",
                "opened_at": _ts(p * 60),
            }
        )

    waves: list[dict[str, object]] = []
    for w in range(1, wave_count + 1):
        # Spread waves across the available phases deterministically.
        phase_index = (w % phase_count) + 1
        deps = [] if w == 1 else [_wave_id(rng.randint(1, w - 1))]
        waves.append(
            {
                "id": _wave_id(w),
                "iter_id": _iter_id(phase_index),
                "title": f"Bench wave {w}",
                "status": "closed",
                "deps": sorted(set(deps)),
                "effort_bucket": rng.choice(["XS", "S", "M", "L", "XL"]),
                "opened_at": _ts(1_000 + w),
            }
        )

    state: dict[str, object] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "size": size,
        "phases": phases,
        "waves": waves,
        "counts": {
            "phases": phase_count,
            "waves": wave_count,
            "events": event_count,
        },
    }

    events: list[dict[str, object]] = []
    for e in range(event_count):
        events.append(
            {
                "seq": e,
                "kind": rng.choice(_EVENT_KINDS),
                "wave_id": _wave_id(rng.randint(1, wave_count)),
                "at": _ts(2_000 + e),
            }
        )

    return BenchCorpus(size=size, state=state, events=events)


def _dumps(payload: object) -> bytes:
    """Serialise *payload* canonically (sorted keys, two-space indent)."""
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)


def seed_fixture(size: FixtureSize, output_dir: Path) -> tuple[Path, Path]:
    """Write the deterministic corpus for *size* under *output_dir*.

    Two files are written: ``<size>.json`` (the state document) and
    ``<size>-event.jsonl`` (one event per line). Re-running with the same
    *size* overwrites both with byte-identical content.

    Args:
        size: One of :data:`FIXTURE_SIZES`.
        output_dir: Directory the two fixture files land in. Created
            (parents included) when absent.

    Returns:
        A ``(state_path, event_path)`` tuple of the two written files.

    Raises:
        ValueError: When *size* is not one of :data:`FIXTURE_SIZES`.
    """
    corpus = seed_corpus(size)
    output_dir.mkdir(parents=True, exist_ok=True)

    state_path = output_dir / f"{size}.json"
    event_path = output_dir / f"{size}-event.jsonl"

    # Trailing newline on the state document so the committed fixture
    # satisfies the end-of-file-fixer hook AND stays byte-identical with a
    # fresh re-seed (the fixer would otherwise append a newline the writer
    # never emits, breaking the determinism guarantee on the committed copy).
    state_path.write_bytes(_dumps(corpus.state) + b"\n")
    # JSONL: one canonical object per line (no inter-line key reordering
    # since each line is independently sorted), trailing newline.
    lines = [orjson.dumps(ev, option=orjson.OPT_SORT_KEYS) for ev in corpus.events]
    event_path.write_bytes(b"\n".join(lines) + b"\n")

    logger.info(f"seed_fixture size={size} state={state_path.name!r} events={len(corpus.events)}")
    return state_path, event_path
