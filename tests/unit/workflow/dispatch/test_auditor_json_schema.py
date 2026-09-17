"""Tests: the forced JSON schema a durable close auditor's answer is bound to.

The durable auditor is spawned with ``--json-schema``, so the report body is
constrained by the runtime instead of requested in prose. These tests pin the
two halves that makes load-bearing:

- the schema is accepted by BOTH validators the spawn crosses -- Ajv in
  draft-07 strict mode inside the claude CLI, and the draft 2020-12 check the
  API runs -- and stays inside the keyword intersection those two leave (no
  ``prefixItems``, no top-level ``anyOf`` / ``oneOf`` / ``allOf``); and
- every body the schema admits is a body
  :func:`~eawf.workflow.dispatch.verdict.parse_auditor_report_body` accepts, so
  a forced answer never lands in the bounded re-ask loop.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any

import pytest
from jsonschema import Draft7Validator, Draft202012Validator
from pydantic import ValidationError

from eawf.kernel.spec.common import EvidenceKind
from eawf.kernel.state.enums import AgentReportVerdict, Confidence
from eawf.kernel.store.kinds.agent_report import (
    AgentReportEvidenceRef,
    AgentReportFollowup,
    AuditorReportBody,
    CriterionVerdict,
)
from eawf.workflow.dispatch.verdict import (
    DurableAuditContext,
    DurableAuditCriterion,
    durable_auditor_json_schema,
    parse_auditor_report_body,
)
from eawf.workflow.dispatch.verdict_schema import (
    auditor_report_json_schema,
    reorder_criterion_rows,
)

pytestmark = pytest.mark.unit

_WAVE_ID = "P41-I01-W03"
_RECEIPT_URN = f"urn:eawf:v1:store:{_WAVE_ID}/gate_receipt/GR-schema"
_OTHER_URN = f"urn:eawf:v1:store:{_WAVE_ID}/gate_receipt/GR-unbound"
_TEXTS = ("ship the forced schema", "prove the forced schema")

#: The four evidence shapes a row can carry: only the first satisfies the
#: deterministic criterion's mapped-GateReceipt citation.
_EVIDENCE_VARIANTS: dict[str, list[dict[str, str]]] = {
    "receipt": [{"kind": "store_record", "ref": _RECEIPT_URN}],
    "unbound-receipt": [{"kind": "store_record", "ref": _OTHER_URN}],
    "artifact": [{"kind": "artifact", "ref": "tests/unit/x.py"}],
    "none": [],
}


def _context(*texts: str, deterministic: bool = True) -> DurableAuditContext:
    """Return a close context with one deterministic and one judged criterion."""
    first, second = texts or _TEXTS
    return DurableAuditContext(
        wave_id=_WAVE_ID,
        close_attempt_id="CA-03",
        integration_id="WI-03",
        integrated_sha="a" * 40,
        tree_sha="b" * 40,
        spec_digest="c" * 64,
        criteria_digest="d" * 64,
        gate_manifest_digest="e" * 64,
        policy_digest="f" * 64,
        runner_digest="1" * 64,
        dependency_binding_digest="2" * 64,
        criteria=(
            DurableAuditCriterion(
                criterion_id="CR-01",
                text=first,
                deterministic=deterministic,
                gate_receipt_urns=(_RECEIPT_URN,) if deterministic else (),
            ),
            DurableAuditCriterion(criterion_id="CR-02", text=second, deterministic=False),
        ),
    )


def _schema(*texts: str) -> dict[str, Any]:
    """Return the forced schema for the two-criterion context, asserting it exists."""
    schema = durable_auditor_json_schema(_context(*texts))
    assert schema is not None
    return schema


def _body(
    *,
    verdict: str = "pass",
    confidence: str = "high",
    target_id: str = _WAVE_ID,
    passed: tuple[bool, bool] = (True, True),
    evidence: str = "receipt",
    refutations: list[str] | None = None,
    reverse: bool = False,
) -> dict[str, Any]:
    """Return one candidate auditor body over the varied dimensions."""
    rows = [
        {
            "criterion": _TEXTS[0],
            "passed": passed[0],
            "evidence_refs": copy.deepcopy(_EVIDENCE_VARIANTS[evidence]),
        },
        {
            "criterion": _TEXTS[1],
            "passed": passed[1],
            "evidence_refs": [{"kind": "artifact", "ref": "tests/unit/y.py"}],
        },
    ]
    return {
        "role": "auditor",
        "verdict": verdict,
        "confidence": confidence,
        "summary": "re-read the frozen close inputs against the criteria",
        "target_id": target_id,
        "criteria": list(reversed(rows)) if reverse else rows,
        "refutations": list(refutations or []),
    }


def _shape_objects(node: object) -> list[dict[str, Any]]:
    """Return every object schema on the body-shape spine of *node*.

    Walks only ``properties`` / ``items`` / ``anyOf`` -- the spine that
    describes the body's shape -- and collects the object nodes that declare
    a property set. The ``if`` / ``then`` / ``else`` / ``allOf`` / ``contains``
    subschemas are cross-field CONSTRAINTS that name a single key each, so
    closing them would forbid every other field; an ``anyOf`` node only
    dispatches to branches that close on their own.
    """
    if not isinstance(node, dict):
        return []
    found = [node] if node.get("type") == "object" and "properties" in node else []
    for child in node.get("properties", {}).values():
        found.extend(_shape_objects(child))
    found.extend(_shape_objects(node.get("items")))
    for branch in node.get("anyOf", []):
        found.extend(_shape_objects(branch))
    return found


def _keywords(node: object) -> set[str]:
    """Return every JSON Schema keyword used anywhere under *node*."""
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _keywords(value)}
    if isinstance(node, list):
        return {key for value in node for key in _keywords(value)}
    return set()


def _string_bound(schema: dict[str, Any], field: str, key: str) -> int:
    """Return *key* of *field* in a model JSON schema, seeing past an optional union."""
    node = schema["properties"][field]
    if "anyOf" in node:
        node = next(part for part in node["anyOf"] if part.get("type") == "string")
    return int(node[key])


# --------------------------------------------------------------------------- #
# The schema is legal in both dialects the spawn crosses.
# --------------------------------------------------------------------------- #


def test_durable_auditor_json_schema_is_legal_in_both_dialects() -> None:
    """Ajv draft-07 (the CLI) and draft 2020-12 (the API) both accept it."""
    schema = _schema()

    Draft7Validator.check_schema(schema)
    Draft202012Validator.check_schema(schema)


def test_durable_auditor_json_schema_avoids_the_rejected_keywords() -> None:
    """No ``prefixItems`` anywhere and no combinator at the top level.

    The CLI's Ajv runs in draft-07 and refuses ``prefixItems`` as an unknown
    keyword; the API refuses ``anyOf`` / ``oneOf`` / ``allOf`` at the top level
    of a structured-output schema. Both facts were measured against the live
    CLI, so a schema that reintroduces either flag would fail every durable
    audit rather than one test.
    """
    schema = _schema()

    assert "prefixItems" not in _keywords(schema)
    assert not {"anyOf", "oneOf", "allOf"} & set(schema)
    assert {"if", "then", "else"} <= set(schema)


def test_durable_auditor_json_schema_closes_every_object_level() -> None:
    """Every object on the body-shape spine forbids an unnamed field."""
    schema = _schema()
    shapes = _shape_objects(schema)

    assert len(shapes) >= 5
    assert all(shape["additionalProperties"] is False for shape in shapes)


@pytest.mark.parametrize(
    ("path", "extra"),
    [
        pytest.param((), "findings", id="body"),
        pytest.param(("criteria", 0), "criterion_id", id="criteria-row"),
    ],
)
def test_durable_auditor_json_schema_rejects_an_extra_field(
    path: tuple[Any, ...], extra: str
) -> None:
    """The closed levels red on the exact extra keys a live auditor added."""
    body = _body()
    target: Any = body
    for step in path:
        target = target[step]
    target[extra] = ["anything"]

    assert not Draft7Validator(_schema()).is_valid(body)


# --------------------------------------------------------------------------- #
# The criterion echo is a value the schema supplies, not one the model writes.
# --------------------------------------------------------------------------- #


def test_durable_auditor_json_schema_pins_each_criterion_text() -> None:
    """One row branch per criterion, each pinning that criterion's exact text."""
    branches = _schema()["properties"]["criteria"]["items"]["anyOf"]

    assert [branch["properties"]["criterion"]["const"] for branch in branches] == list(_TEXTS)


def test_durable_auditor_json_schema_refuses_a_dropped_word() -> None:
    """A criterion echo missing one word is not admitted at all.

    This is the live failure the flag exists for: an auditor echoed a criterion
    back one word short and burned the whole re-ask budget on it.
    """
    body = _body()
    body["criteria"][0]["criterion"] = _TEXTS[0].replace("forced ", "")

    assert not Draft7Validator(_schema()).is_valid(body)


def test_durable_auditor_json_schema_binds_the_receipt_to_its_own_criterion() -> None:
    """Only the deterministic row must cite a mapped GateReceipt URN."""
    validator = Draft7Validator(_schema())

    assert validator.is_valid(_body(evidence="receipt"))
    assert not validator.is_valid(_body(evidence="unbound-receipt"))
    assert not validator.is_valid(_body(evidence="artifact"))
    assert not validator.is_valid(_body(evidence="none"))


def test_durable_auditor_json_schema_ties_the_verdict_to_its_rows() -> None:
    """A close-ready verdict needs every row passing and no refutations."""
    validator = Draft7Validator(_schema())

    assert validator.is_valid(_body(verdict="pass", passed=(True, True)))
    assert not validator.is_valid(_body(verdict="pass", passed=(True, False)))
    assert not validator.is_valid(_body(verdict="pass", refutations=["row two is wrong"]))
    assert validator.is_valid(_body(verdict="fail", passed=(False, True)))
    assert not validator.is_valid(_body(verdict="fail", passed=(True, True)))


def test_durable_auditor_json_schema_string_bounds_match_the_report_models() -> None:
    """Each mirrored length bound still equals the bound its model declares."""
    props = _schema()["properties"]
    body = AuditorReportBody.model_json_schema()
    ref = AgentReportEvidenceRef.model_json_schema()
    followup = AgentReportFollowup.model_json_schema()
    row = _schema()["properties"]["criteria"]["items"]["anyOf"][0]["properties"]

    assert props["summary"]["maxLength"] == _string_bound(body, "summary", "maxLength")
    assert props["verdict"]["enum"] == [verdict.value for verdict in AgentReportVerdict]
    assert props["confidence"]["enum"] == [level.value for level in Confidence]
    assert props["evidence_refs"]["items"]["properties"]["kind"]["enum"] == list(
        EvidenceKind.__args__
    )
    assert props["evidence_refs"]["items"]["properties"]["note"]["maxLength"] == _string_bound(
        ref, "note", "maxLength"
    )
    assert props["followups"]["items"]["properties"]["title"]["maxLength"] == _string_bound(
        followup, "title", "maxLength"
    )
    assert props["followups"]["items"]["properties"]["detail"]["maxLength"] == _string_bound(
        followup, "detail", "maxLength"
    )
    assert len(row["criterion"]["const"]) <= _string_bound(
        CriterionVerdict.model_json_schema(), "criterion", "maxLength"
    )


# --------------------------------------------------------------------------- #
# Everything the schema admits, the durable parser accepts.
# --------------------------------------------------------------------------- #


def test_parse_auditor_report_body_accepts_every_schema_admitted_body() -> None:
    """Sweep the candidate space: admitted implies parsed, in both dialects.

    The sweep varies the verdict, the row order, the pass bits, the refutation
    list, the deterministic row's evidence, an extra body key and the target id
    -- every dimension the durable contract judges. A candidate the schema
    admits must parse; the counts guard against a vacuous sweep in either
    direction.
    """
    context = _context()
    schema = _schema()
    draft7 = Draft7Validator(schema)
    draft2020 = Draft202012Validator(schema)
    admitted = 0
    refused = 0

    for verdict, reverse, passed, refutations, evidence, extra, target in itertools.product(
        [member.value for member in AgentReportVerdict],
        [False, True],
        [(True, True), (True, False), (False, True), (False, False)],
        [[], ["the second row does not hold"]],
        list(_EVIDENCE_VARIANTS),
        [False, True],
        [_WAVE_ID, "P41-I01-W99"],
    ):
        body = _body(
            verdict=verdict,
            passed=passed,
            evidence=evidence,
            refutations=refutations,
            reverse=reverse,
            target_id=target,
        )
        if extra:
            body["findings"] = ["an extra list the body model forbids"]
        assert draft7.is_valid(body) == draft2020.is_valid(body)
        if not draft7.is_valid(body):
            refused += 1
            continue
        admitted += 1
        parse_auditor_report_body(copy.deepcopy(body), durable_context=context)

    assert admitted >= 8
    assert refused > admitted


def test_parse_auditor_report_body_accepts_rows_in_any_order() -> None:
    """A permuted answer is canonicalised back to the required order."""
    context = _context()
    reversed_body = _body(reverse=True)

    parsed = parse_auditor_report_body(reversed_body, durable_context=context)

    assert [row.criterion for row in parsed.criteria] == list(_TEXTS)
    assert [row["criterion"] for row in reversed_body["criteria"]] == list(reversed(_TEXTS))


@pytest.mark.parametrize(
    "confidence",
    [pytest.param(level.value, id=level.value) for level in Confidence],
)
def test_parse_auditor_report_body_accepts_every_admitted_confidence(confidence: str) -> None:
    """Each confidence the schema admits is a confidence the body model takes."""
    body = _body(confidence=confidence)

    assert Draft7Validator(_schema()).is_valid(body)
    parse_auditor_report_body(body, durable_context=_context())


def test_durable_auditor_json_schema_refuses_a_numeric_confidence() -> None:
    """The single most common schema failure cannot be emitted at all."""
    body = _body()
    body["confidence"] = 0.9

    assert not Draft7Validator(_schema()).is_valid(body)


@pytest.mark.parametrize("count", [0, 1, 3])
def test_durable_auditor_json_schema_pins_the_row_count(count: int) -> None:
    """One row per criterion: a short, empty or padded answer is refused."""
    body = _body()
    rows = body["criteria"]
    body["criteria"] = (rows * 2)[:count]

    assert not Draft7Validator(_schema()).is_valid(body)


# --------------------------------------------------------------------------- #
# Boundaries of the criterion list itself.
# --------------------------------------------------------------------------- #


def test_auditor_report_json_schema_with_no_criteria_admits_only_an_empty_list() -> None:
    """A close with no required criterion forces an empty, close-ready answer."""
    schema = auditor_report_json_schema(target_id=_WAVE_ID, criteria=())
    assert schema is not None
    validator = Draft7Validator(schema)
    body = _body()
    body["criteria"] = []

    Draft7Validator.check_schema(schema)
    assert validator.is_valid(body)
    body["verdict"] = "fail"
    assert not validator.is_valid(body)


def test_auditor_report_json_schema_with_one_criterion_pins_a_single_row() -> None:
    """The single-criterion boundary still pins exactly one echoing row."""
    schema = auditor_report_json_schema(target_id=_WAVE_ID, criteria=((_TEXTS[0], ()),))
    assert schema is not None
    body = _body()
    body["criteria"] = [{"criterion": _TEXTS[0], "passed": True, "evidence_refs": []}]

    assert not Draft7Validator(schema).is_valid(body)
    body["criteria"][0]["evidence_refs"] = [{"kind": "artifact", "ref": "tests/unit/z.py"}]
    assert Draft7Validator(schema).is_valid(body)


def test_durable_auditor_json_schema_accepts_a_maximum_length_criterion() -> None:
    """A criterion at the body model's 500-char ceiling still pins and parses."""
    longest = "x" * 500
    schema = _schema(longest, _TEXTS[1])
    body = _body()
    body["criteria"][0]["criterion"] = longest

    assert Draft7Validator(schema).is_valid(body)
    parsed = parse_auditor_report_body(body, durable_context=_context(longest, _TEXTS[1]))
    assert parsed.criteria[0].criterion == longest


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(_TEXTS[0], _TEXTS[0], id="duplicate-text"),
        pytest.param("y" * 501, _TEXTS[1], id="over-long-text"),
    ],
)
def test_durable_auditor_json_schema_declines_unpinnable_criteria(first: str, second: str) -> None:
    """Criteria no schema can pin exactly degrade to an unforced spawn."""
    assert durable_auditor_json_schema(_context(first, second)) is None


def test_durable_auditor_json_schema_drops_the_receipt_binding_when_not_deterministic() -> None:
    """A judged criterion's row may cite any evidence, so no receipt is forced."""
    context = _context(deterministic=False)
    schema = durable_auditor_json_schema(context)
    assert schema is not None

    branch = schema["properties"]["criteria"]["items"]["anyOf"][0]

    assert "contains" not in branch["properties"]["evidence_refs"]
    assert Draft7Validator(schema).is_valid(_body(evidence="artifact"))


# --------------------------------------------------------------------------- #
# reorder_criterion_rows: boundaries and error paths.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not a body", id="not-a-mapping"),
        pytest.param({"criteria": "not a list"}, id="criteria-not-a-list"),
        pytest.param({"criteria": []}, id="wrong-row-count"),
        pytest.param({"criteria": [{"passed": True}, {"passed": True}]}, id="row-without-echo"),
        pytest.param({"criteria": [{"criterion": 7}, {"criterion": 8}]}, id="echo-not-a-string"),
        pytest.param(
            {"criteria": [{"criterion": _TEXTS[0]}, {"criterion": "paraphrase"}]},
            id="uncovered-criterion",
        ),
    ],
)
def test_reorder_criterion_rows_returns_input_when_it_cannot_pair(raw: object) -> None:
    """Anything that is not an exact one-row-per-criterion cover comes back as-is."""
    assert reorder_criterion_rows(raw, texts=list(_TEXTS)) is raw


def test_reorder_criterion_rows_with_no_texts_is_a_noop() -> None:
    """The empty-criteria boundary leaves an empty row list untouched."""
    raw = {"criteria": []}

    assert reorder_criterion_rows(raw, texts=[]) is raw


def test_reorder_criterion_rows_pairs_an_annotated_echo() -> None:
    """One trailing deterministic annotation still pairs a row to its criterion."""
    raw = {
        "criteria": [
            {"criterion": f"{_TEXTS[1]} (deterministic=false)"},
            {"criterion": _TEXTS[0]},
        ]
    }

    reordered = reorder_criterion_rows(raw, texts=list(_TEXTS))

    assert isinstance(reordered, dict)
    assert [row["criterion"] for row in reordered["criteria"]] == [
        _TEXTS[0],
        f"{_TEXTS[1]} (deterministic=false)",
    ]


def test_parse_auditor_report_body_still_refuses_a_paraphrase_after_reordering() -> None:
    """A row that pairs with no criterion keeps its verbatim-check message."""
    body = _body()
    body["criteria"][0]["criterion"] = "ship it"

    with pytest.raises(ValidationError, match="durable audit criterion mismatch for 'CR-01'"):
        parse_auditor_report_body(body, durable_context=_context())
