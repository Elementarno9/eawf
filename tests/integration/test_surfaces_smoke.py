"""P26-I01 surfaces smoke test (verify-implements pass, P26-W15).

A thin end-to-end sweep over the C04 + C05 surface deliverables (waves
W02-W14) that asserts each contract boundary still holds: the CLI help
surface, the daemonless ``config validate`` deprecation-clean output, the
0..5 exit-code surface, the ``ErrorEnvelope`` strict shape, the 17-skill
registry, the ``SkillManifest`` invariants (BOT-06), the per-runtime
cache-control injection gate, and the skill -> adapter handshake reject.

Each assertion exercises the real symbol path (no mocks of the unit
under test) so a regression in any surface fails here fast, independent
of the deeper per-wave unit suites.

Two assertions are intentionally NOT here yet — they depend on follow-up
waves surfaced by the audit:

* **W26** appends the user-facing catalog-parity assertion (the six C04b
  skills are currently registered in the runtime registry but absent from
  the user-facing catalog at 11; W26 closes the gap to 17).
* **W25** appends the daemonless-rejection-breadth assertion (the
  mutating-verb daemon-escalation rejection is wired to ``state rpc``
  only; W25 extends it across all mutating verbs).

The integration ``conftest`` forces ``EAWF_DAEMONLESS=1`` autouse; the
config-validate assertion also sets it explicitly so the test documents
its own daemonless precondition.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli import exit_codes
from eawf.cli.app import app
from eawf.cli.errors import ErrorEnvelope
from eawf.config import layered
from eawf.runtimes.cache_control import inject_cache_control
from eawf.runtimes.dispatch import AdapterManifestMismatchError, resolve_adapter
from eawf.runtimes.plugin_manifest import SkillManifest
from eawf.skills import _bootstrap as _skills_bootstrap  # noqa: F401 — registers all skills
from eawf.skills import registry

runner = CliRunner()


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Provide an isolated repo root + sandboxed global-config path.

    The global layer is redirected to a per-test tmp file (mirroring
    ``test_cli_config.py``) so neither the host's real
    ``~/.config/eawf/config.yaml`` nor a stray key written by an earlier
    test in the same session leaks into the ``config validate`` view.
    """
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    fake_global = tmp_path / "global.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.chdir(repo)
    yield repo


# --- assertion 1: CLI help surface (W06) -----------------------------------


def test_cli_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output


# --- assertion 2: daemonless config validate is deprecation-clean (W02) ----


def test_daemonless_config_validate_clean_no_deprecation(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    # The blitz recursion guard writes ``EAWF_BLITZ_DEPTH_COUNTER`` straight
    # into ``os.environ`` (it is not on the env-layer control-knob
    # allowlist), so a blitz test earlier in the session can leak it into
    # the ``EAWF_*`` env config layer and trip strict ``extra="forbid"``
    # validation. Clear it the same way the blitz suites do at setup.
    monkeypatch.delenv("EAWF_BLITZ_DEPTH_COUNTER", raising=False)
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "deprecated_runtime_kind" not in result.output


# --- assertion 3: 0..5 exit-code surface (W04) -----------------------------


def test_exit_codes_expose_zero_through_five() -> None:
    surface = {
        exit_codes.OK,
        exit_codes.USER_ERROR,
        exit_codes.VALIDATION_ERROR,
        exit_codes.STATE_CONFLICT,
        exit_codes.DAEMON_UNREACHABLE,
        exit_codes.INTERNAL_ERROR,
    }
    assert surface == {0, 1, 2, 3, 4, 5}
    assert exit_codes.name_for(5) == "INTERNAL_ERROR"


def test_exit_codes_name_for_out_of_range_raises_key_error() -> None:
    with pytest.raises(KeyError):
        exit_codes.name_for(6)


# --- assertion 4: ErrorEnvelope strict shape (W04) -------------------------


def test_error_envelope_rejects_unknown_key() -> None:
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        ErrorEnvelope(
            error="UserError",
            message="bad input",
            exit_code=1,
            exit_name="USER_ERROR",
            bogus_key="nope",
        )


def test_error_envelope_accepts_real_fields() -> None:
    env = ErrorEnvelope(
        error="StateConflict",
        message="lock held",
        exit_code=3,
        exit_name="STATE_CONFLICT",
        suggested_next_step="run `eawf doctor`",
        data={"kind": "LockConflict"},
        correlation_id="rpc-7",
        protocol_version="1.0",
    )
    assert env.schema_version == "1.0"
    assert env.error == "StateConflict"
    assert env.exit_code == 3
    assert env.data["kind"] == "LockConflict"


# --- assertion 5: 17-skill registry after bootstrap (W11) ------------------


def test_skill_registry_holds_seventeen_after_bootstrap() -> None:
    registered = registry.list_registered()
    assert len(registered) == 17


# --- assertion 6: SkillManifest invariants / BOT-06 (W10) ------------------


def test_skill_manifest_empty_runtime_raises_value_error() -> None:
    with pytest.raises(ValueError):
        SkillManifest(
            name="/probe",
            description="probe skill",
            runtime=[],
            output_envelope_kind="probe_result",
        )


def test_skill_manifest_target_dir_rejected_use_output_dir() -> None:
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        SkillManifest(
            name="/probe",
            description="probe skill",
            runtime=["claude-code"],
            output_envelope_kind="probe_result",
            target_dir="/tmp/x",
        )


# --- assertion 7: per-runtime cache-control injection gate (W14) -----------


def test_inject_cache_control_claude_appends_marker() -> None:
    out = inject_cache_control(runtime_id="claude-code", cache_prefix="HEAD")
    assert out is not None
    assert out.startswith("HEAD")
    assert out != "HEAD"
    assert "cache_control" in out


@pytest.mark.parametrize("runtime_id", ["opencode", "codex"])
def test_inject_cache_control_no_op_runtimes_unchanged(runtime_id: str) -> None:
    assert inject_cache_control(runtime_id=runtime_id, cache_prefix="HEAD") == "HEAD"


# --- assertion 8: skill -> adapter handshake reject (W13) ------------------


def test_resolve_adapter_override_off_manifest_rejected() -> None:
    manifest = SkillManifest(
        name="/claude-only",
        description="claude-only skill",
        runtime=["claude-code"],
        output_envelope_kind="probe_result",
    )
    with pytest.raises(AdapterManifestMismatchError):
        resolve_adapter(manifest=manifest, preference=None, override="codex")
