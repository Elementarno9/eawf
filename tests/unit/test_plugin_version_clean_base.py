"""Binding-proof regression for SHIP-3: programmatic version reads stay clean.

P30-I15-W02 added :func:`compose_display_version`, which layers a PEP 440
``+dev.g<sha>`` local segment onto the base when running from an editable
checkout. That decoration is a **display-surface concern only** -- the
``eawf --version`` banner. Programmatic consumers (the three runtime
plugin manifests, ``eawf.__version__``, and
``importlib.metadata.version("eawf")``) MUST read the *clean base* so the
wheel and the editable checkout emit byte-identical manifests; an editable
checkout decorating a manifest version would desync the wheel-vs-source
plugin trees and churn drift detection.

This module pins that invariant three ways:

- **clean-base equality** -- each runtime's ``_PLUGIN_VERSION`` equals the
  stored ``eawf.__version__`` with no ``+dev`` substring, so a future wave
  that routes a runtime through :func:`compose_display_version` reds the
  gate.
- **importlib clean base** -- ``importlib.metadata.version("eawf")`` (the
  installed-distribution read) carries no ``+dev`` local segment.
- **no display-composer coupling** -- none of the three
  ``plugin_install`` modules import or reference
  :func:`compose_display_version`, so the decoration cannot leak into a
  manifest derivation without first tripping this static check.
"""

from __future__ import annotations

import importlib.metadata

import eawf
from eawf.runtime.runtimes.claude import plugin_install as claude_plugin_install
from eawf.runtime.runtimes.codex import plugin_install as codex_plugin_install
from eawf.runtime.runtimes.opencode import plugin_install as opencode_plugin_install

_DEV_LOCAL_SEGMENT: str = "+dev"
_DISPLAY_COMPOSER: str = "compose_display_version"

_RUNTIME_MODULES = (
    claude_plugin_install,
    codex_plugin_install,
    opencode_plugin_install,
)


def test_plugin_version_is_clean_base() -> None:
    """Each runtime ``_PLUGIN_VERSION`` is the clean base -- never ``+dev``.

    The regression reds if a future wave routes a programmatic consumer
    through :func:`compose_display_version`: the moment a runtime derives
    ``_PLUGIN_VERSION`` from the dev-decorated display string, the
    ``+dev`` substring appears and the equality with ``eawf.__version__``
    breaks.
    """
    for module in _RUNTIME_MODULES:
        plugin_version = module._PLUGIN_VERSION
        assert plugin_version == eawf.__version__, (
            f"{module.__name__}._PLUGIN_VERSION={plugin_version!r} "
            f"diverged from clean base {eawf.__version__!r}"
        )
        assert _DEV_LOCAL_SEGMENT not in plugin_version, (
            f"{module.__name__}._PLUGIN_VERSION={plugin_version!r} "
            f"carries a display-only {_DEV_LOCAL_SEGMENT} local segment"
        )


def test_importlib_version_is_clean_base() -> None:
    """``importlib.metadata.version('eawf')`` carries no ``+dev`` segment.

    The installed-distribution metadata read is a programmatic consumer
    (plugin manifests, packaging tools); it must report the clean base,
    not the editable-checkout display decoration.
    """
    metadata_version = importlib.metadata.version("eawf")
    assert _DEV_LOCAL_SEGMENT not in metadata_version, (
        f"importlib.metadata.version('eawf')={metadata_version!r} "
        f"carries a display-only {_DEV_LOCAL_SEGMENT} local segment"
    )
    assert metadata_version == eawf.__version__, (
        f"importlib.metadata.version('eawf')={metadata_version!r} "
        f"diverged from eawf.__version__={eawf.__version__!r}"
    )


def test_runtime_module_dunder_version_is_clean_base() -> None:
    """The shared ``eawf.__version__`` the runtimes read carries no ``+dev``.

    All three ``_PLUGIN_VERSION`` derivations bind ``eawf.__version__``
    directly; pinning the source itself clean makes the binding-proof
    complete -- the decoration lives only on the display surface.
    """
    assert _DEV_LOCAL_SEGMENT not in eawf.__version__, (
        f"eawf.__version__={eawf.__version__!r} carries a display-only "
        f"{_DEV_LOCAL_SEGMENT} local segment"
    )


def test_runtimes_do_not_import_display_composer() -> None:
    """None of the three ``plugin_install`` modules touch the display composer.

    A runtime that imports or references :func:`compose_display_version`
    is one edit away from threading the ``+dev`` decoration into a
    manifest derivation. This static check reds the moment any runtime
    couples to the display-only composer, before the decorated string can
    reach an emitted manifest.
    """
    for module in _RUNTIME_MODULES:
        assert not hasattr(module, _DISPLAY_COMPOSER), (
            f"{module.__name__} exposes {_DISPLAY_COMPOSER!r} -- the "
            f"display-only composer must not reach a plugin manifest"
        )
        source = inspect_module_source(module)
        assert _DISPLAY_COMPOSER not in source, (
            f"{module.__name__} references {_DISPLAY_COMPOSER!r} in source -- "
            f"programmatic version reads must not route through the display composer"
        )
        assert "_version_display" not in source, (
            f"{module.__name__} imports the display-version module -- "
            f"programmatic version reads must stay on the clean base"
        )


def inspect_module_source(module: object) -> str:
    """Return the source text of *module* for the static-coupling check.

    Raises:
        OSError: when the module source cannot be located on disk.
    """
    import inspect

    return inspect.getsource(module)  # type: ignore[arg-type]
