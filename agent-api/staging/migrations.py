"""Runtime schema check for the staging subsystem (Phase 9 Slice 9.3).

The ``copilot_pending_extractions`` table itself was created by Slice 9.1's
migration in ``audit/schema.sql`` (lines ~120–230). This module ships the
read-only verification helper Slice 9.3 needs at app startup so a deploy
that skipped the schema bootstrap fails fast with a clear log line rather
than 500-ing the first ``/pending-extractions`` request.
"""

from __future__ import annotations

import logging
from typing import Any

from audit import writer as audit_writer

_logger = logging.getLogger(__name__)


_VERIFY_SQL = """
    SELECT to_regclass('public.copilot_pending_extractions') AS tbl
"""


async def verify_schema() -> bool:
    """Return True if ``copilot_pending_extractions`` exists. NEVER raises."""
    try:
        pool = await audit_writer.get_pool()
    except Exception as exc:
        _logger.warning(
            "staging_schema_verify_pool_failed",
            extra={"error_type": type(exc).__name__},
        )
        return False
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            row: Any = await conn.fetchrow(_VERIFY_SQL)
    except Exception as exc:
        _logger.warning(
            "staging_schema_verify_query_failed",
            extra={"error_type": type(exc).__name__, "error": str(exc)},
        )
        return False
    return bool(row and row["tbl"] is not None)


__all__ = ["verify_schema"]
