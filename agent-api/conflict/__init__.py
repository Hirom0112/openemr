"""Cross-source conflict detection pass — Phase 9 Slice 9.7.

Public surface (importable from ``conflict``):

* :func:`detect_cross_source_conflicts` — pure deterministic detector.
* :class:`CrossSourceConflict`, :class:`CrossSourceConflictValue` — DTOs.
* :func:`rank_sources`, :func:`source_pair_label` — ordering helpers.

Architecture: see W2_ARCHITECTURE.md §5.11. The package is a near-leaf —
the importlinter contract ``conflict-is-mostly-leaf`` forbids reaching
into ``agent``, ``graph``, ``documents``, ``rag``, ``demographics``, the
clinical siblings (``triage``, ``query``, ``briefing``, ``verification``,
``medication``, ``handoff``), or the ``auth`` / ``checkpointer`` plumbing.
``extractors.schemas`` (typed citation payloads) and ``observability``
(logging) are allowed.
"""

from .detector import detect_cross_source_conflicts
from .ranking import rank_sources, source_pair_label
from .schemas import (
    ConflictOutcome,
    CrossSourceConflict,
    CrossSourceConflictValue,
)

__all__ = [
    "ConflictOutcome",
    "CrossSourceConflict",
    "CrossSourceConflictValue",
    "detect_cross_source_conflicts",
    "rank_sources",
    "source_pair_label",
]
