"""This machine's own certification of each installed runtime version.

A committed canary export certifies the runtime versions a release was cut on,
and a release artifact cannot follow an operator who upgrades the harness the
next day. The daemon therefore certifies an installed version itself: it runs
the conformance runner's probe stage against the installed binary and records
what the probe concluded here, as one row per attempt.

The rows are machine-local. A certification describes the binary installed on
this machine, and the controls it admits reach processes on this machine, so a
clone starts with none and certifies its own installation.

A passed probe certifies the version: each capability the probe has a rule for
is recorded verified or unsupported, and the certification expires after the
same ninety days the committed exports use. A failed probe quarantines the
version, and the row names what the probe found missing, so a refusal can say
why rather than only that.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Final

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.kernel.store.paths import local_store_path

logger = logging.getLogger(__name__)

#: How long a machine certification stays current: the interval the committed
#: exports certify for, so a version certified here ages like one certified there.
CERTIFICATION_LIFETIME: Final = timedelta(days=90)


def machine_certification_path(state_path: Path) -> Path:
    """Return the machine-local store the rows of the tree at *state_path* live in.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        ``<state_dir>/local/runtime_certification.jsonl``.
    """
    return local_store_path(state_path, StoreKind.RUNTIME_CERTIFICATION)


def append_machine_certification(state_path: Path, row: MachineCertification) -> None:
    """Append one probe's row to the tree's machine-local store.

    Args:
        state_path: Path to the tree's ``state.json``.
        row: The row to record.

    Raises:
        StateConflict: The store's append lock cannot be acquired.
    """
    append_envelope(
        machine_certification_path(state_path),
        Envelope(
            id=f"{row.runtime_id}:{row.harness_version}:{row.verified_at.isoformat()}",
            kind=StoreKind.RUNTIME_CERTIFICATION,
            scope_id=row.tuple_digest,
            created_at=row.verified_at,
            summary=f"{row.runtime_id} {row.harness_version} {row.outcome}",
            payload=row.model_dump(mode="json"),
        ),
    )
    logger.info(
        f"append_machine_certification runtime={row.runtime_id!r} "
        f"version={row.harness_version!r} outcome={row.outcome}"
    )


def read_machine_certifications(state_path: Path) -> tuple[MachineCertification, ...]:
    """Return every row this machine recorded for the tree, in append order.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        The rows; empty when no version was ever probed here.

    Raises:
        pydantic.ValidationError: A line is not an envelope, or its payload is
            not a machine certification.
    """
    path = machine_certification_path(state_path)
    if not path.is_file():
        return ()
    return tuple(
        MachineCertification.model_validate(Envelope.model_validate_json(line).payload)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


__all__ = [
    "CERTIFICATION_LIFETIME",
    "append_machine_certification",
    "machine_certification_path",
    "read_machine_certifications",
]
