"""Derive a wave's commit SHA from git history via the ``[P##-W##]`` prefix.

The derive step is the fallback path behind ``eawf wave show --commit``:
when ``Wave.commit`` has not been pinned (via ``wave close --commit
<ref>``), the commit subject's ``[P##-W##]`` prefix is the durable
signal. Git history rewrites preserve subjects, so the SHA stays
discoverable even after cherry-pick or rebase.

The helpers in this module are intentionally thin subprocess wrappers
that return ``None`` rather than raise when git is unavailable or the
wave has not yet been committed. Callers (renderers, validators) treat
``None`` as "SHA not yet derivable" and degrade gracefully.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS: float = 5.0


DriftKind = Literal["pinned_but_missing", "pinned_mismatch", "closed_no_pin", "closed_unfindable"]


@dataclass(frozen=True)
class Drift:
    """One git/state mismatch row surfaced by :func:`detect_git_state_drift`.

    Attributes:
        wave_id: The wave whose state-recorded commit pointer disagrees
            with what ``git log --grep`` produces.
        kind: Which mismatch shape we hit:

            - ``pinned_but_missing`` — state has ``Wave.commit`` set, but
              ``git log --grep`` returns no commit (commit not on any
              reachable ref; suggests a force-push or repo-clean).
            - ``pinned_mismatch`` — state and git both produce a SHA,
              but they disagree (suggests a rebase that rewrote the
              wave commit without ``eawf wave close --commit <ref>``).
            - ``closed_no_pin`` — CLOSED wave with no ``Wave.commit``
              and no derivable SHA from git history (no commit subject
              carries the wave's bracketed prefix).
            - ``closed_unfindable`` — same as ``closed_no_pin`` but the
              git binary itself was unavailable; the drift is
              indeterminate and the operator should re-run with a
              working git on PATH before acting.
        state_commit: The SHA recorded in ``Wave.commit`` (``None`` for
            the ``closed_no_pin`` / ``closed_unfindable`` kinds).
        git_commit: The SHA ``git log --grep`` returned (``None`` for
            the ``pinned_but_missing`` / ``closed_no_pin`` /
            ``closed_unfindable`` kinds).
    """

    wave_id: str
    kind: DriftKind
    state_commit: str | None = None
    git_commit: str | None = None


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


def _git_merge_base_head_main(
    *, repo_root: Path | None = None, fallback: str = "origin/main"
) -> str:
    """Return ``git merge-base HEAD main`` or ``fallback`` on failure.

    Used as the diff-base of last resort when a wave-anchored SHA is
    unavailable (no ``wave_id`` was threaded into the gate, or the
    wave has not yet committed). The merge-base is preferred over the
    raw ``main`` ref because it scopes the diff to "commits unique to
    this branch", which is what every other ``changed_files`` caller
    already expects.

    Args:
        repo_root: Repository working directory; defaults to the process
            cwd.
        fallback: String returned when git is missing, ``main`` is not
            reachable, or the call times out. Defaults to ``origin/main``
            so callers can still feed it to ``git diff <base>...HEAD``
            via ``changed_files``.

    Returns:
        The 40-char merge-base SHA on success; ``fallback`` otherwise.
    """
    if shutil.which("git") is None:
        logger.debug("_git_merge_base_head_main git=not-on-path")
        return fallback
    try:
        out = subprocess.run(
            ["git", "merge-base", "HEAD", "main"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"_git_merge_base_head_main status=failed err={exc!s}")
        return fallback
    if out.returncode != 0:
        logger.debug(
            f"_git_merge_base_head_main status=non-zero rc={out.returncode} "
            f"stderr={out.stderr.strip()!r}"
        )
        return fallback
    sha = out.stdout.strip()
    if not sha:
        return fallback
    return sha


def derive_diff_base(
    wave_id: str | None,
    *,
    repo_root: Path | None = None,
    fallback: str = "origin/main",
) -> str:
    """Return a diff-base ref suitable for ``git diff <base>...HEAD``.

    Threading order matches the W15 audit-DSL runner contract:

    1. When *wave_id* resolves via :func:`derive_wave_sha`, return
       ``f"{sha}~1"`` so the diff scopes to the wave's own delta.
    2. Otherwise, fall back to ``git merge-base HEAD main`` (per
       :func:`_git_merge_base_head_main`).
    3. If even the merge-base lookup fails, return *fallback* — keeps
       the call site fail-open (matches :data:`~eawf.platform.lint.
       _conditional.DEFAULT_DIFF_BASE`).

    The fallback chain matters because audit gates run in environments
    that range from a fully-fledged repo (with the wave already
    committed) to a fresh clone in CI (where ``derive_wave_sha`` legitimately
    returns ``None``).
    """
    if wave_id is not None:
        sha = derive_wave_sha(wave_id, repo_root=repo_root)
        if sha is not None:
            return f"{sha}~1"
    return _git_merge_base_head_main(repo_root=repo_root, fallback=fallback)


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


def detect_git_state_drift(state: State, *, repo_root: Path | None = None) -> list[Drift]:
    """Compare every CLOSED wave's recorded commit against git history.

    For each ``WaveStatus.CLOSED`` row, four mismatch shapes get
    surfaced (see :class:`DriftKind`):

    1. ``Wave.commit`` is set AND ``derive_wave_sha`` returns ``None``
       — the pinned commit is no longer reachable from any ref.
    2. ``Wave.commit`` is set AND ``derive_wave_sha`` returns a
       different SHA — the wave was rebased after pinning.
    3. ``Wave.commit`` is ``None`` AND ``derive_wave_sha`` returns
       ``None`` because git has no matching subject — the wave closed
       without recording its commit and git no longer carries the
       bracketed prefix.
    4. ``Wave.commit`` is ``None`` AND git is unavailable on PATH —
       we cannot decide; surfaced as ``closed_unfindable`` so the
       operator knows the gap is indeterminate.

    Waves whose status is not CLOSED are ignored: only closed waves
    have a stable expectation about which commit anchors them.

    Args:
        state: The validated :class:`State` to walk.
        repo_root: Repository working directory; defaults to the
            process cwd via the subprocess machinery in
            :func:`derive_wave_sha`.

    Returns:
        List of :class:`Drift` rows, ordered by ``wave_id`` so render
        output stays stable across runs. Empty list when every closed
        wave reconciles cleanly.
    """
    from eawf.kernel.state.enums import WaveStatus

    git_available = shutil.which("git") is not None

    drifts: list[Drift] = []
    for wave_id in sorted(state.waves):
        wave = state.waves[wave_id]
        if wave.status != WaveStatus.CLOSED:
            continue
        derived = derive_wave_sha(wave_id, repo_root=repo_root) if git_available else None
        pinned = wave.commit
        if pinned is not None:
            if derived is None:
                drifts.append(
                    Drift(
                        wave_id=wave_id,
                        kind="pinned_but_missing",
                        state_commit=pinned,
                        git_commit=None,
                    )
                )
            elif not _shas_match(pinned, derived):
                drifts.append(
                    Drift(
                        wave_id=wave_id,
                        kind="pinned_mismatch",
                        state_commit=pinned,
                        git_commit=derived,
                    )
                )
            continue
        # pinned is None
        if not git_available:
            drifts.append(
                Drift(wave_id=wave_id, kind="closed_unfindable", state_commit=None, git_commit=None)
            )
            continue
        if derived is None:
            drifts.append(
                Drift(wave_id=wave_id, kind="closed_no_pin", state_commit=None, git_commit=None)
            )
    logger.info(
        f"detect_git_state_drift waves={len(state.waves)} drifts={len(drifts)} "
        f"git_available={git_available}"
    )
    return drifts


def _shas_match(a: str, b: str) -> bool:
    """Compare two git SHAs prefix-tolerantly.

    ``Wave.commit`` is validated as ``ShaStr`` (40-hex), but tests and
    ad-hoc tools sometimes record a short prefix; ``derive_wave_sha``
    always returns the full 40-hex. Both directions of prefix-match
    succeed when one ref is a strict prefix of the other.
    """
    if a == b:
        return True
    return a.startswith(b) or b.startswith(a)
