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
    """Return the bracketed commit subject prefix for *wave_id*.

    ``P19-I01-W04`` -> ``"[P19-W04]"``. ``None`` if *wave_id* is malformed.
    """
    pair = _phase_and_wave(wave_id)
    if pair is None:
        return None
    phase, wave = pair
    return f"[{phase}-{wave}]"


def derive_wave_sha(wave_id: str, *, repo_root: Path | None = None) -> str | None:
    """Return the most recent commit SHA whose subject carries the wave's prefix.

    Walks ``git log --all --grep=<prefix> --format=%H -n 1`` so the lookup
    survives cherry-picks and is branch-agnostic. Returns ``None`` when:

    - git is not installed,
    - the prefix cannot be derived from *wave_id*,
    - no commit matches the prefix.

    Logs a debug line on failure rather than raising — renderers should
    degrade to an empty SHA, not crash.
    """
    prefix = commit_prefix(wave_id)
    if prefix is None:
        return None
    if shutil.which("git") is None:
        logger.debug(f"derive_wave_sha wave={wave_id} git=not-on-path")
        return None
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
        logger.debug(f"derive_wave_sha wave={wave_id} status=timeout")
        return None
    except (FileNotFoundError, OSError) as exc:
        logger.debug(f"derive_wave_sha wave={wave_id} status=os-error err={exc!s}")
        return None
    if out.returncode != 0:
        logger.debug(f"derive_wave_sha wave={wave_id} status=non-zero rc={out.returncode}")
        return None
    sha = out.stdout.strip().splitlines()
    if not sha:
        return None
    return sha[0]
