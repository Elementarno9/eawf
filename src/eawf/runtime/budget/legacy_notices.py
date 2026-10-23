"""Event types of the legacy over-budget pause rows.

The detector used to raise a durable pause each time a wave crossed an
advisory band. Those rows are not deleted: they stay in the epoch-1 event
store, and the pause migration reads them by these event types.
"""

from __future__ import annotations

from typing import Final

#: ``event_type`` of a legacy pause row in the epoch-1 event store.
PAUSE_EVENT_TYPE: Final = "needs_user_pause"

#: ``event_type`` of a legacy resume row, which answers the pause it names.
RESUME_EVENT_TYPE: Final = "needs_user_resume"
