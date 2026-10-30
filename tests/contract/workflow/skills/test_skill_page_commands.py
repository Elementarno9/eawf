"""Contract: every ``eawf`` command a rendered skill page names is a real command.

A host session (Claude Code, Codex, OpenCode) acts by running ``eawf`` CLI
commands, so a page that names a command the CLI does not carry, or an option
that command does not declare, sends the agent into a usage error. The test
renders every shipped page through each host's renderer, reads every
``eawf ...`` mention out of inline code and fenced code, and resolves it
against the live Click tree: the command path must exist and every
``--option`` the mention names must be a parameter of that command or a root
option such as ``--json`` or ``--workspace``. Placeholders such as ``<KEY>``
and argument values are positional tokens and are not resolved.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
import typer

from eawf.runtime.runtimes.codex.skills import render_codex_skill
from eawf.runtime.runtimes.opencode.plugin_install import install_plugin
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS
from eawf.surfaces.render.skills.render import render_skill_md_from_spec
from eawf.workflow.skills.bodies.chassis import chassis_findings, shipped_skill_page
from eawf.workflow.skills.catalog import SKILL_CATALOG, shipped_skill_specs

_INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_MENTION = re.compile(r"(?<![\w./-])eawf(?:\s+[^\s`]+)+")
_OPTION = re.compile(r"^--[a-z][a-z0-9-]*")
_ROUTE = re.compile(r"`([a-z_]+(?:\.[a-z_]+)+)`")
_NAMED_BY = re.compile(r"`eawf ([^`]+)` \($")


def _root() -> click.Group:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    return root


def _mentions(page: str) -> list[str]:
    """Return every ``eawf ...`` mention in the code of *page*, in page order."""
    fenced = [match.group(1) for match in _FENCE.finditer(page)]
    prose = _FENCE.sub("", page)
    spans = [*fenced, *(match.group(1) for match in _INLINE_CODE.finditer(prose))]
    return [
        match.group(0)
        for span in spans
        for line in span.splitlines()
        for match in _MENTION.finditer(line)
    ]


def command_problem(mention: str, root: click.Group) -> str | None:
    """Return why *mention* does not resolve against *root*, or ``None`` when it does.

    The mention is walked one token at a time: a token naming a subcommand of
    the current group descends into it, and the first token that names none
    ends the command path. Every later ``--option`` token, stripped of an
    ``=value`` and of grammar brackets, must be declared by the resolved
    command or by the root group.
    """
    tokens = mention.split()[1:]
    command: click.Command = root
    path = ["eawf"]
    rest: list[str] = []
    for index, token in enumerate(tokens):
        if isinstance(command, click.Group) and not token.startswith("-"):
            child = command.get_command(click.Context(command), token)
            if child is not None:
                command, path = child, [*path, token]
                continue
            if command is root or not path[1:]:
                return f"no command {' '.join([*path, token])!r}"
        rest = tokens[index:]
        break
    if isinstance(command, click.Group) and rest and not rest[0].startswith("-"):
        return f"no command {' '.join([*path, rest[0]])!r}"
    declared = {opt for param in (*command.params, *root.params) for opt in param.opts}
    declared |= {opt for param in command.params for opt in param.secondary_opts}
    for token in rest:
        option = _OPTION.match(token.lstrip("[(|"))
        if option is not None and option.group(0) not in declared:
            return f"{' '.join(path)!r} has no option {option.group(0)}"
    return None


def _claude_pages() -> dict[str, str]:
    return {spec.skill_name: render_skill_md_from_spec(spec) for spec in shipped_skill_specs()}


def _codex_pages() -> dict[str, str]:
    return {spec.skill_name: render_codex_skill(spec) for spec in shipped_skill_specs()}


def _opencode_pages(tmp_path: Path) -> dict[str, str]:
    install_plugin(tmp_path, persist_manifest=False)
    commands = sorted((tmp_path / ".opencode" / "commands").glob("*.md"))
    assert commands, "the OpenCode render wrote no command page"
    return {path.stem: path.read_text(encoding="utf-8") for path in commands}


@pytest.mark.parametrize("host", ["claude", "codex", "opencode"])
def test_every_named_eawf_command_exists_with_its_options(host: str, tmp_path: Path) -> None:
    if host == "claude":
        pages = _claude_pages()
    elif host == "codex":
        pages = _codex_pages()
    else:
        pages = _opencode_pages(tmp_path)
    root = _root()
    named = 0
    offenders: list[str] = []
    for skill, page in sorted(pages.items()):
        for mention in _mentions(page):
            named += 1
            problem = command_problem(mention, root)
            if problem is not None:
                offenders.append(f"{host}/{skill}: `{mention}`: {problem}")
    assert named, f"no {host} page names an eawf command; the extractor is broken"
    assert not offenders, "\n".join(offenders)


def test_mentions_read_inline_and_fenced_code_only() -> None:
    page = (
        "Run `eawf task ready <KEY>` then eawf is idle.\n\n"
        "```bash\neawf plan submit --from-spec plan.yaml\n```\n"
    )
    assert _mentions(page) == ["eawf plan submit --from-spec plan.yaml", "eawf task ready <KEY>"]


def test_mentions_of_a_page_without_code_are_empty() -> None:
    assert _mentions("") == []
    assert _mentions("The eawf daemon owns state.") == []


@pytest.mark.parametrize(
    "mention",
    [
        "eawf task ready <KEY>",
        "eawf plan submit --from-spec <path> --idempotency-key <key> --actor <agent>",
        "eawf plan apply <PRV-KEY> --expected-revision <N>",
        "eawf --json plan show",
        "eawf plan show --json",
        "eawf plan approve <PRV-KEY> --approved-by=<OP-KEY>",
        "eawf why",
        "eawf campaign",
        "eawf skill check-report /plan [--json]",
    ],
)
def test_a_real_command_and_its_options_resolve(mention: str) -> None:
    assert command_problem(mention, _root()) is None


@pytest.mark.parametrize(
    ("mention", "problem"),
    [
        ("eawf tasks ready", "no command 'eawf tasks'"),
        ("eawf task readyy <KEY>", "no command 'eawf task readyy'"),
        ("eawf plan submit --from-specs x", "'eawf plan submit' has no option --from-specs"),
        ("eawf plan approve --actor=OP-0001 --nope", "'eawf plan approve' has no option --nope"),
    ],
)
def test_an_unknown_command_or_option_is_named(mention: str, problem: str) -> None:
    assert command_problem(mention, _root()) == problem


def _route_verbs() -> dict[str, set[str]]:
    """Map each daemon route a CLI verb drives to those verbs.

    A projection route is left out: it is the read a mutating verb makes
    first, so no verb carrying it is the command that performs the read.
    """
    verbs: dict[str, set[str]] = {}
    for verb, effect in CLI_VERB_EFFECTS.items():
        for route in effect.routes:
            if not route.startswith("projection."):
                verbs.setdefault(route, set()).add(verb)
    return verbs


def route_problems(page: str, route_verbs: dict[str, set[str]]) -> list[str]:
    """Return each route on *page* a CLI verb drives but no command beside it names.

    A host session cannot call a daemon route; it runs the ``eawf`` command
    that drives it, so the route may appear only in parentheses straight after
    one of its commands.
    """
    problems: list[str] = []
    for match in _ROUTE.finditer(page):
        route = match.group(1)
        if route not in route_verbs:
            continue
        named = _NAMED_BY.search(page[: match.start()])
        if named is None or named.group(1) not in route_verbs[route]:
            commands = ", ".join(f"eawf {verb}" for verb in sorted(route_verbs[route]))
            problems.append(f"`{route}` is named without its command ({commands})")
    return problems


def test_every_route_a_command_drives_is_named_by_that_command() -> None:
    route_verbs = _route_verbs()
    offenders = [
        f"{spec.skill_name}: {problem}"
        for spec in shipped_skill_specs()
        for problem in dict.fromkeys(route_problems(spec.body, route_verbs))
    ]
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize(
    ("page", "problems"),
    [
        ("", []),
        ("Reads `projection.campaign.view` only.", []),
        ("Run `eawf plan submit` (`planning.plan_revision.submit`).", []),
        (
            "Call `planning.plan_revision.submit`.",
            ["`planning.plan_revision.submit` is named without its command (eawf plan submit)"],
        ),
        (
            "Run `eawf plan apply` (`planning.plan_revision.submit`).",
            ["`planning.plan_revision.submit` is named without its command (eawf plan submit)"],
        ),
    ],
)
def test_route_problems_boundaries(page: str, problems: list[str]) -> None:
    assert route_problems(page, _route_verbs()) == problems


@pytest.mark.parametrize(
    ("added", "refused"),
    [
        ("Run `eawf plan apply <PRV-KEY> --actor <operator>`.", False),
        ("Name yourself with `--actor`.", True),
        ("Run `eawf plan apply` with --actor set.", True),
    ],
)
def test_chassis_refuses_a_grammar_option_only_outside_an_eawf_command(
    added: str, refused: bool
) -> None:
    entry = SKILL_CATALOG.entry("plan")
    assert entry is not None
    page = shipped_skill_page(entry).replace(
        "## 5. Constraints\n\n", f"## 5. Constraints\n\n{added}\n"
    )
    finding = "option --actor is not in the /plan grammar"
    assert (finding in chassis_findings(page, entry)) is refused
