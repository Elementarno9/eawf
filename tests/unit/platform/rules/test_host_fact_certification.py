"""Certify host facts per runtime and fail a render over the smallest certified cap."""

from __future__ import annotations

import copy
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.platform.rules import render
from eawf.platform.rules.carriers import builtin_carrier_roles, carrier_target
from eawf.platform.rules.host_facts import (
    HOST_RUNTIMES,
    HostFact,
    HostFactError,
    HostFactEvidenceError,
    HostFactInheritanceError,
    HostFactRegistry,
    certified_document_cap,
    label_measurement,
    load_host_facts,
    measure_codex_document,
    parse_host_facts,
    read_codex_document_cap,
    smallest_certified_cap,
    stale_host_facts,
)
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    RuleProjectionBudgetError,
    plan_rule_projections,
    render_rule_projections,
)
from eawf.surfaces.cli.commands import sync

_CONSTITUTION = "Keep the release notes in the changelog under the version heading."


def _document() -> dict[str, Any]:
    """Return a mutable copy of the shipped host-fact document."""
    raw = files("eawf.platform.rules.data").joinpath("host_facts.yaml").read_bytes()
    document: dict[str, Any] = yaml.safe_load(raw)
    return copy.deepcopy(document)


def _certified(
    runtime: str, value: int, evidence: str, *, measured_at: date | None = None
) -> dict[str, Any]:
    # Measured today by default, so a test that does not concern staleness
    # never turns stale as the calendar moves.
    return {
        "status": "certified",
        "measured_on": runtime,
        "default_value": value,
        "evidence": evidence,
        "measured_at": measured_at or date.today(),
        "remeasure_after_days": 30,
    }


def _with_doc_caps(**caps: int | None) -> HostFactRegistry:
    """Build a registry whose project-document caps are ``caps`` per runtime."""
    document = _document()
    for runtime, cap in caps.items():
        document[runtime]["project_document_cap_bytes"] = (
            _certified(runtime, cap, f"boundary probe on {runtime}")
            if cap is not None
            else {"status": "uncertified", "measured_on": runtime, "reason": "not measured"}
        )
    return parse_host_facts(document)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
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
        "instruction": _CONSTITUTION,
        "verification": {"method": "review"},
    }
    document = {"schema_version": 1, "modules": [], "rules": [rule]}
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    return root


def _use(monkeypatch: pytest.MonkeyPatch, registry: HostFactRegistry) -> None:
    monkeypatch.setattr(render, "load_host_facts", lambda: registry)


# ---- gate-fire proof -------------------------------------------------------


def test_plan_rule_projections_one_byte_over_the_smallest_certified_cap_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes = {p.record.target: p.record.byte_count for p in plan_rule_projections(repo).projections}
    policy = sizes[POLICY_TARGET]
    # Claude reads only the policy projection; its smaller certified cap binds it.
    _use(monkeypatch, _with_doc_caps(claude=policy, codex=policy + 100))
    at_cap = {p.record.target: p.record for p in plan_rule_projections(repo).projections}
    assert at_cap[POLICY_TARGET].headroom_bytes == 0
    assert at_cap[POLICY_TARGET].cap_runtime == "claude"
    assert at_cap[CARD_TARGET].cap_runtime == "codex"
    _use(monkeypatch, _with_doc_caps(claude=policy - 1, codex=policy + 100))
    with pytest.raises(RuleProjectionBudgetError) as excinfo:
        plan_rule_projections(repo)
    message = str(excinfo.value)
    assert f"{POLICY_TARGET} renders {policy} bytes" in message
    assert f"{policy - 1}-byte project-document cap certified for claude" in message
    assert excinfo.value.code == "rule_projection_budget"


def test_plan_rule_projections_records_and_reports_uncertified_readers(repo: Path) -> None:
    plan = plan_rule_projections(repo)
    records = {p.record.target: p.record for p in plan.projections}
    assert records[POLICY_TARGET].uncertified_readers == ("claude",)
    assert records[POLICY_TARGET].cap_runtime == "codex"
    assert records[CARD_TARGET].uncertified_readers == ("opencode",)
    warnings = plan.manifest.host_fact_warnings
    assert any(
        w.startswith(f"{POLICY_TARGET} is read by claude with no certified") and "for codex" in w
        for w in warnings
    )
    assert any(w.startswith(f"{CARD_TARGET} is read by opencode") for w in warnings)


def test_rule_projections_sync_reports_uncertified_readers_to_the_operator(
    repo: Path,
) -> None:
    changed, warnings = sync._rule_projections(repo, write=True, rules=True)
    assert POLICY_TARGET in changed
    assert any("claude with no certified project-document cap" in w for w in warnings)
    written = render.ProjectionManifest.model_validate_json(
        (repo / render.PROJECTION_MANIFEST_PATH).read_bytes()
    )
    assert {r.target: r.uncertified_readers for r in written.projections}[POLICY_TARGET] == (
        "claude",
    )
    payload = {
        "mode": "write",
        "target": "demo",
        "profiles_enabled": [],
        "regions_added": [],
        "regions_updated": [],
        "regions_unchanged": [],
        "agents_md_changed": True,
        "claude_md_changed": True,
        "manifest_changed": True,
        "host_fact_warnings": warnings,
    }
    lines = sync._format_text(payload).splitlines()
    assert lines[1:] == [f"warning: {w}" for w in warnings]


def test_rule_projections_without_a_rule_source_reports_nothing(repo: Path) -> None:
    assert sync._rule_projections(repo, write=False, rules=False) == ([], [])


def test_plan_rule_projections_fully_certified_readers_warn_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _with_doc_caps(claude=100_000, codex=100_000, opencode=100_000))
    plan = plan_rule_projections(repo)
    assert all(p.record.uncertified_readers == () for p in plan.projections)
    assert plan.manifest.host_fact_warnings == ()


def test_plan_rule_projections_flags_a_certified_fact_past_its_remeasure_age(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _document()
    document["codex"]["project_document_cap_bytes"] = _certified(
        "codex", 32768, "old probe", measured_at=date(2000, 1, 1)
    )
    _use(monkeypatch, parse_host_facts(document))
    manifest = plan_rule_projections(repo).manifest
    assert [(f.runtime, f.fact) for f in manifest.stale_host_facts] == [
        ("codex", "project_document_cap_bytes")
    ]
    assert manifest.stale_host_facts[0].remeasure_due == date(2000, 1, 31)
    assert any(
        w.startswith("stale host fact: codex project_document_cap_bytes")
        for w in (manifest.host_fact_warnings)
    )


def test_load_host_facts_shipped_certified_facts_carry_a_remeasure_age() -> None:
    fact = load_host_facts().codex.project_document_cap_bytes
    assert fact.measured_at == date(2026, 8, 12)
    assert fact.remeasure_due == date(2026, 11, 10)


@pytest.mark.parametrize(
    ("today", "stale"),
    [(date(2026, 9, 30), False), (date(2026, 10, 1), False), (date(2026, 10, 2), True)],
    ids=["inside", "on-due-date", "one-day-past"],
)
def test_stale_host_facts_boundary(today: date, stale: bool) -> None:
    document = _document()
    document["codex"]["project_document_cap_bytes"] = _certified(
        "codex", 32768, "probe", measured_at=date(2026, 9, 1)
    )
    registry = parse_host_facts(document)
    found = stale_host_facts(registry, today=today)
    assert bool(found) is stale


def test_stale_host_facts_with_nothing_certified_is_empty() -> None:
    registry = _with_doc_caps(claude=None, codex=None, opencode=None)
    assert stale_host_facts(registry, today=date(2999, 1, 1)) == ()


@pytest.mark.parametrize(
    ("drop", "match"),
    [("measured_at", "measured_at date"), ("remeasure_after_days", "remeasure_after_days")],
)
def test_host_fact_certified_without_its_measurement_age_is_refused(drop: str, match: str) -> None:
    fact = _certified("codex", 10, "probe")
    del fact[drop]
    with pytest.raises(HostFactEvidenceError, match=match):
        HostFact.model_validate(fact)


def test_host_fact_uncertified_with_a_measurement_date_is_refused() -> None:
    with pytest.raises(HostFactEvidenceError, match="uncertified host fact carries no"):
        HostFact.model_validate(
            {
                "status": "uncertified",
                "measured_on": "claude",
                "reason": "not measured",
                "measured_at": date(2026, 1, 1),
            }
        )


def test_host_fact_zero_remeasure_age_is_a_schema_error() -> None:
    fact = _certified("codex", 10, "probe")
    fact["remeasure_after_days"] = 0
    with pytest.raises(ValidationError):
        HostFact.model_validate(fact)


def test_parse_host_facts_refuses_a_codex_fact_copied_from_the_claude_record() -> None:
    document = _document()
    document["claude"]["context_window_tokens"] = _certified("claude", 1000, "window probe")
    document["codex"]["context_window_tokens"] = copy.deepcopy(
        document["claude"]["context_window_tokens"]
    )
    with pytest.raises(HostFactInheritanceError, match="codex context_window_tokens was measured"):
        parse_host_facts(document)


def test_parse_host_facts_refuses_copied_evidence_relabelled_as_codex() -> None:
    document = _document()
    document["claude"]["context_window_tokens"] = _certified("claude", 1000, "window probe")
    document["codex"]["context_window_tokens"] = _certified("codex", 1000, "window probe")
    with pytest.raises(HostFactInheritanceError, match="cites the evidence certified for claude"):
        parse_host_facts(document)


def test_label_measurement_under_a_raised_cap_is_non_portable() -> None:
    codex = load_host_facts().codex
    measurement = label_measurement(
        codex, fact="project_document_cap_bytes", measured=40000, configured_value=65536
    )
    assert measurement.portability == "non_portable"
    assert measurement.diverged == "project_document_cap_bytes"
    assert measurement.certified_value == 32768
    assert "non-portable: codex project_document_cap_bytes configured 65536" in measurement.note


# ---- the shipped record ----------------------------------------------------


def test_load_host_facts_ships_one_record_per_runtime() -> None:
    registry = load_host_facts()
    assert tuple(record.runtime for record in registry.records) == HOST_RUNTIMES
    assert registry.codex.project_document_cap_bytes.default_value == 32768
    assert registry.claude.project_document_cap_bytes.status == "uncertified"
    assert registry.opencode.project_document_cap_bytes.status == "uncertified"
    assert registry.claude.import_shim == "CLAUDE.md"
    assert registry.codex.import_shim is None


def test_load_host_facts_records_unmeasured_facts_as_uncertified() -> None:
    for record in load_host_facts().records:
        for name in ("context_window_tokens", "auto_compaction_threshold_tokens"):
            fact = record.fact(name)
            assert fact.status == "uncertified"
            assert fact.default_value is None
            assert fact.reason


def test_certified_document_cap_refuses_an_uncertified_runtime() -> None:
    assert certified_document_cap("codex") == 32768
    with pytest.raises(HostFactEvidenceError, match="claude project-document cap is uncertified"):
        certified_document_cap("claude")


def test_host_fact_error_is_not_folded_into_a_schema_error() -> None:
    assert not issubclass(HostFactError, ValueError)


# ---- record validation -----------------------------------------------------


def test_parse_host_facts_refuses_a_record_filed_under_another_runtime() -> None:
    document = _document()
    document["opencode"] = copy.deepcopy(document["codex"])
    with pytest.raises(HostFactInheritanceError, match="opencode host-fact record describes codex"):
        parse_host_facts(document)


def test_parse_host_facts_refuses_a_certified_fact_without_evidence() -> None:
    document = _document()
    fact = _certified("claude", 10, "probe")
    del fact["evidence"]
    document["claude"]["tool_output_cap_tokens"] = fact
    with pytest.raises(HostFactEvidenceError, match="needs a default_value and the evidence"):
        parse_host_facts(document)


def test_parse_host_facts_refuses_an_uncertified_fact_with_a_value() -> None:
    document = _document()
    document["claude"]["tool_output_cap_tokens"]["default_value"] = 10
    with pytest.raises(HostFactEvidenceError, match="uncertified host fact carries no"):
        parse_host_facts(document)


def test_parse_host_facts_refuses_a_threshold_past_the_window() -> None:
    document = _document()
    document["codex"]["context_window_tokens"] = _certified("codex", 100, "window")
    document["codex"]["auto_compaction_threshold_tokens"] = _certified("codex", 101, "compact")
    with pytest.raises(HostFactEvidenceError, match="exceeds context_window_tokens 100"):
        parse_host_facts(document)


def test_parse_host_facts_threshold_equal_to_the_window_is_accepted() -> None:
    document = _document()
    document["codex"]["context_window_tokens"] = _certified("codex", 100, "window")
    document["codex"]["auto_compaction_threshold_tokens"] = _certified("codex", 100, "compact")
    assert parse_host_facts(document).codex.auto_compaction_threshold_tokens.default_value == 100


def test_parse_host_facts_refuses_repeated_reads_and_shared_shims() -> None:
    document = _document()
    document["codex"]["reads"] = ["card", "card"]
    with pytest.raises(HostFactEvidenceError, match="repeats a projection"):
        parse_host_facts(document)
    document = _document()
    document["opencode"]["import_shim"] = "CLAUDE.md"
    with pytest.raises(HostFactEvidenceError, match="same import shim"):
        parse_host_facts(document)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["codex"].update({"surprise": 1}),
        lambda d: d.pop("opencode"),
        lambda d: d["codex"].update({"reads": []}),
        lambda d: d["codex"]["project_document_cap_bytes"].update({"default_value": 0}),
        lambda d: d.update({"schema_version": 2}),
    ],
    ids=["extra-field", "missing-runtime", "empty-reads", "zero-cap", "unknown-schema"],
)
def test_parse_host_facts_rejects_a_schema_mismatch(mutate: Any) -> None:
    document = _document()
    mutate(document)
    with pytest.raises((ValidationError, HostFactEvidenceError)):
        parse_host_facts(document)


def test_host_fact_registry_is_frozen() -> None:
    with pytest.raises(ValidationError):
        load_host_facts().codex.runtime = "claude"


def test_host_fact_lookups_refuse_unknown_names() -> None:
    registry = load_host_facts()
    with pytest.raises(KeyError):
        registry.runtime("gemini")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        registry.codex.fact("latency")  # type: ignore[arg-type]


# ---- cap resolution --------------------------------------------------------


def test_smallest_certified_cap_takes_the_minimum_over_readers_only() -> None:
    registry = _with_doc_caps(claude=100, codex=200, opencode=50)
    policy = smallest_certified_cap(registry, "policy")
    assert policy is not None
    assert (policy.runtime, policy.cap_bytes) == ("claude", 100)
    card = smallest_certified_cap(registry, "card")
    assert card is not None
    assert (card.runtime, card.cap_bytes) == ("opencode", 50)
    assert card.readers == ("codex", "opencode")


def test_smallest_certified_cap_names_uncertified_readers() -> None:
    card = smallest_certified_cap(load_host_facts(), "card")
    assert card is not None
    assert (card.runtime, card.cap_bytes, card.uncertified) == ("codex", 32768, ("opencode",))


def test_smallest_certified_cap_with_no_certified_reader_is_none() -> None:
    assert smallest_certified_cap(_with_doc_caps(claude=None, codex=None), "policy") is None


def test_plan_rule_projections_fails_closed_without_a_certified_cap(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _with_doc_caps(codex=None))
    with pytest.raises(RuleProjectionBudgetError, match="has no certified project-document cap"):
        plan_rule_projections(repo)


def test_render_rule_projections_ignores_a_raised_configured_cap(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    size = max(p.record.byte_count for p in plan_rule_projections(repo).projections)
    codex_home = repo.parent / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        f"project_doc_max_bytes = {size * 4}\n", encoding="utf-8"
    )
    _use(monkeypatch, _with_doc_caps(codex=size - 1))
    with pytest.raises(RuleProjectionBudgetError, match="certified for codex"):
        render_rule_projections(repo)
    assert not (repo / CARD_TARGET).exists()


def test_plan_rule_projections_writes_the_shim_the_record_names(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _document()
    document["claude"]["import_shim"] = "CLAUDE.local.md"
    _use(monkeypatch, parse_host_facts(document))
    plan = plan_rule_projections(repo)
    assert plan.manifest.generated == (
        "CLAUDE.local.md",
        *(carrier_target(role) for role in builtin_carrier_roles()),
    )
    assert plan.generated[0].text == f"@{POLICY_TARGET}\n"


# ---- measurement portability ----------------------------------------------


@pytest.mark.parametrize("configured", [None, 32768])
def test_label_measurement_at_the_default_is_portable(configured: int | None) -> None:
    measurement = label_measurement(
        load_host_facts().codex,
        fact="project_document_cap_bytes",
        measured=0,
        configured_value=configured,
    )
    assert measurement.portability == "portable"
    assert measurement.diverged is None
    assert measurement.note == "portable"


def test_label_measurement_configuring_an_uncertified_fact_is_non_portable() -> None:
    measurement = label_measurement(
        load_host_facts().claude,
        fact="tool_output_cap_tokens",
        measured=1,
        configured_value=1,
    )
    assert measurement.portability == "non_portable"
    assert measurement.certified_value is None


def test_label_measurement_rejects_a_negative_measurement() -> None:
    with pytest.raises(ValidationError):
        label_measurement(
            load_host_facts().codex,
            fact="project_document_cap_bytes",
            measured=-1,
            configured_value=None,
        )


def test_measure_codex_document_reads_the_codex_home(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("project_doc_max_bytes = 32769\n", encoding="utf-8")
    measurement = measure_codex_document(100, codex_home=tmp_path)
    assert measurement.portability == "non_portable"
    assert measurement.configured_value == 32769


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, None),
        ("model = 'x'\n", None),
        ("project_doc_max_bytes = 0\n", None),
        ("project_doc_max_bytes = true\n", None),
        ("project_doc_max_bytes = 'big'\n", None),
        ("not [valid toml\n", None),
        ("project_doc_max_bytes = 1\n", 1),
    ],
    ids=["absent", "no-key", "zero", "bool", "string", "invalid", "single"],
)
def test_read_codex_document_cap(tmp_path: Path, content: str | None, expected: int | None) -> None:
    if content is not None:
        (tmp_path / "config.toml").write_text(content, encoding="utf-8")
    assert read_codex_document_cap(tmp_path) == expected
