"""The facts a projected row states beyond its status, read off the stored record.

A route row carries its status, its title and the record it is filed under; everything
else a console draws about it -- a Run's attempt and runtime, a Task's criteria, a
notice's question -- is a fact read here from the stored row and, through
:class:`Links`, from the records of other collections the same document holds.
:mod:`~eawf.kernel.projection.compute` builds each row and asks :func:`row_facts` for
what to state beside it, so the route bindings and the per-record reading stay apart.

A notice is a ledger line a route lists without binding its collection; its kind decides
which facts it states, and :data:`NOTICE_KINDS` is the closed set of those kinds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final

from eawf.kernel.migration.epoch2.continuation import read_legacy_row
from eawf.kernel.migration.epoch2.cutover import ROW_PAYLOAD_FIELD
from eawf.kernel.runtime.sandbox_decision import SANDBOX_DECISION_KIND
from eawf.kernel.store.tiers import Epoch2Collection

#: The payload field that marks a row the cutover converted whole into a native
#: collection, as opposed to an imported lifecycle record that continues its lifecycle.
NATIVE_IMPORT_KEY: Final = "record_key"

#: The fields a record's title is read from, in preference order, where they are not
#: simply ``title``.
TITLE_FIELDS: Final[Mapping[Epoch2Collection, tuple[str, ...]]] = MappingProxyType(
    {
        Epoch2Collection.TASK: ("title", "intent"),
        Epoch2Collection.PERMISSION: ("request_scope",),
        Epoch2Collection.OPEN_QUESTION: ("question",),
    }
)

#: The fact prefixes one principal's own disposition of a pending action is stated under,
#: each followed by that principal's key: what they answered, until when they snoozed it,
#: and when they last did either.
ANSWERED_FACT: Final = "answered."
SNOOZED_FACT: Final = "snoozed."
ACTED_FACT: Final = "acted."

#: The fact prefix a Task states each of its criteria under, followed by the criterion's
#: one-based place: ``<id> · <kind> · <gates> · <text>``, the text last so it is the
#: part a narrow cell gives up.
CRITERION_FACT: Final = "criterion."

#: Where each record names the record it is filed under, as a path into the stored row.
PARENT_FIELD: Final[Mapping[Epoch2Collection, tuple[str, ...]]] = MappingProxyType(
    {
        Epoch2Collection.MILESTONE: ("primary_track_ref",),
        Epoch2Collection.BATCH: ("milestone_ref",),
        Epoch2Collection.TASK: ("batch_ref",),
        Epoch2Collection.RUN: ("scope", "task_ref"),
    }
)


#: The field a row spelled back for a replay carries its facts in. A stored document row
#: never states it, so a fresh read derives every fact again, while a replay -- whose
#: document holds only the rows the client already had -- keeps the facts it held rather
#: than losing the ones read off another collection.
FACTS_FIELD: Final = "projected_facts"

#: The instants a Run states about itself, read verbatim as the document stores them.
_RUN_INSTANTS: Final = ("created_at", "started_at", "ended_at", "updated_at")

#: The fact each member of a Run's runtime tuple is stated under, by member name.
RUNTIME_FACTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "harness": "runtime_harness",
        "harness_version": "runtime_version",
        "provider": "runtime_provider",
        "model": "runtime_model",
    }
)

#: The fact a Run's vendor session digest is stated under.
SESSION_FACT: Final = "session"

#: The Run statuses that are an outcome: the ones a Run never leaves.
_RUN_OUTCOMES: Final = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

#: The payload kind of a child Run admitted past a ``child_runs`` ceiling, as the run
#: ledger files it. It is a notice: it records an overrun nobody can answer.
CEILING_BREACH_KIND: Final = "child_ceiling_breach"

#: The payload kind of a stall fact, as the run ledger files it: a running Run went quiet
#: past its interval. Attention lists each one still standing as a stalled item.
STALL_KIND: Final = "run_stall"

#: The kind of a verdict observation: one independent audit verdict a Batch's
#: verification cycle holds, read off the Batch ledger with the producer that reached it.
#: It is derived at read time and never stored beside the Milestone it is listed under.
VERDICT_OBSERVATION_KIND: Final = "verdict_observation"

#: The kind of the jury calibration Trust draws: the validation report over the labelled
#: cohort of verdicts and the authority the calibration gate returned for it. It is scored
#: when the route is served and never stored.
JURY_CALIBRATION_KIND: Final = "jury_calibration"

#: The key the one jury calibration row is listed under among the verdicts.
CALIBRATION_KEY: Final = "jury-calibration"

#: The kind of one juror's score: the validation report over the labelled verdicts that
#: one ``(agent_role, runtime)`` cast. It is scored beside the calibration and never stored.
JUROR_SCORE_KIND: Final = "juror_score"

#: The key prefix each juror score row is listed under among the verdicts.
JUROR_KEY_PREFIX: Final = "jury-juror-"

#: The kind of a Run Attention lists by its state: a failed Run that is still the newest
#: attempt of an open Task, or a Run running with no stall over it.
RUN_STATE_KIND: Final = "run_state"

#: The payload kind of a proof receipt as the receipt ledger files it: one gate run over
#: a Task's criteria, with the result it reached.
PROOF_RECEIPT_KIND: Final = "proof_receipt"

#: The kinds a notice row may be. Each is a line of a ledger a route lists from without
#: binding the collection: a row of any other kind is a record of that collection.
NOTICE_KINDS: Final = frozenset(
    {
        CEILING_BREACH_KIND,
        STALL_KIND,
        SANDBOX_DECISION_KIND,
        VERDICT_OBSERVATION_KIND,
        JURY_CALIBRATION_KIND,
        JUROR_SCORE_KIND,
        RUN_STATE_KIND,
        PROOF_RECEIPT_KIND,
    }
)


def notice_kind(row: Any) -> str | None:
    """Return the notice kind a stored row is, or ``None`` when it is a record of its collection.

    The ledger line states its payload kind; a row spelled back for a replay states the
    kind among the facts it was projected with.
    """
    if not isinstance(row, dict):
        return None
    carried = row.get(FACTS_FIELD)
    kind = carried.get("kind") if isinstance(carried, dict) else row.get("payload_kind")
    return kind if kind in NOTICE_KINDS else None


def title_of(collection: Epoch2Collection, fields: Mapping[str, Any]) -> str | None:
    """Return the title a stored record states, or ``None`` when it states none.

    A Task names itself by its intent rather than a title, so its intent is its title.
    """
    for name in TITLE_FIELDS.get(collection, ("title",)):
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parent_key_of(collection: Epoch2Collection, fields: Mapping[str, Any]) -> str | None:
    """Return the key of the record ``fields`` is filed under, or ``None`` when none is stated.

    The reference is a URN whose last path segment is the entity key, which is the key the
    parent's own row is stored under.
    """
    path = PARENT_FIELD.get(collection)
    if path is None:
        return None
    value: Any = fields
    for name in path:
        value = value.get(name) if isinstance(value, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.rstrip("/").rsplit("/", 1)[-1] or None


#: The collections a record's containment chain climbs through, each to the next, in the
#: order the fields in :data:`PARENT_FIELD` name them.
_CHAIN: Final[Mapping[Epoch2Collection, Epoch2Collection]] = MappingProxyType(
    {
        Epoch2Collection.RUN: Epoch2Collection.TASK,
        Epoch2Collection.TASK: Epoch2Collection.BATCH,
        Epoch2Collection.BATCH: Epoch2Collection.MILESTONE,
        Epoch2Collection.MILESTONE: Epoch2Collection.TRACK,
    }
)


def _text(value: Any) -> str | None:
    """Return ``value`` stripped when it is a non-blank string, else ``None``."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _key_of(ref: Any) -> str | None:
    """Return the entity key a URN reference ends in, or ``None`` when it states none."""
    text = _text(ref)
    if text is None:
        return None
    return text.rstrip("/").rsplit("/", 1)[-1] or None


class Links:
    """The document's other records, looked up by key while one projection is built.

    A frame names the Task a Run runs, its status and the attempt the Run is; a Track
    counts the Runs under it. Those facts sit on records of other collections, so they are
    read here once per build rather than by the console, which holds only the route's own
    rows. Lookups are by key and cached, so a build reads each record at most once.
    """

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document
        self._fields: dict[tuple[Epoch2Collection, str], tuple[Mapping[str, Any], str | None]] = {}
        self._attempts: dict[str, list[str]] | None = None
        self._runs_under: dict[str, int] | None = None

    def _rows(self, collection: Epoch2Collection) -> Mapping[str, Any]:
        rows = self._document.get(collection.value)
        return rows if isinstance(rows, dict) else {}

    def fields(
        self, collection: Epoch2Collection, key: str
    ) -> tuple[Mapping[str, Any], str | None]:
        """Return a record's stored fields and its status; empty when it is not held."""
        found = self._fields.get((collection, key))
        if found is None:
            found = stored_fields(collection, key, self._rows(collection).get(key))
            self._fields[(collection, key)] = found
        return found

    def parent(self, collection: Epoch2Collection, key: str) -> str | None:
        """Return the key of the record ``key`` is filed under, when it names one."""
        return parent_key_of(collection, self.fields(collection, key)[0])

    def chain(self, collection: Epoch2Collection, key: str) -> dict[str, str]:
        """Return every record ``key`` is filed under, keyed by collection name."""
        chain: dict[str, str] = {}
        at: tuple[Epoch2Collection, str] | None = (collection, key)
        while at is not None and at[0] in _CHAIN:
            parent = self.parent(*at)
            if parent is None:
                break
            at = (_CHAIN[at[0]], parent)
            chain[at[0].value] = parent
        return chain

    def attempts(self, task: str) -> list[str]:
        """Return the Runs of ``task`` in the order they were created, oldest first."""
        if self._attempts is None:
            by_task: dict[str, list[tuple[str, str]]] = {}
            for key, row in self._rows(Epoch2Collection.RUN).items():
                fields, _status = self.fields(Epoch2Collection.RUN, key)
                owner = parent_key_of(Epoch2Collection.RUN, fields)
                if owner is not None and isinstance(row, dict):
                    by_task.setdefault(owner, []).append(
                        (_text(fields.get("created_at")) or "", key)
                    )
            self._attempts = {
                task: [k for _at, k in sorted(runs)] for task, runs in by_task.items()
            }
        return self._attempts.get(task, [])

    def runs_under(self, key: str) -> int:
        """Return how many Runs are filed, through their Task, under the record ``key``."""
        if self._runs_under is None:
            tally: dict[str, int] = {}
            for run in self._rows(Epoch2Collection.RUN):
                for owner in self.chain(Epoch2Collection.RUN, run).values():
                    tally[owner] = tally.get(owner, 0) + 1
            self._runs_under = tally
        return self._runs_under.get(key, 0)


def stored_fields(
    collection: Epoch2Collection, key: str, row: Any
) -> tuple[Mapping[str, Any], str | None]:
    """Return the fields a stored row states and its status, whatever form it was stored in.

    A native record states its fields directly; an imported one states them inside the
    record the cutover wrapped, and its status beside it. A row that is neither states
    nothing a fact could be read from.
    """
    if not isinstance(row, dict):
        return {}, None
    if "urn" in row or ROW_PAYLOAD_FIELD not in row:
        return row, _text(row.get("status"))
    payload = row[ROW_PAYLOAD_FIELD]
    if isinstance(payload, dict) and NATIVE_IMPORT_KEY in payload:
        return {}, _text(row.get("status"))
    try:
        legacy = read_legacy_row(collection, key, row)
    except ValueError:
        return {}, None
    return legacy.record.record, _text(legacy.status)


def _run_facts(
    key: str, fields: Mapping[str, Any], status: str | None, links: Links
) -> dict[str, str]:
    """Return what the document states about a Run, its Task and its place among attempts.

    A Run whose stored status is one it never leaves states that status as its outcome.
    """
    facts = {name: _text(fields.get(name)) for name in _RUN_INSTANTS}
    scope = fields.get("scope")
    facts["purpose"] = _text(scope.get("purpose")) if isinstance(scope, dict) else None
    failure = fields.get("failure")
    if isinstance(failure, dict):
        facts["failure"] = _text(failure.get("message"))
        facts["failure_code"] = _text(failure.get("code"))
    runtime = fields.get("runtime_tuple")
    if isinstance(runtime, dict):
        facts.update({fact: _text(runtime.get(name)) for name, fact in RUNTIME_FACTS.items()})
    session = fields.get("vendor_session")
    if isinstance(session, dict):
        facts[SESSION_FACT] = _text(session.get("session_digest"))
    if status in _RUN_OUTCOMES:
        facts["outcome"] = status
    task = parent_key_of(Epoch2Collection.RUN, fields)
    if task is not None:
        task_fields, task_status = links.fields(Epoch2Collection.TASK, task)
        facts["task_title"] = title_of(Epoch2Collection.TASK, task_fields)
        facts["task_status"] = task_status
        facts.update(links.chain(Epoch2Collection.TASK, task))
        attempts = links.attempts(task)
        if key in attempts:
            facts["attempt"] = str(attempts.index(key) + 1)
            facts["attempts"] = str(len(attempts))
    return {name: value for name, value in facts.items() if value}


def _breach_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a ceiling breach states: whose ceiling, which child, and by how much.

    The subject is the Run whose ceiling was passed, which is where the delegation that
    overran it was made.
    """
    child = _key_of(fields.get("child_run_ref"))
    ceiling, descendants = fields.get("ceiling"), fields.get("descendants")
    stated = isinstance(ceiling, int) and isinstance(descendants, int) and child is not None
    facts = {
        "kind": CEILING_BREACH_KIND,
        "subject": _key_of(fields.get("ancestor_run_ref")),
        "child": child,
        "question": (
            f"{child} made the subtree {descendants} Runs past child_runs={ceiling}"
            if stated
            else None
        ),
        "recorded_at": _text(fields.get("recorded_at")),
    }
    return {name: value for name, value in facts.items() if value}


def _stall_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a stall states: which Run went quiet, since when, and against what interval.

    The subject is the Run; a Run that never produced anything was silent since it started.
    """
    since = _text(fields.get("last_activity_at"))
    elapsed, interval = fields.get("elapsed_seconds"), fields.get("interval_seconds")
    question = None
    if isinstance(elapsed, int | float) and isinstance(interval, int) and since is not None:
        question = f"stopped responding · silent {int(elapsed)}s past its {interval}s interval"
    what = _text(fields.get("last_activity_kind"))
    facts = {
        "kind": STALL_KIND,
        "subject": _key_of(fields.get("run_ref")),
        "question": question,
        "last_activity_at": since,
        "last_activity": what.replace("_", " ") if what else "nothing since it started",
        "raised_at": _text(fields.get("raised_at")),
    }
    return {name: value for name, value in facts.items() if value}


def _decision_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a sandbox decision states: its Run, its outcome and the rule that decided.

    The call's raw target is not among them: a decision never carries it.
    """
    revision = fields.get("policy_revision")
    facts = {
        "kind": SANDBOX_DECISION_KIND,
        "run": _key_of(fields.get("run_ref")),
        "decision": _text(fields.get("decision")),
        "reason": _text(fields.get("reason")),
        "rule": _text(fields.get("rule")),
        "rule_value": _text(fields.get("rule_value")),
        "policy_revision": str(revision) if isinstance(revision, int) else None,
        "decided_at": _text(fields.get("decided_at")),
    }
    return {name: value for name, value in facts.items() if value}


def _verdict_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a verdict observation states: its site, subject, verdict and producer.

    The producer is answered for as ``(agent_role, runtime)``; a part no record states
    reads unknown in its place rather than dropping the pair.
    """
    role, runtime = _text(fields.get("agent_role")), _text(fields.get("runtime"))
    facts = {
        "kind": VERDICT_OBSERVATION_KIND,
        "site": _text(fields.get("site")),
        "subject": _text(fields.get("subject")),
        "batch": _key_of(fields.get("batch_ref")),
        "milestone": _key_of(fields.get("milestone_ref")),
        "verdict": _text(fields.get("verdict")),
        "agent_role": role,
        "runtime": runtime,
        "occurred_at": _text(fields.get("occurred_at")),
        "answered_by": f"{role or '? unknown'} · {runtime or '? unknown'}",
    }
    if facts["verdict"] and facts["subject"]:
        facts["question"] = f"audit found {facts['verdict']} on {facts['subject']}"
    return {name: value for name, value in facts.items() if value}


def _run_state_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a Run listed by its state says: its Task and why it is listed."""
    failure = fields.get("failure")
    said = _text(failure.get("message")) if isinstance(failure, dict) else None
    status = _text(fields.get("status"))
    facts = {
        "kind": RUN_STATE_KIND,
        "subject": parent_key_of(Epoch2Collection.RUN, fields),
        # a failed Run is listed with its reason; a replayed row states none and keeps its own
        "question": (f"failed · {said}" if said else None)
        if status == "FAILED"
        else (status or "").lower(),
        "started_at": _text(fields.get("started_at")),
    }
    return {name: value for name, value in facts.items() if value}


def _proof_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a proof receipt states: its Task, its gate and the criteria it answered."""
    receipt = fields.get("receipt")
    held = receipt if isinstance(receipt, dict) else {}
    criteria = held.get("criterion_ids")
    facts = {
        "kind": PROOF_RECEIPT_KIND,
        "task": _key_of(fields.get("task_ref")),
        "receipt": _text(held.get("id")),
        "gate": _text(held.get("gate_id")),
        "criteria": ",".join(c for c in criteria if isinstance(c, str))
        if isinstance(criteria, list)
        else None,
        "result": _text(held.get("result")),
        "ended_at": _text(held.get("ended_at")),
    }
    return {name: value for name, value in facts.items() if value}


def _calibration_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a jury calibration states: its cohort, its metrics and the authority.

    A metric the report left undefined is absent, never zero.
    """
    facts = {
        "kind": JURY_CALIBRATION_KIND,
        **{
            name: str(value)
            for name in ("cohort", "known_bad", "min_scored", "brier", "co_error")
            if isinstance(value := fields.get(name), int | float) and not isinstance(value, bool)
        },
        "authority": _text(fields.get("authority")),
    }
    return {name: value for name, value in facts.items() if value}


def _juror_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a juror score states: the juror, its scored count and its Brier score.

    A juror under the cohort floor states no Brier score, never a zero one.
    """
    facts = {
        "kind": JUROR_SCORE_KIND,
        "agent_role": _text(fields.get("agent_role")),
        "runtime": _text(fields.get("runtime")),
        **{
            name: str(value)
            for name in ("cohort", "brier")
            if isinstance(value := fields.get(name), int | float) and not isinstance(value, bool)
        },
    }
    return {name: value for name, value in facts.items() if value}


#: What each notice kind states about itself, by kind.
_NOTICE_FACTS: Final[Mapping[str, Callable[[Mapping[str, Any]], dict[str, str]]]] = (
    MappingProxyType(
        {
            CEILING_BREACH_KIND: _breach_facts,
            STALL_KIND: _stall_facts,
            SANDBOX_DECISION_KIND: _decision_facts,
            VERDICT_OBSERVATION_KIND: _verdict_facts,
            JURY_CALIBRATION_KIND: _calibration_facts,
            JUROR_SCORE_KIND: _juror_facts,
            RUN_STATE_KIND: _run_state_facts,
            PROOF_RECEIPT_KIND: _proof_facts,
        }
    )
)


def _permission_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a provider permission states: its Run, its deadline and who may decide it.

    The approve and deny authorities are stated apart, because they need not be one
    class, and whether the repository may approve is stated as the record resolved it,
    so a surface renders that affordance disabled with the approving classes named.
    """
    authority = fields.get("approval_authority")
    classes = authority if isinstance(authority, dict) else {}
    may = fields.get("repository_may_approve")
    facts = {
        "kind": "provider_permission",
        "subject": _key_of(fields.get("run_ref")),
        "question": _text(fields.get("request_scope")),
        "tool": _text(fields.get("tool_id")),
        "action_class": _text(fields.get("action_class")),
        "deadline_at": _text(fields.get("deadline_at")),
        "deadline_owner": _text(fields.get("deadline_owner")),
        "approve": ", ".join(str(c) for c in classes.get("approve", ())),
        "deny": ", ".join(str(c) for c in classes.get("deny", ())),
        "repository_may_approve": ("yes" if may else "no") if isinstance(may, bool) else None,
    }
    return {name: value for name, value in facts.items() if value}


def _question_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what an open question states: what it was asked under, when, and what it offers."""
    options = fields.get("options")
    facts = {
        "kind": "question",
        "subject": _key_of(fields.get("scope_ref")),
        "question": _text(fields.get("question")),
        "blocking": "yes" if fields.get("blocking") is True else None,
        "options": str(len(options)) if isinstance(options, list | tuple) and options else None,
        "created_at": _text(fields.get("created_at")),
    }
    return {name: value for name, value in facts.items() if value}


def _action_facts(fields: Mapping[str, Any], links: Links) -> dict[str, str]:
    """Return what the document states about a pending action and where its subject sits."""
    requested = fields.get("requested_by")
    subject_ref = _text(fields.get("subject_ref"))
    facts = {
        "kind": _text(fields.get("kind")),
        "question": _text(fields.get("question")),
        "requested_by": (
            _text(requested.get("principal_id")) if isinstance(requested, dict) else None
        ),
        "created_at": _text(fields.get("created_at")),
        "subject": _key_of(subject_ref),
    }
    rows = fields.get("dispositions")
    for row in rows if isinstance(rows, list) else ():
        who = row.get("principal_id") if isinstance(row, dict) else None
        if not isinstance(who, str):
            continue
        outcome, option = row.get("outcome"), row.get("option_id")
        facts[f"{ANSWERED_FACT}{who}"] = f"{outcome} {option}" if outcome and option else None
        facts[f"{SNOOZED_FACT}{who}"] = _text(row.get("snoozed_until"))
        facts[f"{ACTED_FACT}{who}"] = _text(row.get("acted_at"))
    if subject_ref is not None:
        kind = subject_ref.rstrip("/").rsplit("/", 2)
        collection = next(
            (c for c in Epoch2Collection if len(kind) == 3 and c.value == kind[1]), None
        )
        subject = facts["subject"]
        if collection is not None and subject is not None:
            facts["subject_kind"] = collection.value
            if collection in _CHAIN or collection is Epoch2Collection.TRACK:
                facts[collection.value] = subject
            facts.update(links.chain(collection, subject))
    return {name: value for name, value in facts.items() if value}


def _batch_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a Batch states about where it merges and the exact head it was bound at.

    A merging Batch is drawn as the facts it holds beside the questions it cannot answer,
    so its branch, its bound head and the instant it last moved are read here verbatim.
    """
    binding = fields.get("current_head_binding")
    failure = fields.get("failure")
    tasks = fields.get("task_refs")
    facts = {
        "target_branch": _text(fields.get("target_branch")),
        "head": _text(binding.get("head_sha")) if isinstance(binding, dict) else None,
        "updated_at": _text(fields.get("updated_at")),
        "failure": _text(failure.get("message")) if isinstance(failure, dict) else None,
        "tasks": str(len(tasks)) if isinstance(tasks, list | tuple) else None,
    }
    return {name: value for name, value in facts.items() if value}


def _task_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a stored Task states beyond its status, blanks left out.

    The graph fields travel with the row so a coordinator derives the
    concurrency plan from the Tasks it reads rather than being told one.
    """
    stated = {
        "due": _key_of(fields.get("due_scope")),
        "updated_at": _text(fields.get("updated_at")),
        "priority": _text(fields.get("priority")),
        "run": _key_of(fields.get("active_run_ref")),
    }
    binding = fields.get("integrated_binding")
    if isinstance(binding, dict):
        stated["integrated"] = _text(binding.get("head_sha"))
    criteria = fields.get("criteria")
    if isinstance(criteria, list | tuple) and criteria:
        stated["criteria"] = str(len(criteria))
        for place, criterion in enumerate(criteria, start=1):
            stated[f"{CRITERION_FACT}{place}"] = _criterion_text(criterion)
    depends_on = fields.get("depends_on")
    if isinstance(depends_on, list | tuple):
        stated["depends_on"] = ",".join(key for ref in depends_on if (key := _key_of(ref)))
    claims = fields.get("write_claims")
    if isinstance(claims, list | tuple):
        stated["write_claims"] = ",".join(claim for item in claims if (claim := _text(item)))
    if fields.get("exclusive") is True:
        stated["exclusive"] = "true"
    return {name: value for name, value in stated.items() if value}


def _criterion_text(criterion: Any) -> str | None:
    """Return one stored criterion as its fact: ``<id> · <kind> · <gates> · <text>``."""
    if not isinstance(criterion, dict):
        return None
    gates = criterion.get("gate_ids")
    named = [gate for gate in gates if isinstance(gate, str)] if isinstance(gates, list) else []
    parts = (
        _text(criterion.get("id")) or "?",
        _text(criterion.get("kind")) or "?",
        f"gates {','.join(named)}" if named else "no gate",
        _text(criterion.get("text")) or "?",
    )
    return " · ".join(parts)


def _placement_facts(
    key: str, fields: Mapping[str, Any], stored: Any, links: Links
) -> dict[str, str]:
    """Return what a Track or Milestone states about where it sits: its Runs and its date.

    A replayed row keeps the Run count it was projected with unless Runs are found again.
    """
    facts: dict[str, str] = {}
    runs = links.runs_under(key)
    if runs or FACTS_FIELD not in (stored if isinstance(stored, dict) else {}):
        facts["runs"] = str(runs)
    target = _text(fields.get("target_date"))
    if target is not None:
        facts["target_date"] = target
    return facts


def row_facts(collection: Epoch2Collection, key: str, stored: Any, links: Links) -> dict[str, str]:
    """Return the facts the document states about one stored row, beyond its status.

    A row spelled back for a replay carries the facts it was projected with; those are
    kept wherever the rebuilt document can no longer state them afresh.
    """
    carried = stored.get(FACTS_FIELD) if isinstance(stored, dict) else None
    facts = {
        name: value
        for name, value in (carried.items() if isinstance(carried, dict) else ())
        if isinstance(name, str) and _text(value)
    }
    fields, status = stored_fields(collection, key, stored)
    notice = notice_kind(stored)
    if notice is not None:
        facts.update(_NOTICE_FACTS[notice](fields))
    elif collection is Epoch2Collection.RUN:
        facts.update(_run_facts(key, fields, status, links))
    elif collection is Epoch2Collection.PENDING_ACTION:
        facts.update(_action_facts(fields, links))
    elif collection is Epoch2Collection.PERMISSION:
        facts.update(_permission_facts(fields))
    elif collection is Epoch2Collection.OPEN_QUESTION:
        facts.update(_question_facts(fields))
    elif collection is Epoch2Collection.TASK:
        facts.update(_task_facts(fields))
    elif collection is Epoch2Collection.BATCH:
        facts.update(_batch_facts(fields))
    elif collection in (Epoch2Collection.TRACK, Epoch2Collection.MILESTONE):
        facts.update(_placement_facts(key, fields, stored, links))
    return facts


__all__ = [
    "ACTED_FACT",
    "ANSWERED_FACT",
    "CALIBRATION_KEY",
    "CEILING_BREACH_KIND",
    "CRITERION_FACT",
    "FACTS_FIELD",
    "JUROR_KEY_PREFIX",
    "JUROR_SCORE_KIND",
    "JURY_CALIBRATION_KIND",
    "NATIVE_IMPORT_KEY",
    "NOTICE_KINDS",
    "PARENT_FIELD",
    "PROOF_RECEIPT_KIND",
    "RUNTIME_FACTS",
    "RUN_STATE_KIND",
    "SESSION_FACT",
    "SNOOZED_FACT",
    "STALL_KIND",
    "TITLE_FIELDS",
    "VERDICT_OBSERVATION_KIND",
    "Links",
    "notice_kind",
    "parent_key_of",
    "row_facts",
    "stored_fields",
    "title_of",
]
