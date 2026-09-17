"""Fixtures shared by the ``release.*`` handler modules.

Two setups live here.

**The unpatched walk.** :func:`dev1_checkout` is a real git checkout
whose published HEAD carries ``0.7.0.dev1``: a version module, a
changelog, the committed cutover rehearsal records, the three CI
receipts and an ``origin`` remote whose ``main`` holds the commit. A
state root inside it is all the handlers need to sweep that commit for
real, so a record pinned to it publishes without any probe patched, and
:func:`walk_to_verifying` carries it on through the receipt-bearing
reconciles. The publish, observe, walk and CLI store-record modules run
on this.

**The patched probes.** :func:`green_probes` makes every readiness row
pass over the throwaway :func:`ctx` state root, whose recorded source
commit exists nowhere. It is kept on purpose for the modules whose
subject is not the sweep -- the crash boundaries, the ambiguous
outcome, the artifact already on the target, the receipt mapping and
the waiver params -- so a readiness regression reds the modules that
test readiness rather than every module that happens to publish first.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.publication import PublicationOperation, latest_attempt
from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseStatus,
    ReleaseTargetStatus,
)
from eawf.kernel.spec.release_config import ReleaseConfig, load_release_config
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.release import (
    approve,
    compute_readiness_method,
    publish,
    reconcile,
)
from eawf.workflow.evidence.migration_rehearsal import REHEARSAL_EVIDENCE_DIR, evidence_dir
from eawf.workflow.release.ledger import record_operation, request_fingerprint
from eawf.workflow.release.observation import FrozenManifest
from eawf.workflow.release.target_machine import advance_target_attempt
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_probes import CHANGELOG_FILENAME, VERSION_MODULE_PATH
from tests._release_helpers import recorded_response, stage_passing_receipts

#: Recorded registry answers and the frozen manifest they are judged
#: against, shared with the unit-tier adapter fixtures.
FIXTURES = Path(__file__).parents[4] / "fixtures" / "release" / "observations"

#: This repository, which the dev1 checkout copies its cutover records from.
REPO_ROOT = Path(__file__).resolve().parents[5]

#: The recorded-response stem of each configured leg's adapter.
ADAPTER_STEMS = {"pypi": "package_index", "npm": "npm_registry", "github": "source_host_release"}

SOURCE_SHA = "a" * 40
TREE_SHA = "b" * 40
PROOF_DIGEST = f"sha256:{'1' * 64}"
EFFECT = "receipt://target/effect"
RELEASE_KEY = "REL-0.7.0.dev1"

#: Revision the approved record starts at, and therefore the
#: ``expected_revision`` the first publication verb must present.
APPROVED_REVISION = 4

DEV1_VERSION: Final = "0.7.0.dev1"

#: Every leg the dev1 checkpoint configures, in configuration order.
TARGET_IDS: Final = ("pypi", "npm", "github")

#: The run id each walked publish job's receipt names.
RUN_ID: Final = "17482930165"

#: Receipts and the locks the stores take are never committed, exactly
#: as in the real repository.
GITIGNORE_TEXT: Final = "dist/\n.ea/locks/\n.ea/**/*.lock\n"


def run_async(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Drive one coroutine test body to completion."""
    asyncio.run(body())


def dev1_config() -> ReleaseConfig:
    """Return the authored dev1 checkpoint configuration."""
    return load_release_config(DEV1_RELEASE_CONFIG_YAML, train=V07_TRAIN)


def manifest_payload() -> dict[str, Any]:
    """Return the committed ``0.7.0.dev1`` frozen manifest as JSON."""
    decoded: dict[str, Any] = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return decoded


def manifest_digest() -> str:
    """Return the digest the frozen manifest recomputes to."""
    return FrozenManifest.model_validate(manifest_payload()).digest


def response_payload(target_id: str, case: str) -> dict[str, Any]:
    """Return the reader's recorded answer for *target_id* in *case*.

    Args:
        target_id: Publication target whose adapter judges it.
        case: Fixture case stem, e.g. ``match`` or ``mismatch``.

    Returns:
        The recorded answer as the leg's reader returns it, as JSON.
    """
    return recorded_response(target_id, case).model_dump(mode="json")


def approved_payload(**overrides: Any) -> dict[str, Any]:
    """Return a serialized APPROVED ``0.7.0.dev1`` record."""
    record = Release(
        uid=UUID(int=33),
        key=RELEASE_KEY,
        version="0.7.0.dev1",
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=ReleaseStatus.APPROVED,
        approval_ref="receipt://approval/dev1",
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/manifest",
        manifest_digest=manifest_digest(),
        revision=APPROVED_REVISION,
    )
    return {**record.model_dump(mode="json"), **overrides}


def publish_params(**overrides: Any) -> dict[str, Any]:
    """Return well-formed ``release.publish`` params."""
    params: dict[str, Any] = {
        "release": approved_payload(),
        "expected_revision": APPROVED_REVISION,
        "idempotency_key": "publish-0.7.0.dev1-33",
        "approved_manifest_digest": manifest_digest(),
        "proof_digest": PROOF_DIGEST,
    }
    params.update(overrides)
    return params


def moved(payload: dict[str, Any], status: ReleaseStatus) -> dict[str, Any]:
    """Return *payload* at *status*, one revision on.

    Args:
        payload: A serialized release record.
        status: The status the caller drives it to out of band.

    Returns:
        The successor payload, so the next verb's ``expected_revision``
        is the one the record now carries.
    """
    return {**payload, "status": status.value, "revision": payload["revision"] + 1}


def record_snapshot(
    ctx: MethodContext,
    operation: PublicationOperation,
    *,
    key: str,
) -> PublicationOperation:
    """Make *operation* the current snapshot of the release in the ledger.

    Args:
        ctx: Method context bound to the state root.
        operation: The snapshot to append.
        key: Idempotency key, which becomes the envelope id.

    Returns:
        The snapshot as recorded.
    """
    return record_operation(
        Path(str(ctx.state_path)),
        operation,
        idempotency_key=key,
        fingerprint=request_fingerprint("test.seed", {"key": key}),
        recorded_at=datetime.now(UTC),
        summary=f"seed {key}",
    )


class CountingPublisher:
    """A publisher stand-in that counts the external calls made to it.

    No adapter uploads anything in this distribution, so the external
    effect a duplicate publication would repeat is modelled here: one
    call per dispatched attempt row. ``__call__`` returns the running
    count, so a second call for a row cannot answer what the first did.

    Attributes:
        dispatched: The ``(target_id, attempt)`` pairs called, in order.
    """

    def __init__(self) -> None:
        """Start with nothing dispatched."""
        self.dispatched: list[tuple[str, int]] = []

    def __call__(self, *, target_id: str, attempt: int) -> int:
        """Record one external call and return the running total."""
        self.dispatched.append((target_id, attempt))
        return len(self.dispatched)

    def calls_for(self, target_id: str) -> int:
        """Return how many external calls *target_id* has taken."""
        return sum(1 for called, _attempt in self.dispatched if called == target_id)

    def per_target(self) -> dict[str, int]:
        """Return the call count of every target dispatched so far."""
        return {called: self.calls_for(called) for called, _attempt in self.dispatched}


def dispatch_queued(
    operation: PublicationOperation,
    publisher: CountingPublisher,
) -> PublicationOperation:
    """Call *publisher* once per queued leg and move each into flight.

    The ledger is the deduplication: a row already in flight or settled
    is not re-dispatched, so a resume that replays the ledger makes no
    second external call. Nothing here consults a side table of "what
    did we already send" -- that table is the thing that goes stale.

    Args:
        operation: The snapshot replayed from the ledger.
        publisher: The counting stand-in for the external call.

    Returns:
        The successor with every queued leg dispatched.
    """
    for target in dev1_config().targets:
        row = latest_attempt(operation, target.target_id)
        if row is None or row.status is not ReleaseTargetStatus.QUEUED:
            continue
        publisher(target_id=target.target_id, attempt=row.attempt)
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
        )
    return operation


@pytest.fixture
def green_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every readiness signal pass so the chokepoint recomputes green.

    For the modules whose subject is not the sweep; see the module
    docstring. A module about publishing itself uses
    :func:`dev1_checkout` instead.
    """

    def passing(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, remediation="")

    registry = dict.fromkeys(ReleaseSignalName, passing)
    monkeypatch.setattr("eawf.runtime.release.chokepoint.build_tag_probes", lambda _: registry)
    monkeypatch.setattr("eawf.runtime.release.chokepoint.build_receipt_probes", lambda _: registry)


@pytest.fixture
def closed_propagation_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close the propagation window, so an absent version is a miss at once.

    The handler reads the wall clock, so a test cannot wait the window
    out. A leg that reported before the read-back is past a closed
    window by the time the handler reads the clock.
    """
    monkeypatch.setattr("eawf.workflow.release.settlement.PROPAGATION_WINDOW", timedelta(0))


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


# --- the unpatched dev1 walk ------------------------------------------------


def git(repo: Path, *args: str) -> str:
    """Run one git command inside *repo* and return its stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def _write(repo: Path, relative: str, text: str) -> None:
    """Write *text* to *relative* under *repo*, creating its parents."""
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_dev1_checkout(root: Path) -> Path:
    """Return a checkout under *root* whose published HEAD is the dev1 cut."""
    repo = root / "checkout"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch", "main")
    git(repo, "config", "user.name", "EAWF Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgSign", "false")
    git(repo, "config", "core.hooksPath", ".git/hooks")
    _write(repo, ".gitignore", GITIGNORE_TEXT)
    _write(repo, VERSION_MODULE_PATH, f'__version__ = "{DEV1_VERSION}"\n')
    _write(
        repo,
        CHANGELOG_FILENAME,
        f"# Changelog\n\n## [{DEV1_VERSION}]\n\n### Added\n- The {DEV1_VERSION} checkpoint.\n\n"
        f"### Migration\n- No persisted schema is migrated by {DEV1_VERSION}.\n",
    )
    _write(repo, ".ea/state.json", json.dumps({}))
    shutil.copytree(evidence_dir(REPO_ROOT), repo.joinpath(*REHEARSAL_EVIDENCE_DIR))
    bare = root / "origin.git"
    git(root, "init", "--quiet", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--message", "feat: cut the dev1 checkpoint")
    git(repo, "push", "--quiet", "origin", "main")
    stage_passing_receipts(repo, version=DEV1_VERSION, source_sha=git(repo, "rev-parse", "HEAD"))
    return repo


@pytest.fixture(scope="session")
def dev1_checkout_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return the published dev1 checkout every test copies.

    Built once per session: an init, a commit and a push cost far more
    than a directory copy, and the ancestry probe reads only the local
    remote-tracking ref, so a copy proves the same ancestry.
    """
    return _build_dev1_checkout(tmp_path_factory.mktemp("dev1-template"))


@pytest.fixture
def dev1_checkout(tmp_path: Path, dev1_checkout_template: Path) -> Path:
    """Return a private copy of the published dev1 checkout.

    Each test writes its ledger and record collection into its own copy.
    """
    repo = tmp_path / "checkout"
    shutil.copytree(dev1_checkout_template, repo, symlinks=True)
    return repo


def context_for(repo: Path) -> MethodContext:
    """Return a method context whose state root lives inside *repo*."""
    return MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=repo / ".ea" / "state.json",
    )


@pytest.fixture
def walk_ctx(dev1_checkout: Path) -> MethodContext:
    """Return a method context bound to the private dev1 checkout."""
    return context_for(dev1_checkout)


def pinned_payload(
    repo: Path,
    *,
    status: ReleaseStatus = ReleaseStatus.APPROVED,
) -> dict[str, Any]:
    """Return the serialized dev1 record pinned to *repo*'s published HEAD.

    Args:
        repo: The dev1 checkout the record pins.
        status: ``approved`` (the default) or ``candidate``, which
            carries no approval reference.

    Returns:
        The record at :data:`APPROVED_REVISION`, bound to the committed
        frozen manifest.
    """
    record = Release(
        uid=UUID(int=33),
        key=RELEASE_KEY,
        version=DEV1_VERSION,
        channel=ReleaseChannel.DEV,
        authority_epoch=1,
        status=status,
        approval_ref=None if status is ReleaseStatus.CANDIDATE else "receipt://approval/dev1",
        source_sha=git(repo, "rev-parse", "HEAD"),
        source_tree_sha=git(repo, "rev-parse", "HEAD^{tree}"),
        manifest_ref="artifact://release/manifest",
        manifest_digest=manifest_digest(),
        revision=APPROVED_REVISION,
    )
    return record.model_dump(mode="json")


def pinned_publish_params(repo: Path, **overrides: Any) -> dict[str, Any]:
    """Return ``release.publish`` params for the record pinned to *repo*."""
    payload = pinned_payload(repo)
    params: dict[str, Any] = {
        "release": payload,
        "expected_revision": payload["revision"],
        "idempotency_key": "publish-0.7.0.dev1-walk",
        "approved_manifest_digest": manifest_digest(),
        "proof_digest": PROOF_DIGEST,
    }
    params.update(overrides)
    return params


def publication_receipt(target_id: str, **overrides: Any) -> dict[str, Any]:
    """Return the receipt a green publish job for *target_id* uploads."""
    payload: dict[str, Any] = {
        "target_id": target_id,
        "version": DEV1_VERSION,
        "artifact_digests": {f"eawf-{DEV1_VERSION}-{target_id}": f"sha256:{'1' * 64}"},
        "job_conclusion": "success",
        "run_id": RUN_ID,
    }
    payload.update(overrides)
    return payload


def receipt_reconcile_params(
    release: dict[str, Any],
    target_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    """Return receipt-bearing ``release.reconcile`` params for *release*."""
    params: dict[str, Any] = {
        "release": release,
        "expected_revision": release["revision"],
        "idempotency_key": f"reconcile-{target_id}-walk",
        "target_id": target_id,
        "receipt": publication_receipt(target_id),
    }
    params.update(overrides)
    return params


def matched_observe_params(
    release: dict[str, Any],
    target_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    """Return ``release.observe_target`` params reading *target_id* back as a match."""
    params: dict[str, Any] = {
        "release": release,
        "expected_revision": release["revision"],
        "idempotency_key": f"observe-{target_id}-walk",
        "target_id": target_id,
        "manifest": manifest_payload(),
        "response": response_payload(target_id, "match"),
    }
    params.update(overrides)
    return params


async def approve_pinned(ctx: MethodContext, repo: Path) -> dict[str, Any]:
    """Approve the candidate pinned to *repo* through the two daemon verbs.

    Args:
        ctx: Method context bound to *repo*'s state root.
        repo: The dev1 checkout the candidate pins.

    Returns:
        The approved record, which ``release.approve`` also recorded.
    """
    candidate = pinned_payload(repo, status=ReleaseStatus.CANDIDATE)
    swept = await compute_readiness_method(ctx, {"version": DEV1_VERSION, "release": candidate})
    approved = await approve(
        ctx,
        {
            "release": candidate,
            "readiness": swept["readiness"],
            "approval_ref": "receipt://approval/dev1",
        },
    )
    release: dict[str, Any] = approved["release"]
    return release


async def walk_to_verifying(ctx: MethodContext, repo: Path) -> dict[str, Any]:
    """Publish the record pinned to *repo* and reconcile every leg's receipt.

    Args:
        ctx: Method context bound to *repo*'s state root.
        repo: The dev1 checkout the record pins.

    Returns:
        The record the last reconcile answered with, at VERIFYING.
    """
    result = await publish(ctx, pinned_publish_params(repo))
    for target_id in TARGET_IDS:
        result = await reconcile(ctx, receipt_reconcile_params(result["release"], target_id))
    release: dict[str, Any] = result["release"]
    return release
