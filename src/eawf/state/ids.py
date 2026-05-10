"""ID grammar for eawf.

Project codes: ``^[A-Z][A-Z0-9_-]{1,15}$``.
Phase IDs: ``P\\d{2}`` (e.g., ``P01``).
Iter IDs: ``P\\d{2}-I\\d{2}`` (e.g., ``P13-I04``).
Wave IDs: ``P\\d{2}-I\\d{2}-W\\d{2}`` (e.g., ``P13-I04-W01``).
Hypothesis IDs: ``H\\d{2}-\\d{2}`` plus an optional subproject prefix.

All IDs use two-digit zero-padded suffixes per `eawf-v0.1-plan.md` §10.
"""

from __future__ import annotations

import re

RE_PROJECT_CODE = re.compile(r"^[A-Z][A-Z0-9_-]{1,15}$")
RE_PHASE = re.compile(r"^P\d{2}$")
RE_ITER = re.compile(r"^P\d{2}-I\d{2}$")
RE_WAVE = re.compile(r"^P\d{2}-I\d{2}-W\d{2}$")
RE_HYPOTHESIS = re.compile(r"^H\d{2}-\d{2}$")
RE_HYPOTHESIS_SCOPED = re.compile(r"^[A-Z][A-Z0-9_-]{1,15}-H\d{2}-\d{2}$")


def normalize_to_project_code(name: str) -> str:
    """Coerce *name* into a valid project code or raise :class:`ValueError`.

    Normalisation: uppercase, then collapse spaces and underscores into
    dashes (the canonical separator). Validates the result against
    :data:`RE_PROJECT_CODE`. Used by ``eawf clone-repo`` to derive a
    code from the cloned directory's basename when ``--project-code``
    is not supplied — keeping the rule in one place stops two surfaces
    drifting on what counts as "valid".
    """
    candidate = name.upper().replace(" ", "-").replace("_", "-")
    if not RE_PROJECT_CODE.fullmatch(candidate):
        raise ValueError(
            f"cannot derive valid project_code from {name!r}; "
            f"got {candidate!r} which fails {RE_PROJECT_CODE.pattern}"
        )
    return candidate


_MAX_SUFFIX = 99


def is_project_code(s: str) -> bool:
    """Return ``True`` if ``s`` matches the project-code grammar."""
    return bool(RE_PROJECT_CODE.fullmatch(s))


def is_phase_id(s: str) -> bool:
    """Return ``True`` if ``s`` is a valid phase ID (e.g., ``P01``)."""
    return bool(RE_PHASE.fullmatch(s))


def is_iter_id(s: str) -> bool:
    """Return ``True`` if ``s`` is a valid iter ID (e.g., ``P13-I04``)."""
    return bool(RE_ITER.fullmatch(s))


def is_wave_id(s: str) -> bool:
    """Return ``True`` if ``s`` is a valid wave ID (e.g., ``P13-I04-W01``)."""
    return bool(RE_WAVE.fullmatch(s))


def is_hypothesis_id(s: str) -> bool:
    """Return ``True`` if ``s`` is a valid hypothesis ID, optionally subproject-prefixed."""
    return bool(RE_HYPOTHESIS.fullmatch(s) or RE_HYPOTHESIS_SCOPED.fullmatch(s))


def parents_of(lifecycle_id: str) -> tuple[str, ...]:
    """Return the chain of parent IDs for a lifecycle ID.

    - Phase ID ``P03`` → ``()``.
    - Iter ID ``P03-I02`` → ``("P03",)``.
    - Wave ID ``P13-I04-W01`` → ``("P13", "P13-I04")``.

    Raises ``ValueError`` if ``lifecycle_id`` does not match any lifecycle pattern.
    """
    if is_phase_id(lifecycle_id):
        return ()
    if is_iter_id(lifecycle_id):
        phase_id = lifecycle_id.split("-", 1)[0]
        return (phase_id,)
    if is_wave_id(lifecycle_id):
        parts = lifecycle_id.split("-")
        phase_id = parts[0]
        iter_id = f"{parts[0]}-{parts[1]}"
        return (phase_id, iter_id)
    raise ValueError(f"not a recognised lifecycle id: {lifecycle_id!r}")


def _smallest_free_suffix(used: set[int]) -> int:
    for n in range(1, _MAX_SUFFIX + 1):
        if n not in used:
            return n
    raise ValueError("all 99 suffixes are in use; allocation saturated")


def allocate_next_phase_id(existing: set[str]) -> str:
    """Return the smallest free phase ID not present in ``existing``.

    Raises ``ValueError`` if all 99 suffixes are taken.
    """
    used: set[int] = set()
    for pid in existing:
        if RE_PHASE.fullmatch(pid):
            used.add(int(pid[1:]))
    return f"P{_smallest_free_suffix(used):02d}"


def allocate_next_iter_id(phase_id: str, existing: set[str]) -> str:
    """Return the smallest free iter ID under ``phase_id`` not in ``existing``.

    Raises ``ValueError`` if ``phase_id`` is not a valid phase ID, or if
    all 99 iter suffixes are taken.
    """
    if not is_phase_id(phase_id):
        raise ValueError(f"invalid phase id: {phase_id!r}")
    used: set[int] = set()
    prefix = f"{phase_id}-I"
    for iid in existing:
        if iid.startswith(prefix) and is_iter_id(iid):
            used.add(int(iid[len(prefix) :]))
    return f"{phase_id}-I{_smallest_free_suffix(used):02d}"


def allocate_next_wave_id(iter_id: str, existing: set[str]) -> str:
    """Return the smallest free wave ID under ``iter_id`` not in ``existing``.

    Raises ``ValueError`` if ``iter_id`` is not a valid iter ID, or if
    all 99 wave suffixes are taken.
    """
    if not is_iter_id(iter_id):
        raise ValueError(f"invalid iter id: {iter_id!r}")
    used: set[int] = set()
    prefix = f"{iter_id}-W"
    for wid in existing:
        if wid.startswith(prefix) and is_wave_id(wid):
            used.add(int(wid[len(prefix) :]))
    return f"{iter_id}-W{_smallest_free_suffix(used):02d}"
