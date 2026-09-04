"""Release preflight reds on a module-length exemption past its grant.

The pyproject exclusion list is a set of bounded promises to split
oversized modules. Tagging a release while one of those promises is
already broken renews it by inaction, which is the failure the expiry
date exists to stop. So the sweep must carry the finding: an expired
grant makes the realization row red and names the module, the expiry
date itself is still clean (the grant is inclusive), a malformed list
becomes a blocked row rather than an exception, and a clean list leaves
the row unproven rather than claiming the unmeasured assertions hold.
"""

from __future__ import annotations

import copy
import json
import textwrap
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import (
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalStatus,
    compute_readiness,
)

_NOW = datetime(2027, 2, 1, 12, 0, tzinfo=UTC)
_EXPIRES = date(2027, 1, 31)
_MODULE = "src/eawf/surfaces/tui/app.py"


def _config() -> ReleaseConfig:
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    return load_release_config({"release": body}, train=V07_TRAIN)


def _repo(tmp_path: Path, exclude_body: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [tool.eawf.lint]
            enabled = ["EAWF010"]

            [tool.eawf.lint.eawf010]
            max-loc = 1400
            exclude = [
            {exclude_body}
            ]
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


def _grant(path: str = _MODULE, expires: str = "2027-01-31") -> str:
    return f'    {{ path = "{path}", expires = "{expires}", reason = "awaiting a split" }},'


def _sweep(repo_root: Path, *, today: date) -> Any:
    inputs = TagPreflightInputs(
        repo_root=repo_root,
        version="0.7.0.dev1",
        tag="v0.7.0.dev1",
        package_version="0.7.0.dev1",
        remote="origin",
        today=today,
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


def test_exclusion_expiry_reds_the_realization_row_one_day_past_the_grant(
    tmp_path: Path,
) -> None:
    readiness = _sweep(_repo(tmp_path, _grant()), today=date(2027, 2, 1))
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is ReleaseSignalStatus.FAIL
    assert row.failure_code is ReleaseSignalFailureCode.PERFECT_REALIZATION_FAILED


def test_exclusion_expiry_red_row_names_the_module(tmp_path: Path) -> None:
    readiness = _sweep(_repo(tmp_path, _grant()), today=date(2027, 2, 1))
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert _MODULE in row.remediation
    assert "module_length_exclusion" in row.remediation


def test_exclusion_expiry_red_row_carries_a_per_module_evidence_ref(tmp_path: Path) -> None:
    readiness = _sweep(_repo(tmp_path, _grant()), today=date(2027, 2, 1))
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.evidence_refs == (f"module_length_exclusion:{_MODULE}:expired:{_EXPIRES}",)


def test_exclusion_expiry_names_every_lapsed_module(tmp_path: Path) -> None:
    body = "\n".join([_grant(), _grant(path="src/eawf/other.py", expires="2026-12-01")])
    readiness = _sweep(_repo(tmp_path, body), today=date(2027, 2, 1))
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert _MODULE in row.remediation
    assert "src/eawf/other.py" in row.remediation


def test_exclusion_expiry_is_clean_exactly_on_the_expiry_date(tmp_path: Path) -> None:
    readiness = _sweep(_repo(tmp_path, _grant()), today=_EXPIRES)
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is ReleaseSignalStatus.UNAVAILABLE


def test_exclusion_expiry_is_clean_the_day_before_the_expiry_date(tmp_path: Path) -> None:
    readiness = _sweep(_repo(tmp_path, _grant()), today=date(2027, 1, 30))
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is ReleaseSignalStatus.UNAVAILABLE


def test_exclusion_expiry_clean_row_never_claims_a_pass(tmp_path: Path) -> None:
    readiness = _sweep(_repo(tmp_path, _grant()), today=_EXPIRES)
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is not ReleaseSignalStatus.PASS
    assert "no producer" in row.remediation


def test_exclusion_expiry_empty_grant_list_leaves_the_row_unproven(tmp_path: Path) -> None:
    readiness = _sweep(_repo(tmp_path, ""), today=date(2030, 1, 1))
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is ReleaseSignalStatus.UNAVAILABLE


def test_exclusion_expiry_malformed_grant_blocks_rather_than_raising(tmp_path: Path) -> None:
    readiness = _sweep(_repo(tmp_path, f'    "{_MODULE}",'), today=date(2027, 2, 1))
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is ReleaseSignalStatus.BLOCKED
    assert "ExclusionConfigError" in row.remediation


def test_exclusion_expiry_keeps_the_sweep_total(tmp_path: Path) -> None:
    readiness = _sweep(_repo(tmp_path, _grant()), today=date(2027, 2, 1))
    assert len(readiness.signals) == len(ReleaseSignalName)
    assert readiness.ready is False


def test_exclusion_expiry_row_is_reported_but_not_yet_gate_bound(tmp_path: Path) -> None:
    """The red row is computed and readable, but no dev1 gate binds it yet.

    The finding reaches the sweep and the ``release preflight`` output; what
    it does not yet do is deny an approval, because the dev1 binding table
    declares no gate over the realization row. This assertion is the
    tripwire on that seam: it reds the day a gate binds the row, which is
    the day this test should be replaced by one asserting the denial.
    """
    readiness = _sweep(_repo(tmp_path, _grant()), today=date(2027, 2, 1))
    assert readiness.row(ReleaseSignalName.PERFECT_REALIZATION).status is (ReleaseSignalStatus.FAIL)
    assert ReleaseSignalName.PERFECT_REALIZATION not in readiness.required_signals


def _renewed(decision: str, *, expires: str = "2026-01-31") -> str:
    return (
        f'    {{ path = "{_MODULE}", expires = "{expires}", reason = "awaiting a split", '
        f'renewal = {{ decision = "{decision}", owner = "eawf-maintainers" }} }},'
    )


def _with_decisions(repo_root: Path, *ids: str) -> Path:
    ea = repo_root / ".ea"
    ea.mkdir(exist_ok=True)
    (ea / "state.json").write_text(
        json.dumps({"decisions": {name: {"id": name} for name in ids}}), encoding="utf-8"
    )
    return repo_root


def test_exclusion_expiry_renewal_against_an_unknown_decision_reds_the_row(
    tmp_path: Path,
) -> None:
    repo = _with_decisions(_repo(tmp_path, _renewed("D-GHOST")), "D-REAL")
    row = _sweep(repo, today=date(2027, 2, 1)).row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is ReleaseSignalStatus.FAIL
    assert "D-GHOST" in row.remediation
    assert row.evidence_refs == ("module_length_exclusion:unratified-renewal",)


def test_exclusion_expiry_renewal_reds_before_the_grant_even_lapses(tmp_path: Path) -> None:
    repo = _with_decisions(_repo(tmp_path, _renewed("D-GHOST", expires="2030-01-01")), "D-REAL")
    row = _sweep(repo, today=date(2027, 2, 1)).row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is ReleaseSignalStatus.FAIL
    assert "absent from state.json" in row.remediation


def test_exclusion_expiry_renewal_against_a_recorded_decision_is_accepted(tmp_path: Path) -> None:
    repo = _with_decisions(_repo(tmp_path, _renewed("D-REAL", expires="2030-01-01")), "D-REAL")
    row = _sweep(repo, today=date(2027, 2, 1)).row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is ReleaseSignalStatus.UNAVAILABLE


def test_exclusion_expiry_renewal_needs_state_json_to_be_present(tmp_path: Path) -> None:
    row = _sweep(_repo(tmp_path, _renewed("D-REAL")), today=date(2027, 2, 1)).row(
        ReleaseSignalName.PERFECT_REALIZATION
    )
    assert row.status is ReleaseSignalStatus.FAIL


def test_exclusion_expiry_repo_grants_are_live_today() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    readiness = _sweep(repo_root, today=datetime.now(UTC).date())
    row = readiness.row(ReleaseSignalName.PERFECT_REALIZATION)
    assert row.status is not ReleaseSignalStatus.FAIL, row.remediation
