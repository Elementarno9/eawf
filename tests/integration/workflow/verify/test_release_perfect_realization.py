"""The perfect-realization release row conjoins decidable assertions and can pass.

Every assertion a checkout can decide is decided, pass or fail with a
reason, and the row passes when they all hold and no assertion the
checkpoint requires is undecided. The packet run is the one assertion no
checkout decides; only the stable checkpoint requires it.
"""

from __future__ import annotations

import copy
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.spec.release import ReleaseChannel
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.surfaces.cli import verb_catalog as verb_catalog_module
from eawf.surfaces.cli import verb_closure
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify import realization_assertions
from eawf.workflow.verify.realization_assertions import (
    REQUIREMENT_CATALOG_PATH,
    REQUIREMENT_TRACE_TOOL,
    RealizationAssertion,
    cli_closure_outcome,
    packet_run_outcome,
    realization_outcome,
    rendered_rules_outcome,
    requirement_trace_outcome,
)
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import (
    ReleaseReadiness,
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalStatus,
    compute_readiness,
)

_NOW = datetime(2027, 2, 1, 12, 0, tzinfo=UTC)


def _config() -> ReleaseConfig:
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    return load_release_config({"release": body}, train=V07_TRAIN)


def _sweep(repo_root: Path) -> ReleaseReadiness:
    inputs = TagPreflightInputs(
        repo_root=repo_root,
        version="0.7.0.dev1",
        tag="v0.7.0.dev1",
        package_version="0.7.0.dev1",
        remote="origin",
        today=date(2027, 2, 1),
    )
    return compute_readiness(
        _config(),
        probes={
            ReleaseSignalName.PERFECT_REALIZATION: build_tag_probes(inputs)[
                ReleaseSignalName.PERFECT_REALIZATION
            ]
        },
        observed_revision="deadbee",
        computed_at=_NOW,
    )


def _clean_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.eawf.lint]\nenabled = ["EAWF010"]\n', encoding="utf-8"
    )
    return tmp_path


def _held(name: str) -> ReleaseSignalOutcome:
    return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, evidence_refs=(f"{name}:ok",))


def _verdicts(
    **overrides: ReleaseSignalOutcome,
) -> dict[RealizationAssertion, ReleaseSignalOutcome]:
    verdicts = {assertion: _held(assertion.value) for assertion in RealizationAssertion}
    verdicts[RealizationAssertion.PACKET_RUN] = packet_run_outcome()
    for name, outcome in overrides.items():
        verdicts[RealizationAssertion(name)] = outcome
    return verdicts


def _broken(name: str) -> ReleaseSignalOutcome:
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.FAIL,
        remediation=f"{name}: broken",
        evidence_refs=(f"{name}:bad",),
    )


# ---- the row through the tag chokepoint --------------------------------------


def test_perfect_realization_row_passes_when_every_decidable_assertion_holds(
    tmp_path: Path,
) -> None:
    row = _sweep(_clean_repo(tmp_path)).row(ReleaseSignalName.PERFECT_REALIZATION)

    assert row.status is ReleaseSignalStatus.PASS, row.remediation
    assert row.failure_code is None
    assert "packet_run:unavailable:not-required-before-stable" in row.evidence_refs
    named = {ref.split(":", 1)[0] for ref in row.evidence_refs}
    assert named == {assertion.value for assertion in RealizationAssertion}


def test_perfect_realization_row_reds_and_names_every_broken_assertion(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    (repo / REQUIREMENT_CATALOG_PATH).parent.mkdir(parents=True)
    (repo / REQUIREMENT_CATALOG_PATH).write_text("{}", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[tool.eawf.lint]\nenabled = ["EAWF010"]\n\n[tool.eawf.lint.eawf010]\nexclude = [\n'
        '  { path = "src/eawf/big.py", expires = "2027-01-31", reason = "split later" },\n]\n',
        encoding="utf-8",
    )

    row = _sweep(repo).row(ReleaseSignalName.PERFECT_REALIZATION)

    assert row.status is ReleaseSignalStatus.FAIL
    assert row.failure_code is ReleaseSignalFailureCode.PERFECT_REALIZATION_FAILED
    assert "module_length_exclusion" in row.remediation
    assert "requirement_trace" in row.remediation
    assert all(
        ref.split(":", 1)[0] in {"module_length_exclusion", "requirement_trace"}
        for ref in row.evidence_refs
    )


# ---- the conjunction ----------------------------------------------------------


def test_realization_outcome_passes_a_candidate_without_the_packet_run() -> None:
    outcome = realization_outcome(_verdicts(), channel=ReleaseChannel.RC)

    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs[-1] == "packet_run:unavailable:not-required-before-stable"
    assert len(outcome.evidence_refs) == len(RealizationAssertion)


def test_realization_outcome_keeps_stable_unavailable_until_the_packet_run_has_a_producer() -> None:
    outcome = realization_outcome(_verdicts(), channel=ReleaseChannel.STABLE)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert outcome.remediation.startswith("packet_run:")
    assert outcome.evidence_refs == ()


def test_realization_outcome_passes_stable_once_the_packet_run_holds() -> None:
    outcome = realization_outcome(
        _verdicts(packet_run=_held("packet_run")), channel=ReleaseChannel.STABLE
    )

    assert outcome.status is ReleaseSignalStatus.PASS
    assert "packet_run:ok" in outcome.evidence_refs


def test_realization_outcome_keeps_a_required_undecided_assertion_unavailable() -> None:
    undecided = ReleaseSignalOutcome(
        status=ReleaseSignalStatus.UNAVAILABLE, remediation="cli_closure: no verdict"
    )

    outcome = realization_outcome(_verdicts(cli_closure=undecided), channel=ReleaseChannel.DEV)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert outcome.remediation == "cli_closure: no verdict"


def test_realization_outcome_reds_with_only_the_broken_assertions_evidence() -> None:
    outcome = realization_outcome(
        _verdicts(rendered_rules=_broken("rendered_rules")), channel=ReleaseChannel.RC
    )

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.remediation == "rendered_rules: broken"
    assert outcome.evidence_refs == ("rendered_rules:bad",)


def test_realization_outcome_a_break_outranks_an_undecided_required_assertion() -> None:
    outcome = realization_outcome(
        _verdicts(
            cli_closure=_broken("cli_closure"), requirement_trace=_broken("requirement_trace")
        ),
        channel=ReleaseChannel.STABLE,
    )

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.remediation == "requirement_trace: broken; cli_closure: broken"
    assert outcome.evidence_refs == ("requirement_trace:bad", "cli_closure:bad")


# ---- requirement trace ----------------------------------------------------------


def _traced(repo: Path, tool_body: str) -> Path:
    (repo / REQUIREMENT_CATALOG_PATH).parent.mkdir(parents=True)
    (repo / REQUIREMENT_CATALOG_PATH).write_text("{}", encoding="utf-8")
    (repo / REQUIREMENT_TRACE_TOOL).parent.mkdir(parents=True)
    (repo / REQUIREMENT_TRACE_TOOL).write_text(tool_body, encoding="utf-8")
    return repo


def test_requirement_trace_holds_on_a_working_copy_with_no_census(tmp_path: Path) -> None:
    outcome = requirement_trace_outcome(tmp_path)

    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs == ("requirement_trace:no-catalog",)


def test_requirement_trace_holds_when_the_census_check_passes(tmp_path: Path) -> None:
    repo = _traced(tmp_path, "raise SystemExit(0)\n")

    outcome = requirement_trace_outcome(repo)

    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs == (f"requirement_trace:{REQUIREMENT_CATALOG_PATH}:fresh",)


def test_requirement_trace_breaks_with_the_census_report(tmp_path: Path) -> None:
    repo = _traced(
        tmp_path,
        "import sys\n"
        "print('requirement-trace: stale census, 1 row(s) differ: XYZ-001', file=sys.stderr)\n"
        "print('requirement-trace: 1 unowned id(s): XYZ-002', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
    )

    outcome = requirement_trace_outcome(repo)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.remediation == (
        "requirement_trace: stale census, 1 row(s) differ: XYZ-001; "
        f"1 unowned id(s): XYZ-002; then commit {REQUIREMENT_CATALOG_PATH}"
    )


def test_requirement_trace_breaks_when_a_census_has_no_tool(tmp_path: Path) -> None:
    (tmp_path / REQUIREMENT_CATALOG_PATH).parent.mkdir(parents=True)
    (tmp_path / REQUIREMENT_CATALOG_PATH).write_text("{}", encoding="utf-8")

    outcome = requirement_trace_outcome(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.evidence_refs == (f"requirement_trace:{REQUIREMENT_CATALOG_PATH}",)


# ---- rendered rules --------------------------------------------------------------


class _Plan:
    outputs = (("AGENTS.md", "rendered\n"), ("CLAUDE.md", "local\n"))


def _ruled_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: tuple[str, ...]) -> Path:
    (tmp_path / ".ea").mkdir()
    (tmp_path / ".ea" / "rules.yaml").write_text("rules: []\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("rendered\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "AGENTS.md"], check=True)
    monkeypatch.setattr(realization_assertions, "plan_rule_projections", lambda root: _Plan())
    monkeypatch.setattr(realization_assertions, "projection_drift", lambda root, plan: drift)
    return tmp_path


def test_rendered_rules_holds_on_a_working_copy_with_no_rule_source(tmp_path: Path) -> None:
    outcome = rendered_rules_outcome(tmp_path)

    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs == ("rendered_rules:no-rule-source",)


def test_rendered_rules_ignores_drift_in_a_local_only_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = rendered_rules_outcome(_ruled_repo(tmp_path, monkeypatch, ("CLAUDE.md",)))

    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs == ("rendered_rules:AGENTS.md",)


def test_rendered_rules_breaks_on_a_drifted_committed_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ruled_repo(tmp_path, monkeypatch, ("AGENTS.md", "CLAUDE.md"))

    outcome = rendered_rules_outcome(repo)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "1 committed projection(s)" in outcome.remediation
    assert outcome.evidence_refs == ("rendered_rules:AGENTS.md:drifted",)


def test_rendered_rules_the_repository_projection_is_its_own_render() -> None:
    repo = Path(__file__).resolve().parents[4]

    outcome = rendered_rules_outcome(repo)

    assert outcome.status is ReleaseSignalStatus.PASS, outcome.remediation
    assert "rendered_rules:AGENTS.md" in outcome.evidence_refs


# ---- CLI closure -----------------------------------------------------------------


def test_cli_closure_holds_on_the_installed_command_tree() -> None:
    outcome = cli_closure_outcome()

    assert outcome.status is ReleaseSignalStatus.PASS, outcome.remediation
    (ref,) = outcome.evidence_refs
    assert ref.startswith("cli_closure:") and ref.endswith("-verbs")


def test_cli_closure_breaks_on_an_undeclared_root_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    dropped, *kept = verb_closure.ROOT_ENTRY_EXCEPTIONS
    monkeypatch.setattr(verb_closure, "ROOT_ENTRY_EXCEPTIONS", tuple(kept))

    outcome = cli_closure_outcome()

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.evidence_refs == (f"cli_closure:root:{dropped.name}",)


def test_cli_closure_breaks_when_the_verb_catalog_refuses_the_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> None:
        raise verb_catalog_module.VerbCatalogError("verb 'x y' declares no effect class")

    monkeypatch.setattr(verb_catalog_module, "verb_catalog", refuse)

    outcome = cli_closure_outcome()

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.remediation == "cli_closure: verb 'x y' declares no effect class"
    assert outcome.evidence_refs == ("cli_closure:verb-catalog",)


def test_rendered_rules_names_a_rule_source_with_no_committed_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _ruled_repo(tmp_path, monkeypatch, ())
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "--cached", "AGENTS.md"], check=True)

    outcome = rendered_rules_outcome(repo)

    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs == ("rendered_rules:no-committed-projection",)
