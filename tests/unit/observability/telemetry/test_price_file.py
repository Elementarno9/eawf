"""The shipped price files load through a closed model; each row names its rates' source."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.observability.telemetry.pricing import (
    PRICE_FILE_DIR,
    PriceFile,
    check_pricing_currency,
    load_price_book,
    load_price_file,
)

_PROVENANCE_KEYS = {"source_url", "verified_on", "effective_from", "verified_by"}
_WRITE_CLASSES = {"anthropic": {"5m", "1h"}, "openai": {"30m", "12h"}}


def _shipped() -> list[Path]:
    return sorted(PRICE_FILE_DIR.glob("*.yaml"))


def _row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "model-a",
        "match": ["model-a"],
        "effective_from": None,
        "effective_to": None,
        "source_url": "https://example.invalid/pricing",
        "verified_on": "2026-11-10",
        "verified_by": "fetch",
        "rates": {"input": 1, "output": 5, "cache_read": 0.1, "cache_write": {"5m": 1.25, "1h": 2}},
        "tiers": [],
        "modifiers": {"fast": None, "geo_us": None},
    }
    row.update(changes)
    return row


def _file(*rows: dict[str, Any], aliases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"vendor": "anthropic", "aliases": aliases or [], "models": list(rows)}


def test_every_row_has_provenance() -> None:
    paths = _shipped()
    assert {path.name for path in paths} == {"anthropic.yaml", "openai.yaml"}
    for path in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        loaded = load_price_file(path)
        assert raw["vendor"] == loaded.vendor
        assert loaded.models, path.name
        for raw_row, row in zip(raw["models"], loaded.models, strict=True):
            assert set(raw_row) >= _PROVENANCE_KEYS, (path.name, row.id)
            assert row.source_url.startswith("https://"), row.id
            if row.rates is None:
                # Named but unpriced: no rate, no verification claim, a stated reason.
                assert row.verified_on is None and row.verified_by is None, row.id
                assert row.note, row.id
                continue
            assert row.verified_on is not None, row.id
            assert row.verified_by in {"fetch", "eye"}, row.id
            for rates in (row.rates, *(tier.rates for tier in row.tiers)):
                assert set(rates.cache_write) == _WRITE_CLASSES[loaded.vendor], row.id
                stated = (rates.input, rates.output, rates.cache_read, *rates.cache_write.values())
                assert all(rate is None or isinstance(rate, Decimal) for rate in stated), row.id
                assert all(isinstance(rate, Decimal) for rate in stated[:3]), row.id


def test_anthropic_rows_are_all_verified_by_fetch_on_the_table_date() -> None:
    anthropic = load_price_file(PRICE_FILE_DIR / "anthropic.yaml")
    assert {row.verified_by for row in anthropic.models} == {"fetch"}
    assert {str(row.verified_on) for row in anthropic.models} == {"2026-11-10"}


def test_shipped_rates_hold_the_stated_cache_write_multipliers() -> None:
    report = check_pricing_currency()

    assert report.is_current, report.findings
    assert report.model_count == len(load_price_book(repo_root=None, overrides={}).rows)


def test_currency_check_reports_a_drifted_write_rate(tmp_path: Path) -> None:
    drifted = _row(
        rates={"input": 1, "output": 5, "cache_read": 0.1, "cache_write": {"5m": 1.5, "1h": 2}}
    )
    (tmp_path / "anthropic.yaml").write_text(yaml.safe_dump(_file(drifted)), encoding="utf-8")

    report = check_pricing_currency(
        load_price_book(repo_root=None, directory=tmp_path, overrides={})
    )

    assert not report.is_current
    assert [(f.model_id, f.field) for f in report.findings] == [("model-a", "cache_write.5m")]


def test_a_single_row_file_loads() -> None:
    loaded = PriceFile.model_validate(_file(_row()))

    assert [row.id for row in loaded.models] == ["model-a"]
    assert loaded.models[0].rates is not None
    assert loaded.models[0].rates.cache_read == Decimal("0.1")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_file(_row(price=1)), "Extra inputs"),
        ({**_file(_row()), "currency": "usd"}, "Extra inputs"),
        (_file({k: v for k, v in _row().items() if k != "source_url"}), "source_url"),
        (_file({k: v for k, v in _row().items() if k != "verified_on"}), "verified_on"),
        (_file({k: v for k, v in _row().items() if k != "effective_from"}), "effective_from"),
        (_file(_row(source_url="http://example.invalid")), "source_url"),
        (_file(_row(verified_by="guess")), "verified_by"),
        (_file(_row(verified_on=None)), "needs verified_on and verified_by"),
        (_file(_row(rates=None, verified_on=None, verified_by=None)), "carries a note"),
        (_file(_row(effective_from="2026-11-10", effective_to="2026-11-10")), "must follow"),
        (
            _file(
                _row(rates={"input": 1, "output": 5, "cache_read": 0.1, "cache_write": {"30m": 1}})
            ),
            "are not anthropic's",
        ),
        (
            _file(_row(rates={"input": -1, "output": 5, "cache_read": 0.1, "cache_write": {}})),
            "greater than or equal",
        ),
        (
            _file(
                _row(rates={"input": 1, "output": 5, "cache_read": 0.1, "cache_write": {"2h": 1}})
            ),
            "unknown cache_write class",
        ),
        (_file(_row(), _row(id="model-b")), "answer one id on one date"),
        (
            _file(
                _row(),
                aliases=[{"alias": "a", "target": "missing", "from": "2026-11-10", "to": None}],
            ),
            "which no row prices",
        ),
        (
            _file(
                _row(
                    tiers=[
                        {"above_prompt_tokens": 200, "rates": _row()["rates"]},
                        {"above_prompt_tokens": 100, "rates": _row()["rates"]},
                    ]
                )
            ),
            "strictly ascend",
        ),
        ({"vendor": "mistral", "aliases": [], "models": []}, "vendor"),
    ],
)
def test_a_malformed_file_is_refused(body: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        PriceFile.model_validate(body)


def test_rows_in_back_to_back_windows_do_not_overlap() -> None:
    old = _row(effective_to="2026-06-01")
    new = _row(effective_from="2026-06-01")

    loaded = PriceFile.model_validate(_file(old, new))

    assert len(loaded.models) == 2


def test_an_empty_price_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no price file"):
        load_price_book(repo_root=None, directory=tmp_path, overrides={})


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "anthropic.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_price_file(path)


def test_the_digest_follows_the_file_bytes(tmp_path: Path) -> None:
    path = tmp_path / "anthropic.yaml"
    path.write_text(yaml.safe_dump(_file(_row())), encoding="utf-8")
    first = load_price_book(repo_root=None, directory=tmp_path, overrides={})
    path.write_text(yaml.safe_dump(_file(_row(note="changed"))), encoding="utf-8")
    second = load_price_book(repo_root=None, directory=tmp_path, overrides={})

    assert first.digest != second.digest
    assert len(first.version) == 16
