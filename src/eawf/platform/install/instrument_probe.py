"""External-tool probe with on-disk JSON cache.

The probe answers a single question: "are the external tools required by the
enabled profiles available on ``$PATH`` at this Eä install?". Each profile
declares a list of :class:`InstrumentSpec` entries; the probe runs every spec
exactly once per ``probe(...)`` call, then persists the result to a JSON file
so subsequent CLI invocations (``eawf doctor``, ``eawf init``, ...) can
short-circuit instead of re-shelling-out.

Probe outcomes per spec are normalised to one of three statuses:

- ``ok``      — ``shutil.which`` resolved the binary (and the optional
                version probe succeeded).
- ``warn``    — the spec is marked ``kind="soft"`` and the binary was not
                found; callers SHOULD surface this as a warning but MUST
                NOT abort.
- ``fail``    — the spec is marked ``kind="hard"`` and the binary was not
                found; :func:`probe` raises
                :class:`eawf.surfaces.cli.errors.UserError`
                (``kind="InstrumentMissing"``, CLI exit 6).

Cache shape (``v=1``)::

    {
      "probe_version": 1,
      "profile_ids": ["core"],
      "results": [
        {"name": "git", "kind": "hard", "status": "ok",
         "path": "/opt/homebrew/bin/git", "version": "git version 2.46.0",
         "detail": null},
        ...
      ]
    }

Cache invalidation is the caller's responsibility. ``eawf doctor --reprobe``
deletes the file on disk before calling :func:`probe` again, which is
equivalent to passing ``reprobe=True``.

Environment overrides:

- ``EA_INSTRUMENT_PROBE`` — when set to a non-empty string, the value is
  treated as the cache-file path and overrides the ``cache_path`` argument.
  The plan calls this out so a CI runner can shove every probe into a
  scratch directory without touching the workspace ``.ea/``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import orjson
from pydantic import BaseModel, ConfigDict

from eawf.platform.subprocess_detach import detached_subprocess_kwargs
from eawf.surfaces.cli.errors import UserError

logger = logging.getLogger(__name__)


PROBE_VERSION: int = 1

# Hard timeout for ``--version`` shell-outs. A hung probe must not stall
# ``eawf doctor`` (which runs as part of ``eawf init`` end-to-end).
_VERSION_TIMEOUT_SECONDS: float = 5.0

_ENV_CACHE_OVERRIDE: str = "EA_INSTRUMENT_PROBE"


class InstrumentSpec(BaseModel):
    """Declarative description of a single external-tool requirement.

    Attributes:
        name: Binary name passed to :func:`shutil.which`. Must match the
            executable filename (no path components).
        kind: ``"hard"`` requirements abort the probe via
            :class:`UserError` (``kind="InstrumentMissing"``); ``"soft"``
            requirements warn only.
        probe: ``"which"`` checks PATH only; ``"version"`` additionally runs
            ``[name, *version_args]`` and captures stdout.
        version_args: Argv tail for the version probe (default ``["--version"]``).
            Ignored when ``probe == "which"``.
        version_regex: Optional regex applied to stdout of the version probe.
            When the regex does not match, the spec downgrades to ``warn``
            (hard) or stays ``warn`` (soft). When ``None``, any non-empty
            stdout passes.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["hard", "soft"]
    probe: Literal["which", "version"] = "which"
    version_args: list[str] = []
    version_regex: str | None = None


class ProbeResult(BaseModel):
    """Normalised result for a single :class:`InstrumentSpec`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["hard", "soft"]
    status: Literal["ok", "warn", "fail"]
    path: str | None = None
    version: str | None = None
    detail: str | None = None


class ProbeReport(BaseModel):
    """Full probe payload — one entry per spec, plus identifying metadata."""

    model_config = ConfigDict(extra="forbid")

    probe_version: int
    profile_ids: list[str]
    results: list[ProbeResult]


# Per-profile registry. v0.1 only ships the ``core`` set; other profiles wire
# in via Wave W02 once the profile composition lands.
INSTRUMENT_REQUIREMENTS: dict[str, list[InstrumentSpec]] = {
    "core": [
        InstrumentSpec(
            name="git",
            kind="hard",
            probe="version",
            version_args=["--version"],
            version_regex=r"^git version",
        ),
        InstrumentSpec(
            name="python",
            kind="hard",
            probe="version",
            version_args=["--version"],
            version_regex=r"^Python\s+\d",
        ),
        InstrumentSpec(
            name="uv",
            kind="hard",
            probe="version",
            version_args=["--version"],
            version_regex=r"^uv\s+\d",
        ),
    ],
}


def resolve_cache_path(cache_path: Path) -> Path:
    """Apply the ``EA_INSTRUMENT_PROBE`` env override, if any.

    The env var trumps the explicit argument so CI runners can pin every
    probe to a scratch directory without touching the call site.
    """
    override = os.environ.get(_ENV_CACHE_OVERRIDE)
    if override:
        return Path(override)
    return cache_path


def probe_one(spec: InstrumentSpec) -> ProbeResult:
    """Run a single spec and return its normalised :class:`ProbeResult`.

    The function never raises for a missing binary — it returns
    ``status="fail"`` on missing hard tools and lets the caller decide whether
    to abort. Subprocess failures during the version probe demote the result
    to ``warn`` (so a hard tool that lives on PATH but errors on
    ``--version`` still passes the install gate).
    """
    resolved = shutil.which(spec.name)
    if resolved is None:
        if spec.kind == "soft":
            return ProbeResult(
                name=spec.name,
                kind=spec.kind,
                status="warn",
                detail=f"{spec.name} not on PATH",
            )
        return ProbeResult(
            name=spec.name,
            kind=spec.kind,
            status="fail",
            detail=f"{spec.name} not on PATH",
        )

    if spec.probe == "which":
        return ProbeResult(
            name=spec.name,
            kind=spec.kind,
            status="ok",
            path=resolved,
        )

    args = spec.version_args or ["--version"]
    # stdin=DEVNULL + detach isolate this non-interactive version probe from the
    # parent's controlling terminal. Inside the live TUI's Doctor-mode gather the
    # parent fd 0 is the controlling TTY; a dead stdin stops the child reading it,
    # but a child that merely SHARES the parent's controlling terminal can still
    # provoke a terminal escape-reply (a Device-Attributes / capability response)
    # written back onto the shared TTY -- which the App's stdin reader then parses
    # as synthetic digit-mode-switch keypresses. detached_subprocess_kwargs() puts
    # the child in its own session (POSIX) / windowless console (win32) so it has
    # no controlling terminal and can neither provoke nor receive such a reply,
    # all without touching the App's own fd 0.
    try:
        # Annotate explicitly so the splatted detach kwargs do not collapse the
        # ``subprocess.run`` overload to its ``Any``-returning fallback (which
        # would make ``proc.stdout`` Any).
        proc: subprocess.CompletedProcess[str] = subprocess.run(
            [spec.name, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            **detached_subprocess_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"probe_one tool={spec.name!r} status=failed exc={exc!r}")
        return ProbeResult(
            name=spec.name,
            kind=spec.kind,
            status="warn",
            path=resolved,
            detail=f"version probe failed: {exc}",
        )

    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return ProbeResult(
            name=spec.name,
            kind=spec.kind,
            status="warn",
            path=resolved,
            detail=f"version probe exited {proc.returncode}",
        )

    if spec.version_regex is not None and not re.search(spec.version_regex, stdout):
        return ProbeResult(
            name=spec.name,
            kind=spec.kind,
            status="warn",
            path=resolved,
            version=stdout or None,
            detail=f"version stdout did not match {spec.version_regex!r}",
        )

    return ProbeResult(
        name=spec.name,
        kind=spec.kind,
        status="ok",
        path=resolved,
        version=stdout or None,
    )


def _read_cache(cache_path: Path) -> ProbeReport | None:
    """Return the cached report or ``None`` if absent / unreadable / stale."""
    if not cache_path.exists():
        return None
    try:
        raw = cache_path.read_bytes()
    except OSError as exc:
        logger.warning(f"_read_cache path={cache_path} status=unreadable exc={exc!r}")
        return None
    try:
        body = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        logger.warning(f"_read_cache path={cache_path} status=invalid_json exc={exc!r}")
        return None
    try:
        report = ProbeReport.model_validate(body)
    except Exception as exc:
        logger.warning(f"_read_cache path={cache_path} status=schema_mismatch exc={exc!r}")
        return None
    if report.probe_version != PROBE_VERSION:
        logger.info(
            f"_read_cache version-mismatch probe_version={report.probe_version} "
            f"current={PROBE_VERSION}; treating cache as stale"
        )
        return None
    return report


def _write_cache(cache_path: Path, report: ProbeReport) -> None:
    """Persist *report* to *cache_path* atomically.

    Writes to a sibling tempfile in *cache_path*'s parent directory and then
    ``os.replace``\\s it onto *cache_path*. Concurrent probes may clobber each
    other's payload but never observe a torn file — the rename is atomic on
    POSIX and Windows. Heavier portalock-backed coordination is unnecessary
    for this read-mostly cache (callers fall back to a fresh probe on any
    read error).
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    raw = orjson.dumps(report.model_dump(mode="json"), option=orjson.OPT_INDENT_2)
    fd, tmp_name = tempfile.mkstemp(
        dir=cache_path.parent,
        prefix=f"{cache_path.name}.tmp.",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, cache_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    logger.info(f"_write_cache path={cache_path} bytes={len(raw)}")


def probe(
    profile_ids: list[str],
    *,
    cache_path: Path,
    reprobe: bool = False,
) -> ProbeReport:
    """Probe every requirement for *profile_ids* and return the report.

    Args:
        profile_ids: List of enabled profile IDs. Unknown IDs are skipped
            silently — the doctor surface validates IDs against
            :data:`eawf.kernel.config.profile.KNOWN_PROFILES` before calling here.
        cache_path: Default cache location. Overridden by
            ``EA_INSTRUMENT_PROBE`` when that env var is set.
        reprobe: When True, ignore any cached payload and re-run every spec
            (overwriting the cache file).

    Returns:
        :class:`ProbeReport` with one :class:`ProbeResult` per spec, in spec
        order, deduplicated by ``name`` across profiles (first occurrence
        wins).

    Raises:
        UserError: when at least one ``kind="hard"`` requirement
            failed (``kind="InstrumentMissing"``). The exception message
            lists every missing hard tool.
    """
    target_path = resolve_cache_path(cache_path)

    if not reprobe:
        cached = _read_cache(target_path)
        if cached is not None and list(cached.profile_ids) == list(profile_ids):
            return cached

    seen: set[str] = set()
    specs: list[InstrumentSpec] = []
    for pid in profile_ids:
        for spec in INSTRUMENT_REQUIREMENTS.get(pid, []):
            if spec.name in seen:
                continue
            seen.add(spec.name)
            specs.append(spec)

    results: list[ProbeResult] = [probe_one(s) for s in specs]
    report = ProbeReport(
        probe_version=PROBE_VERSION,
        profile_ids=list(profile_ids),
        results=results,
    )
    _write_cache(target_path, report)

    missing_hard = [r for r in results if r.kind == "hard" and r.status == "fail"]
    if missing_hard:
        names = ", ".join(r.name for r in missing_hard)
        raise UserError(f"required external tool(s) missing: {names}", kind="InstrumentMissing")

    return report
