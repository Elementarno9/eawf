"""The rule render budget and runtime certification read one host-fact record."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.runtime import host_facts as kernel_facts
from eawf.kernel.runtime.certification import CertifiedFactName, CertifiedRuntimeFacts
from eawf.platform.rules import host_facts as rule_facts
from eawf.platform.rules import render
from eawf.platform.rules.render import CARD_TARGET, plan_rule_projections


def _document() -> dict[str, Any]:
    """Return a mutable copy of the shipped host-fact document."""
    raw = files("eawf.platform.rules.data").joinpath("host_facts.yaml").read_bytes()
    document: dict[str, Any] = yaml.safe_load(raw)
    return copy.deepcopy(document)


def _certified(runtime: str, value: int, *, measured_at: date) -> dict[str, Any]:
    return {
        "status": "certified",
        "measured_on": runtime,
        "default_value": value,
        "evidence": f"{runtime} boundary probe at {value}",
        "measured_at": measured_at,
        "remeasure_after_days": 90,
    }


def _with_codex_cap(cap: int) -> kernel_facts.HostFactRegistry:
    document = _document()
    document["codex"]["project_document_cap_bytes"] = _certified(
        "codex", cap, measured_at=date.today()
    )
    return kernel_facts.parse_host_facts(document)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True)
    rule = {
        "rule_id": "repo.changelog",
        "obligation_id": "demo.changelog",
        "revision": 1,
        "title": "Keep release notes in the changelog",
        "zone": "constitution",
        "force": "must",
        "effectiveness": "behavioral",
        "instruction": "Keep the release notes in the changelog under the version heading.",
        "verification": {"method": "review"},
    }
    document = {"schema_version": 1, "modules": [], "rules": [rule]}
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    return root


# ---- gate-fire proof -------------------------------------------------------


def test_one_certified_cap_change_moves_render_budget_and_certification(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for cap in (40_000, 50_000):
        registry = _with_codex_cap(cap)
        monkeypatch.setattr(render, "load_host_facts", lambda registry=registry: registry)
        monkeypatch.setattr(rule_facts, "load_host_facts", lambda registry=registry: registry)
        records = {p.record.target: p.record for p in plan_rule_projections(repo).projections}
        certified = CertifiedRuntimeFacts.from_host_facts(registry.codex)
        assert certified is not None
        assert records[CARD_TARGET].cap_bytes == cap
        assert certified.certified_cap("project_document_cap_bytes") == cap
        assert rule_facts.certified_document_cap("codex") == cap


def test_rule_host_facts_are_the_kernel_record() -> None:
    for name in (
        "HostFact",
        "RuntimeHostFacts",
        "HostFactRegistry",
        "HostFactError",
        "StaleHostFact",
        "parse_host_facts",
    ):
        assert getattr(rule_facts, name) is getattr(kernel_facts, name)
    assert isinstance(rule_facts.load_host_facts(), kernel_facts.HostFactRegistry)


def test_certified_fact_names_are_the_host_fact_names() -> None:
    assert get_args(CertifiedFactName) == get_args(kernel_facts.HostFactName)
    assert set(get_args(CertifiedFactName)) == set(kernel_facts.HOST_FACT_NAMES)


# ---- deriving certification facts -----------------------------------------


def test_from_host_facts_carries_uncertified_facts_as_absent() -> None:
    facts = CertifiedRuntimeFacts.from_host_facts(rule_facts.load_host_facts().codex)
    assert facts is not None
    assert facts.project_document_cap_bytes == 32768
    assert facts.context_window_tokens is None
    assert facts.tool_output_cap_tokens is None
    assert facts.measured_at == datetime(2026, 8, 12, tzinfo=UTC)
    assert facts.measurement_method == "observed"


def test_from_host_facts_with_nothing_certified_is_none() -> None:
    assert CertifiedRuntimeFacts.from_host_facts(rule_facts.load_host_facts().claude) is None


def test_from_host_facts_takes_the_oldest_measurement_date() -> None:
    document = _document()
    document["codex"]["context_window_tokens"] = _certified(
        "codex", 200_000, measured_at=date(2026, 1, 1)
    )
    registry = kernel_facts.parse_host_facts(document)
    facts = CertifiedRuntimeFacts.from_host_facts(registry.codex)
    assert facts is not None
    assert facts.measured_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert facts.context_window_tokens == 200_000


def test_certified_runtime_facts_still_refuses_a_zero_cap() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        CertifiedRuntimeFacts.model_validate(
            {
                "context_window_tokens": None,
                "auto_compaction_threshold_tokens": None,
                "project_document_cap_bytes": 0,
                "tool_output_cap_tokens": None,
                "measured_at": "2026-09-01T00:00:00Z",
                "measurement_method": "observed",
            }
        )


def test_certified_cap_refuses_an_unknown_fact() -> None:
    facts = CertifiedRuntimeFacts.from_host_facts(rule_facts.load_host_facts().codex)
    assert facts is not None
    with pytest.raises(KeyError):
        facts.certified_cap("latency")  # type: ignore[arg-type]
