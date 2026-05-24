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

W26 added the user-facing catalog-parity assertion below: the six C04b
skills were registered in the runtime registry (17) but absent from the
user-facing catalog (11); W26 closed the gap so ``CANONICAL_SKILL_NAMES``
and the runtime registry agree at 17, proving catalog/registry parity.

W25 added the daemonless-rejection-breadth assertion below: the
mutating-verb daemon-escalation rejection now fires at the shared
``state_transaction`` chokepoint, so a representative mutating verb
(``roadmap revise``) refuses ``--daemonless`` while a read verb
(``roadmap show``) still honours the carve-out.

The integration ``conftest`` forces ``EAWF_DAEMONLESS=1`` autouse; the
config-validate assertion also sets it explicitly so the test documents
its own daemonless precondition. Note the breadth assertion keys on the
explicit ``--daemonless`` *flag*, not the env hatch — the env var routes
mutating verbs to the in-process fallback (which is why every other
integration test still mutates), whereas the flag is a hard rejection.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli import exit_codes
from eawf.cli.app import app
from eawf.cli.errors import ErrorEnvelope
from eawf.kernel.config import layered
from eawf.render.envelope import CANONICAL_SKILL_NAMES
from eawf.runtimes.cache_control import inject_cache_control
from eawf.runtimes.dispatch import AdapterManifestMismatchError, resolve_adapter
from eawf.runtimes.plugin_manifest import SkillManifest
from eawf.workflow.skills import (
    _bootstrap as _skills_bootstrap,  # noqa: F401 — registers all skills
)
from eawf.workflow.skills import registry

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


# --- assertion 5b: user-facing catalog == runtime registry (W26) -----------


def test_user_facing_catalog_matches_runtime_registry() -> None:
    """Catalog/registry parity: ``CANONICAL_SKILL_NAMES`` == ``list_registered``.

    The six C04b skills (W11) were registered in the runtime registry but
    absent from the user-facing catalog (``eawf skill list`` read
    ``CANONICAL_SKILL_NAMES``, frozen at 11). W26 extended the catalog to
    17 so both surfaces agree. The names — not just the counts — must
    match so a skill registered without a catalog row (or vice versa) is
    caught here.
    """
    catalog = set(CANONICAL_SKILL_NAMES)
    registered = set(registry.list_registered())
    assert len(catalog) == 17
    assert len(registered) == 17
    assert catalog == registered, (
        "catalog/registry drift: "
        f"catalog-only={sorted(catalog - registered)} "
        f"registry-only={sorted(registered - catalog)}"
    )


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


# --- assertion 9: daemonless rejection breadth (W25) -----------------------


def _seed_minimal_state(repo: Path) -> Path:
    """Write a minimal valid state.json under ``repo/.ea`` and return its path."""
    import orjson

    state_path = repo / ".ea" / "state.json"
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": "2026-05-20T00:00:00+00:00",
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {
            "P26": {
                "id": "P26",
                "scope_id": "ABC",
                "subproject_id": None,
                "title": "P26",
                "status": "active",
                "iter_ids": ["P26-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-20T00:00:00+00:00",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P26-I01": {
                "id": "P26-I01",
                "phase_id": "P26",
                "title": "I01",
                "status": "active",
                "wave_ids": [],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-20T00:00:00+00:00",
                "closed_at": None,
            }
        },
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    return state_path


def test_mutating_verb_rejects_daemonless_flag(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A representative mutating verb refuses --daemonless: exit 1, kind=InvalidInput.

    Confirms the §5.5 rejection now reaches every state_transaction-backed
    mutating verb (not only ``state rpc``). ``roadmap revise`` stands in for
    the ~11 modules sharing the chokepoint.
    """
    import orjson

    state_path = _seed_minimal_state(repo_root)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setattr(
        "eawf.daemon.spawn.auto_spawn_daemon",
        lambda _r: pytest.fail("mutating verb must reject before any spawn"),
    )
    result = runner.invoke(
        app, ["--daemonless", "--json", "roadmap", "revise", "P26", "--retitle", "X"]
    )
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    payload = orjson.loads(result.stdout)
    assert payload["data"]["kind"] == "InvalidInput"


def test_read_verb_still_honours_daemonless_flag(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read verb keeps working under --daemonless (no rejection regression)."""
    state_path = _seed_minimal_state(repo_root)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setattr(
        "eawf.daemon.spawn.auto_spawn_daemon",
        lambda _r: pytest.fail("read-only verb must not spawn"),
    )
    result = runner.invoke(app, ["--daemonless", "--json", "roadmap", "show"])
    assert result.exit_code == exit_codes.OK, result.output
