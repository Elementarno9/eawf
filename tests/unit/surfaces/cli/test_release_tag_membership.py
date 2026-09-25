"""Gate-fire proof: dev3 ``tag``/``preflight`` resolve membership refs at config load.

``_checkpoint_config`` used to load the checkpoint config with no
membership refs at all, so ``release tag --push`` and ``release
preflight`` refused every dev3 sweep with
``invalid_membership_cardinality`` before a single signal ran -- the
structural cardinality check the loader always runs for a membership
rung, tripped by the CLI passing it nothing rather than by the
checkpoint's own state.

The refs cannot come from a CLI option or a stored record the way
``eawf release create``'s do: the tag is pushed, and CI's own preflight
step (``.github/workflows/release.yaml``) runs
``eawf release preflight <version> --source <sha>`` from an automated
job with nothing to pass and no record open yet either (``tag`` runs
before ``create`` in the dev3 order). ``_checkpoint_config`` instead
resolves them through
:func:`~eawf.workflow.release.admission.committed_membership_refs`,
which reads the committed canary export -- the artifact that does exist
at tag/preflight time.

The resolvable case here uses this checkout's real, committed W37CANARY
Milestone bundle, so the membership *resolution* signal
(``membership_probe``, unchanged by this fix) also runs for real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import pytest
import typer

from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.commands import release_tag
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.workflow.release.admission import committed_membership_refs
from eawf.workflow.release.signal_probes import membership_probe
from eawf.workflow.verify.release_readiness import ReleaseSignalContext, compute_readiness
from tests._release_helpers import all_passing

pytestmark = pytest.mark.unit

#: This checkout, whose committed export backs the resolvable ref below.
REPO_ROOT = Path(__file__).resolve().parents[4]

#: The checkpoint under test: the first membership rung.
DEV3_VERSION = "0.7.0.dev3"
DEV3_RELEASE_KEY = "REL-0.7.0.dev3"

#: The next rung, whose configuration is not authored (error-path fixture).
DEV4_VERSION = "0.7.0.dev4"

#: The real, committed W37CANARY acceptance bundle. Resolves against
#: ``.ea/artifacts/evidence/2026-09-18-dev3-conformance/native-canary-evidence.json``.
RESOLVABLE_REF = (
    "eawf://WSP-W37CANARY/PRJ-W37CANARY/REP-W37CANARY/milestone/MLS-0001#MAB-0001-MLS-0001"
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


# --- committed_membership_refs: reading the export --------------------------


def test_committed_membership_refs_reads_the_dev3_export() -> None:
    assert committed_membership_refs(REPO_ROOT, DEV3_RELEASE_KEY) == (RESOLVABLE_REF,)


def test_committed_membership_refs_empty_for_a_release_key_the_export_does_not_name() -> None:
    """Boundary: the dev2 key, which the committed export was not produced for."""
    assert committed_membership_refs(REPO_ROOT, "REL-0.7.0.dev2") == ()


def test_committed_membership_refs_empty_with_no_export(tmp_path: Path) -> None:
    """Boundary: an empty checkout, e.g. CI's shallow-of-evidence job runner."""
    assert committed_membership_refs(tmp_path, DEV3_RELEASE_KEY) == ()


# --- _checkpoint_config: the cardinality gate --------------------------------


def test_checkpoint_config_resolves_dev3_from_the_committed_export() -> None:
    config = release_tag._checkpoint_config(DEV3_VERSION, repo_root=REPO_ROOT)
    assert config.membership_refs == (RESOLVABLE_REF,)


def test_checkpoint_config_refuses_dev3_with_no_committed_export(tmp_path: Path) -> None:
    """Gate-fire proof (CR-01): a membership rung swept with none still refuses."""
    with pytest.raises(cli_errors.ValidationError) as excinfo:
        release_tag._checkpoint_config(DEV3_VERSION, repo_root=tmp_path)
    assert "invalid_membership_cardinality" in str(excinfo.value)


def test_checkpoint_config_still_resolves_dev2_with_no_matching_export() -> None:
    """Regression guard: the epoch-1 rungs, which forbid refs, are unaffected."""
    config = release_tag._checkpoint_config("0.7.0.dev2", repo_root=REPO_ROOT)
    assert config.membership_refs == ()


def test_checkpoint_config_refuses_an_unauthored_version(tmp_path: Path) -> None:
    """Error path: an unauthored version reports NotFound, not cardinality."""
    with pytest.raises(cli_errors.UserError) as excinfo:
        release_tag._checkpoint_config(DEV4_VERSION, repo_root=tmp_path)
    assert excinfo.value.kind == "NotFound"


# --- resolution: the derived refs pass the sweep's own signal ---------------
#
# Known limit (see the module docstring of release_tag.py): at
# tag/preflight time the refs resolved here come from the same export
# `membership_probe` reads, so this can only fail by the export naming
# no accepted Milestone for this release at all -- it is not a check
# that the eventual `release create` record will agree with what was
# accepted, which is `assert_membership_resolves`'s job.


def test_committed_refs_pass_the_resolution_signal() -> None:
    config = release_tag._checkpoint_config(DEV3_VERSION, repo_root=REPO_ROOT)
    context = ReleaseSignalContext(config, ReleaseSignalName.MEMBERSHIP, None)
    outcome = membership_probe(context, repo_root=REPO_ROOT)
    assert outcome.status is ReleaseSignalStatus.PASS


# --- CLI wiring: both verbs resolve through the committed export ------------
#
# Driven in process (a constructed :class:`typer.Context` plus
# :class:`GlobalFlags`) rather than through a CLI runner, so the suite
# stays inside the unit tier (``eawf024-test-tier-contract`` forbids
# ``CliRunner``/subprocess harnesses there) while still exercising the
# real command bodies. A checkout-level ``eawf --json release preflight``
# regression sits in ``tests/integration/`` instead, since it needs a
# real git checkout to run the command through.


@pytest.fixture(autouse=True)
def _stub_sweep_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the sweep and the git side effects ``tag``/``preflight`` run.

    Only ``_checkpoint_config`` -- the piece under test -- runs for
    real; every other signal and every subprocess call is stubbed, so a
    green result below is attributable to config resolution alone, not
    to this checkout's git or CI-receipt state.
    """

    def fake_sweep_for_tag(config: Any, **_kwargs: Any) -> Any:
        return compute_readiness(config, probes=all_passing(), computed_at=NOW)

    monkeypatch.setattr("eawf.runtime.release.sweep_for_tag", fake_sweep_for_tag)
    monkeypatch.setattr(release_tag, "_dirty_paths", lambda repo_root: ())
    monkeypatch.setattr(release_tag, "_head_revision", lambda repo_root: "a" * 40)


def _run_preflight(version: str) -> int:
    """Run ``release preflight`` in process and return its exit code."""
    ctx = typer.Context(click.Command("preflight"))
    ctx.obj = GlobalFlags(json_output=False)
    try:
        release_tag.release_preflight(
            ctx, version=version, source=None, remote="origin", waiver_count=0
        )
    except click.exceptions.Exit as exit_signal:
        return int(exit_signal.exit_code)
    return exit_codes.OK


def _run_tag(version: str, *, push: bool, dry_run: bool) -> int:
    """Run ``release tag`` in process and return its exit code."""
    ctx = typer.Context(click.Command("tag"))
    ctx.obj = GlobalFlags(json_output=False)
    try:
        release_tag.release_tag(
            ctx,
            version=version,
            push=push,
            remote="origin",
            force=False,
            waive_dirty_tree=None,
            dry_run=dry_run,
        )
    except click.exceptions.Exit as exit_signal:
        return int(exit_signal.exit_code)
    return exit_codes.OK


def test_preflight_exits_zero_for_dev3_in_this_checkout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    exit_code = _run_preflight(DEV3_VERSION)
    assert exit_code == exit_codes.OK, capsys.readouterr().out


def test_preflight_refuses_dev3_with_no_committed_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gate-fire proof (CR-01), at the CLI boundary."""
    monkeypatch.chdir(tmp_path)
    exit_code = _run_preflight(DEV3_VERSION)
    assert exit_code == exit_codes.VALIDATION_ERROR
    assert "invalid_membership_cardinality" in capsys.readouterr().out


def test_tag_push_dry_run_exits_zero_for_dev3_in_this_checkout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    exit_code = _run_tag(DEV3_VERSION, push=True, dry_run=True)
    assert exit_code == exit_codes.OK, capsys.readouterr().out


def test_tag_push_dry_run_refuses_dev3_with_no_committed_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code = _run_tag(DEV3_VERSION, push=True, dry_run=True)
    assert exit_code == exit_codes.VALIDATION_ERROR
    assert "invalid_membership_cardinality" in capsys.readouterr().out


def test_tag_without_push_does_not_need_a_committed_export(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Boundary: ``_checkpoint_config`` only runs behind ``--push``."""
    monkeypatch.chdir(REPO_ROOT)
    exit_code = _run_tag(DEV3_VERSION, push=False, dry_run=True)
    assert exit_code == exit_codes.OK, capsys.readouterr().out
