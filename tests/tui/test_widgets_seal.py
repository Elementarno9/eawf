"""Tests for the Seal image brand mark (graphics-protocol path).

``textual-image`` + Pillow now ship in the default deps, so the seal renders as
an inline image when three preconditions hold: the import resolves, ``resvg`` is
on PATH with the asset on disk, and the host terminal advertises a graphics
protocol. When any is absent -- or the ``EAWF_SEAL_DISABLE`` kill switch is set
(every CI / snapshot run) -- it reports not-capable and hands back ``None`` so
callers fall back to the unicode brand glyph and the goldens stay glyph-based.
These tests pin each precondition independently regardless of the running env.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from eawf.surfaces.render.brand import ACCENT_HEX
from eawf.surfaces.tui.widgets import seal as seal_mod
from eawf.surfaces.tui.widgets.seal import (
    _CURRENT_COLOR,
    _RESVG,
    _SEAL_SVG,
    SEAL_DISABLE_ENV,
    SURFACE_HEX,
    _rasterised_seal,
    _seal_png_path,
    _substitute_current_color,
    deps_present,
    resolve_accent_hex,
    resolve_surface_hex,
    resvg_present,
    seal_capable,
    seal_image_widget,
    terminal_supports_images,
)

#: The matte background hex pinned in these tests -- the dark theme's
#: ``$surface`` the seal's transparent regions are matted onto.
_BG = SURFACE_HEX


@pytest.fixture(autouse=True)
def _clear_seal_cache() -> None:
    """Drop the :func:`seal_capable` ``lru_cache`` around each test."""
    seal_capable.cache_clear()
    _rasterised_seal.cache_clear()
    yield
    seal_capable.cache_clear()
    _rasterised_seal.cache_clear()


def _make_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every seal precondition True (kill switch cleared)."""
    monkeypatch.delenv(SEAL_DISABLE_ENV, raising=False)
    monkeypatch.setattr(seal_mod, "deps_present", lambda: True)
    monkeypatch.setattr(seal_mod, "resvg_present", lambda: True)
    monkeypatch.setattr(seal_mod, "terminal_supports_images", lambda: True)


def test_seal_capable_true_when_all_preconditions_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    # All three preconditions present and the kill switch unset -> capable.
    _make_capable(monkeypatch)
    assert seal_capable() is True


def test_seal_capable_false_when_deps_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _make_capable(monkeypatch)
    monkeypatch.setattr(seal_mod, "deps_present", lambda: False)
    assert seal_capable() is False


def test_seal_capable_false_when_resvg_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _make_capable(monkeypatch)
    monkeypatch.setattr(seal_mod, "resvg_present", lambda: False)
    assert seal_capable() is False


def test_seal_capable_false_when_terminal_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    _make_capable(monkeypatch)
    monkeypatch.setattr(seal_mod, "terminal_supports_images", lambda: False)
    assert seal_capable() is False


def test_seal_disable_env_forces_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # The kill switch wins even when every other precondition holds -- this is
    # the autouse switch the snapshot harness sets to keep goldens glyph-based.
    _make_capable(monkeypatch)
    monkeypatch.setenv(SEAL_DISABLE_ENV, "1")
    assert seal_capable() is False


def test_seal_disable_env_empty_string_does_not_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Boundary: an empty value is not "set" for the kill switch.
    _make_capable(monkeypatch)
    monkeypatch.setenv(SEAL_DISABLE_ENV, "")
    assert seal_capable() is True


def test_seal_image_widget_none_when_not_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    # When not capable the factory returns None (never raises), so a caller can
    # write ``seal_image_widget() or glyph_fallback`` safely.
    monkeypatch.setenv(SEAL_DISABLE_ENV, "1")
    assert seal_image_widget() is None


def test_deps_present_returns_bool() -> None:
    # textual-image ships in default deps, so this is True in the test env; the
    # contract under test is the bool return + import-guard, not the value.
    assert isinstance(deps_present(), bool)


def test_resvg_present_false_when_binary_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seal_mod.shutil, "which", lambda _name: None)
    assert resvg_present() is False


def test_resvg_present_false_when_asset_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    monkeypatch.setattr(seal_mod.shutil, "which", lambda _name: "/usr/bin/resvg")
    monkeypatch.setattr(seal_mod, "_SEAL_SVG", Path(tmp_path) / "does-not-exist.svg")
    assert resvg_present() is False


def test_terminal_supports_images_false_when_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-TTY stdout (pipe / CI / snapshot harness) is never image-capable.
    class _NotTTY:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(seal_mod.sys, "__stdout__", _NotTTY())
    assert terminal_supports_images() is False


def test_terminal_supports_images_false_when_stdout_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seal_mod.sys, "__stdout__", None)
    assert terminal_supports_images() is False


def test_terminal_supports_images_true_for_kitty_window(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(seal_mod.sys, "__stdout__", _TTY())
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert terminal_supports_images() is True


def test_terminal_supports_images_true_for_ghostty(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(seal_mod.sys, "__stdout__", _TTY())
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    assert terminal_supports_images() is True


def test_terminal_supports_images_false_for_plain_xterm(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(seal_mod.sys, "__stdout__", _TTY())
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    assert terminal_supports_images() is False


def test_seal_asset_exists() -> None:
    # The Seal SVG ships in the package so a graphics terminal can rasterise it.
    assert _SEAL_SVG.exists()
    assert _SEAL_SVG.suffix == ".svg"
    assert _RESVG == "resvg"


# --------------------------------------------------------------------------
# CR-01: accent substitution + cache keys on (px, accent hex), never black
# --------------------------------------------------------------------------


def test_substitute_current_color_replaces_every_token() -> None:
    # The SVG asset fills with currentColor (3 sites); resvg renders that black.
    # The substitution swaps every token for the accent hex so no currentColor
    # (and so no black default) survives into the rendered SVG.
    raw = _SEAL_SVG.read_text(encoding="utf-8")
    assert _CURRENT_COLOR in raw
    out = _substitute_current_color(raw, "#16b384")
    assert _CURRENT_COLOR not in out
    assert out.count("#16b384") == raw.count(_CURRENT_COLOR)


def test_resolve_accent_hex_is_the_brand_accent() -> None:
    # The seal renders in the canonical reskin-green accent the header wordmark
    # + offline frame carry -- single-homed in brand.ACCENT_HEX.
    assert resolve_accent_hex() == ACCENT_HEX


def test_resolve_surface_hex_is_the_theme_surface() -> None:
    # W26: the seal's transparent corners + star knockout matte onto the main
    # content surface hex -- single-homed in seal.SURFACE_HEX (the dark theme's
    # $surface the hero sits on). It is a valid #rrggbb literal.
    surface = resolve_surface_hex()
    assert surface == SURFACE_HEX
    assert surface.startswith("#")
    assert len(surface) == 7


def test_seal_png_path_keys_on_px_accent_and_bg() -> None:
    # The cache path embeds the pixel size, the accent hex, AND the matte bg hex,
    # so a re-theme (or the transparent->matted fix) writes a fresh PNG rather
    # than reusing a stale one. Varying any of the three yields a distinct path.
    a = _seal_png_path(32, "#16b384", _BG)
    b = _seal_png_path(32, "#abcdef", _BG)
    c = _seal_png_path(48, "#16b384", _BG)
    d = _seal_png_path(32, "#16b384", "#ffffff")
    assert "16b384" in a.name
    assert _BG.lstrip("#") in a.name  # the bg hex is in the filename
    assert a != b  # accent is part of the key
    assert a != c  # px is part of the key
    assert a != d  # bg is part of the key


def test_seal_png_path_is_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tmp directory is namespaced by the current user so two operators on a
    # shared host never collide on (or read) each other's tmp PNG.
    monkeypatch.setattr(seal_mod.getpass, "getuser", lambda: "alice")
    alice = _seal_png_path(32, "#16b384", _BG)
    monkeypatch.setattr(seal_mod.getpass, "getuser", lambda: "bob")
    bob = _seal_png_path(32, "#16b384", _BG)
    assert alice != bob
    assert "alice" in str(alice.parent)
    assert "bob" in str(bob.parent)


def test_rasterised_seal_passes_accent_svg_to_resvg_never_current_color(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The load-bearing CR-01 assertion: the SVG handed to resvg carries the
    # accent hex and NO currentColor (so the rasterised PNG cannot be black).
    # resvg is stubbed -- we capture the SVG file path it is invoked with and
    # read back what was written there.
    captured: dict[str, str] = {}

    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        # cmd = [resvg, -w, px, -h, px, --background, bg, <svg_path>, <png_path>]
        svg_path = Path(cmd[-2])
        captured["svg"] = svg_path.read_text(encoding="utf-8")
        captured["png"] = cmd[-1]
        Path(cmd[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG sentinel
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(seal_mod.shutil, "which", lambda _name: "/usr/bin/resvg")
    monkeypatch.setattr(seal_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(seal_mod.subprocess, "run", _fake_run)

    out = _rasterised_seal(32, "#16b384", _BG)
    assert out is not None
    assert _CURRENT_COLOR not in captured["svg"]
    assert "#16b384" in captured["svg"]
    # The fill must never be the resvg black default the live defect produced.
    assert "currentColor" not in captured["svg"]
    assert "#000000" not in captured["svg"]
    assert 'fill="black"' not in captured["svg"]


def test_rasterised_seal_invokes_resvg_with_background_matte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # W26 load-bearing assertion: the resvg argv carries ``--background <bg_hex>``
    # so the seal's transparent corners + star knockout are matted onto the
    # surface hex (a graphics terminal otherwise composites the transparent
    # region onto black). The flag value is the exact bg hex passed in, and the
    # SVG / PNG operands still trail the flag (so cmd[-2:] stay the file paths).
    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(seal_mod.shutil, "which", lambda _name: "/usr/bin/resvg")
    monkeypatch.setattr(seal_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(seal_mod.subprocess, "run", _fake_run)

    out = _rasterised_seal(32, "#16b384", _BG)
    assert out is not None
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "--background" in cmd, "resvg must matte the transparent regions"
    # The flag value is the exact surface hex the seal mattes onto.
    assert cmd[cmd.index("--background") + 1] == _BG
    # The SVG + PNG operands still trail every flag.
    assert cmd[-2].endswith(".svg")
    assert cmd[-1].endswith(".png")
    # The PNG filename carries the bg hex so a stale transparent PNG can't survive.
    assert _BG.lstrip("#") in cmd[-1]


def test_rasterised_seal_bg_hex_is_part_of_cache_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Two different matte backgrounds at the same (px, accent) render to distinct
    # PNG paths -- the bg hex is in the filename AND the lru_cache key, so a stale
    # transparent PNG cannot be served when the surface changes.
    seen: list[str] = []

    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(cmd[-1])
        Path(cmd[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(seal_mod.shutil, "which", lambda _name: "/usr/bin/resvg")
    monkeypatch.setattr(seal_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(seal_mod.subprocess, "run", _fake_run)

    dark = _rasterised_seal(32, "#16b384", "#1e1e1e")
    light = _rasterised_seal(32, "#16b384", "#f5f5f5")
    assert dark is not None and light is not None
    assert dark != light, "a different matte bg must resolve to a different PNG path"
    # The lru_cache did NOT collapse the two distinct bg keys into one resvg run.
    assert len(seen) == 2


def test_rasterised_seal_overwrites_stale_black_png(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A pre-existing (black) PNG at the cache path must NOT survive the fix: the
    # rasterise overwrites it rather than short-circuiting on its existence.
    monkeypatch.setattr(seal_mod.shutil, "which", lambda _name: "/usr/bin/resvg")
    monkeypatch.setattr(seal_mod.tempfile, "gettempdir", lambda: str(tmp_path))

    stale_path = _seal_png_path(32, "#16b384", _BG)
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_bytes(b"STALE-BLACK-PNG")

    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(cmd[-1]).write_bytes(b"FRESH-ACCENT-PNG")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(seal_mod.subprocess, "run", _fake_run)
    out = _rasterised_seal(32, "#16b384", _BG)
    assert out == stale_path
    # The stale bytes are gone -- the resvg run rewrote the cache path.
    assert stale_path.read_bytes() == b"FRESH-ACCENT-PNG"


def test_rasterised_seal_none_when_resvg_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seal_mod.shutil, "which", lambda _name: None)
    assert _rasterised_seal(32, "#16b384", _BG) is None


@pytest.mark.filterwarnings("ignore:Image.Image.getdata is deprecated:DeprecationWarning")
@pytest.mark.skipif(shutil.which("resvg") is None, reason="resvg not on PATH")
def test_rasterised_seal_real_resvg_pixels_match_accent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # End-to-end pixel proof (only when resvg is actually installed): the seal
    # disc carries the accent colour, never the black resvg would emit for an
    # unsubstituted currentColor. With the surface matte the transparent regions
    # are filled with the surface hex and the disc edges anti-alias from accent
    # to surface, so the proof is: the densest disc pixels carry the accent AND
    # NO pixel is black (the live defect).
    pil = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(seal_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    accent_hex = "#16b384"
    out = _rasterised_seal(64, accent_hex, _BG)
    assert out is not None and out.exists()
    expected = (int(accent_hex[1:3], 16), int(accent_hex[3:5], 16), int(accent_hex[5:7], 16))
    image = pil.open(out).convert("RGBA")
    pixels = list(image.getdata())
    tol = 24

    # The disc is the dominant accent fill: at least some pixels are squarely
    # the accent green (never the resvg black default).
    def _is_accent(r: int, g: int, b: int) -> bool:
        return (
            abs(r - expected[0]) <= tol
            and abs(g - expected[1]) <= tol
            and abs(b - expected[2]) <= tol
        )

    accent_pixels = [(r, g, b) for (r, g, b, _a) in pixels if _is_accent(r, g, b)]
    assert accent_pixels, "the disc must carry the accent green, not black"
    # NO pixel is black -- the live defect this fix forecloses.
    for r, g, b, _a in pixels:
        assert not (r < 16 and g < 16 and b < 16), "pixel is black -- the defect"


@pytest.mark.filterwarnings("ignore:Image.Image.getdata is deprecated:DeprecationWarning")
@pytest.mark.skipif(shutil.which("resvg") is None, reason="resvg not on PATH")
def test_rasterised_seal_real_resvg_matte_fills_corners_opaque(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # W26 end-to-end matte proof (only when resvg is installed): the corner
    # pixels -- transparent in the raw SVG, which a graphics terminal would
    # composite onto black -- are now OPAQUE and carry the surface matte hex,
    # so the seal blends into the live TUI surface rather than a black square.
    pil = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(seal_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    out = _rasterised_seal(64, "#16b384", _BG)
    assert out is not None and out.exists()
    image = pil.open(out).convert("RGBA")
    bg = (int(_BG[1:3], 16), int(_BG[3:5], 16), int(_BG[5:7], 16))
    # The top-left corner sits outside the fisheye disc -> matte fill, fully opaque.
    corner = image.getpixel((0, 0))
    assert corner[3] == 255, "the matte must make the corner OPAQUE (no transparency)"
    tol = 24
    assert abs(corner[0] - bg[0]) <= tol
    assert abs(corner[1] - bg[1]) <= tol
    assert abs(corner[2] - bg[2]) <= tol
