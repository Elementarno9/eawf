"""JSON Schema export for eawf state and related documents.

``generate_state_schema()`` derives the schema from the Pydantic ``State``
model using ``mode="serialization"`` so the output reflects the wire format
rather than Python-side coercions.  ``dump_schemas()`` writes three files
to a target directory:

- ``state.schema.json``       — generated from ``State``.
- ``config.schema.json``      — placeholder; Phase 2 fills the body.
- ``skill-output.schema.json`` — generated from ``OutputEnvelope`` at
  Phase 4 W01.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import orjson

from eawf.render.envelope import OutputEnvelope
from eawf.state.models import State

logger = logging.getLogger(__name__)

PLACEHOLDER: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}

_ORJSON_OPTS = orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS


def generate_state_schema() -> dict[str, Any]:
    """Return the JSON Schema for the ``State`` model as a plain dict.

    Uses ``mode="serialization"`` so the schema reflects the wire format
    (StrEnum values as strings, datetimes as ISO-8601 strings, etc.).
    ``$schema`` and ``title`` are injected so consumers can rely on them.
    """
    schema: dict[str, Any] = State.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "EawfState"
    return schema


def generate_skill_output_schema() -> dict[str, Any]:
    """Return the JSON Schema for :class:`OutputEnvelope` as a plain dict.

    Uses ``mode="serialization"`` so the schema reflects the wire format
    (datetimes as ISO-8601 strings, ``EnvelopeWarning`` as nested object).
    ``$schema`` and ``title`` are injected so consumers can rely on them.
    """
    schema: dict[str, Any] = OutputEnvelope.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "EawfSkillOutput"
    return schema


def dump_schemas(out_dir: Path) -> None:
    """Write all schema files to *out_dir*, creating it if necessary.

    Files written (deterministic, sorted keys):
    - ``state.schema.json``
    - ``config.schema.json``
    - ``skill-output.schema.json``
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    state_schema = generate_state_schema()
    (out_dir / "state.schema.json").write_bytes(orjson.dumps(state_schema, option=_ORJSON_OPTS))
    logger.debug(f"wrote {out_dir / 'state.schema.json'}")

    (out_dir / "config.schema.json").write_bytes(
        orjson.dumps(
            {**PLACEHOLDER, "title": "EawfConfig"},
            option=_ORJSON_OPTS,
        )
    )
    logger.debug(f"wrote {out_dir / 'config.schema.json'}")

    skill_output_schema = generate_skill_output_schema()
    (out_dir / "skill-output.schema.json").write_bytes(
        orjson.dumps(skill_output_schema, option=_ORJSON_OPTS)
    )
    logger.debug(f"wrote {out_dir / 'skill-output.schema.json'}")
