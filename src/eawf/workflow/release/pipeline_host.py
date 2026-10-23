"""The real git, forge and proof adapters behind the post-merge pipeline.

:class:`GitHubReleaseHost` is the :class:`~eawf.workflow.release.pipeline.PipelineHost`
the ``eawf release pipeline`` verb runs with. It shells out to ``git``
and ``gh`` from the checkout and nothing else; every decision about what
the answers mean stays in :mod:`eawf.workflow.release.pipeline`.

The gate proofs are the one step that runs in a separate runtime. The
daemon runs each proof command with its own environment, so unless the
caller names a PATH the proofs see the daemon's. They must not find other
agent CLIs, whose presence changes what the canary gates observe, but
they do need ``git``, ``uv`` and ``uvx`` -- and on a typical machine all
of them share one package-manager ``bin`` directory, so dropping that
directory drops the toolchain too. :func:`isolated_proof_path` hides the
agent CLIs by dropping their directories and keeps the toolchain by
linking it into a private directory placed first on the PATH. The proofs
then run through ``eawf release receipts --proof-path``, which hands that
PATH to the daemon already serving the repository: a second daemon on the
same store would be a second writer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.projection.row_facts import parent_key_of, stored_fields
from eawf.kernel.state.epoch2.authority import (
    NativeAuthorityRequiredError,
    require_native_authority,
)
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerError, latest_rows
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope
from eawf.workflow.release.pipeline import PUBLISH_WORKFLOWS, PipelineHostError
from eawf.workflow.release.pipeline_receipts import RECEIPT_FILENAMES
from eawf.workflow.verify.release_probes import release_tree_status

logger = logging.getLogger(__name__)

#: Agent CLIs the gate proofs must not find on PATH.
HIDDEN_PROGRAMS: Final[tuple[str, ...]] = ("codex",)

#: Programs the gate proofs run, linked back onto the isolated PATH.
PROOF_TOOLCHAIN: Final[tuple[str, ...]] = ("git", "uv", "uvx")

#: How long a forge run may take before the wait gives up. The dry run
#: builds twice and the publish runs wait on registries.
RUN_TIMEOUT_SECONDS: Final[float] = 5400.0

#: Seconds between polls of a forge run.
POLL_SECONDS: Final[float] = 30.0

#: Wall budget of one ``git`` or ``gh`` query.
COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0

#: Wall budget of the gate proofs: the stabilization proof runs the full
#: suite, about an hour.
PROOF_TIMEOUT_SECONDS: Final[float] = 3.0 * 3600

#: One ``Batch:`` trailer line, the form ``eawf publish`` writes.
_BATCH_TRAILER_RE: Final[re.Pattern[str]] = re.compile(r"^Batch:\s+(\S+)\s*$", re.MULTILINE)

#: One ``Task:`` trailer line, the form every delivery commit carries.
_TASK_TRAILER_RE: Final[re.Pattern[str]] = re.compile(r"^Task:\s+(\S+)\s*$", re.MULTILINE)


def isolated_proof_path(path: str, *, shim_dir: Path) -> str:
    """Return *path* without the directories of :data:`HIDDEN_PROGRAMS`, toolchain kept.

    Args:
        path: The PATH to filter.
        shim_dir: A private directory the toolchain is linked into and
            which leads the returned PATH.

    Returns:
        The isolated PATH.

    Raises:
        PipelineHostError: When a :data:`PROOF_TOOLCHAIN` program is not
            on *path* at all.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    for program in PROOF_TOOLCHAIN:
        found = shutil.which(program, path=path)
        if found is None:
            raise PipelineHostError(f"{program} is not on PATH; the gate proofs run it")
        link = shim_dir / program
        if not link.exists():
            link.symlink_to(found)
    kept = [
        entry
        for entry in path.split(os.pathsep)
        if entry and not any((Path(entry) / name).exists() for name in HIDDEN_PROGRAMS)
    ]
    return os.pathsep.join([str(shim_dir), *kept])


class GitHubReleaseHost:
    """The checkout, its remote and the GitHub forge, via ``git`` and ``gh``."""

    def __init__(
        self,
        repo_root: Path,
        *,
        state_path: Path,
        remote: str = "origin",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind the host to one checkout.

        Args:
            repo_root: The checkout the pipeline runs in.
            state_path: The project's ``state.json``; its directory is the
                native tree Milestone Batches and Tasks are read from.
            remote: The remote ``main`` and the tags live on.
            sleep: How the host waits between polls.
        """
        self._repo_root = repo_root
        self._state_path = state_path
        self._remote = remote
        self._sleep = sleep

    @property
    def repo_root(self) -> Path:
        """The checkout the pipeline runs in."""
        return self._repo_root

    def _run(self, argv: Sequence[str], *, timeout: float = COMMAND_TIMEOUT_SECONDS) -> str:
        try:
            result = subprocess.run(
                list(argv),
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                **no_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PipelineHostError(f"{argv[0]} {argv[1]} failed: {exc}") from exc
        if result.returncode != 0:
            raise PipelineHostError(
                f"{' '.join(argv[:3])} exited {result.returncode}: {result.stderr.strip()[-400:]}"
            )
        return result.stdout

    def _gh_json(self, argv: Sequence[str]) -> Any:
        return json.loads(self._run(["gh", *argv]) or "null")

    def dirty_paths(self) -> tuple[str, ...]:
        """Return the uncommitted paths of the checkout, minus the release stores."""
        status = release_tree_status(self._repo_root)
        if status.returncode != 0:
            raise PipelineHostError(f"git status failed: {status.stderr.strip()}")
        return tuple(line for line in status.stdout.splitlines() if line.strip())

    def head(self) -> str:
        """Return the checkout's HEAD commit."""
        return self._run(["git", "rev-parse", "HEAD"]).strip()

    def remote_main(self) -> str:
        """Fetch the remote and return the commit its ``main`` points at."""
        self._run(["git", "fetch", self._remote], timeout=RUN_TIMEOUT_SECONDS)
        return self._run(["git", "rev-parse", f"refs/remotes/{self._remote}/main"]).strip()

    def which(self, program: str) -> str | None:
        """Return where *program* is on PATH, or ``None``."""
        return shutil.which(program)

    def target_ids(self, version: str) -> tuple[str, ...]:
        """Return the publication targets the checkpoint configuration declares."""
        from eawf.workflow.release.admission import checkpoint_release_config

        try:
            config = checkpoint_release_config(version, repo_root=self._repo_root)
        except (KeyError, ValueError) as exc:
            raise PipelineHostError(f"checkpoint {version} configuration: {exc}") from exc
        return tuple(target.target_id for target in config.targets)

    def preflight(self, version: str, *, source: str) -> str | None:
        """Sweep readiness at *source*; ``None`` when ready, else the first red row."""
        from eawf.runtime.release import sweep_for_tag
        from eawf.workflow.release.admission import checkpoint_release_config

        try:
            config = checkpoint_release_config(version, repo_root=self._repo_root)
            readiness = sweep_for_tag(
                config,
                version=version,
                repo_root=self._repo_root,
                remote=self._remote,
                source=source,
                waiver_count=0,
                computed_at=datetime.now(UTC),
            )
        except (KeyError, ValueError) as exc:
            return f"the sweep could not run: {exc}"
        if readiness.ready:
            return None
        red = readiness.first_red
        if red is None:
            return f"{readiness.waiver_count} outstanding waiver(s)"
        row = readiness.row(red)
        code = row.failure_code.value if row.failure_code is not None else row.status.value
        return f"{red.value} ({code}): {row.remediation}"

    def remote_tag(self, tag: str) -> str | None:
        """Return the commit *tag* points at on the remote, or ``None``."""
        listing = self._run(
            [
                "git",
                "ls-remote",
                "--tags",
                self._remote,
                f"refs/tags/{tag}",
                f"refs/tags/{tag}^{{}}",
            ]
        )
        refs: dict[str, str] = {}
        for line in listing.splitlines():
            sha, _, ref = line.partition("\t")
            if ref:
                refs[ref.strip()] = sha.strip()
        # An annotated tag lists its own object and, peeled, the commit it tags.
        return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")

    def push_tag(self, tag: str, *, revision: str) -> None:
        """Create the annotated *tag* at *revision* unless it exists there, then push it."""
        existing = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=False,
            **no_window_kwargs(),
        ).stdout.strip()
        if existing and existing != revision:
            raise PipelineHostError(
                f"local tag {tag} points at {existing[:12]}, not {revision[:12]}"
            )
        if not existing:
            self._run(["git", "tag", "-a", tag, "-m", f"Release {tag}", revision])
        self._run(["git", "push", self._remote, f"refs/tags/{tag}"], timeout=RUN_TIMEOUT_SECONDS)
        logger.info(f"push_tag tag={tag!r} remote={self._remote!r}")

    def _await_run(self, run_id: str) -> None:
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
        while True:
            view = self._gh_json(["run", "view", run_id, "--json", "status,conclusion"])
            if view.get("status") == "completed":
                if view.get("conclusion") != "success":
                    raise PipelineHostError(f"run {run_id} concluded {view.get('conclusion')}")
                return
            if time.monotonic() > deadline:
                raise PipelineHostError(f"run {run_id} still {view.get('status')} at the deadline")
            self._sleep(POLL_SECONDS)

    def _find_run(self, argv: Sequence[str], *, accept: Callable[[dict[str, Any]], bool]) -> str:
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS / 6
        while True:
            runs = self._gh_json(["run", "list", *argv, "--json", "databaseId,headSha,createdAt"])
            for run in runs or ():
                if accept(run):
                    return str(run["databaseId"])
            if time.monotonic() > deadline:
                raise PipelineHostError(f"no run appeared for gh run list {' '.join(argv)}")
            self._sleep(POLL_SECONDS)

    def run_build_receipts(self, *, channel: str, source: str, dest: Path) -> None:
        """Dispatch the release dry run on ``main``, wait for it, download its receipts."""
        started = datetime.now(UTC) - timedelta(minutes=1)
        self._run(
            ["gh", "workflow", "run", "release.yaml", "--ref", "main", "-f", f"channel={channel}"]
        )
        run_id = self._find_run(
            ["--workflow", "release.yaml", "--event", "workflow_dispatch", "--limit", "20"],
            accept=lambda run: (
                run.get("headSha") == source
                and datetime.fromisoformat(str(run.get("createdAt"))) >= started
            ),
        )
        self._await_run(run_id)
        for name in RECEIPT_FILENAMES:
            self._run(["gh", "run", "download", run_id, "-n", name, "-D", str(dest / name)])
        logger.info(f"run_build_receipts workflow_run={run_id} channel={channel!r}")

    def wait_publication(self, *, tag: str, dest: Path) -> None:
        """Wait for every publish run of *tag* and download its publication receipts."""
        for workflow in PUBLISH_WORKFLOWS:
            run_id = self._find_run(
                ["--workflow", workflow, "--branch", tag, "--event", "push", "--limit", "5"],
                accept=lambda run: True,
            )
            self._await_run(run_id)
            self._run(
                [
                    "gh",
                    "run",
                    "download",
                    run_id,
                    "-p",
                    "publication-receipt-*",
                    "-D",
                    str(dest / Path(workflow).stem),
                ]
            )
            logger.info(f"wait_publication tag={tag!r} workflow={workflow!r} workflow_run={run_id}")

    def prove_gates(self, version: str) -> dict[str, Any]:
        """Run ``release receipts`` on the serving daemon with the isolated proof PATH."""
        scratch = Path(tempfile.mkdtemp(prefix="eawfp-"))
        proof_path = isolated_proof_path(os.environ.get("PATH", ""), shim_dir=scratch / "bin")
        argv = [sys.executable, "-m", "eawf", "--json", "release", "receipts", version]
        try:
            result = subprocess.run(
                [*argv, "--proof-path", proof_path],
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=PROOF_TIMEOUT_SECONDS,
                **no_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PipelineHostError(f"release receipts failed: {exc}") from exc
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        try:
            reply = DomainEnvelope.model_validate_json(result.stdout).result
        except ValidationError:
            reply = None
        if reply is None or "receipts" not in reply:
            detail = (result.stdout or result.stderr).strip()[-400:]
            raise PipelineHostError(f"release receipts exited {result.returncode}: {detail}")
        return reply

    def _tree_rows(self, collection: Epoch2Collection) -> dict[str, Mapping[str, Any]]:
        """Return every record of *collection* in the native tree beside ``state_path``.

        A terminal record leaves the document for the collection's ledger, so the ledger's
        latest row per key is read too; a row the document still holds wins over it.

        Raises:
            NativeAuthorityRequiredError: The tree is not native.
            LedgerError: The ledger is torn or its snapshot does not match.
            ValueError: A ledger line does not validate.
            OSError: The document or the ledger cannot be read.
        """
        authority = require_native_authority(self._state_path.parent)
        assert authority.target is not None and authority.generation_id is not None
        document = authority.target.generation_path(authority.generation_id) / (GENERATION_DOCUMENT)
        rows = {
            **latest_rows(ledger_path(document, collection)),
            **document_rows(read_document(document), collection),
        }
        return {key: stored_fields(collection, key, row)[0] for key, row in rows.items()}

    def milestone_batch_heads(self, milestone_key: str) -> dict[str, str]:
        """Return Batch key -> pinned head for the COMPLETED Batches of *milestone_key*.

        A Batch that closed has compacted out of the document into its ledger, so both are
        read. The head is the commit the Batch pinned when it closed on landing.

        Raises:
            PipelineHostError: The tree is not native, or a Batch row cannot be read or
                does not validate.
        """
        try:
            heads: dict[str, str] = {}
            for key, row in self._tree_rows(Epoch2Collection.BATCH).items():
                if (
                    parent_key_of(Epoch2Collection.BATCH, row) != milestone_key
                    or row.get("status") != BatchStatus.COMPLETED.value
                ):
                    continue
                binding = DeliveryBatch.model_validate(row).current_head_binding
                assert binding is not None, "a COMPLETED Batch always pins its head"
                heads[key] = binding.head_sha
        except (NativeAuthorityRequiredError, LedgerError, ValueError, OSError) as exc:
            raise PipelineHostError(f"milestone {milestone_key} batches: {exc}") from exc
        return heads

    def batch_carrier_commits(self, ref: str) -> dict[str, tuple[str, ...]]:
        """Return Batch key -> the commits reachable from *ref* carrying its work.

        A commit carries a Batch when a ``Task:`` trailer names a Task placed in it, or a
        ``Batch:`` trailer names it. The whole message is matched rather than git's
        trailer block, which misses a trailer set apart by a blank line.

        Raises:
            PipelineHostError: The log cannot be read, or the tree's Tasks cannot.
        """
        listing = self._run(
            ["git", "log", "-E", "--grep=^Batch:", "--grep=^Task:", "--format=%H%x00%B%x1e", ref]
        )
        try:
            placed = {
                key: batch
                for key, row in self._tree_rows(Epoch2Collection.TASK).items()
                if (batch := parent_key_of(Epoch2Collection.TASK, row)) is not None
            }
        except (NativeAuthorityRequiredError, LedgerError, ValueError, OSError) as exc:
            raise PipelineHostError(f"tasks behind {ref}: {exc}") from exc
        found: dict[str, list[str]] = {}
        for entry in listing.split("\x1e"):
            sha, _, message = entry.strip().partition("\x00")
            keys = set(_BATCH_TRAILER_RE.findall(message)) | {
                placed[task] for task in _TASK_TRAILER_RE.findall(message) if task in placed
            }
            for key in sorted(keys):
                found.setdefault(key, []).append(sha)
        return {key: tuple(shas) for key, shas in found.items()}

    def sleep(self, seconds: float) -> None:
        """Wait *seconds*."""
        self._sleep(seconds)


__all__ = [
    "HIDDEN_PROGRAMS",
    "PROOF_TOOLCHAIN",
    "GitHubReleaseHost",
    "isolated_proof_path",
]
