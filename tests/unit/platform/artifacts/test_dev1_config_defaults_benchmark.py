"""Tests for the dev1 configuration-defaults benchmark artifact and its loader.

Two contracts are pinned here. The committed benchmark artifact must record
sample size, p50 and p90 for each of the four numeric defaults a dev1 gate
reads, and must list the seven defaults no dev1 gate reads as unmeasured at
their proposal value. The loader must reject any artifact whose two row sets
fail to partition the eleven contract defaults exactly, so a default can never
read as measured because its row went missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.platform.artifacts.config_defaults_benchmark import (
    DEV1_CONFIG_DEFAULTS_BENCHMARK_PATH,
    GATE_READ_DEFAULT_KEYS,
    MEASURED_SECTION_HEADING,
    PACKET_NUMERIC_DEFAULTS,
    UNMEASURED_SECTION_HEADING,
    UNREAD_DEFAULT_KEYS,
    ConfigDefaultsBenchmark,
    ConfigDefaultsBenchmarkError,
    load_config_defaults_benchmark,
    parse_config_defaults_benchmark,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]

_MEASURED_ROWS: tuple[str, ...] = (
    "| `release.target_timeout_seconds` | 1800 | ci runs | 46 | 45.5 | 1123.0 | holds |",
    "| `release.target_retry_limit` | 2 | ci runs | 46 | 1.0 | 2.0 | holds |",
    "| `runtime.default_wall_seconds` | 2400 | sessions | 12 | 40.2 | 208.6 | holds |",
    "| `runtime.lost_grace_seconds` | 30 | probe | 12 | 15.005 | 15.007 | holds |",
)

_UNMEASURED_ROWS: tuple[str, ...] = (
    "| `planning.max_revision_attempts` | 3 | unmeasured | no dev1 gate reads it |",
    "| `runtime.global_capacity` | 8 | unmeasured | no dev1 gate reads it |",
    "| `runtime.per_provider_capacity` | 4 | unmeasured | no dev1 gate reads it |",
    "| `runtime.max_task_attempts` | 3 | unmeasured | no dev1 gate reads it |",
    "| `delivery.max_repair_tasks` | 2 | unmeasured | no dev1 gate reads it |",
    "| `delivery.max_audit_cycles` | 2 | unmeasured | no dev1 gate reads it |",
    "| `activity.query_page_size` | 100 | unmeasured | no dev1 gate reads it |",
)


def _document(
    *,
    measured: tuple[str, ...] = _MEASURED_ROWS,
    unmeasured: tuple[str, ...] = _UNMEASURED_ROWS,
    measured_heading: str = MEASURED_SECTION_HEADING,
    unmeasured_heading: str = UNMEASURED_SECTION_HEADING,
) -> str:
    """Build a benchmark markdown body carrying the two row tables.

    Args:
        measured: Measured-table data rows, pipe-delimited.
        unmeasured: Unmeasured-table data rows, pipe-delimited.
        measured_heading: Heading for the measured section.
        unmeasured_heading: Heading for the unmeasured section.

    Returns:
        A markdown body the parser accepts, chassis sections aside.
    """
    lines = [
        "# Benchmark",
        "",
        measured_heading,
        "",
        "| Configuration key | Proposal default | Source | Sample size | p50 | p90 | Verdict |",
        "|---|---|---|---|---|---|---|",
        *measured,
        "",
        unmeasured_heading,
        "",
        "| Configuration key | Proposal default | Status | Reason |",
        "|---|---|---|---|",
        *unmeasured,
        "",
    ]
    return "\n".join(lines)


@pytest.fixture(scope="module")
def committed_benchmark() -> ConfigDefaultsBenchmark:
    """Load the committed benchmark artifact through the production loader."""
    return load_config_defaults_benchmark(_REPO_ROOT / DEV1_CONFIG_DEFAULTS_BENCHMARK_PATH)


# --- the committed artifact -------------------------------------------------


def test_measured_rows_pin_the_four_gate_read_defaults(
    committed_benchmark: ConfigDefaultsBenchmark,
) -> None:
    keys = {row.key for row in committed_benchmark.measured}
    assert keys == GATE_READ_DEFAULT_KEYS
    assert keys == {
        "release.target_timeout_seconds",
        "release.target_retry_limit",
        "runtime.default_wall_seconds",
        "runtime.lost_grace_seconds",
    }
    for row in committed_benchmark.measured:
        assert row.proposal_value == PACKET_NUMERIC_DEFAULTS[row.key]


def test_measured_rows_carry_sample_size_p50_and_p90(
    committed_benchmark: ConfigDefaultsBenchmark,
) -> None:
    for row in committed_benchmark.measured:
        assert row.sample_size >= 1
        assert row.p90 >= row.p50
        assert row.source.strip()
        assert row.verdict.strip()


def test_measured_rows_record_the_taken_measurement(
    committed_benchmark: ConfigDefaultsBenchmark,
) -> None:
    by_key = {row.key: row for row in committed_benchmark.measured}
    assert by_key["release.target_timeout_seconds"].sample_size == 46
    assert by_key["release.target_timeout_seconds"].p50 == pytest.approx(45.5)
    assert by_key["release.target_timeout_seconds"].p90 == pytest.approx(1123.0)
    assert by_key["release.target_retry_limit"].sample_size == 46
    assert by_key["release.target_retry_limit"].p90 == pytest.approx(2.0)
    assert by_key["runtime.default_wall_seconds"].sample_size == 12
    assert by_key["runtime.default_wall_seconds"].p90 == pytest.approx(208.6)
    assert by_key["runtime.lost_grace_seconds"].sample_size == 12
    assert by_key["runtime.lost_grace_seconds"].p90 == pytest.approx(15.007)


def test_unmeasured_rows_pin_the_seven_unread_defaults(
    committed_benchmark: ConfigDefaultsBenchmark,
) -> None:
    keys = {row.key for row in committed_benchmark.unmeasured}
    assert keys == UNREAD_DEFAULT_KEYS
    assert keys == {
        "planning.max_revision_attempts",
        "runtime.global_capacity",
        "runtime.per_provider_capacity",
        "runtime.max_task_attempts",
        "delivery.max_repair_tasks",
        "delivery.max_audit_cycles",
        "activity.query_page_size",
    }
    for row in committed_benchmark.unmeasured:
        assert row.status == "unmeasured"
        assert row.proposal_value == PACKET_NUMERIC_DEFAULTS[row.key]
        assert row.reason.strip()


def test_measured_and_unmeasured_sets_partition_the_eleven(
    committed_benchmark: ConfigDefaultsBenchmark,
) -> None:
    measured = {row.key for row in committed_benchmark.measured}
    unmeasured = {row.key for row in committed_benchmark.unmeasured}
    assert len(measured) == 4
    assert len(unmeasured) == 7
    assert measured & unmeasured == set()
    assert measured | unmeasured == set(PACKET_NUMERIC_DEFAULTS)
    assert len(PACKET_NUMERIC_DEFAULTS) == 11


def test_measured_benchmark_path_is_repo_relative() -> None:
    assert not DEV1_CONFIG_DEFAULTS_BENCHMARK_PATH.is_absolute()
    assert (_REPO_ROOT / DEV1_CONFIG_DEFAULTS_BENCHMARK_PATH).is_file()


# --- row-level rejection paths ----------------------------------------------


def test_measured_row_rejects_p90_below_p50() -> None:
    rows = (
        "| `release.target_timeout_seconds` | 1800 | ci | 46 | 900.0 | 10.0 | holds |",
        *_MEASURED_ROWS[1:],
    )
    with pytest.raises(ValidationError, match="below p50"):
        parse_config_defaults_benchmark(_document(measured=rows))


def test_measured_row_rejects_zero_sample_size() -> None:
    rows = (
        "| `release.target_timeout_seconds` | 1800 | ci | 0 | 45.5 | 1123.0 | holds |",
        *_MEASURED_ROWS[1:],
    )
    with pytest.raises(ValidationError):
        parse_config_defaults_benchmark(_document(measured=rows))


def test_measured_row_rejects_proposal_value_drift() -> None:
    rows = (
        "| `release.target_timeout_seconds` | 1799 | ci | 46 | 45.5 | 1123.0 | holds |",
        *_MEASURED_ROWS[1:],
    )
    with pytest.raises(ValidationError, match="disagrees with the contract value 1800"):
        parse_config_defaults_benchmark(_document(measured=rows))


def test_measured_row_rejects_empty_source() -> None:
    rows = (
        "| `release.target_timeout_seconds` | 1800 |  | 46 | 45.5 | 1123.0 | holds |",
        *_MEASURED_ROWS[1:],
    )
    with pytest.raises(ValidationError):
        parse_config_defaults_benchmark(_document(measured=rows))


def test_measured_row_rejects_unmeasured_only_key() -> None:
    rows = (
        "| `activity.query_page_size` | 100 | ci | 46 | 45.5 | 1123.0 | holds |",
        *_MEASURED_ROWS[1:],
    )
    with pytest.raises(ValidationError, match="not a dev1 gate-read default"):
        parse_config_defaults_benchmark(_document(measured=rows))


def test_unmeasured_row_rejects_gate_read_key() -> None:
    rows = (
        "| `runtime.lost_grace_seconds` | 30 | unmeasured | no dev1 gate reads it |",
        *_UNMEASURED_ROWS[1:],
    )
    with pytest.raises(ValidationError, match="read by a dev1 gate"):
        parse_config_defaults_benchmark(_document(unmeasured=rows))


def test_unmeasured_row_rejects_status_other_than_unmeasured() -> None:
    rows = (
        "| `planning.max_revision_attempts` | 3 | measured | no dev1 gate reads it |",
        *_UNMEASURED_ROWS[1:],
    )
    with pytest.raises(ConfigDefaultsBenchmarkError, match="status must be unmeasured"):
        parse_config_defaults_benchmark(_document(unmeasured=rows))


def test_unmeasured_row_rejects_empty_reason() -> None:
    rows = (
        "| `planning.max_revision_attempts` | 3 | unmeasured |  |",
        *_UNMEASURED_ROWS[1:],
    )
    with pytest.raises(ValidationError):
        parse_config_defaults_benchmark(_document(unmeasured=rows))


# --- set-level rejection paths ----------------------------------------------


def test_measured_set_missing_a_row_is_rejected() -> None:
    with pytest.raises(ValidationError, match="does not cover the dev1 gate-read defaults"):
        parse_config_defaults_benchmark(_document(measured=_MEASURED_ROWS[1:]))


def test_unmeasured_set_missing_a_row_is_rejected() -> None:
    with pytest.raises(ValidationError, match="does not cover the remaining defaults"):
        parse_config_defaults_benchmark(_document(unmeasured=_UNMEASURED_ROWS[1:]))


def test_measured_set_rejects_a_duplicated_key() -> None:
    rows = (*_MEASURED_ROWS, _MEASURED_ROWS[0])
    with pytest.raises(ValidationError, match=r"repeats key release\.target_timeout_seconds"):
        parse_config_defaults_benchmark(_document(measured=rows))


# --- structural rejection paths ---------------------------------------------


def test_measured_section_missing_raises() -> None:
    with pytest.raises(ConfigDefaultsBenchmarkError, match="missing section"):
        parse_config_defaults_benchmark(_document(measured_heading="## Something else"))


def test_unmeasured_section_missing_raises() -> None:
    with pytest.raises(ConfigDefaultsBenchmarkError, match="missing section"):
        parse_config_defaults_benchmark(_document(unmeasured_heading="## Something else"))


def test_measured_section_with_no_rows_raises() -> None:
    with pytest.raises(ConfigDefaultsBenchmarkError, match="carries no config-default rows"):
        parse_config_defaults_benchmark(_document(measured=()))


def test_measured_row_with_wrong_cell_count_raises() -> None:
    rows = (
        "| `release.target_timeout_seconds` | 1800 | ci | 46 | 45.5 | 1123.0 |",
        *_MEASURED_ROWS[1:],
    )
    with pytest.raises(ConfigDefaultsBenchmarkError, match="cells, expected 7"):
        parse_config_defaults_benchmark(_document(measured=rows))


def test_measured_row_with_non_numeric_sample_size_raises() -> None:
    rows = (
        "| `release.target_timeout_seconds` | 1800 | ci | many | 45.5 | 1123.0 | holds |",
        *_MEASURED_ROWS[1:],
    )
    with pytest.raises(ConfigDefaultsBenchmarkError, match="sample size is not numeric"):
        parse_config_defaults_benchmark(_document(measured=rows))


def test_measured_benchmark_empty_text_raises() -> None:
    with pytest.raises(ConfigDefaultsBenchmarkError, match="missing section"):
        parse_config_defaults_benchmark("")


# --- loader-level paths -----------------------------------------------------


def test_load_measured_benchmark_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config_defaults_benchmark(tmp_path / "absent.md")


def test_load_measured_benchmark_rejects_a_chassis_failure(tmp_path: Path) -> None:
    path = tmp_path / "2026-09-04-benchmark.md"
    path.write_text(_document(), encoding="utf-8")
    with pytest.raises(ConfigDefaultsBenchmarkError, match="fails the chassis check"):
        load_config_defaults_benchmark(path)
