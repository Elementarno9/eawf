"""JSON Schema projection of the auditor report body for a schema-forced spawn.

The durable close auditor kept failing the strict report body on free-form
JSON -- an extra ``findings`` list, a paraphrased criterion echo, a numeric
confidence -- and every failure burned one of the three bounded re-asks, so a
close ended as an infrastructure retry rather than a verdict. Forcing the
answer through a schema removes that class outright: the runtime constrains
what the model may emit instead of a prompt paragraph asking for it.

The rendered schema is written to the INTERSECTION of two dialects, because
one spawn crosses two validators that disagree:

- the ``claude`` CLI checks ``--json-schema`` with Ajv in draft-07 strict mode,
  so ``prefixItems`` is an unknown keyword and every subschema wants an
  explicit ``type`` beside ``properties`` / ``items`` / ``required``; and
- the API then re-checks the same document as draft 2020-12, which rejects the
  draft-07 tuple form of ``items`` and refuses ``anyOf`` / ``oneOf`` / ``allOf``
  at the top level.

What survives both: uniform ``items``, ``contains``, nested ``anyOf`` /
``allOf``, and a single top-level ``if`` / ``then`` / ``else``. A per-POSITION
row constraint is therefore not expressible, so the criterion rows are pinned
as a SET instead: every row must match one of the per-criterion branches, and
the exact row count plus one ``contains`` per criterion text forces a bijection
by pigeonhole. Row ORDER is left free, and the report parser restores it before
the verbatim echo check reads each row against its own criterion.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any, get_args

from eawf.kernel.spec.common import EvidenceKind
from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.kernel.store.kinds.agent_report import AgentReportFollowup

logger = logging.getLogger(__name__)

#: One required criterion as the schema needs it: the exact text the answering
#: row must echo, and the GateReceipt store URNs that row must cite. The URN
#: tuple is empty for a criterion with no deterministic receipt binding.
type CriterionSchemaRow = tuple[str, tuple[str, ...]]

#: String bounds the report body models pin. They are mirrored here so the
#: forced schema admits nothing the body model would then reject; a unit test
#: reads each one back off its model field, so drift on either side reds.
_MAX_SUMMARY_CHARS: int = 4000
_MAX_CRITERION_CHARS: int = 500
_MAX_NOTE_CHARS: int = 240
_MAX_FOLLOWUP_TITLE_CHARS: int = 160
_MAX_FOLLOWUP_DETAIL_CHARS: int = 500

_EVIDENCE_KINDS: tuple[str, ...] = get_args(EvidenceKind)
_FOLLOWUP_PRIORITIES: tuple[str, ...] = get_args(
    AgentReportFollowup.model_fields["priority"].annotation
)

#: Verdicts the durable aggregate rule reads as close-ready. Mirrors the close
#: gate's own set; the schema and the gate are held in agreement by the unit
#: test that walks every verdict against every pass / fail row combination.
_CLOSE_READY_VERDICTS: tuple[str, ...] = (
    AgentReportVerdict.PASS.value,
    AgentReportVerdict.PASS_WITH_FOLLOWUPS.value,
)


def _closed_object(*, properties: dict[str, Any], required: Sequence[str]) -> dict[str, Any]:
    """Return an object schema that admits *properties* and nothing else."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
    }


def _evidence_ref_schema() -> dict[str, Any]:
    """Return the closed schema of one evidence reference."""
    return _closed_object(
        properties={
            "kind": {"type": "string", "enum": list(_EVIDENCE_KINDS)},
            "ref": {"type": "string", "minLength": 1},
            "note": {"type": "string", "maxLength": _MAX_NOTE_CHARS},
        },
        required=["kind", "ref"],
    )


def _followup_schema() -> dict[str, Any]:
    """Return the closed schema of one report follow-up."""
    return _closed_object(
        properties={
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_FOLLOWUP_TITLE_CHARS,
            },
            "owner_role": {"type": "string", "enum": [role.value for role in AgentSessionRole]},
            "priority": {"type": "string", "enum": list(_FOLLOWUP_PRIORITIES)},
            "detail": {"type": "string", "maxLength": _MAX_FOLLOWUP_DETAIL_CHARS},
        },
        required=["title"],
    )


def _criterion_row_schema(text: str, receipt_urns: Sequence[str]) -> dict[str, Any]:
    """Return the closed schema of the single row that answers one criterion.

    Args:
        text: The exact criterion text the row's ``criterion`` is pinned to.
        receipt_urns: GateReceipt store URNs the row must cite; empty when the
            criterion carries no deterministic receipt binding.

    Returns:
        The row schema.
    """
    evidence: dict[str, Any] = {
        "type": "array",
        "minItems": 1,
        "items": _evidence_ref_schema(),
    }
    if receipt_urns:
        evidence["contains"] = {
            "type": "object",
            "required": ["kind", "ref"],
            "properties": {
                "kind": {"type": "string", "const": "store_record"},
                "ref": {"type": "string", "enum": list(receipt_urns)},
            },
        }
    return _closed_object(
        properties={
            "criterion": {"type": "string", "const": text},
            "passed": {"type": "boolean"},
            "evidence_refs": evidence,
        },
        required=["criterion", "passed", "evidence_refs"],
    )


def _criteria_schema(criteria: Sequence[CriterionSchemaRow]) -> dict[str, Any]:
    """Return the criteria-array schema: one row per criterion, order free.

    Args:
        criteria: The required criteria, in order.

    Returns:
        An array schema fixed at ``len(criteria)`` rows whose admitted bodies
        carry exactly one row per criterion in any order.
    """
    count = len(criteria)
    schema: dict[str, Any] = {"type": "array", "minItems": count, "maxItems": count}
    if not criteria:
        return schema
    schema["items"] = {
        "type": "object",
        "anyOf": [_criterion_row_schema(text, urns) for text, urns in criteria],
    }
    # Exactly `count` rows, each echoing one of `count` distinct texts, with
    # every text present at least once: by pigeonhole each appears exactly once.
    # This is the only way to say "one row per criterion" in a dialect with no
    # per-position row constraint.
    schema["allOf"] = [
        {
            "type": "array",
            "contains": {
                "type": "object",
                "required": ["criterion"],
                "properties": {"criterion": {"type": "string", "const": text}},
            },
        }
        for text, _urns in criteria
    ]
    return schema


def _aggregate_agreement_schema() -> dict[str, Any]:
    """Return the top-level branch tying the verdict to its criterion rows.

    A close-ready verdict requires every row to pass and forbids refutations; a
    non-close-ready verdict requires at least one failing row. It is carried by
    ``if`` / ``then`` / ``else`` because the API refuses a top-level ``anyOf``.
    """
    return {
        "if": {
            "type": "object",
            "required": ["verdict"],
            "properties": {"verdict": {"type": "string", "enum": list(_CLOSE_READY_VERDICTS)}},
        },
        "then": {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["passed"],
                        "properties": {"passed": {"type": "boolean", "const": True}},
                    },
                },
                "refutations": {"type": "array", "maxItems": 0},
            },
        },
        "else": {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "contains": {
                        "type": "object",
                        "required": ["passed"],
                        "properties": {"passed": {"type": "boolean", "const": False}},
                    },
                }
            },
        },
    }


def auditor_report_json_schema(
    *,
    target_id: str,
    criteria: Sequence[CriterionSchemaRow],
) -> dict[str, Any] | None:
    """Return the forced JSON schema for one durable auditor report body.

    Every admitted document is a body the durable report parser accepts: the
    role, the target, the criterion echoes, the per-row receipt citation and
    the verdict-to-rows agreement are all pinned, and every object level is
    closed, so an extra field is refused by the runtime rather than by a
    re-ask.

    Args:
        target_id: The wave id the body's ``target_id`` is pinned to.
        criteria: The required criteria, in order, as ``(text, receipt_urns)``
            pairs.

    Returns:
        The schema, or ``None`` when the criteria cannot be pinned exactly --
        two criteria carrying the same text (the dialect has no per-text row
        count) or a text longer than the body model admits. The caller then
        spawns unforced and the bounded re-ask loop carries the close.
    """
    texts = [text for text, _urns in criteria]
    if len(set(texts)) != len(texts) or any(len(text) > _MAX_CRITERION_CHARS for text in texts):
        logger.info(f"auditor_report_json_schema target={target_id!r} status=skip-unpinnable")
        return None
    schema = _closed_object(
        properties={
            "role": {"type": "string", "const": "auditor"},
            "verdict": {
                "type": "string",
                "enum": [verdict.value for verdict in AgentReportVerdict],
            },
            "confidence": {
                "type": "string",
                "enum": [confidence.value for confidence in Confidence],
            },
            "summary": {"type": "string", "minLength": 1, "maxLength": _MAX_SUMMARY_CHARS},
            "target_id": {"type": "string", "const": target_id},
            "criteria": _criteria_schema(criteria),
            "refutations": {"type": "array", "items": {"type": "string"}},
            "evidence_refs": {"type": "array", "items": _evidence_ref_schema()},
            "followups": {"type": "array", "items": _followup_schema()},
        },
        required=["role", "verdict", "confidence", "summary", "target_id", "criteria"],
    )
    schema.update(_aggregate_agreement_schema())
    return schema


#: An auditor that has seen a criterion next to its deterministic flag tends to
#: echo both. The flag is not part of the criterion, so exactly one trailing
#: annotation is dropped before the verbatim comparison; a second annotation, a
#: different spelling or any other edit still fails it.
_DETERMINISTIC_ANNOTATION: re.Pattern[str] = re.compile(r" ?\(deterministic=(?:true|false)\)\Z")


def strip_deterministic_annotation(echo: str) -> str:
    """Return *echo* without one trailing ``(deterministic=...)`` annotation."""
    return _DETERMINISTIC_ANNOTATION.sub("", echo, count=1)


def reorder_criterion_rows(raw: object, *, texts: Sequence[str]) -> object:
    """Return *raw* with its criterion rows put back in the required order.

    The forced schema pins WHICH criterion text each row carries but cannot pin
    the order they arrive in, so an auditor may answer the criteria in any
    order. When the rows echo each required criterion exactly once, that order
    carries no information: restoring it lets the verbatim check read every row
    against its own criterion. A body that does not cover *texts* exactly comes
    back untouched, so a coverage gap still surfaces from that check.

    Args:
        raw: The JSON-decoded auditor answer.
        texts: The required criterion texts, in the order the rows must land.

    Returns:
        *raw* with reordered rows, or *raw* itself when no reorder applies.
    """
    if not isinstance(raw, dict):
        return raw
    rows = raw.get("criteria")
    if not isinstance(rows, list) or len(rows) != len(texts):
        return raw
    echoes = [
        {row["criterion"], strip_deterministic_annotation(row["criterion"])}
        if isinstance(row, dict) and isinstance(row.get("criterion"), str)
        else set[str]()
        for row in rows
    ]
    unused = list(range(len(rows)))
    ordered: list[object] = []
    for text in texts:
        index = next((i for i in unused if text in echoes[i]), None)
        if index is None:
            return raw
        unused.remove(index)
        ordered.append(rows[index])
    if ordered == rows:
        return raw
    logger.info(f"reorder_criterion_rows rows={len(rows)} status=reordered")
    return {**raw, "criteria": ordered}


__all__ = [
    "CriterionSchemaRow",
    "auditor_report_json_schema",
    "reorder_criterion_rows",
    "strip_deterministic_annotation",
]
