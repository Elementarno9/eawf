"""Which Runs the epoch-1 corpus really supports, and what they may claim.

A Run is one execution episode. Epoch 1 recorded episodes in two places
and neither of them is a Run: an ``agent_sessions`` row is a provenance
note, and a wave's attempt table is a dispatch log. Only the attempt
table says a subprocess actually ran, so that is the only place a Run is
minted from -- one Run per attempt entry, and none at all for a wave that
was never dispatched.

The outcome is the harder half. A zero exit status says the *process*
stopped cleanly; it does not say the agent did the work, and epoch 1 is
full of attempts that exited zero after their output failed validation.
So an exit status alone classifies nothing. A Run may claim SUCCEEDED
only when a role report binds the provider session the attempt ran under
-- the report is the evidence that an episode really terminated -- and
then, and only then, the exit status decides SUCCEEDED from FAILED.

A provider session id that several attempts or several sessions share
binds nothing, because a report naming it cannot say which episode it
reports on. Attributing it to one of them would be exactly the invented
source fact the importer refuses.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


#: The epoch-1 collection a Run is minted from, and the field on a wave
#: row that holds its dispatch attempts.
RUN_SOURCE_COLLECTION = "waves"
ATTEMPT_TABLE_FIELD = "sessions"

#: The attempt fields the classifier reads.
ATTEMPT_PROVIDER_ID_FIELD = "session_id"
ATTEMPT_EXIT_STATUS_FIELD = "exit_status"

#: The ``agent_sessions`` field that names the provider session an eawf
#: session ran under. It is the only recorded bridge between a report and
#: an attempt; deriving one from the shape of a session id instead would
#: be reading a naming convention as a fact.
SESSION_PROVIDER_ID_FIELD = "runtime_session_id"

#: Where a role report lives and what identifies it. Any store ledger
#: whose stem ends in ``_report`` is a role-report ledger.
REPORT_LEDGER_SUFFIX = "_report"
REPORT_PAYLOAD_FIELD = "payload"
REPORT_HEADER_FIELD = "header"
REPORT_SESSION_FIELD = "session_id"
REPORT_ROLE_FIELD = "role"
REPORT_ID_FIELD = "report_id"

#: The status of a Run whose outcome the source cannot classify. It is
#: terminal -- the episode is over -- and unclassified, which is a
#: different claim from either success or failure.
UNCLASSIFIED_RUN_STATUS = "TERMINAL_UNCLASSIFIED"
SUCCEEDED_RUN_STATUS = "SUCCEEDED"
FAILED_RUN_STATUS = "FAILED"

#: Both facts a Run needs before it may claim success, in the order the
#: classifier checks them.
RUN_SUCCESS_REQUIREMENTS: tuple[str, ...] = ("bound_role_report", "exit_status_zero")

#: How an attempt with no provider session id of its own is addressed.
ATTEMPT_RUN_ID_SEPARATOR = "#attempt-"


class RunSource(StrEnum):
    """The only two source facts a Run may be minted from."""

    RESOLVING_CLAIM = "resolving_claimed_wave_id"
    WAVE_ATTEMPT = "wave_attempt_entry"


class BindingRefusal(StrEnum):
    """Why a provider session id binds no role report.

    Each member is a property of the source, not of the importer: the
    corpus either never reported on the session, or reported on it in a
    way that cannot be attributed to one episode.
    """

    NO_REPORT_NAMES_THE_SESSION = "no_report_names_the_session"
    SHARED_ACROSS_ATTEMPTS = "provider_session_id_shared_across_attempts"
    SHARED_ACROSS_SESSIONS = "provider_session_id_declared_by_several_sessions"
    SEVERAL_REPORTS_NAME_THE_SESSION = "several_reports_name_the_session"


class RunBinding(StrictMigrationModel):
    """The one role report that lets an exit status mean an outcome.

    Attributes:
        provider_session_id: The provider session the report speaks for.
        session_id: The epoch-1 session row that declared that provider
            session, which is what links the report to the attempt.
        report_id: The report row's own id.
        report_role: The agent role that filed it.
    """

    provider_session_id: Annotated[str, Field(min_length=1)]
    session_id: Annotated[str, Field(min_length=1)]
    report_id: Annotated[str, Field(min_length=1)]
    report_role: Annotated[str, Field(min_length=1)]


class MintedRun(StrictMigrationModel):
    """One Run the source really supports, with the fact that supports it.

    Attributes:
        run_source: Which source fact minted this Run.
        source_id: The provider session id the episode ran under, or a
            synthetic ``<wave>#attempt-<key>`` address when the attempt
            recorded none.
        wave_id: The wave the episode belongs to.
        attempt_key: The attempt table's own key, ``None`` for a Run
            minted from a resolving claim rather than an attempt.
        exit_status: The exit status the source recorded, which is
            ``None`` when it recorded none.
        binding: The role report that classifies this Run, or ``None``.
        status: The classified outcome.
    """

    run_source: RunSource
    source_id: Annotated[str, Field(min_length=1)]
    wave_id: Annotated[str, Field(min_length=1)]
    attempt_key: str | None
    exit_status: int | None
    binding: RunBinding | None
    status: Annotated[str, Field(min_length=1)]


def classify_attempt_status(*, exit_status: int | None, binding: RunBinding | None) -> str:
    """Return the outcome one recorded attempt supports.

    Args:
        exit_status: The exit status the source recorded, or ``None``.
        binding: The role report bound to the attempt's provider session,
            or ``None`` when nothing in the source binds it.

    Returns:
        :data:`SUCCEEDED_RUN_STATUS` only for a bound report and a zero
        exit status, :data:`FAILED_RUN_STATUS` for a bound report and any
        other exit status, and :data:`UNCLASSIFIED_RUN_STATUS` whenever
        either fact is missing -- including the common case of an
        unbound attempt that exited zero.
    """
    if binding is None or exit_status is None:
        return UNCLASSIFIED_RUN_STATUS
    if exit_status == 0:
        return SUCCEEDED_RUN_STATUS
    return FAILED_RUN_STATUS


def _text(value: Any) -> str | None:
    """Return ``value`` when it is a non-empty string, else ``None``."""
    if isinstance(value, str) and value:
        return value
    return None


def _exit_status(value: Any) -> int | None:
    """Return ``value`` when it is a recorded exit status, else ``None``.

    ``bool`` is excluded even though Python counts it as an integer: a
    boolean in this slot is a mis-shaped row, not an exit status of 0
    or 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def attempt_entries(row: Mapping[str, Any]) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """Return one wave row's dispatch attempts, in sorted attempt order.

    Args:
        row: The source wave row.

    Returns:
        ``(key, attempt)`` pairs, sorted by key so two passes over one
        row agree. A wave that was never dispatched returns an empty
        tuple, which is what makes it mint no Run at all.
    """
    attempts = row.get(ATTEMPT_TABLE_FIELD)
    if not isinstance(attempts, Mapping):
        return ()
    return tuple(
        (str(key), attempts[key])
        for key in sorted(attempts, key=str)
        if isinstance(attempts[key], Mapping)
    )


def _report_header(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return one report row's header, or ``None`` when it carries none."""
    payload = report.get(REPORT_PAYLOAD_FIELD)
    if not isinstance(payload, Mapping):
        return None
    header = payload.get(REPORT_HEADER_FIELD)
    if not isinstance(header, Mapping):
        return None
    return header


class ReportBindingIndex(StrictMigrationModel):
    """Which provider session ids a role report can speak for.

    Attributes:
        bindings: Provider session id to the single report that binds it.
        refusals: Provider session id to the reason nothing binds it, for
            every provider session the corpus names and no report can be
            attributed to.
    """

    bindings: dict[str, RunBinding]
    refusals: dict[str, BindingRefusal]

    @classmethod
    def build(
        cls,
        *,
        document: Mapping[str, Any],
        report_rows: Iterable[Mapping[str, Any]],
    ) -> ReportBindingIndex:
        """Reconcile the report ledgers against the sessions and attempts.

        Args:
            document: The decoded epoch-1 state document, read for the
                ``agent_sessions`` and ``waves`` collections.
            report_rows: Every role-report ledger row, in a deterministic
                order.

        Returns:
            The index. A provider session appears in exactly one of
            :attr:`bindings` and :attr:`refusals`, never in both.
        """
        sessions_by_provider = _sessions_by_provider(document)
        attempts_by_provider = _attempts_by_provider(document)
        reports_by_session = _reports_by_session(report_rows)

        bindings: dict[str, RunBinding] = {}
        refusals: dict[str, BindingRefusal] = {}
        for provider_id in sorted(set(sessions_by_provider) | set(attempts_by_provider)):
            declaring = sessions_by_provider.get(provider_id, ())
            if len(attempts_by_provider.get(provider_id, ())) > 1:
                refusals[provider_id] = BindingRefusal.SHARED_ACROSS_ATTEMPTS
                continue
            if len(declaring) > 1:
                refusals[provider_id] = BindingRefusal.SHARED_ACROSS_SESSIONS
                continue
            reports = [
                report
                for session_id in declaring
                for report in reports_by_session.get(session_id, ())
            ]
            if not reports:
                refusals[provider_id] = BindingRefusal.NO_REPORT_NAMES_THE_SESSION
                continue
            if len(reports) > 1:
                refusals[provider_id] = BindingRefusal.SEVERAL_REPORTS_NAME_THE_SESSION
                continue
            session_id, report_id, report_role = reports[0]
            bindings[provider_id] = RunBinding(
                provider_session_id=provider_id,
                session_id=session_id,
                report_id=report_id,
                report_role=report_role,
            )
        return cls(bindings=bindings, refusals=refusals)

    def binding_for(self, provider_session_id: str) -> RunBinding | None:
        """Return the report bound to ``provider_session_id``, or ``None``.

        Args:
            provider_session_id: The provider session an attempt ran
                under.

        Returns:
            The binding, or ``None`` when nothing in the source binds it.
        """
        return self.bindings.get(provider_session_id)

    def refusal_for(self, provider_session_id: str) -> BindingRefusal | None:
        """Return why ``provider_session_id`` binds nothing, or ``None``.

        Args:
            provider_session_id: The provider session an attempt ran
                under.

        Returns:
            The refusal reason, or ``None`` when the id does bind a
            report or the corpus never named it at all.
        """
        return self.refusals.get(provider_session_id)


def _sessions_by_provider(document: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Return which epoch-1 sessions declared each provider session id."""
    sessions = document.get("agent_sessions")
    if not isinstance(sessions, Mapping):
        return {}
    declared: dict[str, list[str]] = {}
    for session_id in sorted(sessions, key=str):
        row = sessions[session_id]
        if not isinstance(row, Mapping):
            continue
        provider_id = _text(row.get(SESSION_PROVIDER_ID_FIELD))
        if provider_id is None:
            continue
        declared.setdefault(provider_id, []).append(str(session_id))
    return {provider_id: tuple(ids) for provider_id, ids in declared.items()}


def _attempts_by_provider(document: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Return which wave attempts ran under each provider session id."""
    waves = document.get(RUN_SOURCE_COLLECTION)
    if not isinstance(waves, Mapping):
        return {}
    ran: dict[str, list[str]] = {}
    for wave_id in sorted(waves, key=str):
        row = waves[wave_id]
        if not isinstance(row, Mapping):
            continue
        for key, attempt in attempt_entries(row):
            provider_id = _text(attempt.get(ATTEMPT_PROVIDER_ID_FIELD))
            if provider_id is None:
                continue
            ran.setdefault(provider_id, []).append(f"{wave_id}{ATTEMPT_RUN_ID_SEPARATOR}{key}")
    return {provider_id: tuple(keys) for provider_id, keys in ran.items()}


def _reports_by_session(
    report_rows: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Return the reports each epoch-1 session id carries, in read order."""
    filed: dict[str, list[tuple[str, str, str]]] = {}
    for report in report_rows:
        header = _report_header(report)
        if header is None:
            continue
        session_id = _text(header.get(REPORT_SESSION_FIELD))
        report_id = _text(header.get(REPORT_ID_FIELD)) or _text(report.get("id"))
        report_role = _text(header.get(REPORT_ROLE_FIELD))
        if session_id is None or report_id is None or report_role is None:
            continue
        filed.setdefault(session_id, []).append((session_id, report_id, report_role))
    return {session_id: tuple(rows) for session_id, rows in filed.items()}


def report_ledger_rows(
    ledgers: Mapping[str, tuple[Mapping[str, Any], ...]],
) -> tuple[Mapping[str, Any], ...]:
    """Return every role-report row a snapshot holds, deterministically.

    Args:
        ledgers: Every store ledger's rows, keyed by file stem.

    Returns:
        The rows of each ``*_report`` ledger, ledgers in name order and
        rows in file order, so the binding index does not depend on the
        order the snapshot happened to walk the store directory in.
    """
    return tuple(
        row
        for name in sorted(ledgers)
        if name.endswith(REPORT_LEDGER_SUFFIX)
        for row in ledgers[name]
    )


def mint_attempt_runs(
    *,
    wave_id: str,
    row: Mapping[str, Any],
    bindings: ReportBindingIndex,
) -> tuple[MintedRun, ...]:
    """Mint one Run per recorded dispatch attempt, in attempt order.

    An attempt is a fact: the source says a subprocess ran. Its outcome
    is a fact only where a role report binds the provider session it ran
    under, so an unbound attempt lands unclassified however it exited.

    Args:
        wave_id: The wave's own id, used to address a nameless attempt.
        row: The source wave row.
        bindings: The report bindings the corpus supports.

    Returns:
        The minted runs, one per attempt entry, in sorted attempt order.

    Raises:
        ValidationError: When a minted run violates its model contract.
    """
    runs: list[MintedRun] = []
    for key, attempt in attempt_entries(row):
        provider_id = _text(attempt.get(ATTEMPT_PROVIDER_ID_FIELD))
        source_id = provider_id or f"{wave_id}{ATTEMPT_RUN_ID_SEPARATOR}{key}"
        exit_status = _exit_status(attempt.get(ATTEMPT_EXIT_STATUS_FIELD))
        binding = bindings.binding_for(provider_id) if provider_id is not None else None
        runs.append(
            MintedRun(
                run_source=RunSource.WAVE_ATTEMPT,
                source_id=source_id,
                wave_id=wave_id,
                attempt_key=key,
                exit_status=exit_status,
                binding=binding,
                status=classify_attempt_status(exit_status=exit_status, binding=binding),
            )
        )
    return tuple(runs)


def mint_claim_run(*, wave_id: str, provider_session_id: str) -> MintedRun:
    """Mint the Run a resolving claim-session reference supports.

    A claim names a session the source really holds, which is a fact
    about who took the wave -- not a dispatch record. It carries no exit
    status and no report, so it is always unclassified.

    Args:
        wave_id: The claiming wave's own id.
        provider_session_id: The session id the claim resolved to.

    Returns:
        The minted run.

    Raises:
        ValidationError: When a minted run violates its model contract.
    """
    return MintedRun(
        run_source=RunSource.RESOLVING_CLAIM,
        source_id=provider_session_id,
        wave_id=wave_id,
        attempt_key=None,
        exit_status=None,
        binding=None,
        status=UNCLASSIFIED_RUN_STATUS,
    )


def run_rule_payload() -> dict[str, Any]:
    """Return the digestable form of the run-minting table."""
    return {
        "mints_run_from": [source.value for source in RunSource],
        "default_run_status": UNCLASSIFIED_RUN_STATUS,
        "succeeded_requires": list(RUN_SUCCESS_REQUIREMENTS),
        "succeeded_status": SUCCEEDED_RUN_STATUS,
        "failed_status": FAILED_RUN_STATUS,
        "binding_source_field": SESSION_PROVIDER_ID_FIELD,
        "binding_ledger_suffix": REPORT_LEDGER_SUFFIX,
        "binding_refusals": sorted(refusal.value for refusal in BindingRefusal),
    }
