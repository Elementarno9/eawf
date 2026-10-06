"""The collection gate every telemetry producer consults before it writes.

``telemetry.enabled`` is an opt-out of collection, not of display: when it is
false no producer may append a row whose only consumer is the telemetry
projection. Run spend and token usage are not telemetry -- Run token budgets
and campaign budgets are enforced from them -- so the Run capture and the
``dispatch_cost`` metering events stay ungated.
"""

from __future__ import annotations

from pathlib import Path


def telemetry_enabled(repo_root: Path) -> bool:
    """Return whether telemetry collection is enabled for *repo_root*.

    The value is resolved through the full layered config merge, so a
    global, repo, local or environment layer can each turn collection off.

    Args:
        repo_root: The project root, the parent of its ``.ea`` directory.

    Returns:
        The merged ``telemetry.enabled`` leaf.
    """
    from eawf.kernel.config.layered import get_dotted, merge_config

    merged, _sources = merge_config(repo=repo_root)
    return bool(get_dotted(merged, "telemetry.enabled"))


__all__ = ["telemetry_enabled"]
