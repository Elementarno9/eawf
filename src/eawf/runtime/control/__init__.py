"""The Run control reducer: request, acknowledgement and effect, folded.

The package holds pure decisions only. It reads no file and opens no
lock: the daemon hands it the control facts it has already read from the
append-only ledger, and it answers with the Run status those facts
support, the control cursor they reach, and whether a new request may
take the Run's control lease.

Keeping the fold pure is what makes the answer reproducible after a
process loss. The same ledger lines yield the same Run truth whether they
are replayed at dispatch time, at reconnect, or by an operator inspecting
a tree with no daemon running at all.
"""

from __future__ import annotations
