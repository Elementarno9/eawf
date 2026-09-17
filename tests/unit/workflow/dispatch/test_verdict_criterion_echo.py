"""Tests for the durable auditor's criterion echo and its verbatim check.

A durable close auditor echoes every required criterion text into its report.
The prompt shows each text as a JSON string with the deterministic flag on its
own sub-bullet, the validator tolerates exactly one trailing
``(deterministic=...)`` annotation on the echo and stores the exact text, and
any other change to the text is still refused. The durable prompt also names
the closed field set of the report body.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import Wave
from eawf.kernel.store.kinds.agent_report import (
    AgentReportEvidenceRef,
    AuditorReportBody,
    CriterionVerdict,
)
from eawf.workflow.dispatch.llm_assist import _failure_from_exc
from eawf.workflow.dispatch.verdict import (
    DurableAuditContext,
    DurableAuditCriterion,
    _validate_durable_auditor_body,
    build_auditor_prompt,
    parse_auditor_report_body,
)
from eawf.workflow.dispatch.verdict_schema import strip_deterministic_annotation
from tests._criteria_helpers import legacy_criteria

_WAVE_ID = "P41-I01-W03"
_RECEIPT_URN = f"urn:eawf:v1:store:{_WAVE_ID}/gate_receipt/GR-echo"
_TEXTS = ("ship the echo check", "prove the echo check")


def _context(*texts: str) -> DurableAuditContext:
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
                deterministic=True,
                gate_receipt_urns=(_RECEIPT_URN,),
            ),
            DurableAuditCriterion(criterion_id="CR-02", text=second, deterministic=False),
        ),
    )


def _body(first: str = _TEXTS[0], second: str = _TEXTS[1]) -> dict[str, Any]:
    """Return a passing durable auditor body echoing *first* and *second*."""
    return {
        "role": "auditor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "re-read the close inputs against the criteria",
        "target_id": _WAVE_ID,
        "criteria": [
            {
                "criterion": first,
                "passed": True,
                "evidence_refs": [{"kind": "store_record", "ref": _RECEIPT_URN}],
            },
            {
                "criterion": second,
                "passed": True,
                "evidence_refs": [
                    {
                        "kind": "artifact",
                        "ref": "tests/unit/workflow/dispatch/test_verdict_criterion_echo.py",
                    }
                ],
            },
        ],
        "refutations": [],
    }


def _wave(*texts: str) -> Wave:
    """Return a standalone wave whose success criteria carry *texts*."""
    criteria = [c.model_dump(mode="json") for c in legacy_criteria(*(texts or _TEXTS))]
    return Wave.model_validate(
        {
            "id": _WAVE_ID,
            "iter_id": "P41-I01",
            "title": "check the criterion echo",
            "status": "claimed",
            "file_scopes": ["src/eawf/workflow/dispatch/verdict.py"],
            "success_criteria": criteria,
            "agent_role": "executor",
            "effort_bucket": "XS",
            "opened_at": "2026-07-01T00:00:00Z",
        }
    )


def _prompt(*texts: str, durable: bool = True) -> str:
    """Render the auditor prompt, with the durable context unless told not to."""
    context = _context(*texts) if durable else None
    return build_auditor_prompt(_wave(*texts), diff_base="abc123~1", durable_context=context)


def _flat(text: str) -> str:
    """Collapse the prompt's hard wraps so a sentence matches as one line."""
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# Prompt: flag on its own line, criterion as a JSON string, closed field set.
# --------------------------------------------------------------------------- #


def test_build_auditor_prompt_renders_deterministic_flag_on_its_own_line() -> None:
    """Each criterion line carries only the text; the flag is a sub-bullet."""
    lines = _prompt().splitlines()

    first = lines.index('- `CR-01` criterion: "ship the echo check"')
    second = lines.index('- `CR-02` criterion: "prove the echo check"')

    assert lines[first + 1] == "  - deterministic: true"
    assert lines[first + 2] == f"  - mapped GateReceipt: `{_RECEIPT_URN}`"
    assert lines[second + 1] == "  - deterministic: false"
    assert lines[second + 2] == "  - no deterministic GateReceipt applies"
    assert "(deterministic=" not in _prompt()


def test_build_auditor_prompt_renders_criterion_as_copyable_json_string() -> None:
    """Quotes and backslashes survive as a JSON literal that decodes to the text."""
    tricky = 'keep the "exact" text of a\\b intact'
    prefix = "- `CR-01` criterion: "

    lines = _prompt(tricky, _TEXTS[1]).splitlines()
    rendered = next(line for line in lines if line.startswith(prefix))

    assert rendered.removeprefix(prefix) == json.dumps(tricky)
    assert json.loads(rendered.removeprefix(prefix)) == tricky


def test_build_auditor_prompt_durable_contract_asks_for_an_exact_copy() -> None:
    """The durable contract points the echo at the JSON string, unannotated."""
    flat = _flat(_prompt())

    assert "Set each row's `criterion` to the JSON string shown for that criterion" in flat
    assert "copied character for character" in flat
    assert "no criterion id and no deterministic flag" in flat


def test_build_auditor_prompt_durable_contract_names_closed_field_set() -> None:
    """Every schema field the auditor may set is named, and no other is allowed."""
    flat = _flat(_prompt())
    body_fields = [name for name in AuditorReportBody.model_fields if name != "report_source"]
    rule = flat[flat.index("The body accepts only these fields:") :]

    assert all(f"`{name}`" in rule for name in body_fields)
    assert "`report_source`" not in flat
    assert "A `criteria` row accepts only `criterion`, `passed` and `evidence_refs`" in flat
    assert "an evidence entry accepts only `kind`, `ref` and `note`" in flat
    assert "Any other field, such as `findings`, is rejected" in flat
    assert list(CriterionVerdict.model_fields) == ["criterion", "passed", "evidence_refs"]
    assert list(AgentReportEvidenceRef.model_fields) == ["kind", "ref", "note"]


def test_build_auditor_prompt_without_durable_context_omits_field_rule() -> None:
    """The plain audit prompt keeps its shape: no field rule, no criterion JSON."""
    prompt = _prompt(durable=False)

    assert "The body accepts only these fields" not in prompt
    assert "criterion: " not in prompt


def test_parse_auditor_report_body_rejects_extra_findings_field() -> None:
    """The closed body schema still refuses a ``findings`` list at the durable boundary."""
    raw = _body()
    raw["findings"] = ["the echo check is wired"]

    with pytest.raises(ValidationError, match="extra_forbidden") as excinfo:
        parse_auditor_report_body(raw, durable_context=_context())

    assert excinfo.value.errors()[0]["loc"] == ("findings",)


# --------------------------------------------------------------------------- #
# Validator: one trailing annotation is stripped, nothing else is.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("echo", "expected"),
    [
        ("a (deterministic=true)", "a"),
        ("a (deterministic=false)", "a"),
        ("a(deterministic=true)", "a"),
        ("a", "a"),
        ("", ""),
        ("(deterministic=true)", ""),
        ("a (deterministic=true) (deterministic=false)", "a (deterministic=true)"),
        ("a (deterministic=true) ", "a (deterministic=true) "),
        ("a (deterministic=True)", "a (deterministic=True)"),
    ],
)
def test_strip_deterministic_annotation_drops_one_trailing_annotation(
    echo: str, expected: str
) -> None:
    """Only one exact lowercase annotation at the very end is removed."""
    assert strip_deterministic_annotation(echo) == expected


@pytest.mark.parametrize("flag", ["true", "false"])
@pytest.mark.parametrize("row", [0, 1])
def test_parse_auditor_report_body_accepts_annotated_echo(flag: str, row: int) -> None:
    """An echo carrying one annotation parses and persists the exact text."""
    raw = _body()
    raw["criteria"][row]["criterion"] = f"{_TEXTS[row]} (deterministic={flag})"

    body = parse_auditor_report_body(raw, durable_context=_context())

    assert [c.criterion for c in body.criteria] == list(_TEXTS)
    assert raw["criteria"][row]["criterion"].endswith(f"(deterministic={flag})")


def test_validate_durable_auditor_body_strips_every_annotated_row() -> None:
    """Both rows are canonicalised and the input body is left untouched."""
    annotated = AuditorReportBody.model_validate(
        _body(f"{_TEXTS[0]} (deterministic=true)", f"{_TEXTS[1]} (deterministic=false)")
    )

    body = _validate_durable_auditor_body(annotated, context=_context())

    assert [c.criterion for c in body.criteria] == list(_TEXTS)
    assert [c.passed for c in body.criteria] == [True, True]
    assert body.criteria[0].evidence_refs == annotated.criteria[0].evidence_refs
    assert annotated.criteria[0].criterion.endswith("(deterministic=true)")


def test_validate_durable_auditor_body_exact_echo_returns_body_unchanged() -> None:
    """A verbatim echo needs no rewrite, so the same body comes back."""
    exact = AuditorReportBody.model_validate(_body())

    assert _validate_durable_auditor_body(exact, context=_context()) is exact


def test_validate_durable_auditor_body_text_ending_in_annotation() -> None:
    """A criterion whose own text ends in the annotation still matches exactly."""
    text = "render the flag as (deterministic=true)"
    context = _context(text, _TEXTS[1])

    exact = parse_auditor_report_body(_body(text), durable_context=context)
    annotated = parse_auditor_report_body(
        _body(f"{text} (deterministic=true)"), durable_context=context
    )

    assert exact.criteria[0].criterion == text
    assert annotated.criteria[0].criterion == text
    with pytest.raises(ValidationError, match="mismatch for 'CR-01'"):
        parse_auditor_report_body(
            _body("render the flag as"),
            durable_context=context,
        )


@pytest.mark.parametrize(
    "echo",
    [
        pytest.param("ship echo check", id="paraphrase"),
        pytest.param("ship echo check (deterministic=true)", id="paraphrase-annotated"),
        pytest.param(f"{_TEXTS[0]} (deterministic=true) (deterministic=true)", id="two-flags"),
        pytest.param(f"{_TEXTS[0]} (deterministic=false) (deterministic=true)", id="mixed-flags"),
        pytest.param(f"{_TEXTS[0]} (deterministic=True)", id="capitalised-flag"),
        pytest.param(f"{_TEXTS[0]} (deterministic=true) ", id="trailing-space"),
        pytest.param(f"{_TEXTS[0]}  (deterministic=true)", id="double-space"),
        pytest.param(f"(deterministic=true) {_TEXTS[0]}", id="leading-flag"),
        pytest.param(f"{_TEXTS[0]} (required=true)", id="other-parenthetical"),
        pytest.param(f"CR-01: {_TEXTS[0]}", id="id-prefix"),
        pytest.param(_TEXTS[0][:-1], id="truncated"),
    ],
)
def test_changed_criterion_text_still_fails_the_verbatim_check(echo: str) -> None:
    """Any change beyond one stripped annotation raises naming the criterion."""
    raw = _body(echo)
    typed = AuditorReportBody.model_validate(raw)

    with pytest.raises(ValueError, match="durable audit criterion mismatch for 'CR-01'"):
        _validate_durable_auditor_body(typed, context=_context())
    with pytest.raises(ValidationError, match="durable audit criterion mismatch for 'CR-01'"):
        parse_auditor_report_body(raw, durable_context=_context())


def test_validate_durable_auditor_body_mismatch_names_second_criterion() -> None:
    """A changed judged row is refused under its own id, not the first one."""
    typed = AuditorReportBody.model_validate(_body(second="prove it (deterministic=false)"))

    with pytest.raises(ValueError, match="mismatch for 'CR-02'"):
        _validate_durable_auditor_body(typed, context=_context())


def test_validate_durable_auditor_body_mismatch_shows_exact_json_text() -> None:
    """The failure text gives the model the exact JSON string to copy."""
    typed = AuditorReportBody.model_validate(_body("ship it"))

    with pytest.raises(ValueError) as excinfo:
        _validate_durable_auditor_body(typed, context=_context())

    message = str(excinfo.value)
    assert f"set `criterion` to exactly {json.dumps(_TEXTS[0])}" in message
    assert "copied character for character with no paraphrase or annotation" in message
    assert message.endswith('got "ship it"')


def test_validate_durable_auditor_body_mismatch_survives_reask_detail_cap() -> None:
    """A maximum-length criterion still reaches the re-ask notice in full."""
    text = "x" * 499 + '"'
    raw = _body("y" * 500)
    context = _context(text, _TEXTS[1])

    with pytest.raises(ValidationError) as excinfo:
        parse_auditor_report_body(raw, durable_context=context)
    failure = _failure_from_exc(attempt=1, exc=excinfo.value)

    assert failure.reason == "schema_mismatch"
    assert json.dumps(text) in failure.detail
