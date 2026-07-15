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

# EAWF_RUNTIME_DIR is redirected to a short per-invocation tmp dir so no
# suite daemon binds a socket in -- or mutates state through -- the live
# ~/.eawfd; the conftest autouse fixture sub-isolates each xdist worker
# under it. Rooted in $TMPDIR with a short stem for the 104-byte AF_UNIX cap.

# Parameterized test runner: `just test [MODE] [BASE]`.
#
#   fast     (default) parallel core, skips the real-daemon e2e tier + perf
#            timing, then the TUI render snapshots serial. The fast inner loop;
#            mirrors CI's parallel leg without the slow / flaky tiers.
#   ci       reproduces .github/workflows/ci.yaml's two Pytest steps VERBATIM
#            (parallel core, then serial TUI render + perf timing), including
#            the ubuntu-only `--cov` gate -- the coverage flags are appended
#            here when `uname -s` is Linux, matching the workflow's
#            `startsWith(matrix.os, 'ubuntu-')` conditional (empty elsewhere).
#   changed  pytest only the scope the `BASE...HEAD` diff touches, computed by
#            the stateless tools/changed_scope.py selector (BASE default
#            origin/main). A non-.py change pulls the whole golden tier; a
#            changed src module pulls its mirror test file + package dir.
test mode="fast" base="origin/main":
    #!/usr/bin/env sh
    set -eu
    EAWF_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/eawf-rt.XXXXXX")"
    export EAWF_RUNTIME_DIR
    trap 'rm -rf "$EAWF_RUNTIME_DIR"' EXIT
    case "{{mode}}" in
      fast)
        EAWF_SKIP_PERF=1 uv run pytest -n auto -m "not e2e and not eval" --ignore=tests/snapshots/tui --ignore=tests/perf/tui
        uv run pytest -n0 tests/snapshots/tui
        ;;
      ci)
        if [ "$(uname -s)" = "Linux" ]; then
          cov_parallel="--cov=eawf --cov-report=xml --cov-fail-under=0"
          cov_serial="--cov=eawf --cov-append --cov-report=xml --cov-fail-under=0"
        else
          cov_parallel=""
          cov_serial=""
        fi
        uv run pytest -n auto --ignore=tests/snapshots/tui --ignore=tests/perf/tui $cov_parallel
        uv run pytest -n0 tests/snapshots/tui tests/perf/tui $cov_serial
        ;;
      changed)
        scope="$(uv run python tools/changed_scope.py --base "{{base}}" --existing-only)"
        if [ -z "$scope" ]; then
          echo "changed-scope: no matching tests for {{base}}...HEAD; nothing to run"
          exit 0
        fi
        echo "changed-scope: pytest $scope"
        uv run pytest -n auto $scope
        ;;
      *)
        echo "unknown test mode: {{mode}} (want: fast | ci | changed)" >&2
        exit 2
        ;;
    esac

# Use before pushing a phase PR so the CI test gates surface locally.

# Full CI mirror: parallel core (incl e2e) + serial TUI snapshots + perf.
test-all:
    #!/usr/bin/env sh
    set -eu
    EAWF_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/eawf-rt.XXXXXX")"
    export EAWF_RUNTIME_DIR
    trap 'rm -rf "$EAWF_RUNTIME_DIR"' EXIT
    uv run pytest -n auto --ignore=tests/snapshots/tui --ignore=tests/perf/tui
    uv run pytest -n0 tests/snapshots/tui tests/perf/tui

# TUI render snapshots + perf timing budget, serial (xdist-unsafe by design).
test-tui:
    #!/usr/bin/env sh
    set -eu
    EAWF_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/eawf-rt.XXXXXX")"
    export EAWF_RUNTIME_DIR
    trap 'rm -rf "$EAWF_RUNTIME_DIR"' EXIT
    uv run pytest -n0 tests/snapshots/tui tests/perf/tui
