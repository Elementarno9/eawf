"""``eawf release observe`` and the ``release.observe_target`` RPC.

Under test: the CLI verb assembling the read-back params and rendering
the observation the daemon answers with; the handler resolving the
adapter from the target's declared ``observe_adapter``; a matched
read-back settling the leg and, once every required target is observed,
baking the release; a contradictory one, or one still missing once the
propagation window has closed, moving ``VERIFYING -> RECOVERING``; a
missing one inside the window refused as a retry that writes nothing;
and the two refusals that keep an observation honest -- a manifest the
release never approved, and a read-back that settled nothing.

The handler is driven directly through its module-level coroutine, so
the tests need no live transport; the CLI is driven through a
``DaemonClient`` stand-in that dispatches into the same coroutine, so
the argv-to-params-to-render path is exercised end to end. No probe is
patched and no ledger row is written by hand: every test reaches
VERIFYING through the shared conftest's handler walk -- a publish over
the unpatched dev1 checkout and one receipt-bearing reconcile per leg.

Filed beside :mod:`tests.integration.runtime.daemon.methods.test_publish_rpc`,
whose ``release.reconcile`` coverage this narrows: EAWF025 mirrors a
test onto the source package it exercises, and the subject here is
``eawf.runtime.daemon.methods.release``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.publication import PublicationOperation, require_attempt
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.kernel.spec.release_config import ObservationAdapter
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import observe, reconcile
from eawf.surfaces.cli.app import app
from eawf.workflow.release.adapters import OBSERVATION_ADAPTERS
from eawf.workflow.release.registry_readers import HttpReply, PackageIndexReader
from tests.integration.runtime.daemon.methods.conftest import (
    ADAPTER_STEMS,
    dev1_config,
    manifest_payload,
    response_payload,
    walk_to_verifying,
)

pytestmark = pytest.mark.integration


def _run(body: Callable[[], Coroutine[Any, Any, object]]) -> None:
    """Drive one coroutine test body to completion."""
    asyncio.run(body())


async def verifying(ctx: MethodContext, repo: Path) -> dict[str, Any]:
    """Publish, reconcile every leg's receipt, and return the VERIFYING record."""
    return {"release": await walk_to_verifying(ctx, repo)}


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
    walk_ctx: MethodContext, dev1_checkout: Path, target_id: str
) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        result = await observe(
            walk_ctx,
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


def test_the_dev1_declared_adapters_are_all_implemented() -> None:
    """dev1 names three adapters, and each one has an implementation behind it.

    The enum carries more members than dev1 declares: ``git_ref`` joins from the
    native canary rung onward, so a checkpoint that predates it legitimately
    names a strict subset. The invariant worth holding here is that nothing dev1
    declares is unimplemented, not that dev1 exhausts the enum -- the adapter
    table's totality against the enum is asserted where that table is defined.
    """
    declared = {target.observe_adapter for target in dev1_config().targets}

    assert declared == {
        ObservationAdapter.PACKAGE_INDEX,
        ObservationAdapter.NPM_REGISTRY,
        ObservationAdapter.SOURCE_HOST_RELEASE,
    }
    assert declared <= set(OBSERVATION_ADAPTERS)


def test_observing_every_required_target_bakes_the_release(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        for target_id in ("pypi", "npm", "github"):
            result = await observe(
                walk_ctx,
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
    walk_ctx: MethodContext, dev1_checkout: Path, closed_propagation_window: None, case: str
) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        target_id = "pypi" if case != "default-channel" else "npm"
        result = await observe(
            walk_ctx,
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


def test_an_inconclusive_read_back_is_refused(walk_ctx: MethodContext, dev1_checkout: Path) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        with pytest.raises(DaemonValidationError, match="observation_inconclusive"):
            await observe(
                walk_ctx, observe_params(state, response=response_payload("pypi", "unknown"))
            )

    _run(body)


def test_a_missing_read_back_inside_the_propagation_window_writes_nothing(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        with pytest.raises(DaemonValidationError, match="may still be propagating"):
            await observe(
                walk_ctx,
                observe_params(
                    state,
                    response=response_payload("pypi", "missing"),
                    idempotency_key="observe-propagating-01",
                ),
            )
        result = await observe(
            walk_ctx, observe_params(state, idempotency_key="observe-propagating-02")
        )
        operation = PublicationOperation.model_validate(result["operation"])
        assert require_attempt(operation, "pypi").status is ReleaseTargetStatus.OBSERVED_SUCCESS
        assert Release.model_validate(result["release"]).status is ReleaseStatus.VERIFYING

    _run(body)


def test_an_unreachable_registry_is_refused_rather_than_guessed(
    walk_ctx: MethodContext, dev1_checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def unanswered(url: str, *, headers: Mapping[str, str]) -> HttpReply:
        asked.append(url)
        return HttpReply(status=0)

    monkeypatch.setattr(
        "eawf.workflow.release.adapters.DEFAULT_REGISTRY_READERS",
        {ObservationAdapter.PACKAGE_INDEX: PackageIndexReader(opener=unanswered)},
    )

    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        params = observe_params(state)
        params.pop("response")
        with pytest.raises(DaemonValidationError, match="registry_unreachable"):
            await observe(walk_ctx, params)

    _run(body)
    assert asked == ["https://pypi.org/pypi/eawf/0.7.0.dev1/json"]


def test_a_manifest_the_release_never_approved_is_refused(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        reforged = manifest_payload()
        reforged["targets"]["pypi"]["artifacts"][0]["digest"] = f"sha256:{'7' * 64}"
        with pytest.raises(DaemonValidationError, match="is not the digest release"):
            await observe(walk_ctx, observe_params(state, manifest=reforged))

    _run(body)


def test_an_unconfigured_target_is_refused(walk_ctx: MethodContext, dev1_checkout: Path) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        with pytest.raises(DaemonValidationError, match="configures no target"):
            await observe(walk_ctx, observe_params(state, target_id="crates"))

    _run(body)


def test_a_stale_revision_is_refused(walk_ctx: MethodContext, dev1_checkout: Path) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        with pytest.raises(DaemonValidationError, match="stale_release_revision"):
            await observe(walk_ctx, observe_params(state, expected_revision=99))

    _run(body)


@pytest.mark.parametrize("missing", ("expected_revision", "idempotency_key", "manifest"))
def test_observe_requires_its_keying_fields(
    walk_ctx: MethodContext, dev1_checkout: Path, missing: str
) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        params = observe_params(state)
        params.pop(missing)
        with pytest.raises(Exception, match="alidation"):
            await observe(walk_ctx, params)

    _run(body)


def test_reconcile_cannot_write_an_observed_status(
    walk_ctx: MethodContext, dev1_checkout: Path
) -> None:
    async def body() -> None:
        state = await verifying(walk_ctx, dev1_checkout)
        with pytest.raises(DaemonValidationError, match="observer_only_status"):
            await reconcile(
                walk_ctx,
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

        async def answer() -> dict[str, Any]:
            return await observe(self._ctx, dict(params or {}))

        return asyncio.run(answer())


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
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    cli_files: Callable[[dict[str, Any], str, str], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = asyncio.run(verifying(walk_ctx, dev1_checkout))
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *a, **k: _FakeClient(walk_ctx, calls),
    )
    result = CliRunner().invoke(app, cli_files(state["release"], "pypi", "match"))
    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "release.observe_target"
    assert calls[0]["params"]["target_id"] == "pypi"
    assert "match (matched)" in result.output
    assert "observation://package_index/pypi/eawf@0.7.0.dev1" in result.output


def test_the_cli_reports_the_recovering_route(
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    cli_files: Callable[[dict[str, Any], str, str], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = asyncio.run(verifying(walk_ctx, dev1_checkout))
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *a, **k: _FakeClient(walk_ctx, []),
    )
    result = CliRunner().invoke(app, cli_files(state["release"], "npm", "default-channel"))
    assert result.exit_code == 0, result.output
    assert "prerelease_on_default_channel" in result.output
    assert "recovering" in result.output


def test_the_cli_refuses_a_release_file_for_another_key(
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    cli_files: Callable[[dict[str, Any], str, str], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = asyncio.run(verifying(walk_ctx, dev1_checkout))
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *a, **k: _FakeClient(walk_ctx, []),
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
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = asyncio.run(verifying(walk_ctx, dev1_checkout))
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
    walk_ctx: MethodContext,
    dev1_checkout: Path,
    cli_files: Callable[[dict[str, Any], str, str], list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eawf.surfaces.cli._daemon_client import DaemonRpcError

    state = asyncio.run(verifying(walk_ctx, dev1_checkout))

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
