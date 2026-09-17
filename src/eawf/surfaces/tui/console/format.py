"""Console number, duration, time and date formatting under fixed, locale-free rules.

Every number, date and time a console frame shows is spelled here, so a golden frame is
identical on every machine at one revision. Nothing in this module asks the platform how
to write a value: grouping always uses a comma, times are always UTC, month names come
from the table below rather than from ``strftime`` (whose ``%b`` follows the process
locale), and no value is converted through the local timezone. An identifier that merely
contains digits and a dash is a string, not a value, and never passes through here.
"""

from __future__ import annotations

from datetime import UTC, datetime

MONTHS: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_MINUTE = 60
_HOUR = 3600


def group(n: int) -> str:
    """Return ``n`` with its thousands grouped by a comma, as in ``41,208``.

    The ``,`` format option always emits a comma; only the ``n`` presentation type
    consults the locale, and it is never used.

    Raises:
        TypeError: ``n`` is not an integer, or is a bool.
    """
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError(f"group() takes an int, got {type(n).__name__}")
    return f"{n:,}"


def fixed(x: float, *, places: int) -> str:
    """Return ``x`` with exactly ``places`` decimals and a point separator, as in ``0.31``.

    Raises:
        ValueError: ``places`` is negative.
    """
    if places < 0:
        raise ValueError(f"places must be non-negative, got {places}")
    return f"{x:.{places}f}"


def seconds(x: float) -> str:
    """Return a timer length in seconds without trailing zeros, as in ``1.5s`` or ``5s``.

    Raises:
        ValueError: ``x`` is negative.
    """
    if x < 0:
        raise ValueError(f"a timer length must be non-negative, got {x}")
    return f"{x:g}s"


def span(total: int) -> str:
    """Return a whole-second duration at the grain a reader needs.

    Under a minute it is ``12s``; under an hour ``4m`` or ``2m 08s``; from an hour on
    ``4h`` or ``1h 05m``, because seconds stop carrying meaning at that scale.

    Raises:
        ValueError: ``total`` is negative.
    """
    if total < 0:
        raise ValueError(f"a duration must be non-negative, got {total}")
    if total < _MINUTE:
        return f"{total}s"
    if total < _HOUR:
        minutes, rest = divmod(total, _MINUTE)
        return f"{minutes}m" if rest == 0 else f"{minutes}m {rest:02d}s"
    hours, rest = divmod(total, _HOUR)
    minutes = rest // _MINUTE
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes:02d}m"


def _utc(at: datetime) -> datetime:
    """Return ``at`` in UTC.

    Raises:
        ValueError: ``at`` is naive, so the instant it names is unknown.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"a console time must carry its zone, got naive {at.isoformat()}")
    return at.astimezone(UTC)


def clock_time(at: datetime) -> str:
    """Return the UTC time of day with seconds and no zone suffix, as in ``14:00:12``.

    Raises:
        ValueError: ``at`` is naive.
    """
    utc = _utc(at)
    return f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"


def clock_minute(at: datetime) -> str:
    """Return the UTC time of day to the minute, as in ``14:02``.

    Raises:
        ValueError: ``at`` is naive.
    """
    utc = _utc(at)
    return f"{utc.hour:02d}:{utc.minute:02d}"


def day(at: datetime, *, year: bool = False) -> str:
    """Return the UTC date as a month abbreviation and day, as in ``Jul 28``.

    Args:
        at: The instant to date.
        year: Carry the year too, as in ``Jun 2 2026``.

    Raises:
        ValueError: ``at`` is naive.
    """
    utc = _utc(at)
    text = f"{MONTHS[utc.month - 1]} {utc.day}"
    return f"{text} {utc.year}" if year else text
