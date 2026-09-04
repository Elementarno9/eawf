"""``eawf release observe`` and the ``release.observe_target`` RPC.

Under test: the CLI verb assembling the read-back params and rendering
the observation the daemon answers with; the handler resolving the
adapter from the target's declared ``observe_adapter``; a matched
read-back settling the leg and, once every required target is observed,
baking the release; a missing or contradictory one moving
``VERIFYING -> RECOVERING``; and the two refusals that keep an
observation honest -- a manifest the release never approved, and a
read-back that settled nothing.

The handler is driven directly through its module-level coroutine, so
the tests need no live transport; the CLI is driven through a
``DaemonClient`` stand-in that dispatches into the same coroutine, so
the argv-to-params-to-render path is exercised end to end. The default
signal producers do not exist yet, so the publish that sets these tests
up patches the probe registry -- which is also the honest statement that
observation is gated on producers landing in later waves.

Filed beside :mod:`tests.integration.runtime.daemon.methods.test_publish_rpc`,
whose ``release.reconcile`` coverage this narrows: EAWF025 mirrors a
test onto the source package it exercises, and the subject here is
``eawf.runtime.daemon.methods.release``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.publication import PublicationOperation, require_attempt
from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseStatus,
    ReleaseTargetStatus,
)
from eawf.kernel.spec.release_config import ObservationAdapter, load_release_config
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import observe, publish, reconcile
from eawf.surfaces.cli.app import app
from eawf.workflow.release.ledger import record_operation, request_fingerprint
from eawf.workflow.release.observation import FrozenManifest
from eawf.workflow.release.publication import begin_verification
from eawf.workflow.release.target_machine import advance_target_attempt
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[4] / "fixtures" / "release" / "observations"
SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
PROOF_DIGEST = f"sha256:{'1' * 64}"
EFFECT = "receipt://target/effect"

#: The recorded-response stem of each configured leg's adapter.
ADAPTER_STEMS = {
    "pypi": "package_index",
    "npm": "npm_registry",
    "github": "source_host_release",
}


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Drive one coroutine test body to completion."""
    asyncio.run(body())


def manifest_payload() -> dict[str, Any]:
    """Return the committed ``0.7.0.dev1`` frozen manifest as JSON."""
    return json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def manifest_digest() -> str:
    """Return the digest the frozen manifest recomputes to."""
    return FrozenManifest.model_validate(manifest_payload()).digest


def response_payload(target_id: str, case: str) -> dict[str, Any]:
    """Return the recorded registry answer for *target_id* in *case*."""
    path = FIXTURES / f"{ADAPTER_STEMS[target_id]}-{case}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def dev1_config() -> Any:
    """Return the authored dev1 checkpoint configuration."""
    return load_release_config(DEV1_RELEASE_CONFIG_YAML, train=V07_TRAIN)


@pytest.fixture
def green_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every readiness signal pass so the chokepoint recomputes green."""

    def passing(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, remediation="")

    monkeypatch.setattr(
        "eawf.workflow.verify.release_readiness.DEFAULT_RELEASE_PROBES",
        dict.fromkeys(ReleaseSignalName, passing),
    )


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """Return a method context bound to a tmp state root."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")
    return MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=state_path,
    )


def approved_payload(**overrides: Any) -> dict[str, Any]:
    """Return a serialized APPROVED ``0.7.0.dev1`` record."""
    record = Release(
        uid=UUID(int=28),
        key="REL-0.7.0.dev1",
        version="0.7.0.dev1",
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=ReleaseStatus.APPROVED,
        approval_ref="receipt://approval/dev1",
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/manifest",
        manifest_digest=manifest_digest(),
        revision=4,
    )
    return {**record.model_dump(mode="json"), **overrides}


async def verifying(ctx: MethodContext) -> dict[str, Any]:
    """Publish, drive every leg to reported_success, and verify."""
    published = await publish(
        ctx,
        {
            "release": approved_payload(),
            "expected_revision": 4,
            "idempotency_key": "publish-0.7.0.dev1-28",
            "approved_manifest_digest": manifest_digest(),
            "proof_digest": PROOF_DIGEST,
        },
    )
    config = dev1_config()
    operation = PublicationOperation.model_validate(published["operation"])
    for target in config.targets:
        row = require_attempt(operation, target.target_id)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
        operation = advance_target_attempt(
            operation,
            target=target,
            to=ReleaseTargetStatus.REPORTED_SUCCESS,
            now=row.deadline_at,
            effect_receipt_ref=EFFECT,
        )
    record_operation(
        Path(str(ctx.state_path)),
        operation,
        idempotency_key="seed-adapter-results-28",
        fingerprint=request_fingerprint("test.seed", {"status": "reported_success"}),
        recorded_at=datetime.now(UTC) + timedelta(seconds=1),
        summary="seed adapter results",
    )
    verified = begin_verification(Release.model_validate(published["release"]), config, operation)
    return {"release": verified.model_dump(mode="json")}


def observe_params(state: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return well-formed ``release.observe_target`` params."""
    params: dict[str, Any] = {
        "release": state["release"],
        "expected_revision": state["release"]["revision"],
        "idempotency_key": "observe-0.7.0.dev1-01",
        "target_id": "pypi",
        "manifest": manifest_payload(),
        "response": response_payload("pypi", "match"),
    }
    params.update(overrides)
    return params


# --- the handler ----------------------------------------------------------


@pytest.mark.parametrize("target_id", sorted(ADAPTER_STEMS))
def test_observe_uses_the_adapter_the_target_declares(
    ctx: MethodContext, green_probes: None, target_id: str
) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        result = await observe(
            ctx,
            observe_params(
                state,
                target_id=target_id,
                response=response_payload(target_id, "match"),
                idempotency_key=f"observe-{target_id}-01",
            ),
        )
        declared = {target.target_id: target.observe_adapter for target in dev1_config().targets}
        assert result["observation"]["adapter"] == declared[target_id].value
        assert result["observation"]["result"] == "match"
        operation = PublicationOperation.model_validate(result["operation"])
        row = require_attempt(operation, target_id)
        assert row.status is ReleaseTargetStatus.OBSERVED_SUCCESS
        assert row.observation_receipt_ref == result["observation"]["evidence_ref"]

    _run(body)


def test_the_declared_adapters_are_the_three_implemented_ones() -> None:
    declared = {target.observe_adapter for target in dev1_config().targets}
    assert declared == set(ObservationAdapter)


def test_observing_every_required_target_bakes_the_release(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        for target_id in ("pypi", "npm", "github"):
            result = await observe(
                ctx,
                observe_params(
                    state,
                    target_id=target_id,
                    response=response_payload(target_id, "match"),
                    idempotency_key=f"observe-bake-{target_id}",
                ),
            )
            state = {"release": result["release"]}
        assert Release.model_validate(state["release"]).status is ReleaseStatus.BAKED

    _run(body)


@pytest.mark.parametrize("case", ("missing", "mismatch", "default-channel"))
def test_a_contradicted_read_back_moves_verifying_to_recovering(
    ctx: MethodContext, green_probes: None, case: str
) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        target_id = "pypi" if case != "default-channel" else "npm"
        result = await observe(
            ctx,
            observe_params(
                state,
                target_id=target_id,
                response=response_payload(target_id, case),
                idempotency_key=f"observe-{case}-01",
            ),
        )
        assert Release.model_validate(result["release"]).status is ReleaseStatus.RECOVERING
        assert result["observation"]["result"] in {"missing", "mismatch"}
        operation = PublicationOperation.model_validate(result["operation"])
        assert require_attempt(operation, target_id).status is (
            ReleaseTargetStatus.OBSERVED_MISMATCH
        )

    _run(body)


def test_an_inconclusive_read_back_is_refused(ctx: MethodContext, green_probes: None) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        with pytest.raises(DaemonValidationError, match="observation_inconclusive"):
            await observe(ctx, observe_params(state, response=response_payload("pypi", "unknown")))

    _run(body)


def test_an_unqueried_registry_is_refused_rather_than_guessed(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        params = observe_params(state)
        params.pop("response")
        with pytest.raises(DaemonValidationError, match="registry_unreachable"):
            await observe(ctx, params)

    _run(body)


def test_a_manifest_the_release_never_approved_is_refused(
    ctx: MethodContext, green_probes: None
) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        reforged = manifest_payload()
        reforged["targets"]["pypi"]["artifacts"][0]["digest"] = f"sha256:{'7' * 64}"
        with pytest.raises(DaemonValidationError, match="is not the digest release"):
            await observe(ctx, observe_params(state, manifest=reforged))

    _run(body)


def test_an_unconfigured_target_is_refused(ctx: MethodContext, green_probes: None) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        with pytest.raises(DaemonValidationError, match="configures no target"):
            await observe(ctx, observe_params(state, target_id="crates"))

    _run(body)


def test_a_stale_revision_is_refused(ctx: MethodContext, green_probes: None) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        with pytest.raises(DaemonValidationError, match="stale_release_revision"):
            await observe(ctx, observe_params(state, expected_revision=99))

    _run(body)


@pytest.mark.parametrize("missing", ("expected_revision", "idempotency_key", "manifest"))
def test_observe_requires_its_keying_fields(
    ctx: MethodContext, green_probes: None, missing: str
) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        params = observe_params(state)
        params.pop(missing)
        with pytest.raises(Exception, match="alidation"):
            await observe(ctx, params)

    _run(body)


def test_reconcile_cannot_write_an_observed_status(ctx: MethodContext, green_probes: None) -> None:
    async def body() -> None:
        state = await verifying(ctx)
        with pytest.raises(DaemonValidationError, match="observer_only_status"):
            await reconcile(
                ctx,
                {
                    "release": state["release"],
                    "expected_revision": state["release"]["revision"],
                    "idempotency_key": "reconcile-observed-01",
                    "target_id": "pypi",
                    "status": ReleaseTargetStatus.OBSERVED_SUCCESS.value,
                },
            )

    _run(body)


# --- the CLI verb ---------------------------------------------------------


class _FakeClient:
    """A ``DaemonClient`` stand-in dispatching into the real handler."""

    def __init__(self, ctx: MethodContext, calls: list[dict[str, Any]]) -> None:
        self._ctx = ctx
        self._calls = calls

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._calls.append({"method": method, "params": params})
        return asyncio.run(observe(self._ctx, dict(params or {})))


def _write(path: Path, payload: dict[str, Any]) -> Path:
    """Write *payload* as JSON to *path* and return it."""
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def cli_files(tmp_path: Path) -> Callable[[dict[str, Any], str, str], list[str]]:
    """Return a builder for the argv of one ``release observe`` run."""

    def build(release: dict[str, Any], target_id: str, case: str) -> list[str]:
        return [
            "release",
            "observe",
            "REL-0.7.0.dev1",
            "--target",
            target_id,
            "--release",
            str(_write(tmp_path / "release.json", release)),
            "--manifest",
            str(_write(tmp_path / "manifest.json", manifest_payload())),
            "--idempotency-key",
            f"observe-cli-{target_id}-{case}",
            "--response",
            str(_write(tmp_path / "response.json", response_payload(target_id, case))),
        ]

    return build


def test_the_cli_emits_the_observation(
    ctx: MethodContext,
    green_probes: None,
    cli_files: Callable[[dict[str, Any], str, str], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = asyncio.run(verifying(ctx))
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *a, **k: _FakeClient(ctx, calls),
    )
    result = CliRunner().invoke(app, cli_files(state["release"], "pypi", "match"))
    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "release.observe_target"
    assert calls[0]["params"]["target_id"] == "pypi"
    assert "match (matched)" in result.output
    assert "observation://package_index/pypi/eawf@0.7.0.dev1" in result.output


def test_the_cli_reports_the_recovering_route(
    ctx: MethodContext,
    green_probes: None,
    cli_files: Callable[[dict[str, Any], str, str], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = asyncio.run(verifying(ctx))
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *a, **k: _FakeClient(ctx, []),
    )
    result = CliRunner().invoke(app, cli_files(state["release"], "npm", "default-channel"))
    assert result.exit_code == 0, result.output
    assert "prerelease_on_default_channel" in result.output
    assert "recovering" in result.output


def test_the_cli_refuses_a_release_file_for_another_key(
    ctx: MethodContext,
    green_probes: None,
    cli_files: Callable[[dict[str, Any], str, str], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = asyncio.run(verifying(ctx))
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *a, **k: _FakeClient(ctx, []),
    )
    argv = cli_files(state["release"], "pypi", "match")
    argv[2] = "REL-0.7.0.dev2"
    result = CliRunner().invoke(app, argv)
    assert result.exit_code != 0
    assert "not 'REL-0.7.0.dev2'" in result.output


def test_the_cli_refuses_a_missing_release_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *a, **k: pytest.fail("the daemon must not be called on a bad input"),
    )
    result = CliRunner().invoke(
        app,
        [
            "release",
            "observe",
            "REL-0.7.0.dev1",
            "--target",
            "pypi",
            "--release",
            str(tmp_path / "absent.json"),
            "--manifest",
            str(_write(tmp_path / "manifest.json", manifest_payload())),
            "--idempotency-key",
            "observe-cli-absent",
        ],
    )
    assert result.exit_code != 0
    assert "cannot read release record" in result.output


def test_the_cli_refuses_an_unparseable_manifest(
    ctx: MethodContext,
    green_probes: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = asyncio.run(verifying(ctx))
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *a, **k: pytest.fail("the daemon must not be called on a bad input"),
    )
    broken = tmp_path / "manifest.json"
    broken.write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "release",
            "observe",
            "REL-0.7.0.dev1",
            "--target",
            "pypi",
            "--release",
            str(_write(tmp_path / "release.json", state["release"])),
            "--manifest",
            str(broken),
            "--idempotency-key",
            "observe-cli-broken",
        ],
    )
    assert result.exit_code != 0
    assert "frozen manifest is not valid JSON" in result.output


def test_the_cli_surfaces_a_daemon_refusal(
    ctx: MethodContext,
    green_probes: None,
    cli_files: Callable[[dict[str, Any], str, str], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eawf.surfaces.cli._daemon_client import DaemonRpcError

    state = asyncio.run(verifying(ctx))

    class _Refusing:
        def __enter__(self) -> _Refusing:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            return None

        def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            raise DaemonRpcError(-32602, "validation_failed: observation_inconclusive")

    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient", lambda *a, **k: _Refusing()
    )
    result = CliRunner().invoke(app, cli_files(state["release"], "pypi", "unknown"))
    assert result.exit_code != 0
    assert "daemon rejected release.observe_target" in result.output
