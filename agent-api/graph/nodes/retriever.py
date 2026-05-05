"""Slice 4.4 — LangGraph retriever node backed by the real hybrid-RAG pipeline.

Pulls the user query off the W2 state, runs ``rag.retrieve.search``, and
writes the snippet list (or a ``skipped_reason`` when the message is empty)
into ``state["retrieval"]``. Emits one ``node_handoff`` audit row carrying
ONLY the cardinality — never snippet content, never the raw query.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from audit.models import AuditEvent
from audit import writer as audit_writer

from ..state import W2State

logger = logging.getLogger(__name__)


def _coerce_request_id(state: W2State) -> str | None:
    rid: str | None = state.get("request_id")
    try:
        from observability.json_logging import request_id_var as _rid_var
        ctx_rid = _rid_var.get()
        if ctx_rid:
            rid = ctx_rid
    except (LookupError, ImportError):
        pass
    return rid


async def retriever_node(state: W2State) -> dict[str, Any]:
    """Run hybrid-RAG retrieval for the user message in ``state``.

    No-message → record skipped_reason and return zero snippets. The graph
    edge to ``critic`` is unconditional regardless of outcome (critic
    handles the "no evidence" rendering).
    """
    t0 = time.monotonic()
    raw_message = state.get("message") or ""
    query = raw_message.strip()
    rid = _coerce_request_id(state)

    if not query:
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "graph_retriever_skipped_no_query",
            extra={
                "request_id": rid,
                "session_id": state.get("session_id"),
                "duration_ms": duration_ms,
            },
        )
        try:
            await audit_writer.emit(
                AuditEvent(
                    event_type="node_handoff",
                    request_id=rid,
                    session_id=state.get("session_id"),
                    provider_id=state.get("provider_id"),
                    patient_id=state.get("patient_id"),
                    outcome="success",
                    duration_ms=duration_ms,
                    detail_json={
                        "from_node": "retriever",
                        "to_node": "critic",
                        "skipped_reason": "no_query",
                        "n_results": 0,
                    },
                )
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "graph_retriever_audit_emit_failed",
                extra={"error_type": type(exc).__name__},
            )
        return {
            "retrieval": {
                "snippets": [],
                "skipped_reason": "no_query",
                "fallback_used": False,
            }
        }

    # Local import keeps a hard dependency on rag out of graph collection
    # time — graph remains importable on hosts where rag's optional
    # numpy/pgvector deps are absent.
    from rag import retrieve as _retrieve

    try:
        snippets = await _retrieve.search(query)
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "graph_retriever_search_failed",
            extra={
                "request_id": rid,
                "session_id": state.get("session_id"),
                "error_type": type(exc).__name__,
                "duration_ms": duration_ms,
            },
        )
        try:
            await audit_writer.emit(
                AuditEvent(
                    event_type="node_handoff",
                    request_id=rid,
                    session_id=state.get("session_id"),
                    provider_id=state.get("provider_id"),
                    patient_id=state.get("patient_id"),
                    outcome="failure",
                    duration_ms=duration_ms,
                    detail_json={
                        "from_node": "retriever",
                        "to_node": "critic",
                        "error_type": type(exc).__name__,
                        "n_results": 0,
                    },
                )
            )
        except Exception:
            pass
        return {
            "retrieval": {
                "snippets": [],
                "fallback_used": False,
                "error_type": type(exc).__name__,
            }
        }

    snippet_dicts: list[dict[str, Any]] = []
    for s in snippets:
        d = s._asdict()
        # ISO-format the date so downstream (critic / finalize) can JSON-serialise.
        ivd = d.get("indexed_version_date")
        if ivd is not None and not isinstance(ivd, str):
            try:
                d["indexed_version_date"] = ivd.isoformat()
            except Exception:
                d["indexed_version_date"] = str(ivd)
        snippet_dicts.append(d)

    duration_ms = int((time.monotonic() - t0) * 1000)
    try:
        await audit_writer.emit(
            AuditEvent(
                event_type="node_handoff",
                request_id=rid,
                session_id=state.get("session_id"),
                provider_id=state.get("provider_id"),
                patient_id=state.get("patient_id"),
                outcome="success",
                duration_ms=duration_ms,
                detail_json={
                    "from_node": "retriever",
                    "to_node": "critic",
                    "n_results": len(snippet_dicts),
                    "duration_ms": duration_ms,
                },
            )
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "graph_retriever_audit_emit_failed",
            extra={"error_type": type(exc).__name__},
        )

    return {
        "retrieval": {
            "snippets": snippet_dicts,
            "fallback_used": False,
        }
    }


__all__ = ["retriever_node"]
