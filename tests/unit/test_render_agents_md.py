"""Unit tests for ``eawf.render.agents_md``.

Covers:

- Per-block managed-region emission with hashes parseable by
  :mod:`eawf.render.regions`.
- Re-render preserves hand-written content byte-stable.
- Manifest gains entries (target, region_id, version, hash) for each region.
- Render blocks targeting non-AGENTS.md files are filtered out.
- Atomic write uses tempfile + ``os.replace`` (mock-based).
- Re-rendering an unchanged composed profile reports ``regions_unchanged``
  and leaves ``regions_updated`` empty.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from eawf.profiles.models import ComposedProfile, RenderBlock
from eawf.render import regions
from eawf.render.agents_md import RenderResult, render_agents_md
from eawf.render.manifest import Manifest


def _make_composed(blocks: list[RenderBlock]) -> ComposedProfile:
    """Build a minimal ComposedProfile carrying the supplied render_blocks."""
    return ComposedProfile(
        name="test",
        version="1.0",
        description="",
        render_blocks=blocks,
    )


def _block(
    region_id: str,
    body: str,
    *,
    target: str = "AGENTS.md",
    version: str = "1.0",
) -> RenderBlock:
    """Build a RenderBlock whose body_template is the literal *body* string."""
    return RenderBlock(id=region_id, target=target, body_template=body, version=version)


def test_render_agents_md_writes_managed_regions_for_each_block(tmp_path: Path) -> None:
    """Each AGENTS.md-targeted block round-trips through find_regions with the right hash."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("rules", "## Rules\n\n- one\n- two"),
            _block("style", "## Style\n\n- f-strings only"),
        ]
    )

    result, _ = render_agents_md(composed, target, Manifest(version=1, generated={}))

    text = target.read_text(encoding="utf-8")
    parsed = regions.find_regions(text)
    found = {r.id: r for r in parsed}
    assert set(found.keys()) == {"rules", "style"}
    assert found["rules"].body == "## Rules\n\n- one\n- two"
    assert found["style"].body == "## Style\n\n- f-strings only"
    # Hashes match — declared on marker == compute_hash(body).
    assert found["rules"].declared_hash == regions.compute_hash(found["rules"].body)
    assert found["style"].declared_hash == regions.compute_hash(found["style"].body)
    assert result.regions_added == ["rules", "style"]
    assert result.regions_updated == []
    assert result.regions_unchanged == []


def test_render_agents_md_re_render_preserves_user_content(tmp_path: Path) -> None:
    """A hand-edited paragraph below a managed region survives a re-render byte-for-byte."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed([_block("rules", "## Rules\n\n- one")])
    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    user_addition = "\n\n## User notes\n\nThis paragraph was hand-written.\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(user_addition)
    before = target.read_text(encoding="utf-8")

    result, _ = render_agents_md(composed, target, manifest)
    after = target.read_text(encoding="utf-8")

    assert before == after, "re-render must be byte-stable when nothing changed"
    assert "## User notes" in after
    assert "This paragraph was hand-written." in after
    assert result.hand_edits_preserved is True
    assert result.regions_unchanged == ["rules"]


def test_render_agents_md_updates_manifest(tmp_path: Path) -> None:
    """Manifest gains one entry per emitted region with correct fields."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("alpha", "alpha body", version="1.0"),
            _block("beta", "beta body", version="2.5"),
        ]
    )

    _, manifest = render_agents_md(
        composed,
        target,
        Manifest(version=1, generated={}),
        generator="profile:test",
    )

    target_str = str(target)
    keys = set(manifest.generated.keys())
    assert keys == {f"{target_str}::alpha", f"{target_str}::beta"}

    alpha_entry = manifest.generated[f"{target_str}::alpha"]
    assert alpha_entry.target == target_str
    assert alpha_entry.region_id == "alpha"
    assert alpha_entry.version == "1.0"
    assert alpha_entry.hash == regions.compute_hash("alpha body")
    assert alpha_entry.generator == "profile:test"
    # ISO 8601 with timezone offset is sufficient — exact instant is irrelevant.
    assert "T" in alpha_entry.generated_at
    assert alpha_entry.generated_at.endswith("+00:00")

    beta_entry = manifest.generated[f"{target_str}::beta"]
    assert beta_entry.version == "2.5"
    assert beta_entry.hash == regions.compute_hash("beta body")


def test_render_agents_md_filters_by_target(tmp_path: Path) -> None:
    """Render blocks targeting non-AGENTS.md files are skipped silently."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("rules", "## Rules", target="AGENTS.md"),
            _block("skill-foo", "skill body", target=".claude/skills/foo.md"),
            _block("agent-bar", "agent body", target=".claude/agents/bar.md"),
        ]
    )

    result, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    text = target.read_text(encoding="utf-8")
    region_ids = {r.id for r in regions.find_regions(text)}
    assert region_ids == {"rules"}
    assert result.regions_added == ["rules"]
    # Manifest only holds the AGENTS.md region.
    assert set(manifest.generated.keys()) == {f"{target!s}::rules"}


def test_render_agents_md_atomic_write(tmp_path: Path) -> None:
    """The renderer writes via a sibling tempfile then ``os.replace``."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed([_block("rules", "## Rules")])

    with patch(
        "eawf.render.agents_md.os.replace",
        wraps=__import__("os").replace,
    ) as spy:
        render_agents_md(composed, target, Manifest(version=1, generated={}))

    assert spy.called, "render must go through os.replace"
    src_arg = spy.call_args.args[0]
    dst_arg = spy.call_args.args[1]
    assert str(dst_arg) == str(target)
    assert str(src_arg).startswith(str(target) + ".tmp.")


def test_render_agents_md_unchanged_no_update_needed(tmp_path: Path) -> None:
    """Two consecutive renders of the same composed profile: second is a no-op delta."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("alpha", "alpha body"),
            _block("beta", "beta body"),
        ]
    )

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))
    result, _ = render_agents_md(composed, target, manifest)

    assert sorted(result.regions_unchanged) == ["alpha", "beta"]
    assert result.regions_updated == []
    assert result.regions_added == []


def test_render_agents_md_updated_when_body_changes(tmp_path: Path) -> None:
    """Changing a block's body marks it ``regions_updated`` (not ``unchanged``)."""
    target = tmp_path / "AGENTS.md"
    composed_v1 = _make_composed([_block("rules", "## Rules v1")])
    _, manifest = render_agents_md(composed_v1, target, Manifest(version=1, generated={}))

    composed_v2 = _make_composed([_block("rules", "## Rules v2")])
    result, _ = render_agents_md(composed_v2, target, manifest)

    assert result.regions_updated == ["rules"]
    assert result.regions_unchanged == []
    assert result.regions_added == []
    text = target.read_text(encoding="utf-8")
    region = regions.extract_region(text, "rules")
    assert region is not None
    assert region.body == "## Rules v2"


def test_render_agents_md_returns_render_result_with_target(tmp_path: Path) -> None:
    """``RenderResult.target`` is the path the renderer wrote."""
    target = tmp_path / "nested" / "AGENTS.md"
    composed = _make_composed([_block("rules", "## Rules")])
    result, _ = render_agents_md(composed, target, Manifest(version=1, generated={}))
    assert isinstance(result, RenderResult)
    assert result.target == target


def test_render_agents_md_preserves_other_target_manifest_entries(tmp_path: Path) -> None:
    """Manifest entries for OTHER targets survive an AGENTS.md render."""
    target = tmp_path / "AGENTS.md"
    other_target = str(tmp_path / ".claude" / "skills" / "foo.md")
    seed = Manifest(
        version=1,
        generated={
            f"{other_target}::skill-foo": __import__(
                "eawf.render.manifest", fromlist=["ManifestEntry"]
            ).ManifestEntry(
                target=other_target,
                region_id="skill-foo",
                version="1.0",
                hash="0123456789abcdef",
                generator="profile:other",
                generated_at="2026-01-01T00:00:00+00:00",
            ),
        },
    )
    composed = _make_composed([_block("rules", "## Rules")])

    _, manifest = render_agents_md(composed, target, seed)

    assert f"{other_target}::skill-foo" in manifest.generated
    assert f"{target!s}::rules" in manifest.generated


def test_render_agents_md_empty_compose_no_regions(tmp_path: Path) -> None:
    """A composed profile with zero AGENTS.md blocks: file is created empty / unchanged.

    The renderer always emits a POSIX-compliant trailing ``\\n`` so
    ``end-of-file-fixer`` is a no-op; with zero blocks the file therefore
    contains exactly one newline byte (still effectively empty content).
    """
    target = tmp_path / "AGENTS.md"
    composed = _make_composed([])

    result, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "\n"
    assert result.regions_added == []
    assert result.regions_updated == []
    assert result.regions_unchanged == []
    assert manifest.generated == {}
