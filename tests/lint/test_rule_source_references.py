"""Every ``eawf`` symbol a shipped profile's rule text cites still exists.

Rule text is rendered into ``AGENTS.md`` and ``docs/rules/`` and read by
agents as instructions, so a role such as ``:class:`~eawf.x.Y``` naming a
deleted symbol sends a reader after code that is gone. The gate resolves
every such reference in the bundled profile YAML.
"""

from __future__ import annotations

import pydoc
import re
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

PROFILE_DATA: Final = Path(__file__).resolve().parents[2] / "src/eawf/platform/profiles/data"

#: A Sphinx cross-reference role naming a dotted ``eawf`` target.
REFERENCE: Final = re.compile(r":(?:class|func|meth|attr|mod|data|exc):`~?(eawf(?:\.\w+)+)`")


def unresolved_references(text: str) -> list[str]:
    """Return the ``eawf`` targets in *text* that do not resolve, in first-seen order.

    Args:
        text: Rule source text.

    Returns:
        Each dotted target :func:`pydoc.locate` cannot find, deduplicated.
    """
    targets = dict.fromkeys(match.group(1) for match in REFERENCE.finditer(text))
    return [target for target in targets if not _resolves(target)]


def _resolves(target: str) -> bool:
    """Return whether *target* names an importable object or a model field.

    A Pydantic field is not a class attribute, so ``Model.field`` resolves
    through the parent model's declared fields.
    """
    if pydoc.locate(target) is not None:
        return True
    parent, _, name = target.rpartition(".")
    owner = pydoc.locate(parent)
    return name in getattr(owner, "model_fields", {})


@pytest.mark.parametrize("profile", sorted(PROFILE_DATA.glob("*.yaml")), ids=lambda p: p.name)
def test_every_rule_reference_resolves(profile: Path) -> None:
    assert unresolved_references(profile.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        (":class:`~eawf.kernel.spec.intent.IntentBrief`", []),
        (":mod:`eawf.kernel.runtime.compiled`", []),
        (
            ":class:`~eawf.surfaces.render.narrative.NarrativeBundle`",
            ["eawf.surfaces.render.narrative.NarrativeBundle"],
        ),
        (":attr:`~eawf.kernel.store.kinds.evidence.EvidenceRecord.evidence_kind`", []),
        (
            ":attr:`~eawf.kernel.store.kinds.evidence.EvidenceRecord.no_such_field`",
            ["eawf.kernel.store.kinds.evidence.EvidenceRecord.no_such_field"],
        ),
        (":func:`eawf.gone.f` and :func:`eawf.gone.f`", ["eawf.gone.f"]),
        ("``eawf.gone.f`` without a role", []),
    ],
    ids=[
        "empty",
        "class",
        "module",
        "deleted-class",
        "model-field",
        "missing-field",
        "deduplicated",
        "bare-literal",
    ],
)
def test_unresolved_references_boundaries(text: str, expected: list[str]) -> None:
    assert unresolved_references(text) == expected
