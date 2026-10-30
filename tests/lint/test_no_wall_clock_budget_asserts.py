"""No blocking test asserts an absolute wall-clock budget.

A test that measures elapsed time with the host clock and asserts it under a
fixed number of seconds measures the machine as much as the code: under a
loaded runner or four xdist workers it reds with nothing broken. Absolute
budgets live in the perf lane, :data:`PERF_LANE`, which the nightly CI run
executes serially and reports without blocking. Every other test proves the
same behaviour with a signal load cannot move: a counter, an event or barrier
ordering, or a ratio of two durations measured in the same run.

A *clock read* is a call to ``time.perf_counter``, ``time.monotonic``,
``time.time`` or their ``_ns`` forms, through the ``time`` module or an alias
of it (``import time as t``) or by bare imported name (``from time import
perf_counter as pc``); an event loop's ``time()``, where the receiver's name
carries the word ``loop`` or is a ``get_running_loop()`` or
``get_event_loop()`` call; or ``datetime.now()`` and ``datetime.utcnow()``. A
*timestamp* is a clock read or a name assigned from an expression holding one.
A *duration* is a subtraction with a timestamp operand, a ``timeit.timeit`` or
``timeit.repeat`` result, a name assigned from an expression holding a
duration, a list a duration is appended to, a call to a module function that
returns one, or a name or attribute whose identifier carries the word
``elapsed`` or ends in ``wall_clock_s`` or ``wall_clock_ms``, the names a
measured interval is recorded under. A *measured* duration is one that traces
to a clock read or ``timeit`` by the same rules, without the naming rule.
``len()`` of durations is a count, not a duration. A *budget constant* is a name or
attribute whose identifier carries the word ``budget`` and ends in a time unit
(``_s``, ``_ms``, ``_us``, ``_ns``, ``_seconds``).

Flagged forms:

- ``assert elapsed < 2.0`` and ``assert time.monotonic() - t0 < LIMIT``: an
  ordering assert that bounds a duration from above by anything that is not
  itself a duration;
- ``assert took == pytest.approx(0.5, abs=0.2)``: equality between a measured
  duration and an ``approx`` window that holds no duration, whose upper edge is
  a budget;
- ``if elapsed > 2.0: pytest.fail(...)``, outside any loop: an ``if`` that
  bounds a duration from above as an assert would and whose body calls
  ``pytest.fail`` or raises ``AssertionError``;
- ``[s for s in seen if s > LATENCY_BUDGET_S]`` and ``if lag > LAG_BUDGET_S:``:
  a comprehension filter or ``if`` test ordering anything against a bare budget
  constant, the collect-then-assert-empty idiom.

Allowed forms. The boundary is that a ceiling which only stops a hang is not a
budget: it fires only when the awaited thing never happens, so load cannot
turn a pass into a failure, while a budget asserts how long something that did
happen took.

- a hang ceiling that is not an assert on a duration: a ``timeout=`` argument
  (``join(timeout=5)``, ``wait_for(..., timeout=10)``, ``event.wait(2.0)``),
  ``deadline = time.monotonic() + 30`` then ``while time.monotonic() <
  deadline`` or ``assert time.monotonic() < deadline`` (timestamp against
  timestamp);
- a polling ceiling: an ``if`` on a duration inside a ``while`` or ``for``
  loop, whatever its body raises, and an ``if`` outside a loop that raises
  anything but ``AssertionError`` or ``pytest.fail``;
- a time method on a receiver that is not an event loop (``fake_clock.time()``)
  and ``approx`` on a duration only its name marks (a fake clock's);
- a helper that asserts internally (``assert_under(took, 2.0)``): its body
  bounds a parameter, which no rule here can tell from a count, so it is not
  followed;
- a lower bound, ``assert elapsed >= HOLD_S``: load only makes it truer;
- a same-run ratio, ``assert slow_s > 3 * fast_s``: both sides are durations;
- equality on a duration produced by a fake clock (``==`` is never flagged);
- a token, wave or row budget: only a time-unit suffix makes a budget a clock
  budget;
- anything under :data:`PERF_LANE`.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: The perf lane: the only tests allowed an absolute wall-clock budget. The CI
#: perf step runs exactly these paths, serially and on the nightly run alone.
PERF_LANE: Final = (
    "tests/perf/surfaces/tui",
    "tests/perf/surfaces/cli",
    "tests/perf/runtime",
    "tests/perf/kernel",
)

_DURATION_WORD: Final = "elapsed"
_WALL_CLOCK = re.compile(r"(^|_)wall_clock_(s|ms)$")
_TIME_UNITS: Final = frozenset({"s", "ms", "us", "ns", "seconds"})

_CLOCKS: Final = frozenset(
    {"perf_counter", "perf_counter_ns", "monotonic", "monotonic_ns", "time", "time_ns"}
)
_LOOP_GETTERS: Final = frozenset({"get_running_loop", "get_event_loop"})
_DATETIME_CLOCKS: Final = frozenset({"now", "utcnow"})
_TIMERS: Final = frozenset({"timeit", "repeat"})
_LOOPS: Final = (ast.For, ast.AsyncFor, ast.While)
_SKIP_ENV: Final = "EAWF_SKIP_PERF"
_COLLECTORS: Final = frozenset({"append", "extend", "add", "insert"})
_UPPER: Final = {ast.Lt: "left", ast.LtE: "left", ast.Gt: "right", ast.GtE: "right"}
#: A failing ``if`` states the negation of an assert: ``if took > N: fail``
#: asserts ``took <= N``, so its bounded side is the other operand.
_FAIL_UPPER: Final = {ast.Lt: "right", ast.LtE: "right", ast.Gt: "left", ast.GtE: "left"}


@dataclass(frozen=True)
class Finding:
    """One flagged comparison.

    Attributes:
        path: The repo-relative test file.
        line: The comparison's line number.
        source: The comparison's source text.
    """

    path: str
    line: int
    source: str

    def render(self) -> str:
        """Return ``path:line: source``."""
        return f"{self.path}:{self.line}: {self.source}"


def _words(identifier: str) -> set[str]:
    return set(identifier.lower().split("_"))


def _identifier(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


@dataclass(frozen=True)
class _Imports:
    """The names one module binds to clocks and timers.

    Attributes:
        clocks: Bare names bound to a ``time`` clock function.
        time_modules: Names bound to the ``time`` module.
        datetime_classes: Names bound to the ``datetime.datetime`` class.
        timeit_modules: Names bound to the ``timeit`` module.
        timers: Bare names bound to ``timeit.timeit`` or ``timeit.repeat``.
    """

    clocks: frozenset[str]
    time_modules: frozenset[str]
    datetime_classes: frozenset[str]
    timeit_modules: frozenset[str]
    timers: frozenset[str]


def _imports(tree: ast.Module) -> _Imports:
    found: dict[str, set[str]] = {
        "clocks": set(),
        "time_modules": {"time"},
        "datetime_classes": {"datetime"},
        "timeit_modules": {"timeit"},
        "timers": set(),
    }
    from_names = {"time": ("clocks", _CLOCKS), "timeit": ("timers", _TIMERS)}
    modules = {"time": "time_modules", "timeit": "timeit_modules"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in from_names:
            bucket, wanted = from_names[node.module]
            found[bucket].update(
                alias.asname or alias.name for alias in node.names if alias.name in wanted
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime":
            found["datetime_classes"].update(
                alias.asname or alias.name for alias in node.names if alias.name == "datetime"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules and alias.asname:
                    found[modules[alias.name]].add(alias.asname)
    return _Imports(**{key: frozenset(names) for key, names in found.items()})


def _is_loop(receiver: ast.expr) -> bool:
    if isinstance(receiver, ast.Call):
        return _identifier(receiver.func) in _LOOP_GETTERS
    identifier = _identifier(receiver)
    return identifier is not None and "loop" in _words(identifier)


def _is_clock_read(node: ast.AST, imports: _Imports) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in imports.clocks
    if not isinstance(func, ast.Attribute):
        return False
    receiver = func.value
    if func.attr in _DATETIME_CLOCKS:
        return (isinstance(receiver, ast.Name) and receiver.id in imports.datetime_classes) or (
            isinstance(receiver, ast.Attribute) and receiver.attr == "datetime"
        )
    if func.attr == "time" and _is_loop(receiver):
        return True
    return (
        func.attr in _CLOCKS
        and isinstance(receiver, ast.Name)
        and receiver.id in imports.time_modules
    )


def _is_timer_read(node: ast.AST, imports: _Imports) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in imports.timers
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _TIMERS
        and isinstance(func.value, ast.Name)
        and func.value.id in imports.timeit_modules
    )


class _Scope:
    """Timestamp and duration names of one function, found to a fixpoint.

    A tuple target takes the kind of the whole value, so ``latency, reply =
    probe()`` marks both names when ``probe`` returns a duration.
    """

    def __init__(
        self,
        body: ast.AST,
        imports: _Imports,
        duration_calls: frozenset[str],
        measured_calls: frozenset[str],
    ) -> None:
        self.imports = imports
        self.duration_calls = duration_calls
        self.measured_calls = measured_calls
        self.timestamps: set[str] = set()
        self.durations: set[str] = set()
        self.measured: set[str] = set()
        assignments = [
            node for node in ast.walk(body) if isinstance(node, ast.Assign | ast.AnnAssign)
        ]
        appends = [
            node
            for node in ast.walk(body)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _COLLECTORS
            and isinstance(node.func.value, ast.Name)
        ]
        changed = True
        while changed:
            changed = False
            for call in appends:
                receiver = call.func.value.id  # type: ignore[attr-defined]
                if receiver not in self.durations and any(
                    self.holds_duration(arg) for arg in call.args
                ):
                    self.durations.add(receiver)
                    changed = True
            for node in assignments:
                value = node.value
                if value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [
                    sub.id for t in targets for sub in ast.walk(t) if isinstance(sub, ast.Name)
                ]
                for bucket in self._buckets(value):
                    if not bucket.issuperset(names):
                        bucket.update(names)
                        changed = True

    def _buckets(self, value: ast.expr) -> list[set[str]]:
        """Return the name sets a name assigned from *value* belongs to."""
        if self.holds_duration(value):
            if self.holds_duration(value, by_name=False):
                return [self.durations, self.measured]
            return [self.durations]
        if self.holds_timestamp(value):
            return [self.timestamps]
        return []

    def is_timestamp(self, node: ast.AST) -> bool:
        if _is_clock_read(node, self.imports):
            return True
        return isinstance(node, ast.Name) and node.id in self.timestamps

    def holds_timestamp(self, node: ast.AST) -> bool:
        return any(self.is_timestamp(sub) for sub in ast.walk(node))

    def is_duration(self, node: ast.AST, *, by_name: bool = True) -> bool:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            return self.holds_timestamp(node.left) or self.holds_timestamp(node.right)
        if _is_timer_read(node, self.imports):
            return True
        names = self.durations if by_name else self.measured
        if isinstance(node, ast.Name) and node.id in names:
            return True
        calls = self.duration_calls if by_name else self.measured_calls
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in calls:
            return True
        identifier = _identifier(node) if by_name and isinstance(node, ast.expr) else None
        if identifier is None:
            return False
        return _DURATION_WORD in _words(identifier) or bool(_WALL_CLOCK.search(identifier))

    def holds_duration(self, node: ast.AST, *, by_name: bool = True) -> bool:
        """Return whether *node* holds a duration; *by_name* admits the naming rule."""
        # A count of durations is a counter, not a duration.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
            return False
        if self.is_duration(node, by_name=by_name):
            return True
        return any(
            self.holds_duration(child, by_name=by_name) for child in ast.iter_child_nodes(node)
        )

    def returns_duration(self, function: ast.AST, *, by_name: bool) -> bool:
        return any(
            isinstance(node, ast.Return)
            and node.value is not None
            and self.holds_duration(node.value, by_name=by_name)
            for node in ast.walk(function)
        )


def _is_budget(node: ast.expr) -> bool:
    identifier = _identifier(node)
    if identifier is None:
        return False
    words = identifier.lower().split("_")
    return "budget" in words and words[-1] in _TIME_UNITS


def _pairs(compare: ast.Compare) -> Iterator[tuple[ast.expr, ast.cmpop, ast.expr]]:
    left = compare.left
    for op, right in zip(compare.ops, compare.comparators, strict=True):
        yield left, op, right
        left = right


def _is_approx(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _identifier(node.func) == "approx"


def _approx_flags(left: ast.expr, right: ast.expr, scope: _Scope) -> bool:
    for window, measured in ((left, right), (right, left)):
        if (
            _is_approx(window)
            and not scope.holds_duration(window)
            and scope.holds_duration(measured, by_name=False)
        ):
            return True
    return False


def _assert_flags(
    compare: ast.Compare, scope: _Scope, upper: dict[type[ast.cmpop], str] = _UPPER
) -> bool:
    for left, op, right in _pairs(compare):
        if isinstance(op, ast.Eq) and _approx_flags(left, right, scope):
            return True
        side = upper.get(type(op))
        if side is None:
            continue
        bounded, bound = (left, right) if side == "left" else (right, left)
        if scope.holds_duration(bounded) and not scope.holds_duration(bound):
            return True
    return False


def _filter_flags(compare: ast.Compare) -> bool:
    for left, op, right in _pairs(compare):
        if type(op) not in _UPPER:
            continue
        if _is_budget(left) != _is_budget(right):
            return True
    return False


def _compares(node: ast.AST) -> Iterator[ast.Compare]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Compare):
            yield sub


def _fails(branch: ast.If) -> bool:
    """Return whether *branch*'s body fails the test as an assert would."""
    for statement in branch.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Call) and _identifier(node.func) == "fail":
                return True
            if isinstance(node, ast.Raise) and node.exc is not None:
                raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                if _identifier(raised) == "AssertionError":
                    return True
    return False


def _looped_ifs(function: ast.AST) -> set[int]:
    return {
        id(node)
        for loop in ast.walk(function)
        if isinstance(loop, _LOOPS)
        for node in ast.walk(loop)
        if isinstance(node, ast.If)
    }


def budget_findings(source: str, path: str) -> list[Finding]:
    """Return every absolute wall-clock budget comparison in one test module.

    Args:
        source: The module's source text.
        path: The repo-relative path recorded on each finding.

    Returns:
        One finding per flagged comparison, in line order; empty when the
        module holds none.

    Raises:
        SyntaxError: *source* is not valid Python.
    """
    tree = ast.parse(source)
    imports = _imports(tree)
    found: dict[int, Finding] = {}
    functions = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    # A helper that measures and returns a duration hands it to its caller.
    duration_calls: frozenset[str] = frozenset()
    measured_calls: frozenset[str] = frozenset()
    while True:
        scopes = [
            (function, _Scope(function, imports, duration_calls, measured_calls))
            for function in functions
        ]
        returners = frozenset(f.name for f, sc in scopes if sc.returns_duration(f, by_name=True))
        measurers = frozenset(f.name for f, sc in scopes if sc.returns_duration(f, by_name=False))
        if (returners, measurers) == (duration_calls, measured_calls):
            break
        duration_calls, measured_calls = returners, measurers
    for function in functions:
        scope = _Scope(function, imports, duration_calls, measured_calls)
        looped = _looped_ifs(function)
        for node in ast.walk(function):
            flagged: list[ast.Compare] = []
            if isinstance(node, ast.Assert):
                flagged = [c for c in _compares(node.test) if _assert_flags(c, scope)]
            elif isinstance(node, ast.If):
                fails = id(node) not in looped and _fails(node)
                flagged = [
                    c
                    for c in _compares(node.test)
                    if _filter_flags(c) or (fails and _assert_flags(c, scope, _FAIL_UPPER))
                ]
            elif isinstance(node, ast.comprehension):
                flagged = [c for cond in node.ifs for c in _compares(cond) if _filter_flags(c)]
            for compare in flagged:
                found.setdefault(
                    compare.lineno,
                    Finding(path, compare.lineno, ast.get_source_segment(source, compare) or ""),
                )
    return [found[line] for line in sorted(found)]


def _bindings(body: list[ast.stmt]) -> dict[str, ast.expr]:
    return {
        target.id: node.value
        for node in body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _mentions(
    node: ast.AST,
    bindings: dict[str, ast.expr],
    hit: Callable[[ast.AST], bool],
    seen: frozenset[str],
) -> bool:
    """Return whether *hit* holds for a node in *node*, following bound names."""
    for sub in ast.walk(node):
        if hit(sub):
            return True
        if (
            isinstance(sub, ast.Name)
            and sub.id in bindings
            and sub.id not in seen
            and _mentions(bindings[sub.id], bindings, hit, seen | {sub.id})
        ):
            return True
    return False


def _is_skip_guard(mark: ast.AST, bindings: dict[str, ast.expr]) -> bool:
    """Return whether *mark* is, names or lists a skipif whose condition reads the skip env."""

    def reads_env(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and node.value == _SKIP_ENV

    def skipif_reading_env(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and _identifier(node.func) == "skipif"
            and any(
                _mentions(arg, bindings, reads_env, frozenset())
                for arg in [*node.args, *(keyword.value for keyword in node.keywords)]
            )
        )

    return _mentions(mark, bindings, skipif_reading_env, frozenset())


def _marked(body: list[ast.stmt], bindings: dict[str, ast.expr]) -> bool:
    mark = _bindings(body).get("pytestmark")
    return mark is not None and _is_skip_guard(mark, bindings)


def unguarded_budget_tests(source: str, path: str) -> list[str]:
    """Return each perf-lane test that asserts a budget without the skip guard.

    The blocking CI step runs the perf lane with ``EAWF_SKIP_PERF=1`` so its
    structural checks block while its budgets wait for the nightly run; a
    budget test must therefore be skipped by a ``skipif`` whose condition reads
    that variable, on the test, its class, or a module or class ``pytestmark``.

    Args:
        source: The module's source text.
        path: The repo-relative path the names are qualified with.

    Returns:
        ``path::test_name``, or ``path::Class::test_name`` for a method, for
        every unguarded test holding a finding.

    Raises:
        SyntaxError: *source* is not valid Python.
    """
    lines = {finding.line for finding in budget_findings(source, path)}
    module = ast.parse(source)
    bindings = _bindings(module.body)
    module_guarded = _marked(module.body, bindings)
    tests: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, bool]] = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            tests.append((node.name, node, module_guarded))
        elif isinstance(node, ast.ClassDef):
            class_guarded = (
                module_guarded
                or any(_is_skip_guard(mark, bindings) for mark in node.decorator_list)
                or _marked(node.body, bindings)
            )
            tests.extend(
                (f"{node.name}::{method.name}", method, class_guarded)
                for method in node.body
                if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
            )
    unguarded: list[str] = []
    for name, function, inherited in tests:
        if not function.name.startswith("test_"):
            continue
        guarded = inherited or any(
            _is_skip_guard(mark, bindings) for mark in function.decorator_list
        )
        end = function.end_lineno or function.lineno
        if not guarded and any(function.lineno <= line <= end for line in lines):
            unguarded.append(f"{path}::{name}")
    return unguarded


def _lane_test_sources() -> Iterator[Path]:
    for entry in PERF_LANE:
        yield from sorted((_REPO_ROOT / entry).rglob("test_*.py"))


def _blocking_test_sources() -> Iterator[Path]:
    lane = tuple(_REPO_ROOT / entry for entry in PERF_LANE)
    for path in sorted((_REPO_ROOT / "tests").rglob("*.py")):
        if not any(path.is_relative_to(root) for root in lane):
            yield path


# --- the gate ----------------------------------------------------------------


def test_no_blocking_test_asserts_an_absolute_wall_clock_budget() -> None:
    findings = [
        finding
        for path in _blocking_test_sources()
        for finding in budget_findings(
            path.read_text(encoding="utf-8"), path.relative_to(_REPO_ROOT).as_posix()
        )
    ]
    assert not findings, "\n".join(
        ["absolute wall-clock budget outside the perf lane:", *(f.render() for f in findings)]
    )


def test_every_perf_lane_budget_honours_the_skip_guard() -> None:
    unguarded = [
        name
        for path in _lane_test_sources()
        for name in unguarded_budget_tests(
            path.read_text(encoding="utf-8"), path.relative_to(_REPO_ROOT).as_posix()
        )
    ]
    assert unguarded == []


def test_every_perf_lane_path_exists() -> None:
    assert [entry for entry in PERF_LANE if not (_REPO_ROOT / entry).is_dir()] == []


# --- the detector ------------------------------------------------------------


def _lines(source: str) -> list[int]:
    return [finding.line for finding in budget_findings(source, "t.py")]


def test_empty_module_has_no_finding() -> None:
    assert budget_findings("", "t.py") == []


def test_invalid_source_raises_syntax_error() -> None:
    try:
        budget_findings("def (:\n", "t.py")
    except SyntaxError:
        return
    raise AssertionError("an invalid module parsed")


def test_flags_an_elapsed_name_under_a_literal() -> None:
    source = (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.perf_counter()\n"
        "    elapsed = time.perf_counter() - t0\n"
        "    assert elapsed < 2.0\n"
    )
    assert _lines(source) == [5]


def test_flags_a_bare_imported_clock_and_a_derived_name() -> None:
    source = (
        "from time import monotonic as now\n"
        "def test_x():\n"
        "    start = now()\n"
        "    spent = (now() - start) * 1000\n"
        "    assert spent <= LIMIT_MS\n"
    )
    assert _lines(source) == [5]


def test_flags_an_inline_subtraction_and_a_reversed_operator() -> None:
    source = (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    assert 1.5 > time.monotonic() - t0\n"
    )
    assert _lines(source) == [4]


def test_flags_the_upper_half_of_a_chained_window() -> None:
    source = (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    elapsed = time.monotonic() - t0\n"
        "    assert WAIT_S <= elapsed < WAIT_S + SLACK_S\n"
    )
    assert _lines(source) == [5]


def test_flags_a_duration_a_helper_returns() -> None:
    source = (
        "import time\n"
        "def _probe():\n"
        "    t0 = time.monotonic()\n"
        "    return 'ok', time.monotonic() - t0\n"
        "def _twice():\n"
        "    return _probe()\n"
        "def test_x():\n"
        "    reply, latency = _twice()\n"
        "    assert reply == 'ok'\n"
        "    assert latency < READINESS_S\n"
    )
    assert _lines(source) == [10]


def test_flags_the_max_of_collected_durations() -> None:
    source = (
        "import time\n"
        "def test_x():\n"
        "    timings = []\n"
        "    for _ in range(3):\n"
        "        started = time.monotonic()\n"
        "        timings.append(time.monotonic() - started)\n"
        "    assert max(timings) < 2.0\n"
        "    assert len(timings) <= 3\n"
    )
    assert _lines(source) == [7]


def test_flags_a_duration_named_attribute() -> None:
    assert _lines("def test_x():\n    assert record.wall_clock_s <= pin.budget_s\n") == [2]


def test_flags_a_budget_filter_in_a_comprehension_and_an_if() -> None:
    source = (
        "def test_x():\n"
        "    slow = [s for s in seen if s > LATENCY_BUDGET_S]\n"
        "    if lag >= LAG_BUDGET_S:\n"
        "        slow.append(lag)\n"
        "    assert not slow\n"
    )
    assert _lines(source) == [2, 3]


def test_allows_a_timestamp_deadline() -> None:
    source = (
        "import time\n"
        "def test_x():\n"
        "    deadline = time.monotonic() + 30\n"
        "    while time.monotonic() < deadline:\n"
        "        pass\n"
        "    assert time.monotonic() < deadline, 'hung'\n"
    )
    assert _lines(source) == []


def test_allows_a_lower_bound_and_a_same_run_ratio() -> None:
    source = (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.perf_counter()\n"
        "    fast = time.perf_counter() - t0\n"
        "    slow = time.perf_counter() - t0\n"
        "    assert slow >= HOLD_S\n"
        "    assert slow > 3 * fast\n"
        "    assert fast == 0.0\n"
    )
    assert _lines(source) == []


def test_allows_an_unrelated_ordering_assert() -> None:
    source = "def test_x():\n    assert len(rows) < 10\n    assert count > LIMIT\n"
    assert _lines(source) == []


def test_allows_a_budget_without_a_time_unit_and_a_fake_duration_counter() -> None:
    source = (
        "def test_x():\n"
        "    kept = [w for w in weights if w <= token_budget]\n"
        "    assert result.tokens_used <= result.budget\n"
        "    assert observed(CounterName.DURATION_MS) < 10_000\n"
        "    assert block.juror_wall_clock_seconds < HARD_LIMIT_SECONDS\n"
    )
    assert _lines(source) == []


def test_allows_a_polling_hang_bound_in_an_if() -> None:
    source = (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    if time.monotonic() - t0 > 30:\n"
        "        raise TimeoutError\n"
    )
    assert _lines(source) == []


def test_flags_an_event_loop_clock() -> None:
    source = (
        "import asyncio\n"
        "async def test_x():\n"
        "    loop = asyncio.get_running_loop()\n"
        "    started = loop.time()\n"
        "    took = loop.time() - started\n"
        "    assert took < EXIT_BOUND_SECONDS\n"
        "    t1 = asyncio.get_running_loop().time()\n"
        "    assert asyncio.get_running_loop().time() - t1 < 2.0\n"
        "    assert self._loop.time() - t1 < 2.0\n"
    )
    assert _lines(source) == [6, 8, 9]


def test_allows_a_non_loop_time_method_and_a_loop_deadline() -> None:
    source = (
        "async def test_x():\n"
        "    t0 = fake_clock.time()\n"
        "    assert fake_clock.time() - t0 < 2.0\n"
        "    deadline = loop.time() + 5\n"
        "    while loop.time() < deadline:\n"
        "        pass\n"
    )
    assert _lines(source) == []


def test_flags_a_module_alias_and_an_aliased_bare_clock() -> None:
    source = (
        "import time as t\n"
        "from time import perf_counter as pc\n"
        "def test_x():\n"
        "    t0 = t.monotonic()\n"
        "    assert t.monotonic() - t0 < 1.0\n"
        "    p0 = pc()\n"
        "    assert pc() - p0 < 1.0\n"
    )
    assert _lines(source) == [5, 7]


def test_flags_an_approx_window_on_a_measured_duration() -> None:
    source = (
        "import time\n"
        "import pytest\n"
        "def test_x():\n"
        "    t0 = time.perf_counter()\n"
        "    took = time.perf_counter() - t0\n"
        "    assert took == pytest.approx(0.5, abs=0.2)\n"
        "    assert pytest.approx(0.5) == time.perf_counter() - t0\n"
    )
    assert _lines(source) == [6, 7]


def test_allows_an_approx_on_a_fake_or_same_run_duration() -> None:
    source = (
        "import time\n"
        "import pytest\n"
        "def test_x():\n"
        "    start = clock.now\n"
        "    elapsed = clock.now - start\n"
        "    assert elapsed == pytest.approx(0.5)\n"
        "    t0 = time.perf_counter()\n"
        "    fast = time.perf_counter() - t0\n"
        "    slow = time.perf_counter() - t0\n"
        "    assert slow == pytest.approx(2 * fast, rel=0.5)\n"
    )
    assert _lines(source) == []


def test_flags_a_pytest_fail_after_a_measured_duration() -> None:
    source = (
        "import time\n"
        "import pytest\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    elapsed = time.monotonic() - t0\n"
        "    if elapsed > 2.0:\n"
        "        pytest.fail(f'slow: {elapsed}')\n"
        "    if time.monotonic() - t0 >= LIMIT_S:\n"
        "        raise AssertionError('slow')\n"
    )
    assert _lines(source) == [6, 8]


def test_allows_a_pytest_fail_that_bounds_a_polling_hang() -> None:
    source = (
        "import time\n"
        "import pytest\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    while not ready():\n"
        "        if time.monotonic() - t0 > 30:\n"
        "            pytest.fail('never ready')\n"
        "    worker.join(timeout=5)\n"
        "    assert done.wait(timeout=2.0)\n"
    )
    assert _lines(source) == []


def test_flags_a_datetime_now_difference() -> None:
    source = (
        "from datetime import UTC, datetime, timedelta\n"
        "def test_x():\n"
        "    t0 = datetime.now(UTC)\n"
        "    assert (datetime.now(UTC) - t0).total_seconds() < 2.0\n"
        "    started = datetime.now()\n"
        "    assert datetime.now() - started < timedelta(seconds=2)\n"
    )
    assert _lines(source) == [4, 6]


def test_flags_a_timeit_result_under_a_literal() -> None:
    source = (
        "import timeit\n"
        "from timeit import repeat\n"
        "def test_x():\n"
        "    assert timeit.timeit(run, number=10) < 0.5\n"
        "    assert min(repeat(run, number=1, repeat=3)) < 0.1\n"
    )
    assert _lines(source) == [4, 5]


_LANE_SOURCE = (
    "import os, time, pytest\n"
    "_skip_perf = pytest.mark.skipif(os.environ.get('EAWF_SKIP_PERF') == '1', reason='x')\n"
    "@_skip_perf\n"
    "def test_guarded():\n"
    "    t0 = time.perf_counter()\n"
    "    assert time.perf_counter() - t0 < 1.0\n"
    "def test_bare():\n"
    "    t0 = time.perf_counter()\n"
    "    assert time.perf_counter() - t0 < 1.0\n"
    "def test_structural():\n"
    "    assert len(MODULES) < 3\n"
)


def test_an_unguarded_lane_budget_is_named_and_a_guarded_one_is_not() -> None:
    assert unguarded_budget_tests(_LANE_SOURCE, "t.py") == ["t.py::test_bare"]


_BUDGET_BODY = "    t0 = time.perf_counter()\n    assert time.perf_counter() - t0 < 1.0\n"
_SKIP_CONDITION = 'os.environ.get("EAWF_SKIP_PERF") == "1"'


def test_a_module_pytestmark_reading_the_skip_env_guards_every_test() -> None:
    method = _BUDGET_BODY.replace("    ", "        ")
    source = (
        "import os, time, pytest\n"
        f"pytestmark = [pytest.mark.perf, pytest.mark.skipif({_SKIP_CONDITION}, reason='x')]\n"
        f"def test_a():\n{_BUDGET_BODY}"
        "class TestB:\n"
        f"    def test_b(self):\n{method}"
    )
    assert unguarded_budget_tests(source, "t.py") == []


def test_any_skipif_reading_the_skip_env_guards_a_test() -> None:
    source = (
        "import os, time, pytest\n"
        f"_SKIPPED = {_SKIP_CONDITION}\n"
        "_lane_guard = pytest.mark.skipif(_SKIPPED, reason='x')\n"
        "@pytest.mark.skipif(_SKIPPED, reason='x')\n"
        f"def test_direct():\n{_BUDGET_BODY}"
        "@_lane_guard\n"
        f"def test_named():\n{_BUDGET_BODY}"
        "@pytest.mark.skipif(sys.platform == 'win32', reason='posix')\n"
        f"def test_other_skip():\n{_BUDGET_BODY}"
    )
    assert unguarded_budget_tests(source, "t.py") == ["t.py::test_other_skip"]


def test_a_class_test_method_is_checked_and_a_class_guard_covers_it() -> None:
    method = _BUDGET_BODY.replace("    ", "        ")
    source = (
        "import os, time, pytest\n"
        "class TestBare:\n"
        f"    def test_bare(self):\n{method}"
        f"    @pytest.mark.skipif({_SKIP_CONDITION}, reason='x')\n"
        f"    def test_guarded(self):\n{method}"
        f"@pytest.mark.skipif({_SKIP_CONDITION}, reason='x')\n"
        "class TestGuardedClass:\n"
        f"    def test_y(self):\n{method}"
        "class TestMarkedClass:\n"
        f"    pytestmark = pytest.mark.skipif({_SKIP_CONDITION}, reason='x')\n"
        f"    def test_z(self):\n{method}"
    )
    assert unguarded_budget_tests(source, "t.py") == ["t.py::TestBare::test_bare"]


def test_a_lane_module_without_budgets_has_nothing_unguarded() -> None:
    assert unguarded_budget_tests("def test_x():\n    assert True\n", "t.py") == []


def test_a_finding_renders_path_line_and_source() -> None:
    (finding,) = budget_findings("def test_x():\n    assert elapsed < 1\n", "tests/t.py")
    assert finding.render() == "tests/t.py:2: elapsed < 1"
