# eawf test recipes. The parallel suite mirrors CI (`-n auto`); a bare
# `uv run pytest <path>::<test>` stays single-process = fast for targeted
# / TDD runs, so reach for `just` only when you want the whole suite.
#
# Install just: `brew install just` (macOS), `cargo install just` (any OS,
# incl Windows), or see https://github.com/casey/just#installation.

# Show available recipes.
_default:
    @just --list

# Drops the real-daemon e2e tier (~8s daemon spawn-wait each) and perf timing
# (p99, flaky under local contention), then runs the TUI render snapshots
# serial (xdist-safe but kept off the worker pool here).

# Fast inner loop: parallel core, skips real-daemon e2e + perf timing.
test:
    EAWF_SKIP_PERF=1 uv run pytest -n auto -m "not e2e and not eval" --ignore=tests/snapshots/tui --ignore=tests/perf/tui
    uv run pytest -n0 tests/snapshots/tui

# Use before pushing a phase PR so the CI test gates surface locally.

# Full CI mirror: parallel core (incl e2e) + serial TUI snapshots + perf.
test-all:
    uv run pytest -n auto --ignore=tests/snapshots/tui --ignore=tests/perf/tui
    uv run pytest -n0 tests/snapshots/tui tests/perf/tui

# TUI render snapshots + perf timing budget, serial (xdist-unsafe by design).
test-tui:
    uv run pytest -n0 tests/snapshots/tui tests/perf/tui
