"""``model_session_cwd`` statusline module — the model and project segments.

The powerline's line 1 shows the model and the project apart, so
:func:`build_model` and :func:`build_project` state each on its own segment:
the model's display name, and the basename of the payload's
``workspace.project_dir``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eawf.runtime.runtimes.claude.statusline_modules._host import host_source
from eawf.surfaces.render.statusline import (
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

_MODEL_SOURCE = host_source("model.display_name")
_PROJECT_SOURCE = host_source("workspace.project_dir")


def _model_label(payload: dict[str, Any]) -> str | None:
    """Return the model identifier, or ``None`` when absent.

    Accepts either ``model: "<name>"`` (string) or ``model: {"id":"..."}``
    / ``model: {"name":"..."}`` (mapping) — Claude has shipped both shapes
    historically.
    """
    raw = payload.get("model")
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        for key in ("display_name", "id", "name"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def build_model(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``model:<display name>`` segment, or the marker naming why not.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        state_path: Unused; kept for the uniform module signature.

    Returns:
        The model segment; ``model:n/a(no-model)`` when the payload names none.
    """
    del state_path  # accepted for uniform signature
    model = _model_label(claude_payload)
    if model is None:
        return unavailable_segment("model", "model", "no-model", _MODEL_SOURCE)
    return sourced_segment("model", "model", model, _MODEL_SOURCE)


def build_project(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``project:<basename>`` segment, or the marker naming why not.

    Args:
        claude_payload: Decoded Claude stdin JSON, read for
            ``workspace.project_dir``.
        state_path: Unused; kept for the uniform module signature.

    Returns:
        The project segment; ``project:n/a(no-project-dir)`` when the payload
        names no project directory.
    """
    del state_path  # accepted for uniform signature
    workspace = claude_payload.get("workspace")
    raw = workspace.get("project_dir") if isinstance(workspace, dict) else None
    name = Path(raw).name if isinstance(raw, str) else ""
    if not name:
        return unavailable_segment("project", "project", "no-project-dir", _PROJECT_SOURCE)
    return sourced_segment("project", "project", name, _PROJECT_SOURCE)


__all__ = ["build_model", "build_project"]
