"""Typed loader + drift detector for the cross-runtime capability matrix.

The matrix at :data:`MATRIX_PATH` (``src/eawf/runtimes/capabilities.yaml``)
enumerates eight capability rows across the three v0.3-v0.5 runtimes
(``claude-code`` / ``codex`` / ``opencode``). This module:

* Parses the YAML with strict :class:`pydantic.BaseModel`
  (``extra="forbid"``) validation so cell values stay within the closed
  :data:`CapabilityCell` Literal set.
* Caches the loaded matrix at import time via :func:`load_matrix` so the
  daemon's dispatch router + ``eawf doctor`` share one parse.
* Surfaces a per-runtime capability lookup
  (:func:`get_runtime_capabilities`) so adapter consumers read from the
  YAML rather than maintaining a parallel hard-coded table in
  ``runtimes/<id>/adapter.py``.
* Compares declared capabilities against subprocess probe results
  (:func:`detect_drift`) and returns a row per capability with
  status ``OK`` / ``DRIFT`` / ``MISSING``. The ``eawf doctor --runtime
  <id>`` CLI surface renders the result table.

Drift kinds
-----------

``OK``       — declared cell matches the probe verdict for the capability.
``DRIFT``    — declared cell and probe verdict disagree (e.g. matrix says
               ``supported`` but the probe reports the binary lacks the
               flag); operator surface flags it as a contract break.
``MISSING``  — the runtime binary is not installed (probe ``installed=
               false``); declared cells are reported as ``MISSING`` so the
               operator distinguishes "no binary" from "binary present
               but capability gone".
``UNKNOWN``  — probe carries no evidence either way (``unknown`` cell in
               the matrix). Surfaced as a passing row so operator
               attention focuses on actionable drift.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Closed Literals
# ---------------------------------------------------------------------------

CapabilityCell = Literal["supported", "unsupported", "partial", "unknown"]
"""Closed set of cell values. Pydantic rejects anything else via
:class:`CapabilityRow.model_config = ConfigDict(extra="forbid")` plus
the field-level Literal narrowing below."""

CAPABILITY_CELLS: Final[tuple[CapabilityCell, ...]] = (
    "supported",
    "unsupported",
    "partial",
    "unknown",
)
"""Closed-set tuple mirroring :data:`CapabilityCell` for iteration."""


DriftStatus = Literal["OK", "DRIFT", "MISSING", "UNKNOWN"]
"""Per-capability drift verdict returned by :func:`detect_drift`."""


# Canonical runtime id ordering — pinned for stable matrix iteration +
# render order in the doctor table. Matches ``RuntimeAdapter.id``.
RUNTIME_IDS: Final[tuple[str, ...]] = ("claude-code", "codex", "opencode")

# Canonical capability-row ordering — pinned to keep the rendered table
# deterministic and to keep the schema validator's row-count check
# tight (exactly 8 rows).
CAPABILITY_NAMES: Final[tuple[str, ...]] = (
    "skills",
    "plan_mode",
    "tool_use",
    "sub_agents",
    "streaming",
    "session_resume",
    "cache_control",
    "error_class_surface",
)

EXPECTED_CAPABILITY_ROWS: Final[int] = 8
"""Closed row count — exactly 8 capability rows."""

EXPECTED_RUNTIMES: Final[int] = 3
"""Closed runtime count for v0.3-v0.5."""


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CapabilityRow(BaseModel):
    """One capability row x three runtimes.

    The YAML uses hyphenated keys (``claude-code``) to mirror the
    canonical runtime id string; the loader normalises those to
    underscored field names (``claude_code``) before validation
    (:func:`_normalise_runtime_keys`).
    """

    model_config = ConfigDict(extra="forbid")

    description: str
    claude_code: CapabilityCell
    codex: CapabilityCell
    opencode: CapabilityCell

    def cell(self, runtime_id: str) -> CapabilityCell:
        """Return the cell value for ``runtime_id``.

        Args:
            runtime_id: Canonical runtime id (``claude-code`` / ``codex``
                / ``opencode``).

        Returns:
            The declared :data:`CapabilityCell` for this row x runtime.

        Raises:
            ValueError: ``runtime_id`` is not one of the three v0.3-v0.5
                runtimes.
        """
        if runtime_id == "claude-code":
            return self.claude_code
        if runtime_id == "codex":
            return self.codex
        if runtime_id == "opencode":
            return self.opencode
        raise ValueError(f"unknown runtime: {runtime_id!r}")


class CapabilityMatrix(BaseModel):
    """Top-level capability-matrix shape loaded from ``capabilities.yaml``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    runtimes: tuple[str, ...]
    capabilities: dict[str, CapabilityRow]

    def runtime_ids(self) -> tuple[str, ...]:
        """Return the runtime-id tuple (defensive copy)."""
        return tuple(self.runtimes)

    def capability_names(self) -> tuple[str, ...]:
        """Return capability-name tuple in declaration order."""
        return tuple(self.capabilities.keys())

    def cell(self, capability: str, runtime_id: str) -> CapabilityCell:
        """Return one cell value.

        Args:
            capability: Capability row name (e.g. ``"session_resume"``).
            runtime_id: Canonical runtime id.

        Returns:
            The declared :data:`CapabilityCell`.

        Raises:
            KeyError: ``capability`` is not a row in the matrix.
            ValueError: ``runtime_id`` is not one of the three v0.3-v0.5
                runtimes.
        """
        row = self.capabilities[capability]
        return row.cell(runtime_id)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _default_matrix_path() -> Path:
    """Resolve the packaged ``capabilities.yaml`` path."""
    traversable = resources.files("eawf.runtime.runtimes").joinpath("capabilities.yaml")
    return Path(str(traversable))


MATRIX_PATH: Final[Path] = _default_matrix_path()
"""Module-level resolved path to the packaged capability matrix YAML."""

_ALLOWED_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
    {"schema_version", "runtimes", "capabilities"}
)
"""Closed set of permitted top-level keys in ``capabilities.yaml``."""


def _normalise_runtime_keys(raw_row: Mapping[str, object]) -> dict[str, object]:
    """Map YAML hyphenated keys to model field names.

    The YAML uses ``claude-code`` to mirror the canonical runtime id
    string; Pydantic field names cannot contain hyphens. This helper
    rewrites ``claude-code`` → ``claude_code`` so the Pydantic model
    parses cleanly without needing per-field aliases on every cell.
    """
    out: dict[str, object] = {}
    for key, value in raw_row.items():
        if not isinstance(key, str):
            raise ValueError(f"row key must be string, got {type(key).__name__}")
        out[key.replace("-", "_")] = value
    return out


def load_matrix(path: Path | None = None) -> CapabilityMatrix:
    """Load + validate the capability matrix.

    Args:
        path: Optional override path for tests. Defaults to the packaged
            ``src/eawf/runtimes/capabilities.yaml``.

    Returns:
        Validated :class:`CapabilityMatrix`.

    Raises:
        FileNotFoundError: The YAML file does not exist.
        ValueError: The YAML body fails schema validation (row count
            mismatch, runtime mismatch, unknown cell value, hyphenated
            field key).
    """
    target = path if path is not None else MATRIX_PATH
    if not target.exists():
        raise FileNotFoundError(f"capability matrix not found: {target}")

    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"capability matrix top-level must be a mapping: {target}")

    extra_keys = sorted(set(raw.keys()) - _ALLOWED_TOP_LEVEL_KEYS)
    if extra_keys:
        raise ValueError(f"capability matrix has forbidden top-level keys: {extra_keys}")

    raw_caps = raw.get("capabilities")
    if not isinstance(raw_caps, dict):
        raise ValueError(f"capability matrix missing ``capabilities`` block: {target}")

    normalised_caps: dict[str, dict[str, object]] = {}
    for cap_name, raw_row in raw_caps.items():
        if not isinstance(raw_row, dict):
            raise ValueError(f"capability {cap_name!r} must be a mapping")
        normalised_caps[cap_name] = _normalise_runtime_keys(raw_row)

    body: dict[str, object] = {
        "schema_version": raw.get("schema_version"),
        "runtimes": raw.get("runtimes"),
        "capabilities": normalised_caps,
    }
    matrix = CapabilityMatrix.model_validate(body)

    declared_runtimes = matrix.runtime_ids()
    if declared_runtimes != RUNTIME_IDS:
        raise ValueError(
            f"runtime list mismatch: declared {declared_runtimes!r} expected {RUNTIME_IDS!r}"
        )

    declared_caps = matrix.capability_names()
    if len(declared_caps) != EXPECTED_CAPABILITY_ROWS:
        raise ValueError(
            f"capability row count {len(declared_caps)} != {EXPECTED_CAPABILITY_ROWS} (8 x 3 rule)"
        )
    if declared_caps != CAPABILITY_NAMES:
        raise ValueError(
            f"capability ordering mismatch: declared {declared_caps!r} "
            f"expected {CAPABILITY_NAMES!r}"
        )

    logger.info(
        f"load_matrix path={target!s} runtimes={len(declared_runtimes)} "
        f"capabilities={len(declared_caps)}"
    )
    return matrix


@lru_cache(maxsize=1)
def get_matrix() -> CapabilityMatrix:
    """Return the cached default capability matrix.

    Adapter consumers (``RuntimeAdapter`` selectors, ``eawf doctor``)
    call this rather than re-parsing on every dispatch. The cache is
    invalidated automatically on module reload.
    """
    return load_matrix()


def get_runtime_capabilities(runtime_id: str) -> dict[str, CapabilityCell]:
    """Return ``{capability: cell}`` for one runtime.

    Adapter selection logic reads from this rather than the
    hard-coded class attributes on
    :class:`~eawf.runtime.runtimes.claude.adapter.ClaudeAdapter` (and friends).

    Args:
        runtime_id: Canonical runtime id (``claude-code`` / ``codex``
            / ``opencode``).

    Returns:
        Mapping from capability name to the declared cell value.

    Raises:
        ValueError: ``runtime_id`` is not one of the three v0.3-v0.5
            runtimes.
    """
    if runtime_id not in RUNTIME_IDS:
        raise ValueError(f"unknown runtime: {runtime_id!r}")
    matrix = get_matrix()
    return {name: matrix.cell(name, runtime_id) for name in matrix.capability_names()}


# ---------------------------------------------------------------------------
# Drift detector
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    """Subprocess probe input for the drift detector.

    Mirrors the relevant subset of
    :class:`~eawf.runtime.runtimes.probes.sdk_baseline.RuntimeProbeRow` without
    importing it directly — keeps the drift detector decoupled from the
    pre-release probe module so consumers can supply other probe sources
    later (live capability sniff, daemon health check) under the same
    contract.

    Attributes:
        runtime_id: Canonical runtime id.
        installed: Whether the runtime binary resolves on ``$PATH``.
        observed_flags: Tuple of advertised CLI flag tokens the live
            probe saw in ``--help`` output. Empty when the binary is
            absent or help output failed to parse.
    """

    runtime_id: str
    installed: bool
    observed_flags: tuple[str, ...]


# Per-capability evidence rules used by :func:`detect_drift`. Each rule
# is a tuple of flag substrings that, when present in ``observed_flags``,
# constitute evidence that the runtime *exposes* the capability. The
# rules are deliberately conservative: missing evidence does NOT imply
# the capability is absent (the brief documents per-capability surface),
# only that the probe carries no positive signal. The detector treats
# missing evidence as ``UNKNOWN`` when the declared cell is
# ``supported`` and the runtime binary is installed.
_CAPABILITY_EVIDENCE: Final[Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "session_resume": {
        "claude-code": ("--continue", "--session-id", "--resume"),
        "codex": ("resume", "fork"),
        "opencode": ("--continue", "--session", "--fork"),
    },
    "tool_use": {
        "claude-code": ("--allowedTools", "--allowed-tools"),
        "codex": ("mcp",),
        "opencode": ("--agent",),
    },
    "streaming": {
        "claude-code": ("--output-format",),
        "codex": ("exec",),
        "opencode": ("--format",),
    },
}


@dataclasses.dataclass(frozen=True)
class DriftRow:
    """One drift-detection row for a single capability x runtime."""

    capability: str
    declared: CapabilityCell
    status: DriftStatus
    detail: str


def detect_drift(
    runtime_id: str,
    probe: ProbeResult,
    *,
    matrix: CapabilityMatrix | None = None,
) -> tuple[DriftRow, ...]:
    """Compare declared capabilities against a probe result.

    Returns one :class:`DriftRow` per capability row in the matrix
    (eight rows total). Drift semantics:

    * ``MISSING`` — probe reports ``installed=False`` (covers every
      capability for that runtime; the operator's first action is to
      install the binary, not to diff cells).
    * ``OK`` — declared cell matches the probe verdict, OR no probe
      rule is defined for this capability (declared cell is treated as
      authoritative).
    * ``DRIFT`` — declared cell says ``supported`` but the probe shows
      none of the expected flag tokens (capability appears to be gone
      from the live binary).
    * ``UNKNOWN`` — declared cell is ``unknown``; probe is silent.

    Args:
        runtime_id: Canonical runtime id whose cells are being probed.
        probe: Live probe result for ``runtime_id``.
        matrix: Optional override matrix (defaults to the packaged
            module-level matrix via :func:`get_matrix`). Tests inject
            crafted matrices through this knob.

    Returns:
        Tuple of :class:`DriftRow` rows, one per capability, in the
        canonical ordering of :data:`CAPABILITY_NAMES`.

    Raises:
        ValueError: ``runtime_id`` and ``probe.runtime_id`` disagree, or
            the runtime is not one of the three v0.3-v0.5 ids.
    """
    if probe.runtime_id != runtime_id:
        raise ValueError(
            f"probe runtime mismatch: probe={probe.runtime_id!r} requested={runtime_id!r}"
        )
    if runtime_id not in RUNTIME_IDS:
        raise ValueError(f"unknown runtime: {runtime_id!r}")

    effective = matrix if matrix is not None else get_matrix()
    rows: list[DriftRow] = []

    for cap_name in effective.capability_names():
        declared = effective.cell(cap_name, runtime_id)

        if not probe.installed:
            rows.append(
                DriftRow(
                    capability=cap_name,
                    declared=declared,
                    status="MISSING",
                    detail=f"runtime binary not installed; declared={declared!r}",
                )
            )
            continue

        if declared == "unknown":
            rows.append(
                DriftRow(
                    capability=cap_name,
                    declared=declared,
                    status="UNKNOWN",
                    detail="declared=unknown; probe carries no verdict",
                )
            )
            continue

        evidence = _CAPABILITY_EVIDENCE.get(cap_name, {}).get(runtime_id, ())
        if not evidence:
            # No probe rule — declared cell is authoritative.
            rows.append(
                DriftRow(
                    capability=cap_name,
                    declared=declared,
                    status="OK",
                    detail=f"declared={declared!r}; no probe rule",
                )
            )
            continue

        has_evidence = any(token in probe.observed_flags for token in evidence)

        if declared == "supported" and not has_evidence:
            rows.append(
                DriftRow(
                    capability=cap_name,
                    declared=declared,
                    status="DRIFT",
                    detail=(
                        f"declared=supported but probe shows none of "
                        f"{list(evidence)!r} in observed_flags"
                    ),
                )
            )
            continue

        if declared in {"unsupported", "partial"} and has_evidence:
            # Declared as unsupported/partial but probe shows flags —
            # may indicate the vendor added the surface. Flag as DRIFT
            # so the operator audits the cell.
            matched = tuple(token for token in evidence if token in probe.observed_flags)
            rows.append(
                DriftRow(
                    capability=cap_name,
                    declared=declared,
                    status="DRIFT",
                    detail=(
                        f"declared={declared!r} but probe found {list(matched)!r} in observed_flags"
                    ),
                )
            )
            continue

        rows.append(
            DriftRow(
                capability=cap_name,
                declared=declared,
                status="OK",
                detail=f"declared={declared!r} matches probe evidence",
            )
        )

    return tuple(rows)


def render_drift_table(runtime_id: str, rows: tuple[DriftRow, ...]) -> str:
    """Render a drift-detection result as a plain-text table.

    Returns a stable column-aligned string the doctor CLI surface
    emits when rendering to a TTY (JSON output bypasses this and
    serialises the :class:`DriftRow` tuple directly).
    """
    header = f"runtime={runtime_id}"
    col_widths = (24, 12, 10, 0)
    lines = [
        header,
        "",
        f"{'capability':<{col_widths[0]}}  {'declared':<{col_widths[1]}}  "
        f"{'status':<{col_widths[2]}}  detail",
        f"{'-' * col_widths[0]}  {'-' * col_widths[1]}  {'-' * col_widths[2]}  ------",
    ]
    for row in rows:
        lines.append(
            f"{row.capability:<{col_widths[0]}}  {row.declared:<{col_widths[1]}}  "
            f"{row.status:<{col_widths[2]}}  {row.detail}"
        )
    return "\n".join(lines)


__all__ = [
    "CAPABILITY_CELLS",
    "CAPABILITY_NAMES",
    "EXPECTED_CAPABILITY_ROWS",
    "EXPECTED_RUNTIMES",
    "MATRIX_PATH",
    "RUNTIME_IDS",
    "CapabilityCell",
    "CapabilityMatrix",
    "CapabilityRow",
    "DriftRow",
    "DriftStatus",
    "ProbeResult",
    "detect_drift",
    "get_matrix",
    "get_runtime_capabilities",
    "load_matrix",
    "render_drift_table",
]
