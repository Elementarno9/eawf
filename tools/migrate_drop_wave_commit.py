"""One-shot migration: strip the ``Wave.commit`` field from ``.ea/state.json``.

P19-W04 removes the persisted SHA in favour of runtime derivation via
``[P##-W##]`` commit-subject grep. Existing state files carry the legacy
field; this script walks ``state.waves`` and drops the key, leaving the
file otherwise unchanged. Idempotent — re-running on a migrated file is
a no-op.

Usage::

    uv run python tools/migrate_drop_wave_commit.py [path/to/state.json]

Defaults to ``.ea/state.json`` under the current working directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def migrate_payload(payload: dict) -> int:
    """Drop ``commit`` from each wave dict; return the number of entries touched."""
    waves = payload.get("waves") or {}
    touched = 0
    for wave in waves.values():
        if isinstance(wave, dict) and "commit" in wave:
            del wave["commit"]
            touched += 1
    return touched


def migrate_file(state_path: Path) -> int:
    """Load *state_path*, run the migration, write back atomically if changed."""
    payload = json.loads(state_path.read_text())
    touched = migrate_payload(payload)
    if touched:
        state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return touched


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path(".ea") / "state.json"
    if not target.exists():
        print(f"state file not found: {target}", file=sys.stderr)
        return 2
    touched = migrate_file(target)
    print(f"migrated waves: {touched} (file: {target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
