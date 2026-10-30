"""The epoch-1 to epoch-2 cutover and the generation format it writes.

The cutover was a one-shot whole-corpus import that minted a generation
rather than editing the epoch-1 tree in place. This release no longer
runs it; the readers of the generation format and of the rows it imported
remain here.
"""

from __future__ import annotations
