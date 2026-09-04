"""Loader for the dev1 configuration-defaults benchmark artifact.

The v0.7 configuration contract fixes eleven numeric defaults as
*proposal* defaults that stand only until they are remeasured at the dev1
checkpoint. Four of the eleven are read by a dev1 release gate and so
carry a recorded measurement (sample size, p50, p90, and the history the
numbers came from); the remaining seven are carried explicitly as
unmeasured beside their proposal value.

This module parses that benchmark artifact into typed rows and validates
them. The load-bearing check is the partition: the measured and
unmeasured sets must be disjoint and must cover the eleven keys exactly.
A number labelled unmeasured is safe to act on, but a default that reads
as measured because its row was quietly dropped is not, so the partition
is enforced at parse time rather than trusted to review.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.platform.artifacts.validation import validate_markdown_artifact

logger = logging.getLogger(__name__)

#: Repo-relative location of the benchmark artifact this module parses.
DEV1_CONFIG_DEFAULTS_BENCHMARK_PATH: Final[Path] = Path(
    ".ea/artifacts/research/2026-09-04-dev1-config-defaults-benchmark.md"
)

MEASURED_SECTION_HEADING: Final[str] = "## Measured defaults"
UNMEASURED_SECTION_HEADING: Final[str] = "## Unmeasured defaults"

#: The eleven numeric defaults of the v0.7 configuration contract, keyed by
#: their dotted path and carrying the proposal value verbatim. A parsed row
#: whose value disagrees with this map is a drift between the artifact and
#: the contract it reports on, and is rejected.
PACKET_NUMERIC_DEFAULTS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "planning.max_revision_attempts": 3,
        "runtime.global_capacity": 8,
        "runtime.per_provider_capacity": 4,
        "runtime.default_wall_seconds": 2400,
        "runtime.lost_grace_seconds": 30,
        "runtime.max_task_attempts": 3,
        "delivery.max_repair_tasks": 2,
        "delivery.max_audit_cycles": 2,
        "release.target_retry_limit": 2,
        "release.target_timeout_seconds": 1800,
        "activity.query_page_size": 100,
    }
)

#: The four defaults a dev1 gate reads, and therefore the four that must
#: carry a measurement.
GATE_READ_DEFAULT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "release.target_timeout_seconds",
        "release.target_retry_limit",
        "runtime.default_wall_seconds",
        "runtime.lost_grace_seconds",
    }
)

#: The seven defaults no dev1 gate reads. Derived rather than authored a
#: second time, because two lists over one fact drift.
UNREAD_DEFAULT_KEYS: Final[frozenset[str]] = (
    frozenset(PACKET_NUMERIC_DEFAULTS) - GATE_READ_DEFAULT_KEYS
)

_MEASURED_ROW_WIDTH: Final[int] = 7
_UNMEASURED_ROW_WIDTH: Final[int] = 4

_SECTION_HEADING_RE = re.compile(r"^##\s+(?P<title>[^\n#]+?)\s*$", re.MULTILINE)
_SEPARATOR_ROW_RE = re.compile(r"^\|[\s:|-]+\|$")
_KEY_CELL_RE = re.compile(r"^`(?P<key>[a-z_]+\.[a-z_]+)`$")


class ConfigDefaultsBenchmarkError(ValueError):
    """Raised when the benchmark artifact cannot be parsed into typed rows."""


class MeasuredDefault(BaseModel):
    """One numeric default a dev1 gate reads, with its measurement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    proposal_value: Annotated[int, Field(gt=0)]
    source: Annotated[str, Field(min_length=1)]
    sample_size: Annotated[int, Field(ge=1)]
    p50: Annotated[float, Field(ge=0.0)]
    p90: Annotated[float, Field(ge=0.0)]
    verdict: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _row_agrees_with_contract(self) -> Self:
        if self.key not in GATE_READ_DEFAULT_KEYS:
            raise ValueError(f"measured row key is not a dev1 gate-read default: {self.key}")
        expected = PACKET_NUMERIC_DEFAULTS[self.key]
        if self.proposal_value != expected:
            raise ValueError(
                f"measured row {self.key} proposal value {self.proposal_value} "
                f"disagrees with the contract value {expected}"
            )
        if self.p90 < self.p50:
            raise ValueError(f"measured row {self.key} has p90 {self.p90} below p50 {self.p50}")
        return self


class UnmeasuredDefault(BaseModel):
    """One numeric default no dev1 gate reads, carried at its proposal value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    proposal_value: Annotated[int, Field(gt=0)]
    status: Literal["unmeasured"]
    reason: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _row_agrees_with_contract(self) -> Self:
        if self.key not in UNREAD_DEFAULT_KEYS:
            raise ValueError(f"unmeasured row key is read by a dev1 gate: {self.key}")
        expected = PACKET_NUMERIC_DEFAULTS[self.key]
        if self.proposal_value != expected:
            raise ValueError(
                f"unmeasured row {self.key} proposal value {self.proposal_value} "
                f"disagrees with the contract value {expected}"
            )
        return self


class ConfigDefaultsBenchmark(BaseModel):
    """The parsed benchmark: measured rows plus unmeasured rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    measured: tuple[MeasuredDefault, ...]
    unmeasured: tuple[UnmeasuredDefault, ...]

    @model_validator(mode="after")
    def _sets_partition_the_contract(self) -> Self:
        measured_keys = [row.key for row in self.measured]
        unmeasured_keys = [row.key for row in self.unmeasured]
        _reject_duplicates(measured_keys, label="measured")
        _reject_duplicates(unmeasured_keys, label="unmeasured")
        if set(measured_keys) != GATE_READ_DEFAULT_KEYS:
            missing = sorted(GATE_READ_DEFAULT_KEYS - set(measured_keys))
            raise ValueError(f"measured set does not cover the dev1 gate-read defaults: {missing}")
        if set(unmeasured_keys) != UNREAD_DEFAULT_KEYS:
            missing = sorted(UNREAD_DEFAULT_KEYS - set(unmeasured_keys))
            raise ValueError(f"unmeasured set does not cover the remaining defaults: {missing}")
        covered = set(measured_keys) | set(unmeasured_keys)
        if covered != set(PACKET_NUMERIC_DEFAULTS):
            raise ValueError("measured and unmeasured rows do not partition the contract defaults")
        return self


def _reject_duplicates(keys: list[str], *, label: str) -> None:
    """Raise when *keys* repeats a dotted config path.

    Args:
        keys: The dotted config paths of one parsed section.
        label: The section name used in the rejection message.

    Raises:
        ValueError: When any key appears more than once.
    """
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise ValueError(f"{label} section repeats key {key}")
        seen.add(key)


def _section_body(text: str, heading: str) -> str:
    """Return the body of the ``## ...`` section named by *heading*.

    Args:
        text: The full artifact markdown.
        heading: The exact heading line to locate, including ``## ``.

    Returns:
        The section body, stripped of surrounding whitespace.

    Raises:
        ConfigDefaultsBenchmarkError: When the heading is absent.
    """
    matches = list(_SECTION_HEADING_RE.finditer(text))
    for index, match in enumerate(matches):
        if f"## {match.group('title').strip()}" != heading:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        return text[start:end].strip()
    raise ConfigDefaultsBenchmarkError(f"benchmark artifact is missing section: {heading}")


def _table_rows(body: str, *, width: int, heading: str) -> list[list[str]]:
    """Return the data rows of the first markdown table in *body*.

    The header row and the ``|---|`` separator are dropped; every retained
    row must have exactly *width* cells so a column added or removed in the
    artifact fails loudly instead of shifting the parsed values by one.

    Args:
        body: A section body containing one markdown table.
        width: The exact cell count each data row must carry.
        heading: The owning section heading, used in error messages.

    Returns:
        One list of stripped cell strings per data row.

    Raises:
        ConfigDefaultsBenchmarkError: When the table is absent or a row has
            the wrong cell count.
    """
    rows: list[list[str]] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        if _SEPARATOR_ROW_RE.match(stripped):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if _KEY_CELL_RE.match(cells[0]) is None:
            continue
        if len(cells) != width:
            raise ConfigDefaultsBenchmarkError(
                f"{heading} row {cells[0]} has {len(cells)} cells, expected {width}"
            )
        rows.append(cells)
    if not rows:
        raise ConfigDefaultsBenchmarkError(f"{heading} carries no config-default rows")
    return rows


def _key(cell: str) -> str:
    """Return the dotted config path carried by a backticked *cell*.

    Args:
        cell: The first cell of a table row.

    Returns:
        The dotted config path without its backticks.

    Raises:
        ConfigDefaultsBenchmarkError: When the cell is not a backticked path.
    """
    match = _KEY_CELL_RE.match(cell)
    if match is None:
        raise ConfigDefaultsBenchmarkError(f"not a backticked config key: {cell}")
    return match.group("key")


def _number(cell: str, *, key: str, column: str) -> float:
    """Return *cell* parsed as a number.

    Args:
        cell: The raw cell text.
        key: The dotted config path of the owning row.
        column: The column name, used in the rejection message.

    Returns:
        The parsed value.

    Raises:
        ConfigDefaultsBenchmarkError: When the cell is not numeric.
    """
    try:
        return float(cell)
    except ValueError as exc:
        raise ConfigDefaultsBenchmarkError(
            f"row {key} column {column} is not numeric: {cell!r}"
        ) from exc


def parse_config_defaults_benchmark(text: str) -> ConfigDefaultsBenchmark:
    """Parse the benchmark artifact markdown into validated typed rows.

    Args:
        text: The artifact markdown body.

    Returns:
        The parsed benchmark with both row sets validated.

    Raises:
        ConfigDefaultsBenchmarkError: When a section, table, or cell is
            malformed.
        pydantic.ValidationError: When a parsed row violates the contract
            (unknown key, drifted proposal value, p90 below p50, empty
            sample).
    """
    measured: list[MeasuredDefault] = []
    for cells in _table_rows(
        _section_body(text, MEASURED_SECTION_HEADING),
        width=_MEASURED_ROW_WIDTH,
        heading=MEASURED_SECTION_HEADING,
    ):
        key = _key(cells[0])
        measured.append(
            MeasuredDefault(
                key=key,
                proposal_value=int(_number(cells[1], key=key, column="proposal value")),
                source=cells[2],
                sample_size=int(_number(cells[3], key=key, column="sample size")),
                p50=_number(cells[4], key=key, column="p50"),
                p90=_number(cells[5], key=key, column="p90"),
                verdict=cells[6],
            )
        )
    unmeasured: list[UnmeasuredDefault] = []
    for cells in _table_rows(
        _section_body(text, UNMEASURED_SECTION_HEADING),
        width=_UNMEASURED_ROW_WIDTH,
        heading=UNMEASURED_SECTION_HEADING,
    ):
        key = _key(cells[0])
        unmeasured.append(
            UnmeasuredDefault(
                key=key,
                proposal_value=int(_number(cells[1], key=key, column="proposal value")),
                status=_status(cells[2], key=key),
                reason=cells[3],
            )
        )
    return ConfigDefaultsBenchmark(measured=tuple(measured), unmeasured=tuple(unmeasured))


def _status(cell: str, *, key: str) -> Literal["unmeasured"]:
    """Return the unmeasured-status literal carried by *cell*.

    Args:
        cell: The raw status cell.
        key: The dotted config path of the owning row.

    Returns:
        The literal ``"unmeasured"``.

    Raises:
        ConfigDefaultsBenchmarkError: When the cell says anything else.
    """
    if cell != "unmeasured":
        raise ConfigDefaultsBenchmarkError(f"row {key} status must be unmeasured, got {cell!r}")
    return "unmeasured"


def load_config_defaults_benchmark(path: Path) -> ConfigDefaultsBenchmark:
    """Load, chassis-validate, and parse the benchmark artifact at *path*.

    The chassis check runs first so a benchmark that has lost its
    Summary / References / Provenance / Scrub sections, or that leaked a
    machine-specific path, is rejected before its numbers are trusted.

    Args:
        path: Filesystem path to the artifact markdown.

    Returns:
        The parsed benchmark with both row sets validated.

    Raises:
        FileNotFoundError: When *path* does not exist.
        ConfigDefaultsBenchmarkError: When the chassis check fails or the
            markdown is malformed.
        pydantic.ValidationError: When a parsed row violates the contract.
    """
    text = path.read_text(encoding="utf-8")
    chassis = validate_markdown_artifact(text)
    if not chassis.ok:
        raise ConfigDefaultsBenchmarkError(
            f"benchmark artifact fails the chassis check: {chassis.errors}"
        )
    logger.debug(f"load_config_defaults_benchmark path={path}")
    return parse_config_defaults_benchmark(text)
