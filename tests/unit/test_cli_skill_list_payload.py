"""Golden assertion against :func:`_list_payload` (P10-W01).

The ``skill list --json`` payload shape is consumed by
``skill render --format=json``, by external dashboards that surface
``/skill list`` JSON to operators, and by future runtime adapters
(``claude-agent-sdk`` envelope projection in P10-W03). This module
pins the per-row key set and the canonical seventeen skill names so a
future drift in :func:`_list_payload` is caught at test time, not
downstream.

We deliberately do NOT pin the ``description`` or ``body_schema``
strings — those are docstrings + class names that legitimately
evolve. The key set, the slashed-name ordering, and the
``installed`` status post-W03 bootstrap are the stable contract.
"""

from __future__ import annotations

from eawf.surfaces.cli.commands.skill import _list_payload

_EXPECTED_SKILL_NAMES: tuple[str, ...] = (
    "/research",
    "/prep",
    "/audit",
    "/ship",
    "/review",
    "/polish",
    "/init",
    "/roadmap",
    "/differentiate",
    "/flow",
    "/blitz",
    "/coauthor",
    "/memory",
    "/agent-dispatch",
    "/compress",
    "/wave-spec",
    "/security-review",
)


def test_list_payload_top_level_keys_are_exactly_skills() -> None:
    """Top level is the single ``skills`` key — no metadata, no totals
    sidebar. Pin so a future "add a count column" change updates the
    surface documentation alongside the code.
    """
    payload = _list_payload()
    assert set(payload.keys()) == {"skills"}


def test_list_payload_carries_all_seventeen_canonical_names_in_order() -> None:
    """The frozen seventeen-skill ordering is the surface contract.
    Drift in either name set or order is a breaking change."""
    payload = _list_payload()
    actual_names = [row["name"] for row in payload["skills"]]
    assert actual_names == list(_EXPECTED_SKILL_NAMES)


def test_list_payload_row_keys_are_exactly_the_documented_set() -> None:
    """Per-row keys are exactly ``name``/``status``/``body_schema``/
    ``description``. The ``skill render --format=json`` surface adds a
    ``body`` field on top — that addition is exercised in
    ``test_cli_skill_render.py`` so the two surfaces stay aligned.
    """
    payload = _list_payload()
    expected_keys = {"name", "status", "body_schema", "description"}
    for row in payload["skills"]:
        assert set(row.keys()) == expected_keys, f"unexpected keys in row {row}"


def test_list_payload_status_is_installed_or_missing() -> None:
    """``status`` is one of the documented two values. Catches a future
    third status (e.g. ``stale``) added without documentation.
    """
    payload = _list_payload()
    for row in payload["skills"]:
        assert row["status"] in {"installed", "missing"}


def test_list_payload_body_schema_is_a_dotted_class_path() -> None:
    """``body_schema`` is the fully-qualified class name of the
    skill body model — the format ``<module>.<class>``. Pin via a
    simple dotted-path probe so a future fingerprint scheme that
    drops the module qualifier (e.g. just ``ResearchBody``) fails
    the test.
    """
    payload = _list_payload()
    for row in payload["skills"]:
        body_schema = row["body_schema"]
        assert isinstance(body_schema, str)
        assert "." in body_schema, f"body_schema {body_schema!r} lacks a module qualifier"
        assert body_schema.startswith("eawf.workflow.skills.bodies."), body_schema
