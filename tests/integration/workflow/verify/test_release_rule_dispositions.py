"""LINT-001: a lint rule with no disposition fails the release preflight.

The tag chokepoint's realization probe reads the disposition table against
the rules the tagged tree ships. A rule nobody decided the fate of reds
the row and is named in it; a governed suite hands the row on to the
module-length exclusion check, which leaves it unproven rather than
claiming a pass.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.platform.lint.dispositions import LINT_PACKAGE
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import (
    ReleaseReadiness,
    ReleaseSignalName,
    ReleaseSignalStatus,
    compute_readiness,
)

_NOW = datetime(2027, 2, 1, 12, 0, tzinfo=UTC)


def _config() -> ReleaseConfig:
    body: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))["release"]
    return load_release_config({"release": body}, train=V07_TRAIN)


def _repo(tmp_path: Path, rules: dict[str, str]) -> Path:
    """Return a tree shipping *rules* (module name to rule code) and no exclusions."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.eawf.lint]\nenabled = ["EAWF010"]\n', encoding="utf-8"
    )
    package = tmp_path / LINT_PACKAGE
    package.mkdir(parents=True)
    for module, code in rules.items():
        (package / module).write_text(
            f'"""Rule {code}."""\nRULE_CODE = "{code}"\n', encoding="utf-8"
        )
    return tmp_path


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


def test_lint_001_a_rule_with_no_disposition_reds_the_release_preflight(tmp_path: Path) -> None:
    """Gate fire: a shipped rule code the table does not carry."""
    row = _sweep(_repo(tmp_path, {"eawf099.py": "EAWF099"})).row(
        ReleaseSignalName.PERFECT_REALIZATION
    )

    assert row.status is ReleaseSignalStatus.FAIL
    assert "EAWF099: ships with no disposition" in row.remediation
    assert row.evidence_refs == ("rule_disposition:EAWF099",)


def test_lint_001_a_governed_suite_leaves_the_row_to_the_exclusion_check(tmp_path: Path) -> None:
    row = _sweep(_repo(tmp_path, {"eawf001.py": "EAWF001"})).row(
        ReleaseSignalName.PERFECT_REALIZATION
    )

    assert row.status is ReleaseSignalStatus.UNAVAILABLE
    assert "module_length_exclusion" in row.remediation


def test_lint_001_the_repository_itself_passes_the_disposition_leg() -> None:
    repo = Path(__file__).resolve().parents[4]
    row = _sweep(repo).row(ReleaseSignalName.PERFECT_REALIZATION)

    assert "rule_disposition" not in (row.remediation or "")
