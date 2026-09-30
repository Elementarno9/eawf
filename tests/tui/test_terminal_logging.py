"""The launcher's root-logging swap: no terminal log handler bleeds onto the console."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator

import pytest
from textual.logging import TextualHandler

from eawf.surfaces.tui.terminal_logging import restore_root_logging, swap_root_logging_to_textual


def _has_terminal_stream_handler() -> bool:
    """Return ``True`` when a root handler still writes to stderr/stdout."""
    root = logging.getLogger()
    return any(
        isinstance(h, logging.StreamHandler) and h.stream in (sys.stderr, sys.stdout)
        for h in root.handlers
    )


@pytest.fixture
def _isolated_root_logging() -> Iterator[None]:
    """Save + restore the real root handler list around a swap test."""
    root = logging.getLogger()
    original = list(root.handlers)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in original:
        root.addHandler(handler)


def test_swap_root_logging_removes_stderr_handler(_isolated_root_logging: object) -> None:
    """The swap detaches the stderr StreamHandler and installs a TextualHandler."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(stderr_handler)
    assert _has_terminal_stream_handler()  # precondition: the leak is present

    swap_root_logging_to_textual()

    assert not _has_terminal_stream_handler()  # no handler writes to the screen
    assert any(isinstance(h, TextualHandler) for h in root.handlers)


def test_swap_root_logging_textual_handler_is_timestamped(
    _isolated_root_logging: object,
) -> None:
    """The installed TextualHandler carries a timestamped (asctime) formatter."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    swap_root_logging_to_textual()

    textual = next(h for h in root.handlers if isinstance(h, TextualHandler))
    record = logging.LogRecord(
        name="eawf.demo",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="drive armed",
        args=(),
        exc_info=None,
    )
    rendered = textual.format(record)
    # A YYYY-MM-DD timestamp precedes the level so console latency is measurable.
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rendered)
    assert "INFO eawf.demo drive armed" in rendered


def test_swap_root_logging_also_detaches_stdout(_isolated_root_logging: object) -> None:
    """A stdout-targeting StreamHandler is detached too (both terminal streams)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(logging.StreamHandler(stream=sys.stdout))

    swap_root_logging_to_textual()

    assert not _has_terminal_stream_handler()
    assert any(isinstance(h, TextualHandler) for h in root.handlers)


def test_restore_root_logging_reinstates_prior_handlers(
    _isolated_root_logging: object,
) -> None:
    """Restore reinstates the exact handler list captured before the swap."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(stderr_handler)

    saved = swap_root_logging_to_textual()
    assert not _has_terminal_stream_handler()  # swapped out for the run

    restore_root_logging(saved)

    assert root.handlers == [stderr_handler]  # exact prior list back
    assert _has_terminal_stream_handler()  # scrubbed stderr sink restored
    assert not any(isinstance(h, TextualHandler) for h in root.handlers)
