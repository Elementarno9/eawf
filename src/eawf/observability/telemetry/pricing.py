"""Dated list prices loaded from the shipped price files, and the pricing rules.

The rates live in ``eawf/_data/pricing/<vendor>.yaml``, one file per vendor,
in USD per million tokens as :class:`~decimal.Decimal`. Each model row is in
effect from ``effective_from`` (``None``: since launch) up to, not including,
``effective_to``; a price change closes the old row and adds a new one, so a
request keeps the price of its own UTC date.

A model id resolves by exact row id, then by a row's ``match`` list after
stripping the ``[1m]`` context suffix, then by the alias in effect. Nothing
else matches: an id with no row is unpriced, never zero and never priced at a
neighbour's rate. A row with ``rates: null`` names a model whose rate is not
confirmed; it resolves, and prices none of its tokens.

An operator's ``reflect.pricing.overrides.<model-id>`` in the layered config
replaces the file's rates for that model and marks the price ``override``.
:class:`PriceBook` holds the files and the overrides together with one digest,
so an index can tell that its stored costs were priced under other rates.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Final, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.config.schema import PriceOverride, PriceRates, ReflectConfig

logger = logging.getLogger(__name__)

__all__ = [
    "CACHE_WRITE_1H_MULTIPLIER",
    "CACHE_WRITE_5M_MULTIPLIER",
    "LONG_CONTEXT_SUFFIX",
    "OVERRIDE_RATE_VERSION",
    "PRICE_FILE_DIR",
    "FastRates",
    "PriceAlias",
    "PriceBook",
    "PriceFile",
    "PriceModifiers",
    "PriceQuote",
    "PriceRow",
    "PriceTier",
    "PricingDriftFinding",
    "PricingDriftReport",
    "TokenUsage",
    "active_price_book",
    "check_pricing_currency",
    "load_price_book",
    "load_price_file",
    "price_book_root",
    "price_overrides",
    "request_date",
]

#: The directory the shipped price files are read from.
PRICE_FILE_DIR: Final = Path(__file__).resolve().parents[2] / "_data" / "pricing"

#: The context-window suffix Claude Code appends to a model id; it names no
#: other price, so lookup strips it before reading a row's ``match`` list.
LONG_CONTEXT_SUFFIX: Final = "[1m]"

#: The rate-table version a cost priced by an operator override records.
OVERRIDE_RATE_VERSION: Final = "override"

#: Anthropic's stated cache-write multipliers on base input, which the offline
#: currency check holds every Anthropic row to.
CACHE_WRITE_5M_MULTIPLIER: Final = Decimal("1.25")
CACHE_WRITE_1H_MULTIPLIER: Final = Decimal("2")

_PER_MILLION: Final = Decimal(1_000_000)

Vendor = Literal["anthropic", "openai"]
VerifiedBy = Literal["fetch", "eye"]
PriceSource = Literal["file", "override"]

#: The cache-write classes each vendor bills, which every rate of its file names.
_VENDOR_WRITE_CLASSES: Final[dict[str, frozenset[str]]] = {
    "anthropic": frozenset({"5m", "1h"}),
    "openai": frozenset({"30m", "12h"}),
}


class PriceTier(BaseModel):
    """Rates that replace a row's own above a prompt size.

    Attributes:
        above_prompt_tokens: The tier applies when input, cache-read and
            cache-write tokens together exceed this.
        rates: The rates every class bills at inside the tier.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    above_prompt_tokens: int = Field(gt=0)
    rates: PriceRates


class FastRates(BaseModel):
    """The input and output rates of a fast-mode request; other classes are not covered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)


class PriceModifiers(BaseModel):
    """What a request's speed and inference region do to a row's rates.

    Attributes:
        fast: Rates for ``usage.speed == "fast"``; ``None`` leaves a fast
            request unpriced.
        geo_us: Multiplier for ``usage.inference_geo == "us"``; ``None``
            leaves such a request unpriced.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    fast: FastRates | None
    geo_us: Decimal | None = Field(gt=0)


class PriceRow(BaseModel):
    """One model's prices over one date window, with where they were read.

    Attributes:
        id: The model id the row prices.
        match: Further ids, compared after stripping ``[1m]``, that price here.
        effective_from: First UTC date in effect; ``None`` since launch.
        effective_to: First UTC date no longer in effect; ``None`` open.
        source_url: The vendor page the rates were read from.
        verified_on: When they were last compared with that page.
        verified_by: ``fetch`` for a fetched page, ``eye`` for a person's read.
        rates: The rates, or ``None`` when the model is named but not priced.
        tiers: Larger-prompt rates, by ascending threshold.
        modifiers: Fast-mode and US-region pricing.
        note: Why a row is unpriced, or any caveat on it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    match: tuple[str, ...]
    effective_from: date | None
    effective_to: date | None
    source_url: str = Field(pattern=r"^https://\S+$")
    verified_on: date | None
    verified_by: VerifiedBy | None
    rates: PriceRates | None
    tiers: tuple[PriceTier, ...]
    modifiers: PriceModifiers
    note: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """Hold the window, provenance and tiers together.

        Raises:
            ValueError: The window is empty; a priced row lacks ``verified_on``
                or ``verified_by``; an unpriced row has tiers or no note; the
                tier thresholds do not ascend.
        """
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to <= self.effective_from
        ):
            raise ValueError(f"{self.id}: effective_to must follow effective_from")
        if self.rates is not None and (self.verified_on is None or self.verified_by is None):
            raise ValueError(f"{self.id}: a priced row needs verified_on and verified_by")
        if self.rates is None and (self.tiers or not self.note):
            raise ValueError(f"{self.id}: an unpriced row carries a note and no tiers")
        thresholds = [tier.above_prompt_tokens for tier in self.tiers]
        if thresholds != sorted(set(thresholds)):
            raise ValueError(f"{self.id}: tier thresholds must strictly ascend")
        return self

    def in_effect(self, on: date) -> bool:
        """Return whether the row prices a request made on *on*."""
        return (self.effective_from is None or self.effective_from <= on) and (
            self.effective_to is None or on < self.effective_to
        )


class PriceAlias(BaseModel):
    """A short model name and the id it stands for over one date window."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    alias: str = Field(min_length=1)
    target: str = Field(min_length=1)
    from_: date = Field(alias="from")
    to: date | None

    def in_effect(self, on: date) -> bool:
        """Return whether the alias names its target on *on*."""
        return self.from_ <= on and (self.to is None or on < self.to)


class PriceFile(BaseModel):
    """One vendor's price file.

    Attributes:
        vendor: The vendor whose rates the file holds.
        aliases: Short names and their dated targets.
        models: The model rows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vendor: Vendor
    aliases: tuple[PriceAlias, ...]
    models: tuple[PriceRow, ...]

    @model_validator(mode="after")
    def _closed(self) -> Self:
        """Refuse a foreign write class, overlapping rows or an alias to nothing.

        Raises:
            ValueError: A rate names write classes other than its vendor's; two
                rows that answer one id are in effect on one date; an alias
                targets an id no row prices.
        """
        classes = _VENDOR_WRITE_CLASSES[self.vendor]
        for row in self.models:
            for rates in (row.rates, *(tier.rates for tier in row.tiers)):
                if rates is not None and set(rates.cache_write) != classes:
                    raise ValueError(
                        f"{row.id}: cache_write classes {sorted(rates.cache_write)} "
                        f"are not {self.vendor}'s {sorted(classes)}"
                    )
        for index, row in enumerate(self.models):
            names = {row.id, *row.match}
            for other in self.models[index + 1 :]:
                if names & {other.id, *other.match} and _overlap(row, other):
                    raise ValueError(f"{row.id} and {other.id} answer one id on one date")
        ids = {row.id for row in self.models}
        for alias in self.aliases:
            if alias.target not in ids:
                raise ValueError(
                    f"alias {alias.alias!r} targets {alias.target!r}, which no row prices"
                )
        return self


def _overlap(first: PriceRow, second: PriceRow) -> bool:
    """Return whether the two rows' date windows share a date."""
    starts_before_end = second.effective_to is None or (
        first.effective_from is None or first.effective_from < second.effective_to
    )
    ends_after_start = first.effective_to is None or (
        second.effective_from is None or second.effective_from < first.effective_to
    )
    return starts_before_end and ends_after_start


class TokenUsage(BaseModel):
    """The token classes of one request and the usage fields that move its price.

    Attributes:
        input: Non-cached input tokens.
        output: Output tokens, reasoning included.
        cache_read: Prompt-cache read tokens.
        cache_write: Prompt-cache write tokens by write class.
        speed: ``usage.speed``; ``fast`` applies the fast-mode rates.
        inference_geo: ``usage.inference_geo``; ``us`` applies ``geo_us``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    cache_write: dict[str, int] = Field(default_factory=dict)
    speed: str | None = None
    inference_geo: str | None = None

    @property
    def prompt_tokens(self) -> int:
        """Return input, cache-read and cache-write tokens together: what a tier measures."""
        return self.input + self.cache_read + sum(self.cache_write.values())


class PriceQuote(BaseModel):
    """The price of one request's tokens.

    Attributes:
        model: The model id as the request named it.
        source: ``file`` or ``override`` when a row or override resolved;
            ``None`` when none did.
        row_id: The id of the resolved row or overridden model.
        verified_on: When the resolved rates were last checked; ``None`` for
            an override or an unpriced row.
        cost_usd: The summed cost of the priced classes; ``None`` when nothing
            resolved or no class with tokens had a rate.
        unpriced_tokens: Tokens of classes no rate covers.
        reason: Why some tokens are unpriced; ``None`` when none are.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    source: PriceSource | None
    row_id: str | None
    verified_on: date | None
    cost_usd: Decimal | None
    unpriced_tokens: int = Field(ge=0)
    reason: str | None

    @property
    def priced(self) -> bool:
        """Return whether every token of the request has a price."""
        return self.source is not None and self.cost_usd is not None and not self.unpriced_tokens


@dataclass(frozen=True, slots=True)
class _ClassRates:
    """The per-class rates one request bills at, after tiers and modifiers."""

    input: Decimal | None
    output: Decimal | None
    cache_read: Decimal | None
    cache_write: Mapping[str, Decimal | None]

    @classmethod
    def of(cls, rates: PriceRates) -> _ClassRates:
        return cls(rates.input, rates.output, rates.cache_read, dict(rates.cache_write))

    @classmethod
    def none(cls) -> _ClassRates:
        return cls(None, None, None, {})

    def scaled(self, factor: Decimal) -> _ClassRates:
        def scale(rate: Decimal | None) -> Decimal | None:
            return None if rate is None else rate * factor

        return _ClassRates(
            scale(self.input),
            scale(self.output),
            scale(self.cache_read),
            {key: scale(rate) for key, rate in self.cache_write.items()},
        )


def _rates_for_request(
    rates: PriceRates,
    tiers: Sequence[PriceTier],
    modifiers: PriceModifiers | None,
    usage: TokenUsage,
) -> _ClassRates:
    """Apply the tier, then fast mode, then the US region to a row's rates.

    A tier applies when the prompt exceeds its threshold. A modifier the row
    does not state, or a class a stated modifier does not cover, leaves the
    class unpriced: fast mode states input and output only, and no tier states
    fast-mode rates.
    """
    tier = next((t for t in reversed(tiers) if usage.prompt_tokens > t.above_prompt_tokens), None)
    current = _ClassRates.of(tier.rates if tier is not None else rates)
    if usage.speed == "fast":
        fast = modifiers.fast if modifiers is not None else None
        if fast is None or tier is not None:
            return _ClassRates.none()
        current = _ClassRates(fast.input, fast.output, None, {})
    if usage.inference_geo == "us":
        geo = modifiers.geo_us if modifiers is not None else None
        if geo is None:
            return _ClassRates.none()
        current = current.scaled(geo)
    return current


def _cost(rates: _ClassRates, usage: TokenUsage) -> tuple[Decimal | None, int]:
    """Return the cost of the classes with a rate and the tokens of those without."""
    cost = Decimal(0)
    priced_any = False
    unpriced = 0
    classes: list[tuple[int, Decimal | None]] = [
        (usage.input, rates.input),
        (usage.output, rates.output),
        (usage.cache_read, rates.cache_read),
        *((tokens, rates.cache_write.get(name)) for name, tokens in usage.cache_write.items()),
    ]
    for tokens, rate in classes:
        if rate is None:
            unpriced += tokens
            continue
        priced_any = True
        cost += tokens * rate / _PER_MILLION
    return (cost if priced_any or not unpriced else None), unpriced


@dataclass(frozen=True, slots=True)
class PriceBook:
    """The price files and the operator's overrides, priced together.

    Attributes:
        files: The vendor price files.
        overrides: Override by model id.
        digest: SHA-256 over the files' bytes and the overrides; a stored cost
            priced under another digest is stale.
    """

    files: tuple[PriceFile, ...]
    overrides: Mapping[str, PriceOverride]
    digest: str

    @property
    def version(self) -> str:
        """Return the short digest a file-priced cost records as its rate-table version."""
        return self.digest[:16]

    @property
    def rows(self) -> tuple[PriceRow, ...]:
        """Return every model row of every file."""
        return tuple(row for file in self.files for row in file.models)

    def resolve(self, model: str, *, on: date) -> PriceRow | None:
        """Return the row that prices *model* on *on*, or ``None``.

        Args:
            model: The model id as the request named it.
            on: The request's UTC date.

        Returns:
            The row whose id is *model*, else whose ``match`` holds *model*
            stripped of ``[1m]``, else the row of the alias in effect.
        """
        live = [row for row in self.rows if row.in_effect(on)]
        exact = next((row for row in live if row.id == model), None)
        if exact is not None:
            return exact
        stripped = model.removesuffix(LONG_CONTEXT_SUFFIX)
        matched = next((row for row in live if stripped in row.match), None)
        if matched is not None:
            return matched
        target = self._alias_target(stripped, on)
        return None if target is None else next((row for row in live if row.id == target), None)

    def _alias_target(self, name: str, on: date) -> str | None:
        aliases = (alias for file in self.files for alias in file.aliases)
        return next((a.target for a in aliases if a.alias == name and a.in_effect(on)), None)

    def _override(self, model: str, on: date) -> tuple[str, PriceOverride] | None:
        stripped = model.removesuffix(LONG_CONTEXT_SUFFIX)
        target = self._alias_target(stripped, on)
        for name in (model, stripped, *(() if target is None else (target,))):
            found = self.overrides.get(name)
            if found is not None and (found.effective_from is None or found.effective_from <= on):
                return name, found
        return None

    def quote(self, model: str, usage: TokenUsage, *, on: date) -> PriceQuote:
        """Price one request's tokens.

        Args:
            model: The model id as the request named it.
            usage: The request's token classes and usage fields.
            on: The request's UTC date, which picks the row in effect.

        Returns:
            The quote; an override wins over the file for its model.
        """
        override = self._override(model, on)
        if override is not None:
            name, found = override
            cost, unpriced = _cost(_rates_for_request(found.rates, (), None, usage), usage)
            return PriceQuote(
                model=model,
                source="override",
                row_id=name,
                verified_on=None,
                cost_usd=cost,
                unpriced_tokens=unpriced,
                reason=f"override for {name} does not price every class" if unpriced else None,
            )
        row = self.resolve(model, on=on)
        if row is None or row.rates is None:
            tokens = _cost(_ClassRates.none(), usage)[1]
            return PriceQuote(
                model=model,
                source=None if row is None else "file",
                row_id=None if row is None else row.id,
                verified_on=None,
                cost_usd=None,
                unpriced_tokens=tokens,
                reason=row.note if row is not None else f"no price row for {model} on {on}",
            )
        cost, unpriced = _cost(
            _rates_for_request(row.rates, row.tiers, row.modifiers, usage), usage
        )
        return PriceQuote(
            model=model,
            source="file",
            row_id=row.id,
            verified_on=row.verified_on,
            cost_usd=cost,
            unpriced_tokens=unpriced,
            reason=f"{row.id} states no rate for some classes of this request"
            if unpriced
            else None,
        )

    def quote_totals(self, model: str, usage: TokenUsage, *, first: date, last: date) -> PriceQuote:
        """Price the summed tokens of several requests, only where the sum prices exactly.

        Pricing is linear in the tokens while every request bills at one set
        of rates. That holds when one row, or one override, is in effect from
        the first request's date to the last, and the summed prompt does not
        exceed the row's lowest tier, so no single request reached a tier
        either. Otherwise how the sum splits into requests decides the price,
        and every token is unpriced.

        Args:
            model: The model id the requests named.
            usage: The requests' summed token classes; speed and region apply
                to every request alike.
            first: The UTC date of the first request.
            last: The UTC date of the last request.

        Returns:
            The quote of the sum when exact; else an unpriced quote saying why.

        Raises:
            ValueError: When *last* precedes *first*.
        """
        if last < first:
            raise ValueError(f"last {last} precedes first {first}")
        quote = self.quote(model, usage, on=first)
        if quote.source is None:
            return quote
        reason: str | None = None
        if self._rates_source(model, first) is not self._rates_source(model, last):
            reason = f"the rates of {model} change between {first} and {last}"
        elif quote.source == "file":
            row = self.resolve(model, on=first)
            tiers = row.tiers if row is not None else ()
            if tiers and usage.prompt_tokens > tiers[0].above_prompt_tokens:
                reason = (
                    f"the summed prompt of {usage.prompt_tokens} tokens exceeds the "
                    f"{tiers[0].above_prompt_tokens}-token tier of {model}; "
                    "the requests' own prompts are unknown"
                )
        if reason is None:
            return quote
        return quote.model_copy(
            update={
                "verified_on": None,
                "cost_usd": None,
                "unpriced_tokens": _cost(_ClassRates.none(), usage)[1],
                "reason": reason,
            }
        )

    def _rates_source(self, model: str, on: date) -> PriceOverride | PriceRow | None:
        """Return the override or row that prices *model* on *on*."""
        override = self._override(model, on)
        return override[1] if override is not None else self.resolve(model, on=on)

    def list_rates(self, model: str, *, on: date) -> PriceRates | None:
        """Return the standard rates *model* bills at on *on*, or ``None`` when unpriced.

        Args:
            model: The model id.
            on: The UTC date.

        Returns:
            The override's rates, else the resolved row's base rates.
        """
        override = self._override(model, on)
        if override is not None:
            return override[1].rates
        row = self.resolve(model, on=on)
        return None if row is None else row.rates

    def rate_version(self, quote: PriceQuote) -> str:
        """Return the rate-table version a cost priced by *quote* records."""
        return OVERRIDE_RATE_VERSION if quote.source == "override" else self.version


def load_price_file(path: Path) -> PriceFile:
    """Read and validate one price file.

    Args:
        path: The YAML file.

    Returns:
        The validated file.

    Raises:
        OSError: The file cannot be read.
        yaml.YAMLError: The file is not YAML.
        pydantic.ValidationError: The file breaks the closed model.
    """
    return PriceFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def price_overrides(repo_root: Path | None) -> dict[str, PriceOverride]:
    """Return ``reflect.pricing.overrides`` from the layered config.

    Args:
        repo_root: The repository whose layers compose over the global one;
            ``None`` outside a repository, where the global layer still applies.

    Returns:
        Override by model id; empty when no layer sets one.

    Raises:
        pydantic.ValidationError: The ``reflect`` block is malformed.
    """
    from eawf.kernel.config.layered import merge_config

    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    return dict(ReflectConfig.model_validate(merged.get("reflect") or {}).pricing.overrides)


def load_price_book(
    *,
    repo_root: Path | None,
    directory: Path = PRICE_FILE_DIR,
    overrides: Mapping[str, PriceOverride] | None = None,
) -> PriceBook:
    """Load the price files and the overrides into one book.

    Args:
        repo_root: The repository whose config layers supply overrides;
            ``None`` reads the global layer only. Ignored when *overrides*
            is given.
        directory: Where the ``*.yaml`` price files are.
        overrides: Overrides to use instead of the layered config's.

    Returns:
        The book, with the digest of everything that went into it.

    Raises:
        FileNotFoundError: *directory* holds no price file.
        pydantic.ValidationError: A file or the ``reflect`` block is malformed.
    """
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise FileNotFoundError(f"no price file under {directory}")
    chosen = dict(overrides) if overrides is not None else price_overrides(repo_root)
    hasher = hashlib.sha256()
    for path in paths:
        hasher.update(path.name.encode())
        hasher.update(path.read_bytes())
    hasher.update(
        json.dumps(
            {name: value.model_dump(mode="json") for name, value in sorted(chosen.items())},
            sort_keys=True,
        ).encode()
    )
    files = tuple(load_price_file(path) for path in paths)
    logger.debug(f"load_price_book files={len(files)} overrides={len(chosen)}")
    return PriceBook(files=files, overrides=chosen, digest=hasher.hexdigest())


def price_book_root() -> Path | None:
    """Return the repository whose overrides this process prices with.

    The state resolver's precedence names it: ``EA_STATE``, then the
    ``-w/--workspace`` root the CLI pinned, then the tree above the main
    checkout of the repository holding the working directory. A linked
    worktree's own ``.ea`` is a frozen copy, so it is never the answer.

    Returns:
        The directory holding the resolved ``.ea`` tree; ``None`` outside
        every managed repository, or when the resolver refuses the override
        or the repository.
    """
    from eawf.kernel.state.resolve import home_state, resolve_with_reason
    from eawf.runtime.daemon.runtime_dir import pinned_workspace
    from eawf.runtime.vcs.branch_name import BareRepositoryError

    workspace = pinned_workspace()
    try:
        if os.environ.get("EA_STATE") or workspace is not None:
            state_path, _reason = resolve_with_reason(workspace)
            return state_path.parent.parent
        state_path, found = home_state(Path.cwd())
    except FileNotFoundError, BareRepositoryError:
        return None
    return state_path.parent.parent if found else None


@cache
def _price_book(repo_root: Path | None) -> PriceBook:
    """Return the book for *repo_root*, loaded once per repository per process."""
    return load_price_book(repo_root=repo_root)


def active_price_book(repo_root: Path | None = None) -> PriceBook:
    """Return the book a repository prices with, loaded once per repository.

    Args:
        repo_root: The repository whose config layers supply overrides; when
            ``None``, the one :func:`price_book_root` names, or the global
            layer alone outside every repository.

    Returns:
        The cached book for that repository.
    """
    return _price_book(repo_root if repo_root is not None else price_book_root())


def request_date(ts: datetime | None) -> date:
    """Return the UTC date a request is priced on; today for one with no timestamp.

    A naive timestamp is read as UTC.
    """
    if ts is None:
        return datetime.now(tz=UTC).date()
    return (ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)).astimezone(UTC).date()


class PricingDriftFinding(BaseModel):
    """One drift finding for a single model row.

    Attributes:
        model_id: The row the finding pertains to.
        field: The rate that drifted.
        expected: The value the currency check derived as correct.
        actual: The value in the price file.
        detail: Human-readable explanation of the drift.
    """

    model_config = ConfigDict(extra="forbid")

    model_id: str
    field: str
    expected: Decimal | str
    actual: Decimal | str
    detail: str


class PricingDriftReport(BaseModel):
    """Typed result of a pricing-currency check.

    Attributes:
        pricing_version: The book's rate-table version.
        fetched_at: The oldest ``verified_on`` among priced rows, at UTC midnight.
        model_count: Number of model rows checked.
        is_current: ``True`` when no findings were raised.
        findings: Per-row drift findings (empty when current).
    """

    model_config = ConfigDict(extra="forbid")

    pricing_version: str
    fetched_at: datetime
    model_count: int
    is_current: bool
    findings: list[PricingDriftFinding] = Field(default_factory=list)


def check_pricing_currency(book: PriceBook | None = None) -> PricingDriftReport:
    """Hold every Anthropic rate to Anthropic's stated cache-write multipliers.

    Offline: each priced Anthropic row, and each of its tiers, must bill a
    5-minute write at 1.25x and a 1-hour write at 2x its own input rate. It
    does not fetch the vendor pages.

    Args:
        book: The book to check; the shipped files with no overrides by default.

    Returns:
        The report; current when no rate drifts.
    """
    checked = book if book is not None else load_price_book(repo_root=None, overrides={})
    findings: list[PricingDriftFinding] = []
    for file in checked.files:
        if file.vendor != "anthropic":
            continue
        for row in file.models:
            for rates in (row.rates, *(tier.rates for tier in row.tiers)):
                if rates is None or rates.input is None:
                    continue
                for name, multiplier in (
                    ("5m", CACHE_WRITE_5M_MULTIPLIER),
                    ("1h", CACHE_WRITE_1H_MULTIPLIER),
                ):
                    expected = rates.input * multiplier
                    actual = rates.cache_write.get(name)
                    if actual != expected:
                        findings.append(
                            PricingDriftFinding(
                                model_id=row.id,
                                field=f"cache_write.{name}",
                                expected=expected,
                                actual=str(actual) if actual is None else actual,
                                detail=f"expected input x {multiplier} cache multiplier",
                            )
                        )
    verified = [row.verified_on for row in checked.rows if row.verified_on is not None]
    oldest = min(verified, default=datetime.now(tz=UTC).date())
    report = PricingDriftReport(
        pricing_version=checked.version,
        fetched_at=datetime.combine(oldest, time.min, tzinfo=UTC),
        model_count=len(checked.rows),
        is_current=not findings,
        findings=findings,
    )
    logger.info(
        f"check_pricing_currency models={report.model_count} "
        f"current={report.is_current} findings={len(report.findings)}"
    )
    return report
