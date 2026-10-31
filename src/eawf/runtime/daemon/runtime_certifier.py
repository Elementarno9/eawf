"""Certify the runtime versions installed on this machine through the conformance probe.

A Run's controls are admitted only on a certified runtime version, and the
harnesses ship new versions faster than any release can certify them. So the
daemon certifies an installed version itself, with the conformance runner's
own probe stage: the installed binary is asked for its version and its
``--help`` surface, the advertised flags are confronted with the capability
matrix, and the stage record lands in the conformance journal under the
version's tuple digest. A passed probe records a machine certification; a
failed one records a quarantine naming what the probe did not find. A probe
that never read the binary's ``--help``, because the call timed out or could
not be spawned, found nothing missing and records nothing, so it is retried.

The probe runs no model. It executes the binary's ``--version`` and ``--help``
(and ``codex features list``), each bounded by the probe's own subprocess
timeout, so a certification costs no tokens.

The daemon starts a probe on its own when a Run starts, or a host session is
adopted as one, on a version that holds no current certification, or whose
quarantine is older than :data:`REPROBE_BACKOFF`, since a harness can be fixed
in place. One probe runs at a time per tree, in the background, and a version
is queued again only after the back-off or after a probe that recorded
nothing; while it runs, a control on that version other than a stop is refused
as in progress. ``runtime.auto_certify: false`` turns the background probe
off; ``eawf runtime certify`` still runs one on request.

The background probe stays token-free and dispatches nothing. An operator who
names a canary Task to ``eawf runtime certify`` asks for the whole walk
instead: after a passed probe, :func:`walk_certification` dispatches the
containment canary as a real Run under that Task, compiled from the same
request the tuple serves, and walks the certify stage. The containment probe
set grades what eawf itself observed: once the canary's worker is launched,
the daemon makes every attempt through the wrapper, jail and admitted
environment the launcher recorded for that Run, and the worker's own answer
can only worsen the grade. A canary that misses an axis quarantines the
version; one that passes records a certification carrying what only a
dispatched Run can observe, such as whether the worker filed its report
through its semantic tools and which model it ran. A worker that answers
without filing is one model's miss, so the walk dispatches another canary
before it records a certification that grants later Runs no tool.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import platform
import re
import secrets
import shutil
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from eawf.kernel.config.layered import resolve_auto_certify
from eawf.kernel.config.providers import ProviderConfigError
from eawf.kernel.identity import EntityKind, QualifiedUrn, format_entity_key
from eawf.kernel.runtime.certification import (
    CapabilityCertification,
    CertifiedVersionRange,
    QuarantineTrigger,
)
from eawf.kernel.runtime.compiled import (
    CapabilityObservation,
    CompiledRunSpec,
    RuntimeBinding,
    canonical_digest,
)
from eawf.kernel.runtime.handshake import WorkerHello
from eawf.kernel.runtime.provider import AuthKind, ControlKind
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.kernel.store.ledger import read_ledger_records
from eawf.observability.telemetry.models import RuntimeName
from eawf.runtime.daemon.methods.conformance import StoreStageJournal, runner_for
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.runtime.mcp.grant import SEMANTIC_TOOLS_CAPABILITY
from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    NativeRunLauncher,
)
from eawf.runtime.runtimes.capabilities import RUNTIME_IDS, ProbeResult, detect_drift
from eawf.runtime.runtimes.conformance import (
    CanaryRequest,
    CanaryStageResult,
    CertificationRequest,
    ProbeRequest,
    ProtocolFacts,
    RuntimeTuple,
    canary_outcomes,
    render_canary_prompt,
)
from eawf.runtime.runtimes.containment import ContainmentCallOutcome
from eawf.runtime.runtimes.containment_observation import (
    ContainmentTargets,
    observe_containment,
)
from eawf.runtime.runtimes.host_transcript import scrubbed_words
from eawf.runtime.runtimes.probes.sdk_baseline import probe_runtime
from eawf.runtime.runtimes.quarantine import is_quarantined
from eawf.runtime.sandbox.jail import _CRED_DENY_DIRS
from eawf.workflow.evidence.machine_certification import (
    CERTIFICATION_LIFETIME,
    append_machine_certification,
    read_machine_certifications,
)
from eawf.workflow.evidence.run_certification import (
    ControlGate,
    ControlGateCode,
    decide_run_control,
    runtime_certifications,
)

if TYPE_CHECKING:
    from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
    from eawf.runtime.daemon.native_dispatch import DispatchParams, Publish

logger = logging.getLogger(__name__)

#: The conformance suite a machine probe runs: the one the committed exports ran.
CONFORMANCE_SUITE_VERSION: Final = "1.0.0"

#: A version token as a runtime prints it: ``2.1.288 (Claude Code)`` and
#: ``codex-cli 0.159.2`` both carry exactly one.
_VERSION_TOKEN: Final = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")

#: The tuple's OS class and architecture, by what :mod:`platform` reports.
_OS_CLASSES: Final = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}
_ARCHITECTURES: Final = {
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "x86_64": "x86_64",
    "AMD64": "x86_64",
}

#: The gate codes that leave a version for the probe to certify.
_PROBED_ON: Final = frozenset({ControlGateCode.UNCERTIFIED, ControlGateCode.EXPIRED})

#: How long a quarantined or already-queued version waits before it is probed again.
REPROBE_BACKOFF: Final = timedelta(hours=24)

#: The token ceiling a canary Run is sealed under: the probe set is eight attempts
#: and one report, and the in-flight governor admits no uncapped Run.
CANARY_TOKEN_BUDGET: Final = 200_000

#: How many canaries one walk dispatches before it records a certification that
#: carries no semantic tools. A worker that answers without filing its report is
#: one model's miss, not proof the runtime cannot reach the tools, and a row
#: without them leaves every later Run of the version granted no tool at all.
CANARY_ATTEMPTS: Final = 3

#: The provider a canary of each runtime is dispatched to, which is also the
#: runtime its host facts are filed under.
_CANARY_PROVIDERS: Final[Mapping[str, RuntimeName]] = {"claude-code": "claude", "codex": "codex"}


class RuntimeNotProbedError(ValueError):
    """The installed runtime could not be probed, so nothing was recorded.

    The message leads with the refusal code: ``runtime_not_installed``,
    ``runtime_probe_incomplete``, ``runtime_platform_unknown``,
    ``runtime_capabilities_unruled``, or ``runtime_range_excludes_installed``,
    the one refusal raised after a probe, whose row stays recorded.
    """


def installed_version(printed: str | None) -> str | None:
    """Return the version token a runtime's ``--version`` line names.

    Args:
        printed: The first line the binary printed, or ``None``.

    Returns:
        The first version token, or ``None`` when the line names none.
    """
    if printed is None:
        return None
    match = _VERSION_TOKEN.search(printed)
    return None if match is None else match.group(0)


def _runtime_tuple(runtime_id: str, version: str, flags: tuple[str, ...]) -> RuntimeTuple:
    """Return the tuple a probe of *runtime_id* at *version* on this machine covers.

    The binary authenticates through whatever session it holds on this
    machine, and no managed profile wraps it, so the tuple says exactly that
    rather than borrowing a release's managed values.

    Raises:
        RuntimeNotProbedError: This machine's OS or architecture is not one a
            tuple can name.
    """
    os_class = _OS_CLASSES.get(platform.system())
    architecture = _ARCHITECTURES.get(platform.machine())
    if os_class is None or architecture is None:
        raise RuntimeNotProbedError(
            f"runtime_platform_unknown: {platform.system()} on {platform.machine()} is not a "
            f"platform a certification can name"
        )
    observation = {
        "runtime_id": runtime_id,
        "distribution_version": version,
        "observed_flags": list(flags),
        "os_class": os_class,
        "architecture": architecture,
    }
    return RuntimeTuple.model_validate(
        {
            "manifest_ref": f"driver://{runtime_id}/{version}",
            "manifest_digest": canonical_digest(observation),
            "distribution_version": version,
            "sdk_or_server_version": version,
            "auth_kind": AuthKind.LOCAL_SESSION,
            "model_family": runtime_id,
            "os_class": os_class,
            "architecture": architecture,
            "managed_profile_digest": canonical_digest({}),
            "conformance_suite_version": CONFORMANCE_SUITE_VERSION,
        }
    )


def _probe_installed(
    state_path: Path, runtime_id: str
) -> tuple[MachineCertification, RuntimeTuple]:
    """Walk the probe stage and return its row, unrecorded, beside the tuple it covers.

    Each capability the matrix has a probe rule for and declares supported is
    required; one it declares unsupported is recorded unsupported, as the
    committed exports record it.

    Raises:
        RuntimeNotProbedError: The binary is absent or reports no version,
            its ``--help`` timed out or could not be run, the platform cannot
            be named, or no capability of the runtime has a probe rule.
    """
    observed = probe_runtime(runtime_id)
    version = installed_version(observed.version) if observed.installed else None
    if version is None:
        raise RuntimeNotProbedError(
            f"runtime_not_installed: {runtime_id} has no binary on the daemon's PATH that "
            f"reports a version"
        )
    # Without a help body every flag reads as absent, and quarantining on that
    # would blame the harness for a slow or failed spawn.
    if observed.help_excerpt_sha256 is None:
        raise RuntimeNotProbedError(
            f"runtime_probe_incomplete: {runtime_id} {version} printed no --help "
            f"({observed.error}), so no capability could be judged"
        )
    flags = observed.advertised_sdk_flags
    rows = detect_drift(
        runtime_id, ProbeResult(runtime_id=runtime_id, installed=True, observed_flags=flags)
    )
    ruled = tuple(row for row in rows if row.probe_rule and row.declared != "unknown")
    required = tuple(row.capability for row in ruled if row.declared == "supported")
    if not required:
        raise RuntimeNotProbedError(
            f"runtime_capabilities_unruled: no capability the matrix declares {runtime_id} "
            f"supports has a probe rule"
        )
    evidence_ref = f"artifact://runtime-certification/{runtime_id}/{version}"
    runtime_tuple = _runtime_tuple(runtime_id, version, flags)
    result = runner_for(state_path).run_probe(
        ProbeRequest(
            runtime_tuple=runtime_tuple,
            runtime_id=runtime_id,
            installed=True,
            observed_flags=flags,
            required_capabilities=required,
            evidence_ref=evidence_ref,
        )
    )
    record = result.record
    named = f"{runtime_id} {version}"
    common = {
        "certification_urn": (
            f"certification://{runtime_id}/{version}/{record.completed_at:%Y-%m-%d}"
        ),
        "runtime_id": runtime_id,
        "harness_version": version,
        "tuple_digest": runtime_tuple.tuple_digest,
        "probe": record,
        "verified_at": record.completed_at,
    }
    if result.passed:
        expires_at = record.completed_at + CERTIFICATION_LIFETIME
        row = MachineCertification.model_validate(
            {
                **common,
                "outcome": "certified",
                "capabilities": tuple(
                    CapabilityCertification(
                        capability_id=item.capability,
                        level=item.declared == "supported",
                        status="verified" if item.declared == "supported" else "unsupported",
                        basis="native",
                        evidence_ref=evidence_ref,
                        verified_at=record.completed_at,
                        expires_at=expires_at,
                    )
                    for item in ruled
                ),
                "expires_at": expires_at,
                "reason": (
                    f"{named} passed the conformance probe; certified until {expires_at:%Y-%m-%d}"
                ),
            }
        )
    else:
        missing = {*result.drifted, *result.uncovered}
        row = MachineCertification.model_validate(
            {
                **common,
                "outcome": "quarantined",
                "reason_code": record.reason_code,
                "findings": tuple(
                    f"{item.capability}: {item.detail}"
                    for item in rows
                    if item.capability in missing
                ),
                "reason": (
                    f"{named} failed the conformance probe "
                    f"({record.reason_code}): {', '.join(sorted(missing))}"
                ),
            }
        )
    return row, runtime_tuple


def gate_run_control(
    tree_root: Path, runtime: RunRuntimeTuple | None, control: ControlKind, *, now: datetime
) -> ControlGate:
    """Decide *control* on a Run of the tree at *tree_root* against every record of it.

    Args:
        tree_root: The tree's ``.ea`` directory.
        runtime: The Run's runtime tuple, or ``None`` when it records none.
        control: The control asked for.
        now: The instant expiry is judged at.

    Returns:
        The decision over the committed exports, this machine's probe rows,
        the conformance journal's quarantines and the probes running now.

    Raises:
        ValueError: A committed export or the conformance journal is unreadable.
        pydantic.ValidationError: A machine probe row does not validate.
    """
    state_path = tree_root / "state.json"
    journal = StoreStageJournal(state_path)
    return decide_run_control(
        runtime,
        control,
        certifications=runtime_certifications(tree_root.parent),
        machine=read_machine_certifications(state_path),
        quarantined=lambda digest: is_quarantined(journal.records(tuple_digest=digest)),
        certifying=certifier_for(tree_root).certifying,
        now=now,
    )


class RuntimeCertifier:
    """One tree's prober: one probe at a time, each version at most once per back-off.

    Attributes:
        tree_root: The tree's ``.ea`` directory.
    """

    def __init__(self, tree_root: Path) -> None:
        """Bind the certifier to the tree at *tree_root*.

        Args:
            tree_root: The tree's ``.ea`` directory.
        """
        self.tree_root = tree_root
        self._state_path = tree_root / "state.json"
        self._lock = threading.Lock()
        self._serial = threading.Lock()
        self._probing: set[tuple[str, str]] = set()
        self._attempted: dict[tuple[str, str], datetime] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eawf-certify")

    def certifying(self, runtime_id: str, version: str) -> bool:
        """Report whether a background probe of *runtime_id* at *version* is pending.

        Args:
            runtime_id: The runtime.
            version: The version a Run reported.

        Returns:
            ``True`` from the moment the probe is queued until it finishes.
        """
        with self._lock:
            return (runtime_id, version) in self._probing

    def certify(self, runtime_id: str) -> MachineCertification:
        """Probe the installed *runtime_id* now, after any probe already running.

        Args:
            runtime_id: The runtime to probe.

        Returns:
            The recorded row.

        Raises:
            RuntimeNotProbedError: The runtime could not be probed.
        """
        return self.probe(runtime_id)[0]

    def probe(self, runtime_id: str) -> tuple[MachineCertification, RuntimeTuple]:
        """Probe and record as :meth:`certify` does, also answering the tuple probed.

        Args:
            runtime_id: The runtime to probe.

        Returns:
            The recorded row and the runtime tuple its stage is journaled under.

        Raises:
            RuntimeNotProbedError: The runtime could not be probed.
        """
        with self._serial:
            row, runtime_tuple = _probe_installed(self._state_path, runtime_id)
            append_machine_certification(self._state_path, row)
            return row, runtime_tuple

    def ensure(
        self, runtime_id: str, version: str, *, now: datetime
    ) -> Future[MachineCertification | None] | None:
        """Queue one background probe of *runtime_id* for a Run that reported *version*.

        The probe reads the installed binary, which may have moved on from the
        version the Run reported; it then certifies the installed version, and
        the reported one is not queued again by this process until
        :data:`REPROBE_BACKOFF` has passed, unless the probe recorded nothing.

        Args:
            runtime_id: The runtime.
            version: The version a Run reported.
            now: The instant the back-off is measured from.

        Returns:
            The queued probe, or ``None`` when this process queued this version
            within the back-off.
        """
        key = (runtime_id, version)
        with self._lock:
            queued_at = self._attempted.get(key)
            if queued_at is not None and now - queued_at < REPROBE_BACKOFF:
                return None
            self._attempted[key] = now
            self._probing.add(key)
        logger.info(f"ensure queued runtime={runtime_id!r} version={version!r}")
        return self._executor.submit(self._probe, key)

    def _probe(self, key: tuple[str, str]) -> MachineCertification | None:
        """Run one queued probe, and clear it as pending however it ends.

        A probe that recorded nothing leaves the version free to be queued by
        the next Run start; an unexpected failure is logged here because the
        Future it would surface through is never awaited.
        """
        try:
            return self.certify(key[0])
        except RuntimeNotProbedError as error:
            logger.warning(f"_probe skipped runtime={key[0]!r} version={key[1]!r} cause={error}")
            self._forget(key)
            return None
        except Exception:
            logger.exception(f"_probe failed runtime={key[0]!r} version={key[1]!r}")
            self._forget(key)
            return None
        finally:
            with self._lock:
                self._probing.discard(key)

    def _forget(self, key: tuple[str, str]) -> None:
        """Let *key* be queued again, since its probe recorded nothing."""
        with self._lock:
            self._attempted.pop(key, None)


#: Certifiers by resolved tree root: one per tree for the life of the process,
#: so the pending set a gate reads is the one the Run start queued into.
_CERTIFIERS: dict[Path, RuntimeCertifier] = {}
_CERTIFIERS_LOCK: Final = threading.Lock()


def certifier_for(tree_root: Path) -> RuntimeCertifier:
    """Return the process-wide certifier of the tree at *tree_root*.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        The tree's one certifier, created on first use.
    """
    resolved = tree_root.resolve()
    with _CERTIFIERS_LOCK:
        certifier = _CERTIFIERS.get(resolved)
        if certifier is None:
            certifier = RuntimeCertifier(resolved)
            _CERTIFIERS[resolved] = certifier
        return certifier


def start_auto_certification(
    tree_root: Path, runtime: RunRuntimeTuple, *, now: datetime
) -> Future[MachineCertification | None] | None:
    """Queue a background probe when a Run reports a version nothing certifies.

    Args:
        tree_root: The tree's ``.ea`` directory.
        runtime: The runtime the Run reported.
        now: The instant a certification's expiry is judged at.

    Returns:
        The queued probe, or ``None`` when none was queued: the runtime is not
        one the probe knows, the Run reported no version, ``runtime.auto_certify``
        is off, the version is already certified, its quarantine is younger
        than :data:`REPROBE_BACKOFF`, or this process queued it within the
        back-off.
    """
    version = runtime.harness_version
    if runtime.harness not in RUNTIME_IDS or version is None:
        return None
    gate = gate_run_control(tree_root, runtime, ControlKind.CANCEL, now=now)
    if gate.code is ControlGateCode.QUARANTINED:
        probed = [
            row
            for row in read_machine_certifications(tree_root / "state.json")
            if row.runtime_id == runtime.harness and row.harness_version == version
        ]
        newest = max(probed, key=lambda row: row.verified_at, default=None)
        if newest is None or newest.outcome != "quarantined":
            return None
        if now - newest.verified_at < REPROBE_BACKOFF:
            return None
    elif gate.code not in _PROBED_ON:
        return None
    # composed per call, every layer read afresh, so a switch turned off anywhere holds
    # at once; only a Run on a version a probe would certify pays for the read
    if not resolve_auto_certify(tree_root.parent):
        return None
    return certifier_for(tree_root).ensure(runtime.harness, version, now=now)


#: What a planted decoy credential holds: a fixed marker in no credential format,
#: so nothing that finds it mistakes it for a secret.
DECOY_CREDENTIAL_MARKER: Final = "eawf canary decoy: not a credential\n"

#: The prefix of the scratch directory a decoy credential store is planted in.
DECOY_SCRATCH_PREFIX: Final = "canary-credential-"


@contextmanager
def _credential_targets(tree_root: Path) -> Iterator[tuple[Path, ...]]:
    """Yield the credential stores the canary's credential attempt reads.

    The other tools' stores present in the operator's home are the targets;
    their bytes are never read here, and the attempt in the jail reports
    only whether its read was refused. A host holding none, such as a clean
    CI host, gets a decoy store instead, so the attempt still has something
    to be refused: it is planted inside the tree's ``.ea``, which every
    native Run's jail denies by the same rule that denies a store
    (``deny file-read*`` under seatbelt, a mask under bubblewrap) while
    re-opening only the Run's own workspace, and it is removed once the
    attempts are made, however they end.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Yields:
        The present stores, or the one planted decoy store.

    Raises:
        OSError: The decoy could not be planted.
    """
    home = Path.home()
    present = tuple(path for relative in _CRED_DENY_DIRS if (path := home / relative).exists())
    if present:
        yield present
        return
    scratch = tree_root / "local" / f"{DECOY_SCRATCH_PREFIX}{secrets.token_hex(8)}"
    store = scratch / _CRED_DENY_DIRS[0]
    try:
        store.mkdir(parents=True)
        (store / "credentials").write_text(DECOY_CREDENTIAL_MARKER, encoding="utf-8")
        yield (store,)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _containment_targets(
    tree_root: Path, workspace: Path, *, credential_paths: tuple[Path, ...]
) -> ContainmentTargets:
    """Return what the attempts made in the jail of a canary leased *workspace* reach for."""
    return ContainmentTargets(
        canonical_git=tree_root.parent / ".git",
        daemon_storage=tree_root,
        write_ahead_log=runtime_dir() / "wal",
        outside_writable_roots=workspace.parent,
        credential_paths=credential_paths,
        host_process=os.getpid(),
    )


class _ObservedLauncher:
    """A launcher that keeps what it launched and observes the jail it launched in.

    The supervisor awaits the turn and keeps only how it ended, so this
    keeps hold of the launch. Right after the worker is announced, while
    the Run's workspace and the wrapper the launcher wrote for it are
    certainly in place, the daemon makes every containment attempt in that
    same wrapper and jail itself.

    Attributes:
        provider_kind: The provider the wrapped launcher starts.
        launched: What the wrapped launcher answered, once it did.
        observed: What eawf observed per containment attempt it made.
    """

    def __init__(self, real: NativeRunLauncher, *, tree_root: Path) -> None:
        """Wrap *real*, launching under the tree at *tree_root*."""
        self.provider_kind = real.provider_kind
        self.launched: NativeLaunchOutcome | None = None
        self.observed: tuple[ContainmentCallOutcome, ...] = ()
        self._real = real
        self._tree_root = tree_root

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Launch through the wrapped launcher, observe its jail and keep its answer."""
        self.launched = await self._real.launch(request)
        self.observed = await asyncio.to_thread(self._observe, request.workspace)
        return self.launched

    def _observe(self, workspace: Path) -> tuple[ContainmentCallOutcome, ...]:
        """Make every containment attempt in the jail of the Run leased *workspace*."""
        with _credential_targets(self._tree_root) as credential_paths:
            return observe_containment(
                workspace / MCP_ARTIFACT_DIRNAME,
                workspace=workspace,
                targets=_containment_targets(
                    self._tree_root, workspace, credential_paths=credential_paths
                ),
            )


@dataclass(frozen=True)
class _CanaryRun:
    """What one dispatched canary showed, for the canary and certify stages.

    Attributes:
        served: The spec the tuple serves.
        canary: The spec the canary Run was dispatched under.
        observed: What eawf observed per attempt it made in the canary's jail.
        answered: What its worker's answer reported per attempted call.
        hello: The worker's announcement.
        observed_model: The model its runtime reported running, if it did.
        reported: Whether the worker filed its report through a semantic tool.
        semantic_protocol_version: The semantic protocol its driver speaks.
        turn_failure: Why the worker's turn failed, scrubbed as a record may
            hold it; ``None`` when the turn did not fail.
    """

    served: CompiledRunSpec
    canary: CompiledRunSpec
    observed: tuple[ContainmentCallOutcome, ...]
    answered: tuple[ContainmentCallOutcome, ...]
    hello: WorkerHello
    observed_model: str | None
    reported: bool
    semantic_protocol_version: str
    turn_failure: str | None


def _under_test(
    binding: RuntimeBinding, *, evidence_ref: str, probed: MachineCertification
) -> RuntimeBinding:
    """Return *binding* carrying the semantic tools as the capability its canary tests.

    The canary is how the semantic tools are observed, and its worker can only
    reach them if its own compile grants them; the row the walk records
    attests them only once that worker has actually filed through them.
    """
    assert probed.expires_at is not None, "only a certified probe is walked to its canary"
    held = tuple(
        item for item in binding.capabilities if item.capability_id != SEMANTIC_TOOLS_CAPABILITY
    )
    tested = CapabilityObservation(
        capability_id=SEMANTIC_TOOLS_CAPABILITY,
        effective_level=True,
        status="verified",
        basis="native",
        basis_refs=(binding.certification_ref,),
        certification_evidence_ref=evidence_ref,
        verified_at=probed.verified_at,
        expires_at=probed.expires_at,
    )
    return RuntimeBinding.model_validate({**binding.model_dump(), "capabilities": (*held, tested)})


def _canary_requests(
    context: Epoch2RootContext,
    probed: MachineCertification,
    *,
    task: QualifiedUrn,
    actor: str,
    provider: str,
    evidence_ref: str,
    now: datetime,
) -> tuple[DispatchParams, CompiledRunSpec, CompiledRunSpec]:
    """Return the canary's dispatch request, the spec the tuple serves and the canary's.

    Both specs compile from one Task request on one set of bindings, so the
    canary runs under exactly the authority the tuple will serve; only the
    Run and the prompt differ.

    Raises:
        DaemonValidationError: The provider layers do not load, or the base,
            the bindings or the compiler refused the request.
    """
    # Dispatch reaches the Run-start path, which starts this module's probe.
    from eawf.runtime.daemon.methods.delivery import stored_tasks
    from eawf.runtime.daemon.methods.task_dispatch import head_commit, next_run_urn
    from eawf.runtime.daemon.native_dispatch import (
        DEFAULT_TOOL_GRANTS,
        DispatchRefusal,
        compile_for_dispatch,
        refused,
    )
    from eawf.runtime.daemon.provider_facts import installed_provider_facts
    from eawf.workflow.runtime.task_request import compile_task_request

    tree_root = context.identity.tree_root
    with context.session([task]) as session:
        task_row = next(row for row in stored_tasks(session) if row.urn == task)
    try:
        facts = installed_provider_facts(tree_root, now=now)
    except ProviderConfigError as error:
        raise refused(DispatchRefusal.PROVIDER_CONFIG_INVALID, str(error)) from error
    bindings = tuple(
        _under_test(binding, evidence_ref=evidence_ref, probed=probed)
        if binding.manifest.provider_id == provider
        else binding
        for binding in facts.bindings
    )
    canary_urn = next_run_urn(context, task_row)
    # The served Run is never created: it only names the Run the canary stands
    # for, which must be another Run than the canary itself.
    served_urn = dataclasses.replace(
        canary_urn,
        entity_key=format_entity_key(
            EntityKind.RUN, int(canary_urn.entity_key.removeprefix("RUN-")) + 1
        ),
    )
    base = head_commit(tree_root.parent)

    def compiled(run_urn: QualifiedUrn) -> DispatchParams:
        return compile_task_request(
            task_row,
            run_urn,
            provider=provider,
            config=facts.config,
            registry=facts.registry,
            bindings=bindings,
            unbound=facts.unbound,
            budget=CANARY_TOKEN_BUDGET,
            base=base,
            actor=actor,
            now=now,
        )

    request = compiled(canary_urn)
    request = request.model_copy(
        update={
            "prompt": render_canary_prompt(
                report_tool=DEFAULT_TOOL_GRANTS[0],
                report_schema_ref=request.capsule.report_schema_ref,
            )
        }
    )
    served = compile_for_dispatch(compiled(served_urn), now=now)
    return request, served, compile_for_dispatch(request, now=now)


async def _dispatch_canary(
    context: Epoch2RootContext,
    probed: MachineCertification,
    *,
    task: QualifiedUrn,
    actor: str,
    provider: str,
    evidence_ref: str,
    launchers: Mapping[str, NativeRunLauncher],
    publish: Publish | None,
) -> _CanaryRun:
    """Create the canary Run under *task*, dispatch it, and await its turn.

    Raises:
        DaemonValidationError: The request does not compile, the Run is not
            created, or the dispatch refused.
    """
    # Dispatch reaches the Run-start path, which starts this module's probe.
    from eawf.runtime.candidate.seal import accepted_report_of
    from eawf.runtime.daemon.epoch2_create import CreateRequest, run_create
    from eawf.runtime.daemon.methods.task_dispatch import dispatch_cursor, run_allocation
    from eawf.runtime.daemon.native_dispatch import dispatch_run, run_ledger
    from eawf.runtime.daemon.run_supervisor import supervisor_of

    now = datetime.now(UTC)
    # The canary's Run key is guessed like a dispatch's, so it is guessed and
    # created under the same lock.
    async with run_allocation(context):
        request, served, canary = await asyncio.to_thread(
            _canary_requests,
            context,
            probed,
            task=task,
            actor=actor,
            provider=provider,
            evidence_ref=evidence_ref,
            now=now,
        )
        run_key = request.urn.entity_key
        create = CreateRequest(
            urn=request.urn,
            expected_revision=await asyncio.to_thread(dispatch_cursor, context, task),
            idempotency_key=f"certify-canary-{run_key}-create",
            actor=actor,
            spec={
                "key": run_key,
                "scope": request.compile_request.run_scope.model_dump(mode="json"),
            },
        )
        committed = await asyncio.to_thread(run_create, context=context, request=create, now=now)
    if publish is not None and committed.envelope is not None:
        publish(committed.envelope)
    tree_root = context.identity.tree_root
    observed = {
        kind: _ObservedLauncher(launcher, tree_root=tree_root)
        for kind, launcher in launchers.items()
    }
    await dispatch_run(context, request, now=now, launchers=observed, publish=publish)
    supervised = supervisor_of(context).task_of(str(request.urn))
    if supervised is not None:
        await supervised
    observer = observed[canary.provider_options.provider_kind]
    launched = observer.launched
    assert launched is not None, "an announced dispatch launched its worker"
    turn, answer, turn_failure = launched.turn, None, None
    if turn is not None and turn.done() and not turn.cancelled() and turn.exception() is None:
        outcome = turn.result()
        answer = outcome.answer_text
        if outcome.exit_kind == "failed":
            turn_failure = scrubbed_words(outcome.error_text or "") or "the launcher said nothing"
    with context.session([request.urn]) as session:
        records = read_ledger_records(run_ledger(session))
    binding = next(
        item for item in request.bindings if item.driver_ref == canary.driver_manifest_ref
    )
    logger.info(
        f"_dispatch_canary run={run_key} provider={provider} answered={answer is not None} "
        f"observed={len(observer.observed)} turn_failed={turn_failure is not None}"
    )
    return _CanaryRun(
        served=served,
        canary=canary,
        observed=observer.observed,
        answered=canary_outcomes(answer),
        hello=launched.hello,
        observed_model=None if launched.runtime_tuple is None else launched.runtime_tuple.model,
        reported=accepted_report_of(records, str(request.urn)) is not None,
        semantic_protocol_version=binding.manifest.semantic_protocol_versions[0],
        turn_failure=turn_failure,
    )


def _canary_findings(result: CanaryStageResult) -> tuple[str, ...]:
    """Return one line per parity axis or attempt that failed the canary."""
    if result.containment is None:
        return tuple(
            f"{axis.value}: the canary spec differs from the served spec"
            for axis in result.parity_mismatches
        )
    if result.quarantine_trigger is QuarantineTrigger.TERMINAL_OUTCOME_ANOMALY:
        return (f"worker_turn: failed: {result.turn_failure}",)
    graded = (
        ("escaped containment", result.containment.escaped),
        ("returned bytes through its denial", result.containment.observed),
        ("was not attempted", result.containment.skipped),
    )
    return tuple(
        f"{attempt.value}: {verdict}" for verdict, attempts in graded for attempt in attempts
    )


def _conclude_walk(
    state_path: Path,
    probed: MachineCertification,
    runtime_tuple: RuntimeTuple,
    run: _CanaryRun,
    *,
    evidence_ref: str,
    last: bool,
    version_range: CertifiedVersionRange | None,
) -> MachineCertification:
    """Grade the canary, walk the certify stage after a pass, and record the row.

    The certify stage's own outcome is kept on the row rather than deciding
    it: a certify refused because the shipped host facts measure nothing of
    the runtime refuses the driver certification, whose facts those are, not
    the capabilities this machine's canary observed.

    Args:
        state_path: The tree's ``state.json``.
        probed: The probe's certification the canary was walked from.
        runtime_tuple: The tuple the stages are journaled under.
        run: What the dispatched canary showed.
        evidence_ref: The canary's evidence reference.
        last: Whether no further canary follows this one, so a row without
            the semantic tools says how a later walk can earn them.
        version_range: The versions the driver certification stands for,
            kept only when the certify stage wrote one.

    Returns:
        The recorded row: a quarantine naming every failed attempt, or a
        certification carrying what the canary observed and, when the certify
        stage passed, the driver certification it wrote.
    """
    runner = runner_for(state_path)
    result = runner.run_canary(
        CanaryRequest(
            runtime_tuple=runtime_tuple,
            served_spec=run.served,
            canary_spec=run.canary,
            outcomes=run.observed,
            reported=run.answered,
            turn_failure=run.turn_failure,
            evidence_ref=evidence_ref,
        )
    )
    named = f"{probed.runtime_id} {probed.harness_version}"
    common = probed.model_dump(
        include={"certification_urn", "runtime_id", "harness_version", "tuple_digest", "probe"}
    ) | {"observed_model": run.observed_model}
    if not result.passed:
        findings = _canary_findings(result)
        failed = (
            f"its worker's turn failed: {result.turn_failure}"
            if result.quarantine_trigger is QuarantineTrigger.TERMINAL_OUTCOME_ANOMALY
            else ", ".join(line.split(":")[0] for line in findings)
        )
        row = MachineCertification.model_validate(
            {
                **common,
                "outcome": "quarantined",
                "reason_code": result.record.reason_code,
                "findings": findings,
                "stages": (result.record,),
                "verified_at": result.record.completed_at,
                "reason": (
                    f"{named} failed the containment canary ({result.record.reason_code}): {failed}"
                ),
            }
        )
        append_machine_certification(state_path, row)
        return row
    assert probed.expires_at is not None, "only a certified probe is walked to its canary"
    capabilities = probed.capabilities
    if run.reported:
        capabilities += (
            CapabilityCertification(
                capability_id=SEMANTIC_TOOLS_CAPABILITY,
                level=True,
                status="verified",
                basis="native",
                evidence_ref=evidence_ref,
                verified_at=result.record.completed_at,
                expires_at=probed.expires_at,
            ),
        )
    certified = runner.certify(
        CertificationRequest(
            runtime_tuple=runtime_tuple,
            protocol=ProtocolFacts(
                worker_protocol_version=run.hello.worker_protocol_version,
                semantic_protocol_version=run.semantic_protocol_version,
                event_codec_version=run.hello.event_codec_version,
            ),
            certification_id=re.sub(r"[^a-z0-9_-]", "-", named.lower())[:64],
            capabilities=capabilities,
            runtime=_CANARY_PROVIDERS[probed.runtime_id],
            install_trust="observed",
            evidence_bundle_ref=evidence_ref,
            expires_at=probed.expires_at,
        )
    )
    certify = certified.record
    refusal = "" if certify.reason_code is None else f" ({certify.reason_code})"
    unreported = (
        f"; its worker filed no report through {SEMANTIC_TOOLS_CAPABILITY} in "
        f"{CANARY_ATTEMPTS} canaries, so a dispatched Run is granted no tool until "
        f"`eawf runtime certify {probed.runtime_id} --task` observes one"
        if last and not run.reported
        else ""
    )
    row = MachineCertification.model_validate(
        {
            **common,
            "outcome": "certified",
            "capabilities": capabilities,
            "stages": (result.record, certify),
            "verified_at": certify.completed_at,
            "expires_at": probed.expires_at,
            "certification": certified.certification,
            "version_range": None if certified.certification is None else version_range,
            "reason": (
                f"{named} passed the containment canary and the certify stage "
                f"{certify.outcome}{refusal}; certified until {probed.expires_at:%Y-%m-%d}"
                f"{unreported}"
            ),
        }
    )
    append_machine_certification(state_path, row)
    return row


async def walk_certification(
    context: Epoch2RootContext,
    runtime_id: str,
    *,
    task: QualifiedUrn,
    actor: str,
    launchers: Mapping[str, NativeRunLauncher] | None = None,
    publish: Publish | None = None,
    version_range: CertifiedVersionRange | None = None,
) -> MachineCertification:
    """Walk the probe, canary and certify stages of the installed *runtime_id*.

    The canary is a real Run placed under *task*, which lends it the scope a
    Task Run is compiled with; the canary never claims or starts the Task.
    A passed canary whose worker filed no report is walked again from a fresh
    probe, up to :data:`CANARY_ATTEMPTS` canaries, each recording its own rows.

    Args:
        context: The native context of the tree.
        runtime_id: The runtime to certify.
        task: The Task the canary Run is placed under.
        actor: The principal the canary Run is recorded against.
        launchers: The launcher table, defaulting to the shipped one.
        publish: Where each committed transition is handed for the fan-out.
        version_range: The versions the earned certification stands for
            once each passes a re-probe; ``None`` certifies the installed
            version alone.

    Returns:
        The last recorded row: the probe's quarantine, the canary's
        quarantine, or the certification the walk earned.

    Raises:
        RuntimeNotProbedError: The runtime could not be probed, or no shipped
            launcher dispatches it, and nothing was recorded; or
            *version_range* excludes the installed version, and only the
            probe's row was recorded, before any canary spent tokens.
        DaemonValidationError: The canary did not compile, was not created
            or was not dispatched; only the probe's row was recorded.
    """
    # Dispatch reaches the Run-start path, which starts this module's probe.
    from eawf.runtime.daemon.native_dispatch import NATIVE_LAUNCHERS

    provider = _CANARY_PROVIDERS.get(runtime_id)
    if provider is None:
        raise RuntimeNotProbedError(
            f"runtime_canary_unavailable: no shipped launcher dispatches a {runtime_id} canary"
        )
    tree_root = context.identity.tree_root
    for attempt in range(1, CANARY_ATTEMPTS + 1):
        probed, runtime_tuple = await asyncio.to_thread(certifier_for(tree_root).probe, runtime_id)
        if probed.outcome != "certified":
            return probed
        if version_range is not None and not version_range.covers(probed.harness_version):
            raise RuntimeNotProbedError(
                f"runtime_range_excludes_installed: {runtime_id} {probed.harness_version} is "
                f"installed, outside the range {version_range} it would certify"
            )
        evidence_ref = (
            f"artifact://runtime-certification/{runtime_id}/{probed.harness_version}/canary"
        )
        run = await _dispatch_canary(
            context,
            probed,
            task=task,
            actor=actor,
            provider=provider,
            evidence_ref=evidence_ref,
            launchers=NATIVE_LAUNCHERS if launchers is None else launchers,
            publish=publish,
        )
        row = await asyncio.to_thread(
            _conclude_walk,
            tree_root / "state.json",
            probed,
            runtime_tuple,
            run,
            evidence_ref=evidence_ref,
            last=attempt == CANARY_ATTEMPTS,
            version_range=version_range,
        )
        logger.info(
            f"walk_certification runtime={runtime_id!r} version={row.harness_version!r} "
            f"outcome={row.outcome} stages={[stage.stage for stage in row.stages]} "
            f"canary={attempt}/{CANARY_ATTEMPTS} reported={run.reported}"
        )
        # Only a passed canary whose worker never filed is walked again: a
        # quarantine stands as graded, and a filed report is what was sought.
        if row.outcome != "certified" or run.reported:
            return row
    return row


__all__ = [
    "CANARY_ATTEMPTS",
    "CANARY_TOKEN_BUDGET",
    "CONFORMANCE_SUITE_VERSION",
    "REPROBE_BACKOFF",
    "RuntimeCertifier",
    "RuntimeNotProbedError",
    "certifier_for",
    "gate_run_control",
    "installed_version",
    "start_auto_certification",
    "walk_certification",
]
