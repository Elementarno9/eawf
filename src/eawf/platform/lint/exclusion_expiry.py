"""Bounded grants for the EAWF010 module-length exclusion list.

The ``[tool.eawf.lint.eawf010] exclude`` list is a grandfather list: a
module on it is exempt from the length cap. Left as a bare path list it
is an *unbounded* exemption -- nothing in the configuration says when
the exemption stops being reasonable, so the only pressure on an entry
is the un-grandfather-on-touch rule, which fires only when some wave
happens to edit the file. A module nobody touches sits there forever.

This module makes every entry a **bounded grant**: a path, the date the
grant lapses, and the reason it was given. The two pressures compose
rather than compete. Un-grandfather-on-touch stays the fast path (a wave
that edits the module drops its entry immediately, well before the
date); the expiry is the backstop that puts a deadline on the modules no
wave touches. An entry is therefore never renewed by inaction -- the
only way past the date is an explicit renewal that cites a typed
decision and names an owner, so someone is on record for the extension.

Rejection happens at configuration load, not at commit time. A bare
string entry carries no expiry, so :func:`parse_exclusions` raises and
the *configuration* is what fails: the operator cannot land an unbounded
exemption and discover it later, and every consumer of the lint config
(the pre-commit dispatcher, the release preflight) sees the same
refusal.

The grant is valid **through** its expiry date, the way a passport is:
``expires = 2027-03-01`` means the last clean day is 2027-03-01 and the
entry is expired on 2027-03-02.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

#: Keys every exclusion entry must author.
REQUIRED_KEYS: frozenset[str] = frozenset({"path", "expires", "reason"})

#: Keys an exclusion entry may author. Anything else is a typo the
#: loader refuses rather than silently ignores (``extra="forbid"``
#: parity with the project's Pydantic ingestion paths).
OPTIONAL_KEYS: frozenset[str] = frozenset({"renewal"})

#: Keys a ``renewal`` table must author. Both are mandatory: a decision
#: with no owner is an unattributed extension, and an owner with no
#: decision is an undocumented one.
RENEWAL_KEYS: frozenset[str] = frozenset({"decision", "owner"})


class ExclusionConfigError(ValueError):
    """An EAWF010 exclusion entry is not a bounded, attributed grant."""


@dataclass(frozen=True, slots=True)
class ExclusionRenewal:
    """The explicit extension of one exclusion past its first expiry.

    Attributes:
        decision: Id of the typed ``Decision`` in ``state.json`` that
            ratified the extension.
        owner: Handle accountable for retiring the exclusion. A role or
            team handle, never a personal identifier.
    """

    decision: str
    owner: str


@dataclass(frozen=True, slots=True)
class ModuleExclusion:
    """One bounded grant of exemption from the module-length cap.

    Attributes:
        path: Repo-relative module path the grant covers.
        expires: Last date the grant is honoured, inclusive.
        reason: Why the exemption was given, in the author's words.
        renewal: The decision and owner extending the grant, or ``None``
            for an original grant that has not been renewed.
    """

    path: str
    expires: date
    reason: str
    renewal: ExclusionRenewal | None = None

    def is_expired(self, *, today: date) -> bool:
        """Return whether the grant has lapsed as of *today*.

        The grant covers its expiry date, so a check run exactly on
        ``expires`` is clean and the following day is not.

        Args:
            today: Date to judge the grant against.

        Returns:
            ``True`` once *today* is strictly past :attr:`expires`.
        """
        return today > self.expires

    def days_remaining(self, *, today: date) -> int:
        """Return how many days the grant still has, negative once lapsed.

        Args:
            today: Date to measure from.

        Returns:
            ``0`` on the expiry date itself, negative afterwards.
        """
        return (self.expires - today).days


def _require_text(entry: Mapping[str, object], key: str) -> str:
    """Return a non-blank string field of *entry*.

    Args:
        entry: The raw exclusion table.
        key: Field to read.

    Returns:
        The stripped value.

    Raises:
        ExclusionConfigError: When the key is absent, not a string, or
            blank.
    """
    if key not in entry:
        raise ExclusionConfigError(
            f"EAWF010 exclusion entry {entry!r} is missing the required {key!r} key; "
            f"every entry must author {sorted(REQUIRED_KEYS)}"
        )
    value = entry[key]
    if not isinstance(value, str) or not value.strip():
        raise ExclusionConfigError(
            f"EAWF010 exclusion {key!r} must be a non-blank string, got {value!r}"
        )
    return value.strip()


def _parse_renewal(raw: object, *, path: str) -> ExclusionRenewal:
    """Return the renewal *raw* declares for the exclusion on *path*.

    Args:
        raw: The entry's ``renewal`` value.
        path: Module the renewal belongs to, for the message.

    Returns:
        The parsed renewal.

    Raises:
        ExclusionConfigError: When the renewal is bare (a plain string
            or anything that is not a table), carries an unknown key, or
            leaves ``decision`` or ``owner`` blank.
    """
    if not isinstance(raw, Mapping):
        raise ExclusionConfigError(
            f"EAWF010 exclusion {path!r} carries a bare renewal {raw!r}; a renewal must be a "
            f"table naming both a typed decision and an owner, e.g. "
            f'renewal = {{ decision = "D-EXAMPLE", owner = "eawf-maintainers" }}'
        )
    unknown = sorted(set(raw) - RENEWAL_KEYS)
    if unknown:
        raise ExclusionConfigError(
            f"EAWF010 exclusion {path!r} renewal carries unknown key(s) {unknown}; "
            f"a renewal authors exactly {sorted(RENEWAL_KEYS)}"
        )
    decision = _require_text(raw, "decision")
    owner = _require_text(raw, "owner")
    return ExclusionRenewal(decision=decision, owner=owner)


def parse_exclusion(entry: object) -> ModuleExclusion:
    """Return the bounded grant *entry* declares.

    Args:
        entry: One element of the ``exclude`` array.

    Returns:
        The parsed grant.

    Raises:
        TypeError: When the element is neither a string nor a table, so
            it could not be an exclusion under any reading.
        ExclusionConfigError: When the element is a bare path string
            (no expiry), authors an unknown key, omits a required key,
            or spells ``expires`` as something other than an ISO date.
    """
    if isinstance(entry, str):
        raise ExclusionConfigError(
            f"EAWF010 exclusion {entry!r} has no expiry; an exemption must be a bounded grant, "
            f'e.g. {{ path = "{entry}", expires = "YYYY-MM-DD", reason = "..." }}'
        )
    if not isinstance(entry, Mapping):
        raise TypeError(
            f"EAWF010 exclusion entries must be tables, got {type(entry).__name__}: {entry!r}"
        )
    unknown = sorted(set(entry) - REQUIRED_KEYS - OPTIONAL_KEYS)
    if unknown:
        raise ExclusionConfigError(
            f"EAWF010 exclusion entry carries unknown key(s) {unknown}; an entry authors "
            f"{sorted(REQUIRED_KEYS)} plus optionally {sorted(OPTIONAL_KEYS)}"
        )
    path = _require_text(entry, "path")
    reason = _require_text(entry, "reason")
    raw_expires = entry.get("expires")
    if raw_expires is None:
        raise ExclusionConfigError(
            f"EAWF010 exclusion {path!r} has no expiry; an exemption must be a bounded grant "
            f'with expires = "YYYY-MM-DD"'
        )
    expires = _coerce_date(raw_expires, path=path)
    renewal = _parse_renewal(entry["renewal"], path=path) if "renewal" in entry else None
    return ModuleExclusion(path=path, expires=expires, reason=reason, renewal=renewal)


def _coerce_date(raw: object, *, path: str) -> date:
    """Return the calendar date *raw* spells.

    TOML parses a bare ``2027-03-01`` into a :class:`datetime.date`
    already; a quoted date arrives as a string and is parsed here, so
    both spellings land on the same type.

    Args:
        raw: The entry's ``expires`` value.
        path: Module the value belongs to, for the message.

    Returns:
        The parsed date.

    Raises:
        ExclusionConfigError: When the value is not a date or an ISO
            ``YYYY-MM-DD`` string.
    """
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError as exc:
            raise ExclusionConfigError(
                f"EAWF010 exclusion {path!r} expires={raw!r} is not an ISO YYYY-MM-DD date: {exc}"
            ) from exc
    raise ExclusionConfigError(
        f"EAWF010 exclusion {path!r} expires must be an ISO YYYY-MM-DD date, got {raw!r}"
    )


def parse_exclusions(entries: Sequence[object]) -> tuple[ModuleExclusion, ...]:
    """Return the bounded grants *entries* declares, in authored order.

    Args:
        entries: The raw ``exclude`` array.

    Returns:
        The parsed grants.

    Raises:
        TypeError: When an element is neither a string nor a table.
        ExclusionConfigError: When any element fails
            :func:`parse_exclusion`, or two elements cover one path (two
            grants over one module have two expiry dates, and which one
            binds would be undefined).
    """
    parsed = tuple(parse_exclusion(entry) for entry in entries)
    seen: set[str] = set()
    for exclusion in parsed:
        if exclusion.path in seen:
            raise ExclusionConfigError(
                f"EAWF010 exclusion {exclusion.path!r} is granted twice; one module carries at "
                f"most one grant so its expiry is unambiguous"
            )
        seen.add(exclusion.path)
    return parsed


def expired_exclusions(
    exclusions: Sequence[ModuleExclusion], *, today: date
) -> tuple[ModuleExclusion, ...]:
    """Return the grants that have lapsed as of *today*, in authored order.

    Args:
        exclusions: Parsed grants to judge.
        today: Date to judge them against.

    Returns:
        The lapsed grants; empty when every grant is still live.
    """
    return tuple(entry for entry in exclusions if entry.is_expired(today=today))


def decision_ids_from_state(state_path: Path) -> frozenset[str]:
    """Return the ids of every decision recorded in a state file.

    Args:
        state_path: Path to ``.ea/state.json``.

    Returns:
        The decision ids, empty when the file is absent or records no
        decisions. Absence is not an error here: a renewal validated
        against an empty set simply fails with a message naming the id,
        which is the same refusal the operator needs either way.

    Raises:
        json.JSONDecodeError: When the file exists but is not JSON.
    """
    if not state_path.is_file():
        logger.warning(f"decision_ids_from_state missing state_path={state_path.name!r}")
        return frozenset()
    data = json.loads(state_path.read_text(encoding="utf-8"))
    decisions = data.get("decisions", {}) if isinstance(data, dict) else {}
    if isinstance(decisions, Mapping):
        return frozenset(str(key) for key in decisions)
    return frozenset(str(row.get("id", "")) for row in decisions if isinstance(row, Mapping))


def validate_renewals(
    exclusions: Sequence[ModuleExclusion], *, decision_ids: frozenset[str]
) -> None:
    """Raise unless every renewal cites a decision that exists.

    A renewal is the only way an exclusion outlives its first expiry, so
    the decision it cites has to be real: an id that resolves to no
    ``Decision`` row is an extension nobody ratified.

    Args:
        exclusions: Parsed grants to check.
        decision_ids: Ids present in ``state.json``, e.g. from
            :func:`decision_ids_from_state`.

    Raises:
        ExclusionConfigError: When a renewal cites an unknown decision.
    """
    for entry in exclusions:
        if entry.renewal is None:
            continue
        if entry.renewal.decision not in decision_ids:
            raise ExclusionConfigError(
                f"EAWF010 exclusion {entry.path!r} is renewed against decision "
                f"{entry.renewal.decision!r}, which is absent from state.json; record the "
                f"decision before extending the grant"
            )


__all__ = [
    "OPTIONAL_KEYS",
    "RENEWAL_KEYS",
    "REQUIRED_KEYS",
    "ExclusionConfigError",
    "ExclusionRenewal",
    "ModuleExclusion",
    "decision_ids_from_state",
    "expired_exclusions",
    "parse_exclusion",
    "parse_exclusions",
    "validate_renewals",
]
