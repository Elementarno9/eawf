"""Count the instructions the operator types again, and hold each one to a disposition.

An instruction the operator has to type twice is an instruction the harness did not
carry. Re-stating it does not make it hold: a re-typed prohibition wants a guard, a
re-typed verify-first wants a dispatch default, a re-typed "you missed" wants a lens or
a memory row. This module finds those instructions, counts them over a release window,
and reads the committed triage that says where each one over the configured threshold
went. Staying in prose is one of the dispositions, and it has to carry its argument.

What counts as one operator turn:

- a ``user`` message on a Run that nothing delegated, read off the tree without a daemon;
- a prompt in the host's local session history for this repository that the host marks
  as human-originated. A record without that mark is never a turn, whatever it says:
  peer-session messages, task notifications, compaction summaries and skill preambles
  all arrive as ``user`` records, and only the mark tells them apart. A history written
  before the host kept the mark therefore contributes nothing. Sidechains, tool
  results, harness-wrapped content and the reflection surface's own helper prompts are
  never turns either.

What counts as "the same rule, re-typed": each turn is scrubbed, split into sentences
(a question is consultation, not an instruction, and is skipped), and each sentence is
reduced to its content tokens -- lower-cased words of three or more characters, stop
words dropped, a plural or tense suffix stripped. A sentence restates a rule of the
compiled rule graph when at least :data:`MIN_SHARED_TOKENS` of its tokens appear in the
rule's title and instruction and they make up at least :data:`RULE_COVERAGE` of the
sentence. A sentence that restates no rule is compared with the earlier sentences of the
window: it joins the first cluster whose opening sentence shares
:data:`MIN_SHARED_TOKENS` tokens at a Jaccard similarity of at least
:data:`TURN_SIMILARITY`. A subject is counted once per turn that states it. Every step
is a pure function of the ordered turns, so a fixture pins the count exactly.

Operator wording leaves this machine only as counts and subject ids. The exemplar each
counted row carries is scrubbed, cut short, marked non-quotable, and written only to the
local reflection collection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.config.schema import VerifyConfig
from eawf.kernel.runtime.events import MessageSummaryPayload, RunEventKind
from eawf.kernel.state.types import UtcDatetime
from eawf.observability.reflect.report import NON_QUOTABLE_MARK, reflect_root
from eawf.observability.reflect.runs import RunReading, read_tree_runs
from eawf.observability.reflect.titles import REFLECTION_MARKER
from eawf.observability.telemetry.sources.session_history import (
    claude_history_root,
    discover_history,
)
from eawf.platform.rules.compile import CompiledRule, compile_card_graph
from eawf.platform.rules.loader import RuleSourceMissingError, load_rule_source
from eawf.platform.rules.modules import select_rule_modules
from eawf.platform.rules.render import builtin_rule_provider
from eawf.platform.scrub.scan import redact_text

logger = logging.getLogger(__name__)

#: The committed triage of the subjects over the threshold, relative to the repo root.
RETYPED_TRIAGE_PATH: Final = Path(".ea") / "retyped-triage.yaml"

#: The fewest content tokens a sentence must share with a rule or a cluster to match.
#: Below three, two common verbs ("run", "test") would join unrelated instructions.
MIN_SHARED_TOKENS: Final = 3

#: The share of a sentence's tokens a rule's vocabulary must cover for a restatement.
RULE_COVERAGE: Final = 0.6

#: The Jaccard similarity at which two sentences are one instruction typed twice.
TURN_SIMILARITY: Final = 0.6

#: The longest exemplar a counted row keeps.
EXEMPLAR_MAX_CHARS: Final = 160

#: The prefix of a subject that restates no compiled rule.
TYPED_SUBJECT_PREFIX: Final = "typed:"

#: The dispositions a subject over the threshold may take.
Disposition = Literal["guard", "dispatch_default", "lens", "memory_row", "argued_prose"]

#: The stem of the local file the counted rows are written to, after its date.
RETYPED_STEM_SUFFIX: Final = "-reflect-retyped"

#: Opening text of a prompt the harness wrapped or wrote rather than the operator typed.
_MACHINE_PREFIXES: Final = ("<", "[Request interrupted", "Caveat:")

#: The substrings a history line must carry to be worth decoding: a user record that is
#: not a tool result. A tool result is most of a history's bytes and never a turn.
_USER_RECORD: Final = '"type":"user"'
_TOOL_RESULT: Final = '"type":"tool_result"'

_PLACEHOLDER: Final = re.compile(r"<[a-z-]+>")
_WORD: Final = re.compile(r"[a-z0-9]+")
_SENTENCE_BREAK: Final = re.compile(r"(?<=[.!;])\s+|\n+")
_SUFFIXES: Final = (("ies", "y"), ("ied", "y"), ("ing", ""), ("ed", ""), ("s", ""))
#: Words that carry no instruction of their own.
_STOPWORD_TEXT: Final = (
    "the and for are but not you your yours this that these those with from into onto then "
    "than they them their there here what when where which who whom why how all any each "
    "can could should would will shall may might must have has had was were been being "
    "does did doing its our ours out over under again also just very too only same some "
    "such own off per via let lets get got about above below after before because while "
    "until both either neither nor one two"
)
_STOPWORDS: Final = frozenset(_STOPWORD_TEXT.split())


class RetypedRule(BaseModel):
    """One instruction the operator typed more than once in the window, counted.

    Attributes:
        subject: The compiled rule's id, or ``typed:<digest>`` for an instruction that
            restates no rule; the digest is over the cluster's opening tokens.
        rule_title: The rule's title, or ``None`` for a ``typed:`` subject.
        count: The turns that stated it.
        first_at: When it was first typed in the window.
        last_at: When it was last typed in the window.
        exemplar: The first sentence that stated it, scrubbed and cut short.
        mark: The citation-scope mark; operator wording is never quotable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    rule_title: str | None
    count: int = Field(ge=1)
    first_at: UtcDatetime
    last_at: UtcDatetime
    exemplar: str = Field(max_length=EXEMPLAR_MAX_CHARS)
    mark: str = NON_QUOTABLE_MARK


class RetypedDisposition(BaseModel):
    """Where one re-typed subject went.

    Attributes:
        subject: The subject the disposition is about.
        disposition: The mechanism that now carries it, or ``argued_prose``.
        reference: Where the mechanism lives -- the guard's enforcement reference, the
            dispatch default's key, the lens id, the memory row's name. Required for
            every mechanism and refused for ``argued_prose``.
        argument: Why the subject stays in prose. Required for ``argued_prose`` and
            refused otherwise, so prose is never the silent default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: Annotated[str, StringConstraints(strict=True, min_length=1)]
    disposition: Disposition
    reference: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    argument: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None

    @model_validator(mode="after")
    def _prose_is_argued_and_mechanisms_are_located(self) -> RetypedDisposition:
        """Refuse a disposition that names the wrong kind of backing.

        Raises:
            ValueError: ``argued_prose`` without an argument or with a reference, or a
                mechanism without a reference or with an argument.
        """
        if self.disposition == "argued_prose":
            if self.argument is None or self.reference is not None:
                raise ValueError("an argued_prose disposition carries an argument and no reference")
        elif self.reference is None or self.argument is not None:
            raise ValueError(
                f"a {self.disposition} disposition carries a reference and no argument"
            )
        return self


class RetypedTriageDocument(BaseModel):
    """The on-disk shape of the committed triage.

    Attributes:
        schema_version: Gates unknown future formats.
        dispositions: One disposition per subject, each subject at most once.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    dispositions: tuple[RetypedDisposition, ...] = ()

    @model_validator(mode="after")
    def _one_disposition_per_subject(self) -> RetypedTriageDocument:
        """Refuse a subject triaged twice, which would leave its home ambiguous.

        Raises:
            ValueError: A subject appears more than once.
        """
        seen: set[str] = set()
        for row in self.dispositions:
            if row.subject in seen:
                raise ValueError(f"subject {row.subject!r} is triaged more than once")
            seen.add(row.subject)
        return self


@dataclass(frozen=True, slots=True)
class OperatorTurn:
    """One message the operator typed, already scrubbed.

    Attributes:
        at: When it was typed.
        text: The scrubbed message.
    """

    at: datetime
    text: str


@dataclass(frozen=True, slots=True)
class RuleVocabulary:
    """The content tokens a rule is stated in.

    Attributes:
        subject: The rule's id.
        title: The rule's title.
        tokens: The content tokens of its title and instruction.
    """

    subject: str
    title: str
    tokens: frozenset[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class RetypedTriage:
    """The subjects over the threshold in one window, and the triage they meet.

    Attributes:
        threshold: The configured count a subject must exceed.
        since: The window's start, or ``None`` for the whole history.
        over: The counted rows over the threshold, most-typed first.
        dispositions: The committed triage, keyed by subject.
    """

    threshold: int
    since: datetime | None
    over: tuple[RetypedRule, ...]
    dispositions: Mapping[str, RetypedDisposition] = field(default_factory=dict)

    @property
    def untriaged(self) -> tuple[RetypedRule, ...]:
        """Return the rows over the threshold the triage gives no disposition."""
        return tuple(row for row in self.over if row.subject not in self.dispositions)


def instruction_tokens(text: str) -> frozenset[str]:
    """Return the content tokens of *text*.

    Args:
        text: Scrubbed text; its redaction placeholders carry no content.

    Returns:
        Lower-cased words of three or more characters, stop words dropped and a plural
        or tense suffix stripped where at least four characters remain.
    """
    words = _WORD.findall(_PLACEHOLDER.sub(" ", text.casefold()))
    return frozenset(_stem(word) for word in words if len(word) >= 3 and word not in _STOPWORDS)


def _stem(word: str) -> str:
    """Return *word* with its first matching suffix replaced, when enough remains."""
    for suffix, replacement in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + replacement
    return word


def _sentences(text: str) -> Iterator[tuple[str, frozenset[str]]]:
    """Yield each instruction-shaped sentence of *text* with its tokens."""
    for raw in _SENTENCE_BREAK.split(text):
        sentence = raw.strip()
        if not sentence or sentence.endswith("?"):
            continue
        tokens = instruction_tokens(sentence)
        if len(tokens) >= MIN_SHARED_TOKENS:
            yield sentence, tokens


def _operator_text(text: str) -> str | None:
    """Return *text* scrubbed, or ``None`` when the harness wrote it rather than the operator."""
    stripped = text.strip()
    if not stripped or stripped.startswith(_MACHINE_PREFIXES) or REFLECTION_MARKER in stripped:
        return None
    return redact_text(stripped)


def operator_turns_from_runs(readings: Sequence[RunReading]) -> tuple[OperatorTurn, ...]:
    """Return the operator's messages on the Runs nothing delegated.

    A delegated Run's ``user`` messages are the prompt its parent wrote, so only a Run
    without a parent is read.

    Args:
        readings: The tree's Runs and their event lines.

    Returns:
        One turn per unquarantined ``user`` message summary, scrubbed.
    """
    turns: list[OperatorTurn] = []
    for reading in readings:
        if reading.run.parent_run_ref is not None:
            continue
        for event in reading.events:
            payload = event.payload
            if (
                event.quarantine is not None
                or event.event_kind is not RunEventKind.MESSAGE_SUMMARIZED
                or not isinstance(payload, MessageSummaryPayload)
                or payload.message_role != "user"
            ):
                continue
            text = _operator_text(payload.summary)
            if text is not None:
                turns.append(OperatorTurn(at=event.observed_at or event.recorded_at, text=text))
    return tuple(turns)


def _record_text(record: Mapping[str, Any]) -> str | None:
    """Return the typed prompt a history record carries, or ``None`` when it is none."""
    if record.get("type") != "user" or record.get("isSidechain") or record.get("isMeta"):
        return None
    origin = record.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    blocks = [block for block in content if isinstance(block, dict)]
    if any(block.get("type") == "tool_result" for block in blocks):
        return None
    texts = [block["text"] for block in blocks if isinstance(block.get("text"), str)]
    return "\n".join(texts) if texts else None


def _timestamp(record: Mapping[str, Any]) -> datetime | None:
    """Return the record's timezone-aware timestamp, or ``None``."""
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def operator_turns_from_history(path: Path) -> tuple[OperatorTurn, ...]:
    """Return the operator's typed prompts in one host session history file.

    Reading is fail-open: an unreadable file or a malformed line yields fewer turns,
    because the history is the host's file and an observation, not a record.

    Args:
        path: One session history file.

    Returns:
        One scrubbed turn per human-typed prompt that carries a timestamp.
    """
    turns: list[OperatorTurn] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if _USER_RECORD not in line or _TOOL_RESULT in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                raw = _record_text(record)
                at = _timestamp(record)
                text = None if raw is None or at is None else _operator_text(raw)
                if text is not None and at is not None:
                    turns.append(OperatorTurn(at=at, text=text))
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(f"operator_turns_from_history file={path.name!r} err={type(exc).__name__}")
    return tuple(turns)


def mine_operator_turns(repo_root: Path, *, since: datetime | None) -> tuple[OperatorTurn, ...]:
    """Return every operator turn of the window, from the Runs and the host history.

    A turn both sources carry is kept once.

    Args:
        repo_root: The repository root; its ``.ea`` tree holds the Runs and its path
            names the host history directory.
        since: The window's start, or ``None`` for the whole history.

    Returns:
        The window's turns, oldest first.

    Raises:
        pydantic.ValidationError: A Run row or event line does not validate.
        LedgerTornTailError: The run ledger ends mid-line.
    """
    root = repo_root.resolve()
    turns = set(operator_turns_from_runs(read_tree_runs(root / ".ea")))
    floor = None if since is None else since.timestamp()
    for path in discover_history(claude_history_root(root)):
        if floor is not None and path.stat().st_mtime < floor:
            continue
        turns.update(operator_turns_from_history(path))
    window = sorted(
        (turn for turn in turns if since is None or turn.at >= since),
        key=lambda turn: (turn.at, turn.text),
    )
    logger.info(f"mine_operator_turns turns={len(window)} since={since}")
    return tuple(window)


def rule_vocabularies(rules: Iterable[CompiledRule]) -> tuple[RuleVocabulary, ...]:
    """Return each rule's vocabulary, ordered by rule id so a tie resolves the same way.

    Args:
        rules: The compiled rule graph's effective rules.

    Returns:
        One vocabulary per rule.
    """
    vocabularies = (
        RuleVocabulary(
            subject=str(rule.record.rule_id),
            title=rule.record.title,
            tokens=instruction_tokens(f"{rule.record.title}. {rule.record.instruction}"),
        )
        for rule in rules
    )
    return tuple(sorted(vocabularies, key=lambda vocabulary: vocabulary.subject))


def _restated_rule(
    tokens: frozenset[str], vocabularies: Sequence[RuleVocabulary]
) -> RuleVocabulary | None:
    """Return the rule a sentence restates best, or ``None`` when it restates none."""
    best: RuleVocabulary | None = None
    best_key = (0.0, 0)
    for vocabulary in vocabularies:
        shared = len(tokens & vocabulary.tokens)
        coverage = shared / len(tokens)
        if (
            shared >= MIN_SHARED_TOKENS
            and coverage >= RULE_COVERAGE
            and (coverage, shared) > best_key
        ):
            best, best_key = vocabulary, (coverage, shared)
    return best


@dataclass(slots=True)
class _Tally:
    """One subject's running count."""

    subject: str
    title: str | None
    tokens: frozenset[str]
    exemplar: str
    first_at: datetime
    last_at: datetime
    turns: set[int] = field(default_factory=set)

    def add(self, index: int, at: datetime) -> None:
        self.turns.add(index)
        self.first_at = min(self.first_at, at)
        self.last_at = max(self.last_at, at)

    def row(self) -> RetypedRule:
        return RetypedRule(
            subject=self.subject,
            rule_title=self.title,
            count=len(self.turns),
            first_at=self.first_at,
            last_at=self.last_at,
            exemplar=self.exemplar[:EXEMPLAR_MAX_CHARS],
        )


def _same_instruction(tokens: frozenset[str], cluster: _Tally) -> bool:
    """Return whether a sentence restates a cluster's opening sentence."""
    shared = len(tokens & cluster.tokens)
    return shared >= MIN_SHARED_TOKENS and shared / len(tokens | cluster.tokens) >= TURN_SIMILARITY


def _typed_subject(tokens: frozenset[str]) -> str:
    """Return the stable subject id of a cluster opened by *tokens*."""
    digest = hashlib.sha256(" ".join(sorted(tokens)).encode("utf-8")).hexdigest()
    return f"{TYPED_SUBJECT_PREFIX}{digest[:12]}"


def _cluster_for(
    clusters: list[_Tally], tokens: frozenset[str], sentence: str, at: datetime
) -> _Tally:
    """Return the earliest cluster *tokens* restate, opening a new one when none fits."""
    for cluster in clusters:
        if _same_instruction(tokens, cluster):
            return cluster
    opened = _Tally(_typed_subject(tokens), None, tokens, sentence, at, at)
    clusters.append(opened)
    return opened


def count_retyped_rules(
    turns: Sequence[OperatorTurn], vocabularies: Sequence[RuleVocabulary]
) -> tuple[RetypedRule, ...]:
    """Count, per subject, the turns that state it.

    Args:
        turns: The window's turns, oldest first.
        vocabularies: The compiled rules' vocabularies.

    Returns:
        One row per subject stated at least once, most-typed first and then by subject.
    """
    rules: dict[str, _Tally] = {}
    clusters: list[_Tally] = []
    for index, turn in enumerate(turns):
        for sentence, tokens in _sentences(turn.text):
            restated = _restated_rule(tokens, vocabularies)
            if restated is not None:
                tally = rules.setdefault(
                    restated.subject,
                    _Tally(
                        restated.subject,
                        restated.title,
                        restated.tokens,
                        sentence,
                        turn.at,
                        turn.at,
                    ),
                )
            else:
                tally = _cluster_for(clusters, tokens, sentence, turn.at)
            tally.add(index, turn.at)
    rows = [tally.row() for tally in (*rules.values(), *clusters)]
    return tuple(sorted(rows, key=lambda row: (-row.count, row.subject)))


def resolve_retyped_rule_threshold(repo_root: Path) -> int:
    """Return the count ``verify.retyped_rule_threshold`` resolves to for *repo_root*.

    Args:
        repo_root: Repo root the layered config is composed against.

    Returns:
        The configured threshold; a subject is triaged when its count exceeds it.

    Raises:
        pydantic.ValidationError: The ``verify`` section does not validate.
    """
    from eawf.kernel.config.layered import merge_config

    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    return VerifyConfig.model_validate(merged["verify"]).retyped_rule_threshold


def load_retyped_triage(repo_root: Path) -> RetypedTriageDocument:
    """Return the committed triage; a repository that has none has triaged nothing.

    Args:
        repo_root: The repository root.

    Returns:
        The validated triage.

    Raises:
        pydantic.ValidationError: The file does not match the closed schema.
        yaml.YAMLError: The file is not YAML.
    """
    path = repo_root / RETYPED_TRIAGE_PATH
    if not path.is_file():
        return RetypedTriageDocument(schema_version=1)
    return RetypedTriageDocument.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _compiled_rules(repo_root: Path) -> tuple[CompiledRule, ...]:
    """Return the committed rule graph's rules; a repository without a rule source has none."""
    try:
        selection = select_rule_modules(load_rule_source(repo_root))
    except RuleSourceMissingError:
        return ()
    return compile_card_graph(repo_root, builtin_rules=builtin_rule_provider(selection)).rules


def triage_retyped_rules(repo_root: Path, *, since: datetime | None) -> RetypedTriage:
    """Count the window's re-typed rules and meet the ones over the threshold with the triage.

    Args:
        repo_root: The repository root.
        since: The window's start, or ``None`` for the whole history.

    Returns:
        The subjects over the configured threshold and the committed dispositions.

    Raises:
        RuleSourceError: The rule source fails to load.
        RuleCompileError: The committed rules fail compilation.
        pydantic.ValidationError: The config, the triage, or a Run row does not validate.
    """
    threshold = resolve_retyped_rule_threshold(repo_root)
    turns = mine_operator_turns(repo_root, since=since)
    rows = count_retyped_rules(turns, rule_vocabularies(_compiled_rules(repo_root)))
    document = load_retyped_triage(repo_root)
    return RetypedTriage(
        threshold=threshold,
        since=since,
        over=tuple(row for row in rows if row.count > threshold),
        dispositions={row.subject: row for row in document.dispositions},
    )


def store_retyped_rows(tree_root: Path, rows: Sequence[RetypedRule], *, today: date) -> Path:
    """Write the counted rows to the local reflection collection.

    The rows carry scrubbed operator wording, so they stay on this machine: the
    collection is gitignored and retention-bounded like every reflection report.

    Args:
        tree_root: The tree's ``.ea`` directory.
        rows: The counted rows to keep.
        today: The date the file's stem opens with.

    Returns:
        The written file.
    """
    root = reflect_root(tree_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{today.isoformat()}{RETYPED_STEM_SUFFIX}.json"
    body = [row.model_dump(mode="json") for row in rows]
    path.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = [
    "EXEMPLAR_MAX_CHARS",
    "MIN_SHARED_TOKENS",
    "RETYPED_STEM_SUFFIX",
    "RETYPED_TRIAGE_PATH",
    "RULE_COVERAGE",
    "TURN_SIMILARITY",
    "TYPED_SUBJECT_PREFIX",
    "Disposition",
    "OperatorTurn",
    "RetypedDisposition",
    "RetypedRule",
    "RetypedTriage",
    "RetypedTriageDocument",
    "RuleVocabulary",
    "count_retyped_rules",
    "instruction_tokens",
    "load_retyped_triage",
    "mine_operator_turns",
    "operator_turns_from_history",
    "operator_turns_from_runs",
    "resolve_retyped_rule_threshold",
    "rule_vocabularies",
    "store_retyped_rows",
    "triage_retyped_rules",
]
