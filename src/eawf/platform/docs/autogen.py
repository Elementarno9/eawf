"""Introspection-driven reference-page generator for the Eä docs site.

Six reference surfaces are generated entirely from the live source tree,
never hand-authored, so a doc/source drift cannot survive a build:

- ``cli.md`` — the full ``eawf`` command inventory (top-level commands +
  every sub-group verb), walked from the root :data:`eawf.surfaces.cli.app.app`
  Typer instance.
- ``skills.md`` — the Eä skill catalog, read from
  :data:`eawf.surfaces.render.skills.SKILL_REGISTRY`.
- ``schema.md`` — the JSON Schema of the canonical Pydantic models
  (:class:`~eawf.kernel.state.models.State`, the event envelope, and the output
  envelope), with each schema also dumped to a sibling ``.schema.json``
  file by :func:`dump_schemas`.
- ``enums.md`` — the state enum catalog from
  :mod:`eawf.kernel.state.enums`.
- ``error-codes.md`` — the cause-level :class:`~eawf.surfaces.cli.error_codes.ErrorCode`
  vocabulary folded onto its exit bucket.
- ``exit-codes.md`` — the five-bucket exit-code surface from
  :mod:`eawf.surfaces.cli.exit_codes`.

The generated pages live under ``docs/reference/autogen/`` so they do not
collide with the curated prose pages directly under ``docs/reference/``.
:func:`generate_all` writes the lot; :func:`diff_against_disk` regenerates
in memory and reports any page whose committed bytes differ — the engine
behind ``eawf doc verify --strict``'s drift gate.

All output is deterministic: enums and command groups sort by name, JSON
Schema dumps go through :func:`json.dumps` with ``sort_keys=True`` and a
two-space indent, and a trailing newline closes every file.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
from pydantic import BaseModel

from eawf.kernel.state import enums as state_enums
from eawf.kernel.state.models import State
from eawf.kernel.store.kinds.event import Event
from eawf.surfaces.cli import error_codes as error_codes_mod
from eawf.surfaces.cli import exit_codes as exit_codes_mod
from eawf.surfaces.render.envelope import OutputEnvelope
from eawf.surfaces.render.plan_view import PlanView

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


# Path of the autogen reference directory relative to a repo root.
AUTOGEN_RELDIR: str = "docs/reference/autogen"

# Canonical Pydantic models dumped as JSON Schema, in deterministic order.
# Each entry is ``(stem, model)`` — ``stem`` names the ``.schema.json`` file
# and the schema's section in ``schema.md``.
SCHEMA_MODELS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("state", State),
    ("event", Event),
    ("output-envelope", OutputEnvelope),
)


@dataclass(frozen=True)
class GeneratedPage:
    """One generated reference artifact (markdown page or schema JSON).

    Attributes:
        relpath: Path of the artifact relative to the repo root.
        body: Full file contents (always newline-terminated).
    """

    relpath: str
    body: str


# --- JSON Schema dump --------------------------------------------------------


def _schema_json(model: type[BaseModel]) -> str:
    """Return the deterministic JSON Schema text for *model*."""
    schema = model.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def schema_pages() -> list[GeneratedPage]:
    """Return the ``.schema.json`` dump for every canonical model."""
    pages: list[GeneratedPage] = []
    for stem, model in SCHEMA_MODELS:
        pages.append(
            GeneratedPage(
                relpath=f"{AUTOGEN_RELDIR}/{stem}.schema.json",
                body=_schema_json(model),
            )
        )
    return pages


# --- Bundled runtime schemas (src/eawf/schemas/) ----------------------------
#
# The docs-reference dump above emits validation-mode schemas under
# ``docs/reference/autogen/``. The bundled runtime schemas below are a
# distinct surface: serialization-mode JSON Schema for the four canonical
# wire documents, written via orjson and committed under
# ``src/eawf/schemas/`` as the loadable runtime contract. ``dump_bundled_schemas``
# is the single generator for those files (test-verified against the
# committed copies); it lives here so all schema generation shares one home.

_BUNDLED_PLACEHOLDER: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": True,
}

_BUNDLED_ORJSON_OPTS = orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS


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


def generate_plan_view_schema() -> dict[str, Any]:
    """Return the JSON Schema for :class:`PlanView` as a plain dict.

    Uses ``mode="serialization"`` so the schema mirrors the wire format
    emitted by ``eawf plan show --json``. ``$schema`` and ``title`` are
    injected so consumers can rely on them.
    """
    schema: dict[str, Any] = PlanView.model_json_schema(mode="serialization", by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "EawfPlanView"
    return schema


def dump_bundled_schemas(output_dir: Path) -> None:
    """Write the bundled runtime schema files to *output_dir*.

    Files written (deterministic, sorted keys, orjson two-space indent):

    - ``state.schema.json``        — generated from ``State``.
    - ``config.schema.json``       — placeholder until the config model lands.
    - ``skill-output.schema.json`` — generated from ``OutputEnvelope``.
    - ``plan-view.schema.json``    — generated from ``PlanView``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    state_schema = generate_state_schema()
    (output_dir / "state.schema.json").write_bytes(
        orjson.dumps(state_schema, option=_BUNDLED_ORJSON_OPTS) + b"\n"
    )
    logger.debug(f"dump_bundled_schemas target={output_dir / 'state.schema.json'}")

    (output_dir / "config.schema.json").write_bytes(
        orjson.dumps(
            {**_BUNDLED_PLACEHOLDER, "title": "EawfConfig"},
            option=_BUNDLED_ORJSON_OPTS,
        )
        + b"\n"
    )
    logger.debug(f"dump_bundled_schemas target={output_dir / 'config.schema.json'}")

    skill_output_schema = generate_skill_output_schema()
    (output_dir / "skill-output.schema.json").write_bytes(
        orjson.dumps(skill_output_schema, option=_BUNDLED_ORJSON_OPTS) + b"\n"
    )
    logger.debug(f"dump_bundled_schemas target={output_dir / 'skill-output.schema.json'}")

    plan_view_schema = generate_plan_view_schema()
    (output_dir / "plan-view.schema.json").write_bytes(
        orjson.dumps(plan_view_schema, option=_BUNDLED_ORJSON_OPTS) + b"\n"
    )
    logger.debug(f"dump_bundled_schemas target={output_dir / 'plan-view.schema.json'}")


# --- CLI inventory -----------------------------------------------------------


def _command_help(name: str | None, help_text: str | None, callback: object) -> str:
    """Resolve the one-line summary for a command.

    Prefers the Typer-declared ``help`` string; falls back to the first
    line of the callback's docstring so commands whose help lives in the
    docstring still render a summary. Returns an em-dash placeholder when
    neither is present.
    """
    if help_text:
        return help_text.strip().splitlines()[0]
    if callback is not None:
        doc = inspect.getdoc(callback)
        if doc:
            return doc.splitlines()[0].strip()
    return "—"


def _md_escape(text: str) -> str:
    """Escape pipe characters so a cell never breaks a markdown table."""
    return text.replace("|", "\\|")


def cli_page() -> GeneratedPage:
    """Generate ``cli.md`` from the live root Typer app."""
    from eawf.surfaces.cli.app import app

    lines: list[str] = [
        "# eawf CLI reference",
        "",
        "Auto-generated from `eawf.surfaces.cli.app:app`. Every top-level command and",
        "sub-group verb registered on the root Typer app is listed below; do",
        "not hand-edit — regenerate via `eawf doc verify --strict`.",
        "",
        "## Top-level commands",
        "",
        "| Command | Summary |",
        "|---|---|",
    ]
    top = sorted(
        (
            (c.name or "", _command_help(c.name, c.help, c.callback))
            for c in app.registered_commands
            if c.name and not c.hidden
        ),
        key=lambda row: row[0],
    )
    for name, summary in top:
        lines.append(f"| `{name}` | {_md_escape(summary)} |")

    groups = sorted(
        (g for g in app.registered_groups if g.name),
        key=lambda g: g.name or "",
    )
    lines.extend(["", "## Command groups", ""])
    for group in groups:
        ti = group.typer_instance
        if ti is None:
            continue
        group_help = _command_help(group.name, ti.info.help, None)
        lines.append(f"### `eawf {group.name}`")
        lines.append("")
        lines.append(group_help)
        lines.append("")
        verbs = sorted(
            (
                (sc.name or "", _command_help(sc.name, sc.help, sc.callback))
                for sc in ti.registered_commands
                if sc.name and not sc.hidden
            ),
            key=lambda row: row[0],
        )
        if not verbs:
            lines.append("_No verbs registered._")
            lines.append("")
            continue
        lines.append("| Verb | Summary |")
        lines.append("|---|---|")
        for name, summary in verbs:
            lines.append(f"| `{name}` | {_md_escape(summary)} |")
        lines.append("")
    body = "\n".join(lines).rstrip("\n") + "\n"
    return GeneratedPage(relpath=f"{AUTOGEN_RELDIR}/cli.md", body=body)


# --- Skill catalog -----------------------------------------------------------


def skills_page() -> GeneratedPage:
    """Generate ``skills.md`` from :data:`SKILL_REGISTRY`."""
    from eawf.surfaces.render.skills import SKILL_REGISTRY

    lines: list[str] = [
        "# eawf skill catalog",
        "",
        "Auto-generated from `eawf.surfaces.render.skills:SKILL_REGISTRY`. Each row is",
        "an Eä skill the runtime can install as a slash command.",
        "",
        "| Skill | User-invocable | Argument hint | Description |",
        "|---|---|---|---|",
    ]
    for spec in sorted(SKILL_REGISTRY, key=lambda s: s.skill_name):
        invocable = "yes" if spec.user_invocable else "no"
        hint = spec.argument_hint or "—"
        lines.append(
            f"| `/{spec.skill_name}` | {invocable} | `{_md_escape(hint)}` "
            f"| {_md_escape(spec.description)} |"
        )
    body = "\n".join(lines).rstrip("\n") + "\n"
    return GeneratedPage(relpath=f"{AUTOGEN_RELDIR}/skills.md", body=body)


# --- JSON Schema markdown page ----------------------------------------------


def schema_page() -> GeneratedPage:
    """Generate ``schema.md`` linking each model's dumped JSON Schema."""
    lines: list[str] = [
        "# Eä JSON Schema reference",
        "",
        "Auto-generated from the canonical Pydantic models. The full JSON",
        "Schema of each model is dumped to a sibling `.schema.json` file by",
        "`eawf schema dump`; this page summarises the top-level properties.",
        "",
    ]
    for stem, model in SCHEMA_MODELS:
        schema = model.model_json_schema()
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        lines.append(f"## `{model.__name__}`")
        lines.append("")
        lines.append(f"Full schema: [`{stem}.schema.json`]({stem}.schema.json)")
        lines.append("")
        if not props:
            lines.append("_No top-level properties._")
            lines.append("")
            continue
        lines.append("| Property | Required |")
        lines.append("|---|---|")
        for prop_name in sorted(props):
            req = "yes" if prop_name in required else "no"
            lines.append(f"| `{_md_escape(prop_name)}` | {req} |")
        lines.append("")
    body = "\n".join(lines).rstrip("\n") + "\n"
    return GeneratedPage(relpath=f"{AUTOGEN_RELDIR}/schema.md", body=body)


# --- Enum catalog ------------------------------------------------------------


def _enum_classes() -> Iterator[type[Enum]]:
    """Yield every :class:`Enum` subclass declared in :mod:`eawf.kernel.state.enums`."""
    for attr in dir(state_enums):
        obj = getattr(state_enums, attr)
        if (
            isinstance(obj, type)
            and issubclass(obj, Enum)
            and obj is not Enum
            and obj.__module__ == state_enums.__name__
        ):
            yield obj


def enums_page() -> GeneratedPage:
    """Generate ``enums.md`` from :mod:`eawf.kernel.state.enums`."""
    lines: list[str] = [
        "# eawf state enums",
        "",
        "Auto-generated from `eawf.kernel.state.enums`. Every `StrEnum` defined in",
        "that module is listed with its members.",
        "",
        "| Class | Values |",
        "|---|---|",
    ]
    for enum_cls in sorted(_enum_classes(), key=lambda c: c.__name__):
        values = ", ".join(f"`{member.value}`" for member in enum_cls)
        lines.append(f"| `{enum_cls.__name__}` | {_md_escape(values)} |")
    body = "\n".join(lines).rstrip("\n") + "\n"
    return GeneratedPage(relpath=f"{AUTOGEN_RELDIR}/enums.md", body=body)


# --- Error-code catalog ------------------------------------------------------


def error_codes_page() -> GeneratedPage:
    """Generate ``error-codes.md`` from :class:`ErrorCode`."""
    lines: list[str] = [
        "# eawf error codes",
        "",
        "Auto-generated from `eawf.surfaces.cli.error_codes:ErrorCode`. Each cause-level",
        "member folds onto exactly one of the five exit buckets via",
        "`ErrorCode.exit_code`.",
        "",
        "| Error code | Exit bucket | Exit code |",
        "|---|---|---|",
    ]
    for member in sorted(error_codes_mod.ErrorCode, key=lambda m: m.value):
        bucket_code = member.exit_code
        bucket_name = exit_codes_mod.name_for(bucket_code)
        lines.append(f"| `{member.value}` | `{bucket_name}` | {bucket_code} |")
    body = "\n".join(lines).rstrip("\n") + "\n"
    return GeneratedPage(relpath=f"{AUTOGEN_RELDIR}/error-codes.md", body=body)


# --- Exit-code surface -------------------------------------------------------


def exit_codes_page() -> GeneratedPage:
    """Generate ``exit-codes.md`` from the five-bucket exit-code surface."""
    lines: list[str] = [
        "# eawf exit codes",
        "",
        "Auto-generated from `eawf.surfaces.cli.exit_codes`. The canonical five-bucket",
        "surface every CLI handler exits with.",
        "",
        "| Code | Name |",
        "|---|---|",
    ]
    surface = [
        exit_codes_mod.OK,
        exit_codes_mod.USER_ERROR,
        exit_codes_mod.VALIDATION_ERROR,
        exit_codes_mod.STATE_CONFLICT,
        exit_codes_mod.DAEMON_UNREACHABLE,
        exit_codes_mod.INTERNAL_ERROR,
    ]
    for code in surface:
        lines.append(f"| {code} | `{exit_codes_mod.name_for(code)}` |")
    body = "\n".join(lines).rstrip("\n") + "\n"
    return GeneratedPage(relpath=f"{AUTOGEN_RELDIR}/exit-codes.md", body=body)


# --- Index + orchestration ---------------------------------------------------


def index_page() -> GeneratedPage:
    """Generate the autogen ``index.md`` listing the generated pages."""
    lines: list[str] = [
        "# Auto-generated reference",
        "",
        "These pages are regenerated from the live source tree by",
        "`eawf doc verify --strict`. Do not hand-edit — a drift diff fails CI.",
        "",
        "- [CLI reference](cli.md)",
        "- [Skill catalog](skills.md)",
        "- [JSON Schema reference](schema.md)",
        "- [State enums](enums.md)",
        "- [Error codes](error-codes.md)",
        "- [Exit codes](exit-codes.md)",
        "",
    ]
    body = "\n".join(lines).rstrip("\n") + "\n"
    return GeneratedPage(relpath=f"{AUTOGEN_RELDIR}/index.md", body=body)


def all_pages() -> list[GeneratedPage]:
    """Return every generated reference artifact in deterministic order."""
    pages: list[GeneratedPage] = [
        index_page(),
        cli_page(),
        skills_page(),
        schema_page(),
        enums_page(),
        error_codes_page(),
        exit_codes_page(),
    ]
    pages.extend(schema_pages())
    return sorted(pages, key=lambda p: p.relpath)


def generate_all(repo_root: Path) -> list[Path]:
    """Write every generated reference artifact under *repo_root*.

    Args:
        repo_root: Workspace root the ``docs/reference/autogen`` tree
            lives under.

    Returns:
        The list of written paths (absolute), in deterministic order.
    """
    written: list[Path] = []
    for page in all_pages():
        dest = repo_root / page.relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page.body, encoding="utf-8")
        written.append(dest)
    logger.info(f"generate_all root={str(repo_root)!r} pages={len(written)}")
    return written


def dump_schemas(repo_root: Path) -> list[Path]:
    """Write only the ``.schema.json`` dumps under *repo_root*.

    Args:
        repo_root: Workspace root the ``docs/reference/autogen`` tree
            lives under.

    Returns:
        The list of written schema-file paths (absolute).
    """
    written: list[Path] = []
    for page in schema_pages():
        dest = repo_root / page.relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page.body, encoding="utf-8")
        written.append(dest)
    logger.info(f"dump_schemas root={str(repo_root)!r} files={len(written)}")
    return written


@dataclass(frozen=True)
class AutogenDrift:
    """One reference page whose committed bytes differ from the regenerated body."""

    relpath: str
    reason: str  # "missing" | "changed"


def diff_against_disk(repo_root: Path) -> list[AutogenDrift]:
    """Regenerate every page in memory and report any disk divergence.

    Args:
        repo_root: Workspace root the ``docs/reference/autogen`` tree
            lives under.

    Returns:
        A list of :class:`AutogenDrift` rows — empty when every committed
        page matches the freshly generated body.
    """
    drift: list[AutogenDrift] = []
    for page in all_pages():
        dest = repo_root / page.relpath
        if not dest.exists():
            drift.append(AutogenDrift(relpath=page.relpath, reason="missing"))
            continue
        if dest.read_text(encoding="utf-8") != page.body:
            drift.append(AutogenDrift(relpath=page.relpath, reason="changed"))
    return drift


__all__ = [
    "AUTOGEN_RELDIR",
    "SCHEMA_MODELS",
    "AutogenDrift",
    "GeneratedPage",
    "all_pages",
    "cli_page",
    "diff_against_disk",
    "dump_bundled_schemas",
    "dump_schemas",
    "enums_page",
    "error_codes_page",
    "exit_codes_page",
    "generate_all",
    "generate_plan_view_schema",
    "generate_skill_output_schema",
    "generate_state_schema",
    "index_page",
    "schema_page",
    "schema_pages",
    "skills_page",
]
