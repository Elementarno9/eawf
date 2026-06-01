"""``eawf wal`` — read-only inspection of the daemon write-ahead log.

The daemon persists the post-apply envelope of every state mutation as a
WAL record under ``<runtime_dir>/wal/`` and replays the log on startup
(see :mod:`eawf.runtime.daemon.wal` + :mod:`eawf.runtime.daemon.recovery`).
This group is the operator-facing window onto that log: it reads the WAL
directory directly so the verbs work when the daemon is down (the
post-crash forensic flow), and it is strictly READ-ONLY.

Replay / repair is a daemon recovery operation — the daemon performs it
on boot and exposes a GC + poison-inspect surface under
``eawf daemon replay-wal``. Mutating the WAL out-of-band would bypass the
canonical mutator (AGENTS rule 4), so this group adds no write verbs.

Verbs:

- ``eawf wal status`` — summarise the WAL: per-status counts
  (pending / applied / fsynced / poisoned), newest + oldest record, and
  total on-disk size.
- ``eawf wal list`` — list records (id, status, envelope kind + summary,
  timestamp) with an optional ``--status`` filter and ``--json``.
- ``eawf wal show <record-id>`` — dump one record's decoded envelope.

CLI dispatch only (AGENTS rule 1): the WAL primitive lives under
:mod:`eawf.runtime.daemon.wal`; the handlers parse args and format
output via :func:`eawf.surfaces.cli.output.emit_json_or_text`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.runtime.daemon.wal import WalRecord, WalStatus

logger = logging.getLogger(__name__)


wal_app = typer.Typer(
    name="wal",
    help="Inspect the daemon write-ahead log (read-only: status, list, show).",
    no_args_is_help=True,
    add_completion=False,
)


def _wal_dir() -> Path:
    """Return the daemon WAL directory (``<runtime_dir>/wal/``).

    Resolves through :func:`eawf.runtime.daemon.runtime_dir.runtime_dir`
    so the operator surface targets the same per-user directory the
    daemon writes to. Imported lazily to keep the CLI tree-build path
    free of the daemon runtime (the import-budget gate).
    """
    from eawf.runtime.daemon.runtime_dir import runtime_dir

    return runtime_dir() / "wal"


def _record_summary(record: WalRecord, status: str, path: Path) -> dict[str, Any]:
    """Build the per-record summary row surfaced by ``list`` / ``status``.

    Args:
        record: The decoded WAL record.
        status: The on-disk status the record was found under.
        path: The record's on-disk path.

    Returns:
        A JSON-serialisable mapping with the record id, status, the
        captured envelope's kind + summary, and the record timestamp.
    """
    return {
        "record_id": record.record_id,
        "status": status,
        "envelope_id": record.envelope.id,
        "envelope_kind": record.envelope.kind.value,
        "envelope_summary": record.envelope.summary,
        "written_at": record.written_at.isoformat(),
        "path": str(path),
    }


def _unreadable_summary(path: Path, status: str, exc: Exception) -> dict[str, Any]:
    """Build a summary row for a record whose bytes failed to parse."""
    return {
        "record_id": path.name.split(".")[0],
        "status": status,
        "envelope_id": None,
        "envelope_kind": None,
        "envelope_summary": f"unreadable: {exc}",
        "written_at": None,
        "path": str(path),
    }


def _status_of(path: Path) -> str:
    """Return the WAL status token encoded in *path*'s filename.

    Filenames are ``<record_id>.<status>.json``; the status is the
    second-from-last dotted component. A poisoned record lives under the
    ``poisoned/`` subdirectory and carries the ``poisoned`` token.
    """
    parts = path.name.split(".")
    if len(parts) >= 3:
        return parts[-2]
    return "unknown"


def _iter_rows(wal_dir: Path, status: WalStatus | None) -> list[dict[str, Any]]:
    """Read every live + poisoned WAL record into summary rows.

    Args:
        wal_dir: Directory the WAL lives under.
        status: Optional status filter. When ``None`` every live status
            plus the ``poisoned/`` subdirectory is walked; when set to a
            live status only that status is walked; when set to
            ``POISONED`` only the poisoned subdirectory is walked.

    Returns:
        Summary rows in ``list_records`` order (parseable first, by
        ``written_at``; unparseable last) followed by poisoned rows.
    """
    from eawf.runtime.daemon import wal as wal_mod
    from eawf.runtime.daemon.wal import WalStatus as _WalStatus

    rows: list[dict[str, Any]] = []
    want_live = status is None or status is not _WalStatus.POISONED
    want_poisoned = status is None or status is _WalStatus.POISONED
    if want_live:
        live_filter = None if status is None else status
        for path in wal_mod.list_records(wal_dir, status=live_filter):
            row_status = _status_of(path)
            try:
                record = wal_mod.read_record(path)
            except (ValueError, OSError) as exc:
                rows.append(_unreadable_summary(path, row_status, exc))
                continue
            rows.append(_record_summary(record, row_status, path))
    if want_poisoned:
        for path in wal_mod.list_poisoned(wal_dir):
            try:
                record = wal_mod.read_record(path)
            except (ValueError, OSError) as exc:
                rows.append(_unreadable_summary(path, _WalStatus.POISONED.value, exc))
                continue
            rows.append(_record_summary(record, _WalStatus.POISONED.value, path))
    return rows


@wal_app.command("status")
def wal_status_cmd(ctx: typer.Context) -> None:
    """Summarise the WAL: per-status counts, newest/oldest, total size.

    Reads ``<runtime_dir>/wal/`` directly (no daemon required). An empty
    or absent WAL directory reports honest zeros rather than erroring —
    a fresh install or a daemon that has never mutated state has no WAL.
    """
    from eawf.runtime.daemon import wal as wal_mod
    from eawf.runtime.daemon.wal import WalStatus as _WalStatus

    flags: GlobalFlags = ctx.obj
    wal_dir = _wal_dir()

    counts: dict[str, int] = {s.value: 0 for s in _WalStatus}
    total_bytes = 0
    timestamps: list[str] = []
    for s in (_WalStatus.PENDING, _WalStatus.APPLIED, _WalStatus.FSYNCED):
        paths = wal_mod.list_records(wal_dir, status=s)
        counts[s.value] = len(paths)
        for path in paths:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
    poisoned_paths = wal_mod.list_poisoned(wal_dir)
    counts[_WalStatus.POISONED.value] = len(poisoned_paths)
    for path in poisoned_paths:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            continue

    # Newest / oldest are sourced from the parseable records' written_at
    # so a corrupt body never crashes the summary.
    for row in _iter_rows(wal_dir, status=None):
        if row["written_at"] is not None:
            timestamps.append(row["written_at"])
    timestamps.sort()
    total = sum(counts.values())
    oldest = timestamps[0] if timestamps else None
    newest = timestamps[-1] if timestamps else None

    payload: dict[str, Any] = {
        "wal_dir": str(wal_dir),
        "total": total,
        "counts": counts,
        "oldest": oldest,
        "newest": newest,
        "total_bytes": total_bytes,
    }
    text = (
        f"wal status wal_dir={wal_dir} total={total} "
        f"pending={counts[_WalStatus.PENDING.value]} "
        f"applied={counts[_WalStatus.APPLIED.value]} "
        f"fsynced={counts[_WalStatus.FSYNCED.value]} "
        f"poisoned={counts[_WalStatus.POISONED.value]} "
        f"oldest={oldest} newest={newest} bytes={total_bytes}"
    )
    emit_json_or_text(payload, text, flags=flags)


@wal_app.command("list")
def wal_list_cmd(
    ctx: typer.Context,
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="Filter to one status (pending/applied/fsynced/poisoned).",
        ),
    ] = None,
) -> None:
    """List WAL records (id, status, envelope kind + summary, timestamp).

    Reads the WAL directory directly. Pass ``--status`` to restrict to a
    single lifecycle status; omit it to list every live status plus the
    poisoned records. An empty WAL prints an honest "no WAL records" line.

    Exits ``2`` (usage error) when ``--status`` is not one of the four
    :class:`~eawf.runtime.daemon.wal.WalStatus` tokens.
    """
    from eawf.runtime.daemon.wal import WalStatus as _WalStatus

    flags: GlobalFlags = ctx.obj

    status_filter: WalStatus | None = None
    if status is not None:
        try:
            status_filter = _WalStatus(status)
        except ValueError:
            known = ", ".join(s.value for s in _WalStatus)
            typer.echo(f"unknown --status: {status!r} (known: {known})", err=True)
            raise typer.Exit(code=2) from None

    wal_dir = _wal_dir()
    rows = _iter_rows(wal_dir, status=status_filter)

    if not rows:
        suffix = f" status={status_filter.value}" if status_filter is not None else ""
        text = f"no WAL records{suffix}"
    else:
        text_lines = [f"wal records: {len(rows)}"]
        for row in rows:
            text_lines.append(
                f"  record={row['record_id']} status={row['status']} "
                f"kind={row['envelope_kind']} written_at={row['written_at']} "
                f"summary={row['envelope_summary']!r}"
            )
        text = "\n".join(text_lines)
    payload: dict[str, Any] = {
        "wal_dir": str(wal_dir),
        "status": status_filter.value if status_filter is not None else None,
        "count": len(rows),
        "records": rows,
    }
    emit_json_or_text(payload, text, flags=flags)


@wal_app.command("show")
def wal_show_cmd(
    ctx: typer.Context,
    record_id: Annotated[
        str,
        typer.Argument(help="WAL record id to decode and dump."),
    ],
) -> None:
    """Dump one WAL record's decoded envelope by record id.

    Searches the live statuses (pending -> applied -> fsynced) then the
    ``poisoned/`` subdirectory. Reads the WAL directory directly.

    Exits ``1`` when the record id is not found in any status, and ``1``
    when the located record's bytes fail schema validation.
    """
    from eawf.runtime.daemon import wal as wal_mod
    from eawf.runtime.daemon.wal import WalStatus as _WalStatus

    flags: GlobalFlags = ctx.obj
    wal_dir = _wal_dir()

    located: tuple[Path, str] | None = None
    for s in (_WalStatus.PENDING, _WalStatus.APPLIED, _WalStatus.FSYNCED):
        candidate = wal_dir / f"{record_id}.{s.value}.json"
        if candidate.exists():
            located = (candidate, s.value)
            break
    if located is None:
        poisoned = wal_dir / "poisoned" / f"{record_id}.poisoned.json"
        if poisoned.exists():
            located = (poisoned, _WalStatus.POISONED.value)
    if located is None:
        typer.echo(f"wal record not found: {record_id!r}", err=True)
        raise typer.Exit(code=1)

    path, found_status = located
    try:
        record = wal_mod.read_record(path)
    except (ValueError, OSError) as exc:
        typer.echo(f"wal record unreadable: {record_id!r} ({exc})", err=True)
        raise typer.Exit(code=1) from exc

    payload: dict[str, Any] = {
        "record_id": record.record_id,
        "status": found_status,
        "path": str(path),
        "record": record.model_dump(mode="json"),
    }
    text_lines = [
        f"wal record={record.record_id} status={found_status} path={path}",
        f"  envelope_id={record.envelope.id}",
        f"  envelope_kind={record.envelope.kind.value}",
        f"  envelope_summary={record.envelope.summary!r}",
        f"  written_at={record.written_at.isoformat()}",
        f"  before_state_version={record.before_state_version}",
        f"  after_state_version={record.after_state_version}",
    ]
    if record.poison_reason is not None:
        text_lines.append(f"  poison_reason={record.poison_reason!r}")
    emit_json_or_text(payload, "\n".join(text_lines), flags=flags)
