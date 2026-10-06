"""Every ``statusline.*`` config leaf the catalog keeps has a production reader.

The statusline fits its width from ``COLUMNS`` with a fixed segment order and takes its
colours and glyphs from the theme ``--theme`` or ``EAWF_STATUSLINE_THEME`` names, so it
reads no config leaf. The five leaves once registered for rows, colour, glyphs and the
module lists are retired: the catalog, the built-in layer and the console no longer offer
them, and the config migration strips them from a layer that still states them.
"""

from __future__ import annotations

import pydoc
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import eawf
from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.layered import get_dotted
from eawf.kernel.config.migration import migrate_config_payload
from eawf.kernel.config.registry.config_keys import CONFIG_REGISTRY
from eawf.kernel.config.registry.leaf_catalog import DEPRECATED_LEAF_KEYS, LEAF_KEY_REGISTRY
from eawf.kernel.config.sections import layer_leaf_problems
from eawf.kernel.projection.settings import build_settings_view

SRC_ROOT = Path(eawf.__file__).parent

RETIRED_STATUSLINE_LEAVES = (
    "statusline.color_mode",
    "statusline.glyph_mode",
    "statusline.modules_available",
    "statusline.modules_default",
    "statusline.rows",
)


def _production_call_sites(consumer: str, src_root: Path) -> list[str]:
    """Return ``path:line`` for each call of *consumer*'s bare name under *src_root*.

    A ``def`` line is the definition, not a call, so a reader nothing calls yields none.
    """
    name = consumer.rpartition(".")[2]
    call = re.compile(rf"(?<![\w.])(?:\w+\.)*{re.escape(name)}\(")
    definition = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\(")
    sites: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if definition.match(line) or line.lstrip().startswith("#"):
                continue
            if call.search(line):
                sites.append(f"{path.relative_to(src_root)}:{number}")
    return sites


def _nested(key: str, value: Any) -> dict[str, Any]:
    body: dict[str, Any] = value
    for part in reversed(key.split(".")):
        body = {part: body}
    return body


# ---------- the consumer gate over the statusline section ----------


def _statusline_leaves() -> list[str]:
    return sorted(key for key in LEAF_KEY_REGISTRY if key.startswith("statusline."))


def test_every_catalogued_statusline_leaf_has_a_production_reader() -> None:
    for key in _statusline_leaves():
        entry = LEAF_KEY_REGISTRY[key]
        assert not entry.reserved, key
        assert entry.consumer is not None, key
        assert callable(pydoc.locate(entry.consumer)), (key, entry.consumer)
        assert _production_call_sites(entry.consumer, SRC_ROOT), (key, entry.consumer)


def test_the_statusline_section_keeps_no_leaf() -> None:
    # the width fit and the theme read no leaf; one added later needs a reader above
    assert _statusline_leaves() == []


def test_no_production_module_reads_a_retired_statusline_leaf() -> None:
    catalog = SRC_ROOT / "kernel" / "config" / "registry" / "leaf_catalog.py"
    readers = [
        f"{path.relative_to(SRC_ROOT)}: {key}"
        for path in sorted(SRC_ROOT.rglob("*.py"))
        if path != catalog
        for key in RETIRED_STATUSLINE_LEAVES
        if key in path.read_text(encoding="utf-8")
    ]
    assert readers == []


# ---------- the call-site scan itself ----------


def test_the_scan_finds_no_call_in_an_empty_tree(tmp_path: Path) -> None:
    assert _production_call_sites("pkg.mod.reader", tmp_path) == []


def test_the_scan_refuses_a_reader_only_defined(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "def reader(config):\n    return config\n# reader(config) in prose\n", encoding="utf-8"
    )
    assert _production_call_sites("pkg.mod.reader", tmp_path) == []


def test_the_scan_finds_a_single_call(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("def reader(c):\n    return c\n", encoding="utf-8")
    (tmp_path / "use.py").write_text("value = mod.reader({})\n", encoding="utf-8")
    assert _production_call_sites("pkg.mod.reader", tmp_path) == ["use.py:1"]


def test_the_scan_does_not_match_a_longer_name(tmp_path: Path) -> None:
    (tmp_path / "use.py").write_text("value = my_reader({})\nreaders()\n", encoding="utf-8")
    assert _production_call_sites("pkg.mod.reader", tmp_path) == []


# ---------- the retired leaves are gone everywhere they were offered ----------


@pytest.mark.parametrize("key", RETIRED_STATUSLINE_LEAVES)
def test_a_retired_statusline_leaf_is_offered_nowhere(key: str) -> None:
    assert key in DEPRECATED_LEAF_KEYS
    assert key not in LEAF_KEY_REGISTRY
    assert key not in {entry.key for entry in CONFIG_REGISTRY}
    with pytest.raises(KeyError):
        get_dotted(BUILT_IN_DEFAULTS, key)


@pytest.mark.parametrize("key", RETIRED_STATUSLINE_LEAVES)
def test_a_layer_stating_a_retired_statusline_leaf_is_refused(key: str) -> None:
    assert layer_leaf_problems(_nested(key, 1)) == ["statusline: unknown config key"]


def test_the_migration_strips_every_retired_statusline_leaf() -> None:
    body: dict[str, Any] = {"schema_version": "1.0", "statusline": {}}
    for key in RETIRED_STATUSLINE_LEAVES:
        body["statusline"][key.rpartition(".")[2]] = 1
    upgraded, changed = migrate_config_payload(body)
    assert changed is True
    assert upgraded == {"schema_version": "1.0"}
    assert layer_leaf_problems(upgraded) == []


def test_the_settings_view_offers_no_statusline_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    (tmp_path / ".ea").mkdir()
    view = build_settings_view(
        workspace=tmp_path,
        repo=tmp_path,
        scope_id="P01",
        cursor=0,
        generated_at=datetime(2026, 10, 6, tzinfo=UTC),
        env={},
        branch=None,
    )
    assert not [leaf.key for leaf in view.leaves if leaf.key.startswith("statusline.")]
