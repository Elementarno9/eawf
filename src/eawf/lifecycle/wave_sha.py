"""Derive a wave's commit SHA from git history via the ``[P##-W##]`` prefix.

P19-W04 replaces the persisted ``Wave.commit`` field with a runtime derive
step: the commit subject's ``[P##-W##]`` prefix is the durable signal —
git history rewrites preserve subjects, so the SHA is stable even after
cherry-pick or rebase.

The helpers in this module are intentionally thin subprocess wrappers
that return ``None`` rather than raise when git is unavailable or the
wave has not yet been committed. Callers (renderers, validators) treat
``None`` as "SHA not yet derivable" and degrade gracefully.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS: float = 5.0


def _phase_and_wave(wave_id: str) -> tuple[str, str] | None:
    """Split ``P##-I##-W##`` into ``("P##", "W##")``; return ``None`` on mismatch."""
    parts = wave_id.split("-")
    if len(parts) != 3:
        return None
    phase, _iter, wave = parts
    if not (phase.startswith("P") and wave.startswith("W")):
        return None
    return phase, wave


def commit_prefix(wave_id: str) -> str | None:
    """Return the canonical bracketed commit subject prefix for *wave_id*.

    I01 waves use the short form ``[P##-W##]``; non-I01 waves use the
    long form ``[P##-I##-W##]`` so the iter is disambiguated. ``None``
    if *wave_id* is malformed.

    ``P19-I01-W04`` -> ``"[P19-W04]"``.
    ``P19-I02-W01`` -> ``"[P19-I02-W01]"``.
    """
    pair = _phase_and_wave(wave_id)
    if pair is None:
        return None
    phase, wave = pair
    parts = wave_id.split("-")
    iter_token = parts[1]
    if iter_token == "I01":
        return f"[{phase}-{wave}]"
    return f"[{phase}-{iter_token}-{wave}]"


def _candidate_prefixes(wave_id: str) -> list[str]:
    """Return the prefix forms to grep for, in priority order.

    Executors sometimes emit the long form ``[P##-I01-W##]`` for I01
    waves even though the canonical short form drops the iter token,
    and the reverse happens too. Try the canonical form first, then
    fall back to the other shape so cherry-picked commits are
    discoverable regardless of which executor wrote them.
    """
    pair = _phase_and_wave(wave_id)
    if pair is None:
        return []
    phase, wave = pair
    parts = wave_id.split("-")
    iter_token = parts[1]
    canonical = commit_prefix(wave_id)
    assert canonical is not None  # _phase_and_wave already validated shape
    if iter_token == "I01":
        return [canonical, f"[{phase}-{iter_token}-{wave}]"]
    return [canonical, f"[{phase}-{wave}]"]


def derive_wave_sha(wave_id: str, *, repo_root: Path | None = None) -> str | None:
    """Return the most recent commit SHA whose subject carries the wave's prefix.

    Walks ``git log --all --grep=<prefix> --format=%H -n 1`` so the lookup
    survives cherry-picks and is branch-agnostic. Tries both the
    canonical commit-prefix form and its alternate (long ↔ short) so
    a wave's SHA stays discoverable regardless of which form the
    executor that committed it used.

    Returns ``None`` when:

    - git is not installed,
    - the prefix cannot be derived from *wave_id*,
    - no commit matches any candidate prefix.

    Logs a debug line on failure rather than raising — renderers should
    degrade to an empty SHA, not crash.
    """
    candidates = _candidate_prefixes(wave_id)
    if not candidates:
        return None
    if shutil.which("git") is None:
        logger.debug(f"derive_wave_sha wave={wave_id} git=not-on-path")
        return None
    for prefix in candidates:
        cmd = [
            "git",
            "log",
            "--all",
            f"--grep={prefix}",
            "-F",
            "--format=%H",
            "-n",
            "1",
        ]
        try:
            out = subprocess.run(
                cmd,
                cwd=str(repo_root) if repo_root else None,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.debug(f"derive_wave_sha wave={wave_id} status=timeout prefix={prefix!r}")
            return None
        except (FileNotFoundError, OSError) as exc:
            logger.debug(
                f"derive_wave_sha wave={wave_id} status=os-error prefix={prefix!r} err={exc!s}"
            )
            return None
        if out.returncode != 0:
            rc = out.returncode
            logger.debug(
                f"derive_wave_sha wave={wave_id} status=non-zero rc={rc} prefix={prefix!r}"
            )
            continue
        sha = out.stdout.strip().splitlines()
        if sha:
            return sha[0]
    return None
