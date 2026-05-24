"""SDK pre-release baseline probe — pre-2026-06-15 capability snapshot.

Captures the *advertised* contract of the three runtimes eawf v0.3
dispatches against: ``claude`` (Claude Code), ``codex`` (Codex CLI), and
``opencode`` (OpenCode). The snapshot records, per runtime:

- ``installed`` — boolean (False = binary not on ``$PATH``).
- ``bin_basename`` — basename of the resolved binary (no absolute path,
  to keep the snapshot scrub-clean).
- ``bin_parent_kind`` — coarse classification of the parent directory
  (``homebrew`` / ``user-local`` / ``system`` / ``other``) so a future
  re-probe can spot install-source drift without leaking ``$HOME``.
- ``version`` — first non-empty line of ``<bin> --version``.
- ``subprocess_primary_surface`` — the eawf-adapter invocation form
  (e.g. ``claude -p``) plus the closed flag-set the adapter relies on.
- ``advertised_sdk_flags`` — flags / subcommands surfaced by ``--help``
  that resemble SDK / programmatic-control surface (e.g. ``--session-id``,
  ``--output-format=stream-json``, ``--input-format=stream-json``).
- ``advertised_features`` — runtime-specific feature list when the
  binary exposes one (Codex's ``codex features list``).
- ``help_excerpt_sha256`` — hash of the full ``--help`` body so a future
  re-probe can detect drift without re-shipping the whole text.
- ``error`` — captured stderr / exception message when the probe step
  fails; ``installed: false`` snapshots stay structurally complete.

The probe is **read-only**, idempotent, and emits a single JSON object
to stdout (or to the path passed as ``argv[1]``). It is invoked as::

    uv run python -m eawf.runtime.runtimes.probes.sdk_baseline [output_path]

The intent is captured once now (2026-05-19) and re-probed by a v0.4
hygiene wave after the 2026-06-15 SDK reinstatement, with the two
snapshots compared field-by-field to detect drift.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import platform
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Final[str] = "1.0"
"""Snapshot schema version — bump on field add/rename/remove."""

PROBE_DATE: Final[str] = "2026-05-19"
"""Baseline date stamped into every snapshot (pre-2026-06-15 cut-off)."""

_DEFAULT_TIMEOUT_S: Final[float] = 10.0
"""Wall-clock cap per subprocess call — probes are advertised-only."""

_PROBE_BINARIES: Final[tuple[tuple[str, str], ...]] = (
    ("claude-code", "claude"),
    ("codex", "codex"),
    ("opencode", "opencode"),
)
"""Per-runtime (eawf adapter id, on-disk binary name) pairs."""


@dataclasses.dataclass(frozen=True)
class RuntimeProbeRow:
    """One runtime's advertised baseline."""

    runtime_id: str
    bin_name: str
    installed: bool
    bin_basename: str | None
    bin_parent_kind: str | None
    version: str | None
    subprocess_primary_surface: str
    advertised_sdk_flags: tuple[str, ...]
    advertised_features: tuple[str, ...]
    help_excerpt_sha256: str | None
    error: str | None


@dataclasses.dataclass(frozen=True)
class BaselineSnapshot:
    """Top-level baseline snapshot."""

    schema_version: str
    probe_date: str
    probed_at_utc: str
    platform_system: str
    python_version: str
    runtimes: tuple[RuntimeProbeRow, ...]


# --- Subprocess primary surface. ---
#
# We hard-code the eawf-adapter form per runtime so the snapshot records
# what eawf *intends to invoke*, not just what ``--help`` advertises. A
# future re-probe diffs both this string and the live ``--help`` excerpt
# hash; mismatch means the runtime moved the surface eawf depends on.
_SUBPROCESS_PRIMARY_SURFACE: Final[Mapping[str, str]] = {
    "claude-code": "claude -p <prompt> --output-format=json --session-id=<uuid>",
    "codex": "codex exec <prompt> --json --model <model>",
    "opencode": "opencode run <message> --format json --session <sid>",
}

# Flag substrings whose presence in ``--help`` is treated as advertised
# SDK / programmatic-control surface. Matching is case-sensitive against
# the literal flag token; the probe walks each line of help output.
_SDK_FLAG_HINTS: Final[Mapping[str, tuple[str, ...]]] = {
    "claude-code": (
        "--session-id",
        "--continue",
        "--resume",
        "--fork-session",
        "--output-format",
        "--input-format",
        "--json-schema",
        "--max-budget-usd",
        "--mcp-config",
        "--allowedTools",
        "--allowed-tools",
        "--print",
        "-p",
    ),
    "codex": (
        "exec",
        "resume",
        "fork",
        "mcp",
        "mcp-server",
        "exec-server",
        "features",
        "--enable",
        "--disable",
        "-c",
    ),
    "opencode": (
        "--continue",
        "--session",
        "--fork",
        "--format",
        "--model",
        "--agent",
        "--share",
        "run",
        "serve",
        "session",
    ),
}


def _classify_parent_kind(bin_path: Path) -> str:
    """Coarse-classify a resolved binary's parent directory.

    The probe records this classification instead of the absolute path
    so a snapshot under one operator's machine stays comparable to a
    snapshot under another's — and to keep absolute home paths out of
    the committed artifact body (AGENTS rule 16 scrub gate).

    Args:
        bin_path: Resolved absolute path to a runtime binary.

    Returns:
        One of ``"homebrew"``, ``"user-local"``, ``"system"``,
        ``"other"``.
    """
    parts = bin_path.parts
    if "homebrew" in parts or "Cellar" in parts:
        return "homebrew"
    if ".local" in parts:
        return "user-local"
    if bin_path.is_absolute() and parts[:2] in (("/", "usr"), ("/", "bin"), ("/", "sbin")):
        return "system"
    if bin_path.is_absolute() and len(parts) >= 3 and parts[1] in {"usr", "opt"}:
        return "system"
    return "other"


def _run(argv: list[str], *, timeout: float = _DEFAULT_TIMEOUT_S) -> tuple[int, str, str]:
    """Run *argv* synchronously, capture stdout + stderr, never raise.

    Args:
        argv: Subprocess command vector.
        timeout: Wall-clock cap; on hit the probe records a timeout error.

    Returns:
        ``(returncode, stdout, stderr)`` tuple. ``returncode`` is ``-1``
        when the call timed out or the binary disappeared between
        ``shutil.which`` and ``Popen``.
    """
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"probe_run timeout argv={argv!r}")
        return -1, "", f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        logger.warning(f"probe_run not_found argv={argv!r} err={exc!r}")
        return -1, "", f"binary disappeared: {exc!s}"
    return result.returncode, result.stdout, result.stderr


def _parse_version(stdout: str) -> str | None:
    """Return the first non-empty line of *stdout* trimmed, else ``None``."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _collect_advertised_flags(help_text: str, hints: Iterable[str]) -> tuple[str, ...]:
    """Return the subset of *hints* that appear literally in *help_text*.

    Args:
        help_text: Raw ``--help`` output.
        hints: Candidate flag / subcommand tokens to look for.

    Returns:
        Deduplicated, stably-ordered tuple of the hits.
    """
    seen: list[str] = []
    for hint in hints:
        if hint in help_text and hint not in seen:
            seen.append(hint)
    return tuple(seen)


def _probe_codex_features(bin_basename: str) -> tuple[str, ...]:
    """Codex CLI exposes a feature-flag list — capture stable-state flags only.

    The probe records only flags that are stable + enabled (``stable true``)
    or stable + disabled (``stable false``) so a future re-probe can detect
    when a feature graduated from ``under development`` to ``stable``.

    Args:
        bin_basename: Either ``"codex"`` or empty when codex is not installed.

    Returns:
        Tuple of ``"<feature_name>:<stage>:<bool>"`` rows.
    """
    if bin_basename != "codex":
        return ()
    rc, out, _err = _run(["codex", "features", "list"])
    if rc != 0:
        return ()
    rows: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        # Stage is one or two whitespace-collapsed words; rejoin with the
        # final value, which is always 'true' or 'false'.
        final = parts[-1]
        if final not in {"true", "false"}:
            continue
        stage = " ".join(parts[1:-1])
        rows.append(f"{name}:{stage}:{final}")
    return tuple(rows)


def _probe_one(runtime_id: str, bin_name: str) -> RuntimeProbeRow:
    """Snapshot one runtime; gracefully degrade when the binary is absent."""
    resolved = shutil.which(bin_name)
    if resolved is None:
        logger.info(f"probe_one runtime={runtime_id!r} installed=false")
        return RuntimeProbeRow(
            runtime_id=runtime_id,
            bin_name=bin_name,
            installed=False,
            bin_basename=None,
            bin_parent_kind=None,
            version=None,
            subprocess_primary_surface=_SUBPROCESS_PRIMARY_SURFACE[runtime_id],
            advertised_sdk_flags=(),
            advertised_features=(),
            help_excerpt_sha256=None,
            error=None,
        )
    bin_path = Path(resolved)
    bin_basename = bin_path.name
    parent_kind = _classify_parent_kind(bin_path)

    rc_v, out_v, err_v = _run([bin_name, "--version"])
    version: str | None
    error: str | None = None
    if rc_v != 0:
        version = None
        error = f"--version rc={rc_v} stderr={err_v.strip()[:200]!r}"
    else:
        version = _parse_version(out_v)

    rc_h, out_h, err_h = _run([bin_name, "--help"])
    # Some runtimes (notably ``opencode`` via yargs) write the entire help
    # body to stderr; combine both streams so the flag-collector always
    # sees the rendered text.
    help_text = out_h if out_h.strip() else err_h
    if rc_h != 0 and not help_text.strip():
        if error is None:
            error = f"--help rc={rc_h} stderr={err_h.strip()[:200]!r}"
        return RuntimeProbeRow(
            runtime_id=runtime_id,
            bin_name=bin_name,
            installed=True,
            bin_basename=bin_basename,
            bin_parent_kind=parent_kind,
            version=version,
            subprocess_primary_surface=_SUBPROCESS_PRIMARY_SURFACE[runtime_id],
            advertised_sdk_flags=(),
            advertised_features=(),
            help_excerpt_sha256=None,
            error=error,
        )

    advertised = _collect_advertised_flags(help_text, _SDK_FLAG_HINTS[runtime_id])
    features = _probe_codex_features(bin_basename)
    help_sha = hashlib.sha256(help_text.encode("utf-8")).hexdigest()
    logger.info(
        f"probe_one runtime={runtime_id!r} installed=true "
        f"version={version!r} flags={len(advertised)}"
    )
    return RuntimeProbeRow(
        runtime_id=runtime_id,
        bin_name=bin_name,
        installed=True,
        bin_basename=bin_basename,
        bin_parent_kind=parent_kind,
        version=version,
        subprocess_primary_surface=_SUBPROCESS_PRIMARY_SURFACE[runtime_id],
        advertised_sdk_flags=advertised,
        advertised_features=features,
        help_excerpt_sha256=help_sha,
        error=error,
    )


def probe_all() -> BaselineSnapshot:
    """Probe every runtime in :data:`_PROBE_BINARIES` and return the snapshot."""
    rows = tuple(_probe_one(runtime_id, bin_name) for runtime_id, bin_name in _PROBE_BINARIES)
    return BaselineSnapshot(
        schema_version=SCHEMA_VERSION,
        probe_date=PROBE_DATE,
        probed_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        platform_system=platform.system(),
        python_version=platform.python_version(),
        runtimes=rows,
    )


def snapshot_to_json(snapshot: BaselineSnapshot) -> str:
    """Render *snapshot* as a stable, indented JSON string."""
    payload = dataclasses.asdict(snapshot)
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — probe runtimes, emit JSON snapshot.

    Args:
        argv: Optional argv override (used by tests).

    Returns:
        ``0`` on success; ``1`` on write failure.
    """
    args = sys.argv[1:] if argv is None else argv
    snapshot = probe_all()
    rendered = snapshot_to_json(snapshot)
    if args:
        target = Path(args[0])
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            logger.error(f"main write_failed path={target!s} err={exc!s}")
            return 1
        logger.info(f"main wrote_snapshot path={target!s}")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
