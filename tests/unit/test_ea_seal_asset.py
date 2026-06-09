"""Unit tests for the committed Ea Seal SVG asset.

These tests pin the three success criteria of P30-I02-W29:

1. :data:`eawf.surfaces.render.brand.SEAL_ASSET_PATH` exists and points at
   a real file, so a consumer resolves the asset without a hardcoded
   literal path.
2. The committed ``ea-seal.svg`` parses as valid XML/SVG via the stdlib
   ``xml.etree.ElementTree`` (no exception; the root tag is ``svg``).
3. The SVG carries the evenodd-knockout disc path -- a ``<path>`` element
   whose ``fill-rule`` attribute is ``evenodd`` (the Seal mark).

The suite is dependency-free: only the stdlib ``xml.etree.ElementTree``
parser is used.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.etree.ElementTree import Element

from eawf.surfaces.render.brand import SEAL_ASSET_PATH


def _local_tag(tag: str) -> str:
    """Strip an XML namespace prefix from *tag*.

    ``ElementTree`` reports namespaced tags in Clark notation
    (``{http://www.w3.org/2000/svg}path``); this helper returns the bare
    local name so the assertions match the literal SVG element names.

    Args:
        tag: The (possibly namespaced) element tag as reported by
            :class:`xml.etree.ElementTree.Element`.

    Returns:
        The local tag name with any ``{namespace}`` prefix removed.
    """
    return tag.rsplit("}", 1)[-1]


def test_seal_asset_path_points_at_real_file() -> None:
    """``SEAL_ASSET_PATH`` resolves to an existing committed SVG file."""
    assert isinstance(SEAL_ASSET_PATH, Path)
    assert SEAL_ASSET_PATH.is_file()
    assert SEAL_ASSET_PATH.name == "ea-seal.svg"


def test_seal_asset_parses_as_valid_svg() -> None:
    """The committed asset parses as XML and the root element is ``svg``."""
    tree = ET.parse(SEAL_ASSET_PATH)
    root = tree.getroot()
    assert _local_tag(root.tag) == "svg"


def test_seal_asset_carries_evenodd_knockout_path() -> None:
    """A ``<path>`` element declares ``fill-rule="evenodd"`` (the disc knockout)."""
    root = ET.parse(SEAL_ASSET_PATH).getroot()
    paths = [el for el in root.iter() if _local_tag(el.tag) == "path"]
    assert paths, "expected at least one <path> element in the Seal mark"
    evenodd_paths = [el for el in paths if el.get("fill-rule") == "evenodd"]
    assert evenodd_paths, "expected a <path> with fill-rule='evenodd'"


def test_seal_asset_is_single_colour_current_color() -> None:
    """Every painted element drives off ``currentColor`` (single-colour mark)."""
    root = ET.parse(SEAL_ASSET_PATH).getroot()
    painted: list[Element] = [
        el
        for el in root.iter()
        if el.get("fill") not in (None, "none") or el.get("stroke") not in (None, "none")
    ]
    assert painted, "expected at least one painted element"
    for el in painted:
        colours = {el.get("fill"), el.get("stroke")} - {None, "none"}
        assert colours == {"currentColor"}, f"unexpected colour token on <{_local_tag(el.tag)}>"
