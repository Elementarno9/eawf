"""Memory subsystem for eawf — authoritative ``memory.jsonl`` + state-cache.

``memory.jsonl`` is the source of truth for memory entries (full body, history,
supersede chain). ``state.memory_index`` is a derived cache used for fast list
queries: ``memory add`` writes through (jsonl first, then index); ``memory list``
reads the cache.
"""

from __future__ import annotations
