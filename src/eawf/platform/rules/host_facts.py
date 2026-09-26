"""Load the shipped host facts and resolve the caps a rendered projection is held to.

The host-fact record itself -- one closed record per supported runtime,
each fact certified with its evidence or uncertified with its reason -- is
defined once in :mod:`eawf.kernel.runtime.host_facts`, which runtime
certification reads too. This module loads the document shipped with the
package and answers the render's questions of it.

The certified value is the runtime's default. A value one machine has
configured is diagnostic only: a measurement taken under it is labelled
non-portable, because nobody else can reproduce the number, and it never
relaxes the cap a render is held to.
"""

from __future__ import annotations

import logging
import os
import tomllib
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import NonNegativeInt, PositiveInt

from eawf.kernel.runtime.certification import CertifiedRuntimeFacts
from eawf.kernel.runtime.host_facts import (
    HOST_FACT_NAMES,
    HOST_RUNTIMES,
    HostFact,
    HostFactError,
    HostFactEvidenceError,
    HostFactInheritanceError,
    HostFactName,
    HostFactRegistry,
    ProjectionKind,
    RuntimeHostFacts,
    StaleHostFact,
    parse_host_facts,
    stale_host_facts,
)
from eawf.observability.telemetry.models import RuntimeName
from eawf.platform.rules.records import RuleModel

logger = logging.getLogger(__name__)

_DATA_PACKAGE: Final = "eawf.platform.rules.data"
_DATA_FILE: Final = "host_facts.yaml"

#: The Codex configuration key that overrides the project-document cap.
CODEX_DOC_CAP_KEY: Final = "project_doc_max_bytes"


class CertifiedCap(RuleModel):
    """The cap one projection is held to, and the runtime it comes from.

    Attributes:
        runtime: The runtime whose certified cap is smallest.
        cap_bytes: That cap.
        readers: Every runtime that reads the projection.
        uncertified: Readers with no certified cap, which the cap does not
            cover.
    """

    runtime: RuntimeName
    cap_bytes: PositiveInt
    readers: tuple[RuntimeName, ...]
    uncertified: tuple[RuntimeName, ...]


class HostFactMeasurement(RuleModel):
    """A measurement taken against one fact, labelled for portability.

    Attributes:
        runtime: The runtime the measurement concerns.
        fact: The fact the measurement is judged against.
        measured: The measured quantity, in the fact's unit.
        certified_value: The certified default, when there is one.
        configured_value: The value configured where it was measured.
        portability: ``non_portable`` when the configuration diverged from
            the certified default, so the number certifies nothing.
        diverged: The fact whose configuration diverged, when non-portable.
    """

    runtime: RuntimeName
    fact: HostFactName
    measured: NonNegativeInt
    certified_value: PositiveInt | None
    configured_value: PositiveInt | None
    portability: Literal["portable", "non_portable"]
    diverged: HostFactName | None

    @property
    def note(self) -> str:
        """Return a one-line label naming the diverged fact, or ``portable``."""
        if self.diverged is None:
            return "portable"
        return (
            f"non-portable: {self.runtime} {self.diverged} configured "
            f"{self.configured_value}, certified default {self.certified_value}"
        )


@cache
def load_host_facts() -> HostFactRegistry:
    """Read the certified host facts shipped with the package.

    Returns:
        The registry.

    Raises:
        ValidationError: The shipped document does not match the schema.
        HostFactError: The shipped document borrows or misstates a fact.
    """
    raw = files(_DATA_PACKAGE).joinpath(_DATA_FILE).read_bytes()
    return parse_host_facts(yaml.safe_load(raw))


def smallest_certified_cap(registry: HostFactRegistry, kind: ProjectionKind) -> CertifiedCap | None:
    """Resolve the project-document cap a projection must fit.

    The cap is the smallest certified default among the runtimes that read
    the projection, read through the runtime-certification facts of each
    record; a configured override on any machine is never consulted.

    Args:
        registry: The host facts.
        kind: The projection.

    Returns:
        The cap, naming every reader it does not certify, or ``None`` when
        no reader has a certified cap, which a render treats as a failure
        rather than an unbounded budget.
    """
    readers = tuple(record for record in registry.records if kind in record.reads)
    caps = tuple((record.runtime, _document_cap(record)) for record in readers)
    certified = [(cap, runtime) for runtime, cap in caps if cap is not None]
    if not certified:
        return None
    cap_bytes, runtime = min(certified)
    return CertifiedCap(
        runtime=runtime,
        cap_bytes=cap_bytes,
        readers=tuple(runtime for runtime, _cap in caps),
        uncertified=tuple(runtime for runtime, cap in caps if cap is None),
    )


def _document_cap(record: RuntimeHostFacts) -> int | None:
    """Return a runtime's certified project-document cap, or ``None``.

    Args:
        record: The runtime's host facts.

    Returns:
        The cap runtime certification reads for the runtime.
    """
    facts = CertifiedRuntimeFacts.from_host_facts(record)
    return None if facts is None else facts.certified_cap("project_document_cap_bytes")


def label_measurement(
    record: RuntimeHostFacts,
    *,
    fact: HostFactName,
    measured: int,
    configured_value: int | None,
) -> HostFactMeasurement:
    """Label a measurement portable or non-portable.

    A measurement is portable only when the fact it is judged against was at
    its certified default where it was taken. A configured value that differs
    from the default, or any configured value of an uncertified fact, makes
    the measurement a diagnostic nobody else can reproduce.

    Args:
        record: The runtime the measurement concerns.
        fact: The fact the measurement is judged against.
        measured: The measured quantity.
        configured_value: The value configured where it was measured;
            ``None`` when the default was in force.

    Returns:
        The labelled measurement.

    Raises:
        KeyError: ``fact`` is not a host fact.
        ValidationError: ``measured`` is negative or ``configured_value`` is
            not positive.
    """
    certified = record.fact(fact).default_value
    diverged = configured_value is not None and configured_value != certified
    return HostFactMeasurement(
        runtime=record.runtime,
        fact=fact,
        measured=measured,
        certified_value=certified,
        configured_value=configured_value,
        portability="non_portable" if diverged else "portable",
        diverged=fact if diverged else None,
    )


def certified_document_cap(runtime: RuntimeName) -> int:
    """Return a runtime's certified default project-document cap.

    Args:
        runtime: The runtime.

    Returns:
        The certified cap in bytes.

    Raises:
        KeyError: ``runtime`` is not a supported runtime.
        HostFactEvidenceError: The runtime's cap is uncertified, so there is
            no number to measure against.
    """
    cap = _document_cap(load_host_facts().runtime(runtime))
    if cap is None:
        raise HostFactEvidenceError(f"the {runtime} project-document cap is uncertified")
    return cap


def measure_codex_document(measured: int, *, codex_home: Path | None = None) -> HostFactMeasurement:
    """Label a project-document measurement taken on this machine for Codex.

    Args:
        measured: The measured document size in bytes.
        codex_home: The Codex home; ``None`` for ``$CODEX_HOME`` or
            ``~/.codex``.

    Returns:
        The measurement, non-portable when this machine configures a cap
        other than the certified default.
    """
    home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return label_measurement(
        load_host_facts().codex,
        fact="project_document_cap_bytes",
        measured=measured,
        configured_value=read_codex_document_cap(home),
    )


def read_codex_document_cap(codex_home: Path) -> int | None:
    """Read the project-document cap configured for Codex on this machine.

    Args:
        codex_home: The Codex home directory holding ``config.toml``.

    Returns:
        The configured cap, or ``None`` when no cap is configured, the file
        is absent, or it is unreadable -- an unreadable file leaves the
        default in force as far as a diagnostic can tell.
    """
    path = codex_home / "config.toml"
    if not path.is_file():
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8")).get(CODEX_DOC_CAP_KEY)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.warning(f"codex config unreadable path={path} error={exc}")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


__all__ = [
    "CODEX_DOC_CAP_KEY",
    "HOST_FACT_NAMES",
    "HOST_RUNTIMES",
    "CertifiedCap",
    "HostFact",
    "HostFactError",
    "HostFactEvidenceError",
    "HostFactInheritanceError",
    "HostFactMeasurement",
    "HostFactName",
    "HostFactRegistry",
    "ProjectionKind",
    "RuntimeHostFacts",
    "StaleHostFact",
    "certified_document_cap",
    "label_measurement",
    "load_host_facts",
    "measure_codex_document",
    "parse_host_facts",
    "read_codex_document_cap",
    "smallest_certified_cap",
    "stale_host_facts",
]
