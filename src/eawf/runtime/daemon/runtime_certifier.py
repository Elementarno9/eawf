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
"""

from __future__ import annotations

import logging
import platform
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from eawf.kernel.config.layered import resolve_auto_certify
from eawf.kernel.runtime.certification import CapabilityCertification
from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.runtime.provider import AuthKind, ControlKind
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.runtime.daemon.methods.conformance import StoreStageJournal, runner_for
from eawf.runtime.runtimes.capabilities import RUNTIME_IDS, ProbeResult, detect_drift
from eawf.runtime.runtimes.conformance import ProbeRequest, RuntimeTuple
from eawf.runtime.runtimes.probes.sdk_baseline import probe_runtime
from eawf.runtime.runtimes.quarantine import is_quarantined
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


class RuntimeNotProbedError(ValueError):
    """The installed runtime could not be probed, so nothing was recorded.

    The message leads with the refusal code: ``runtime_not_installed``,
    ``runtime_probe_incomplete``, ``runtime_platform_unknown`` or
    ``runtime_capabilities_unruled``.
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


def certify_installed(state_path: Path, runtime_id: str) -> MachineCertification:
    """Probe the installed *runtime_id* and record what the probe concluded.

    Each capability the matrix has a probe rule for and declares supported is
    required; one it declares unsupported is recorded unsupported, as the
    committed exports record it.

    Args:
        state_path: Path to the tree's ``state.json``.
        runtime_id: The runtime to probe.

    Returns:
        The recorded row: a certification, or a quarantine with findings.

    Raises:
        RuntimeNotProbedError: The binary is absent or reports no version,
            its ``--help`` timed out or could not be run, the platform cannot
            be named, or no capability of the runtime has a probe rule;
            nothing was recorded.
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
    append_machine_certification(state_path, row)
    return row


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
        with self._serial:
            return certify_installed(self._state_path, runtime_id)

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


__all__ = [
    "CONFORMANCE_SUITE_VERSION",
    "REPROBE_BACKOFF",
    "RuntimeCertifier",
    "RuntimeNotProbedError",
    "certifier_for",
    "certify_installed",
    "gate_run_control",
    "installed_version",
    "start_auto_certification",
]
