"""Slice 5.2 — W2 eval case runner.

Executes the W2 graph for a single ``W2EvalCase`` fixture and captures the
extraction, critic decision, soft warns, and emitted log records into a
policy-free :class:`RunOutcome`. The rubric layer (:mod:`evals.scoring`)
interprets the outcome — the runner just collects.

The fixture set itself is owned by the parallel agent under
``tests.fixtures.w2_eval_cases``; we import lazily so an absent fixture
package does not prevent the rubric/scoring tests from running.

Cache integration
-----------------
When ``EVAL_USE_CACHE`` is set to ``read``, ``write``, or ``readwrite``, the
runner wraps the graph invocation with a content-hash filesystem cache
(see :mod:`evals._response_cache`). The cache is keyed by:

    sha256(fixture_bytes || EVAL_CACHE_PROMPT_HASH || EVAL_MODEL_ID || EVAL_CACHE_VERSION)

The default mode is ``off`` — the cache is a no-op until the operator opts in.
"""
from __future__ import annotations

import logging
import threading
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Outcome dataclass
# --------------------------------------------------------------------------- #


@dataclass
class RunOutcome:
    """Captured output of one W2 graph run for rubric scoring."""

    case_id: str
    extraction: Optional[Dict[str, Any]] = None
    critic_decision: Optional[Literal["pass", "soft_warn", "hard_block"]] = None
    critic_violations: List[str] = field(default_factory=list)
    soft_warns: List[Dict[str, Any]] = field(default_factory=list)
    captured_logs: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    # Phase 3 — provenance chain probe. ``ocr_layout`` mirrors graph state
    # (one entry per OCR block, keyed by ``bbox_id``); ``observations`` is
    # the list of FHIR-shaped Observation rows fetched from
    # ``copilot_observations`` (one per id in ``metadata.observation_ids``).
    # When the MySQL probe is unavailable the runner leaves ``observations``
    # as ``None`` and the rubric records ``provenance_chain=None`` (skipped).
    ocr_layout: Optional[List[Dict[str, Any]]] = None
    observations: Optional[List[Dict[str, Any]]] = None
    # Evidence-retrieval bucket — populated when the case routes through the
    # evidence_retriever node. ``retrieval`` mirrors ``state["retrieval"]``
    # (the snippet list returned by ``rag.retrieve.search``); ``finalized``
    # mirrors ``state["finalized"]`` so rubrics can inspect the structured
    # response. ``skipped_reason`` is set to a non-None string (e.g.
    # ``"missing_AUDIT_DB_URL"`` or ``"missing_VOYAGE_API_KEY"``) when the
    # runner declined to call the live retrieval stack — the suite reports
    # these cases as ``skipped`` rather than ``failed``.
    retrieval: Optional[Dict[str, Any]] = None
    finalized: Optional[Dict[str, Any]] = None
    skipped_reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Log capture
# --------------------------------------------------------------------------- #


_GRAPH_LOGGERS = (
    "graph",
    "graph.nodes.supervisor",
    "graph.nodes.extractor",
    "graph.nodes.demographics",
    "graph.nodes.critic",
    "graph.nodes.finalize",
    "graph.nodes.retriever",
    "graph.nodes.structured",
    "extractors.lab",
    "extractors.intake",
    "documents.ocr",
    "evals.runner",
)


class _RecordCaptureHandler(logging.Handler):
    """Capture every log record emitted under the W2 graph loggers.

    Stores a serialized dict per record (level, name, message, plus any
    ``extra`` fields surfaced via ``record.__dict__``). The PHI-in-logs
    rubric scans both the formatted message AND the extra dict for
    synthetic-PHI tokens, so we keep both.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[Dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover — defensive
            message = str(record.msg)
        # Pull out non-standard attributes (the PSR-3-style ``extra={...}``
        # fields injected by call sites). We blacklist the standard
        # LogRecord attribute names so we don't leak filename/lineno noise.
        std_attrs = set(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys())
        extras: Dict[str, Any] = {
            k: v
            for k, v in record.__dict__.items()
            if k not in std_attrs and not k.startswith("_")
        }
        entry = {
            "level": record.levelname,
            "name": record.name,
            "message": message,
            "extra": extras,
        }
        # Per-case isolation: when a ``run_case`` invocation has bound a
        # records list to ``_case_log_records``, route THIS record there
        # instead of the shared handler list. ContextVars are task-local
        # under asyncio (see ``contextvars.copy_context`` semantics) so
        # concurrent ``asyncio.gather``'d calls each write to their own
        # list — no cross-contamination.
        scoped = _case_log_records.get()
        if scoped is not None:
            scoped.append(entry)
            return
        # Out-of-eval / legacy direct usage: fall back to the handler's
        # own list so ``_attach_capture`` callers (see
        # tests/test_evals_no_phi_real_bug.py) keep working.
        self.records.append(entry)


# Per-case log capture — see ``run_case`` for the binding site. The default
# is ``None`` so calls outside an eval run hit the handler-local fallback.
_case_log_records: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar(
    "_case_log_records", default=None
)

# Module-level singleton. Attached lazily on first ``run_case`` call (under
# ``_GLOBAL_HANDLER_LOCK``) and never detached — the parallel runner shares
# it across N concurrent cases. Per-case isolation is delivered by the
# ContextVar above, not by add/remove churn on the logger handler list.
_GLOBAL_CAPTURE_HANDLER: Optional["_RecordCaptureHandler"] = None
_GLOBAL_HANDLER_LOCK = threading.Lock()


def _ensure_global_capture() -> "_RecordCaptureHandler":
    """Attach the shared capture handler exactly once (lazy, thread-safe)."""
    global _GLOBAL_CAPTURE_HANDLER
    if _GLOBAL_CAPTURE_HANDLER is not None:
        return _GLOBAL_CAPTURE_HANDLER
    with _GLOBAL_HANDLER_LOCK:
        if _GLOBAL_CAPTURE_HANDLER is not None:
            return _GLOBAL_CAPTURE_HANDLER
        handler = _RecordCaptureHandler()
        for name in _GRAPH_LOGGERS:
            lg = logging.getLogger(name)
            lg.addHandler(handler)
            if lg.level == logging.NOTSET or lg.level > logging.DEBUG:
                lg.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > logging.DEBUG:
            root.setLevel(logging.DEBUG)
        # Pin third-party loggers to WARNING (see ``_THIRD_PARTY_QUIET_LOGGERS``
        # rationale above). We do NOT restore — the handler is permanent for
        # the lifetime of the process; restoration would re-open the cascade
        # window between cases.
        for name in _THIRD_PARTY_QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        _GLOBAL_CAPTURE_HANDLER = handler
        return handler


# Third-party loggers that dump request/response bodies at DEBUG. When the
# capture handler raises root → DEBUG, these libraries emit the raw vision
# payload (which mirrors the source document, synthetic PHI included). The
# no_phi_in_logs rubric — correctly — flags those emissions even though they
# are not produced by our own code. Pin them to WARNING for the duration of
# the capture so the rubric scans only first-party graph events.
# See docs/SECURITY_TRADEOFFS.md ("Known false-positive on no_phi_in_logs cascade").
_THIRD_PARTY_QUIET_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.http11",
    "httpcore.connection",
    "anthropic",
    "anthropic._base_client",
    "openai",
    "urllib3",
    "langgraph",
    "langchain",
    "langchain_core",
)


def _attach_capture() -> _RecordCaptureHandler:
    handler = _RecordCaptureHandler()
    for name in _GRAPH_LOGGERS:
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        # Ensure DEBUG-level records reach the handler — modules typically
        # don't configure their own level, inheriting WARNING from root.
        if lg.level == logging.NOTSET or lg.level > logging.DEBUG:
            lg.setLevel(logging.DEBUG)
    # Root capture as well so anything we missed still flows through.
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    # Pin third-party loggers to WARNING so their DEBUG request/response body
    # dumps (which mirror the source document) never reach the capture.
    handler._restore_levels = {}  # type: ignore[attr-defined]
    for name in _THIRD_PARTY_QUIET_LOGGERS:
        lg = logging.getLogger(name)
        handler._restore_levels[name] = lg.level  # type: ignore[attr-defined]
        lg.setLevel(logging.WARNING)
    return handler


def _detach_capture(handler: _RecordCaptureHandler) -> None:
    for name in _GRAPH_LOGGERS:
        logging.getLogger(name).removeHandler(handler)
    logging.getLogger().removeHandler(handler)
    # Restore third-party logger levels we pinned in _attach_capture.
    restore = getattr(handler, "_restore_levels", {}) or {}
    for name, level in restore.items():
        logging.getLogger(name).setLevel(level)


# --------------------------------------------------------------------------- #
# Fixture path resolution
# --------------------------------------------------------------------------- #


_FIXTURE_INDEX_CACHE: Optional[Dict[str, Path]] = None


def _build_fixture_index(fixtures_root: Path) -> Dict[str, Path]:
    """Resolve fixture_key -> Path. Lazy + cached.

    Tries the corpus generator first (``tests.fixtures.eval._generate_eval_corpus``).
    If unavailable, falls back to a flat ``{fixtures_root}/{key}.pdf`` lookup.
    """
    global _FIXTURE_INDEX_CACHE
    if _FIXTURE_INDEX_CACHE is not None:
        return _FIXTURE_INDEX_CACHE

    index: Dict[str, Path] = {}
    try:
        from tests.fixtures.eval import _generate_eval_corpus  # type: ignore

        gen = getattr(_generate_eval_corpus, "generate_all", None)
        if gen is not None:
            built = gen()
            if isinstance(built, dict):
                for key, value in built.items():
                    index[str(key)] = Path(str(value))
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        logger.debug("eval_fixture_corpus_unavailable", extra={"error_type": type(exc).__name__})

    _FIXTURE_INDEX_CACHE = index
    return index


def resolve_fixture_path(fixture_key: str, fixtures_root: Path) -> Path:
    """Return a filesystem path for the given fixture key."""
    index = _build_fixture_index(fixtures_root)
    if fixture_key in index:
        return index[fixture_key]
    # Flat fallback: agent-api/tests/fixtures/{key}.pdf
    candidate = fixtures_root / f"{fixture_key}.pdf"
    return candidate


# --------------------------------------------------------------------------- #
# Cache helpers
# --------------------------------------------------------------------------- #


def _outcome_to_dict(outcome: "RunOutcome") -> Dict[str, Any]:
    """Serialise a RunOutcome to a plain dict for caching."""
    return asdict(outcome)


def _outcome_from_dict(data: Dict[str, Any], case_id: str) -> "RunOutcome":
    """Reconstruct a RunOutcome from a cached plain dict."""
    # Guard: unknown keys from future schema additions are silently dropped.
    known = {f.name for f in RunOutcome.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    filtered = {k: v for k, v in data.items() if k in known}
    filtered.setdefault("case_id", case_id)
    return RunOutcome(**filtered)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


async def run_case(
    case: Any,  # W2EvalCase — typed via duck-typing to avoid hard import
    *,
    fixtures_root: Path,
    compile_graph_factory: Optional[Callable[..., Any]] = None,
    cache: Optional[Any] = None,
    cache_mode: Optional[str] = None,
) -> RunOutcome:
    """Execute the W2 graph for one case and return a captured outcome.

    Parameters
    ----------
    case:
        A ``W2EvalCase`` instance (frozen dataclass; see fixture spec).
    fixtures_root:
        Filesystem root used by the flat fallback when the eval-corpus
        generator is not present.
    compile_graph_factory:
        Optional override for graph compilation (used by tests). Defaults
        to :func:`graph.compile_graph` resolved lazily so the import stays
        cheap when only the dataclass is needed.
    cache:
        Optional :class:`evals._response_cache.EvalResponseCache` instance.
        When ``None`` the module-level default cache is used, but only when
        ``cache_mode`` enables reads or writes.
    cache_mode:
        One of ``"off"``, ``"read"``, ``"write"``, ``"readwrite"``.
        Defaults to the ``EVAL_USE_CACHE`` environment variable (``"off"``
        when absent). Passed as an override so the test suite can control it
        without touching the environment.
    """
    # ── Cache bootstrap ──────────────────────────────────────────────────────
    from evals._response_cache import (  # local import — keep startup cheap
        EvalResponseCache,
        cache_reads_enabled,
        cache_writes_enabled,
        derive_cache_key,
        get_default_cache,
        resolve_cache_mode,
    )

    effective_mode = resolve_cache_mode(cache_mode)
    effective_cache: EvalResponseCache = cache if cache is not None else get_default_cache()
    do_read = cache_reads_enabled(effective_mode)
    do_write = cache_writes_enabled(effective_mode)
    case_id = getattr(case, "case_id", "<unknown>")
    # Per-case log isolation. Bind a fresh records list to the ContextVar
    # so this task's emissions (and only this task's) accumulate here. Under
    # ``asyncio.gather`` each task runs in its own copied context, so two
    # concurrent ``run_case`` invocations cannot cross-contaminate.
    case_records: List[Dict[str, Any]] = []
    token = _case_log_records.set(case_records)
    _ensure_global_capture()
    try:
        # ── Evidence-retrieval branch ────────────────────────────────────
        # Cases in the ``evidence_retrieval`` bucket exercise the
        # LangGraph ``evidence_retriever`` node end-to-end against the
        # indexed guideline corpus. We bypass the document path entirely:
        # build an initial state with no ``file_bytes_ref``, the case's
        # ``evidence_query`` as the user message, and a stub ``extraction``
        # so the supervisor's routing rule
        # ("message present AND extraction populated → evidence_retriever",
        # see graph/nodes/supervisor.py:_decide) fires.
        #
        # Real retrieval requires Postgres+pgvector (AUDIT_DB_URL) AND a
        # Voyage embedding key (VOYAGE_API_KEY). When either is missing
        # we emit a structured-log warning and return a RunOutcome with
        # ``skipped_reason`` set — the rubric layer treats that as
        # ``skipped`` instead of ``failed``.
        bucket = getattr(case, "bucket", None)
        evidence_query = getattr(case, "evidence_query", None)
        if bucket == "evidence_retrieval" and evidence_query:
            import os

            missing: list[str] = []
            if not os.environ.get("AUDIT_DB_URL"):
                missing.append("AUDIT_DB_URL")
            if not os.environ.get("VOYAGE_API_KEY"):
                missing.append("VOYAGE_API_KEY")
            if missing:
                reason = "missing_" + "_and_".join(missing)
                logger.warning(
                    "eval_evidence_retrieval_skipped",
                    extra={
                        "case_id": case_id,
                        "missing_env": missing,
                        "reason": reason,
                    },
                )
                return RunOutcome(
                    case_id=case_id,
                    extraction=None,
                    critic_decision=None,
                    captured_logs=list(case_records),
                    error=None,
                    skipped_reason=reason,
                )

            # ── Cache lookup (evidence-retrieval path, fixture_bytes = b"") ──
            _ev_cache_key = derive_cache_key(b"") if (do_read or do_write) else ""
            if do_read and _ev_cache_key:
                _cached = effective_cache.read(_ev_cache_key)
                if _cached is not None:
                    logger.debug(
                        "eval_cache.runner_hit",
                        extra={"case_id": case_id, "branch": "evidence_retrieval"},
                    )
                    return _outcome_from_dict(_cached, case_id)

            chart_patient = dict(getattr(case, "chart_patient", {}) or {})

            async def _empty_file_bytes_provider(_ref: str) -> bytes:
                # Should never be invoked — file_bytes_ref is None — but
                # the graph factory requires the parameter.
                return b""

            async def _fhir_patient_provider_evidence(_pid: str) -> dict:
                return chart_patient

            if compile_graph_factory is None:
                from graph import compile_graph as _compile  # local import

                compile_graph_factory = _compile

            compiled = compile_graph_factory(
                file_bytes_provider=_empty_file_bytes_provider,
                fhir_patient_provider=_fhir_patient_provider_evidence,
            )

            from graph import make_initial_state  # local import

            initial = make_initial_state(
                request_id=f"eval-{case_id}",
                session_id=f"eval-sess-{case_id}",
                provider_id="eval-provider",
                patient_id=str(chart_patient.get("id") or "eval-patient"),
                file_bytes_ref=None,
                doc_type_hint=None,
                message=str(evidence_query),
            )
            # Seed a stub extraction so the supervisor routes to
            # evidence_retriever (see graph/nodes/supervisor.py:_decide).
            initial["extraction"] = {
                "kind": "unknown",
                "schema_version": "1.0",
                "patient_id": str(chart_patient.get("id") or "eval-patient"),
                "document_reference_id": f"eval-stub-{case_id}",
            }

            config = {"configurable": {"thread_id": f"eval-thread-{case_id}"}}
            final = await compiled.ainvoke(initial, config=config)

            outcome_ev = RunOutcome(
                case_id=case_id,
                extraction=final.get("extraction"),
                critic_decision=final.get("critic_decision"),
                critic_violations=list(final.get("critic_violations") or []),
                soft_warns=list(final.get("soft_warns") or []),
                captured_logs=list(case_records),
                error=None,
                retrieval=final.get("retrieval"),
                finalized=final.get("finalized"),
            )
            if do_write and _ev_cache_key:
                effective_cache.write(_ev_cache_key, _outcome_to_dict(outcome_ev))
            return outcome_ev

        # Resolve fixture bytes lazily; the fixture key may not exist on disk
        # yet for some experimental cases.
        fixture_path = resolve_fixture_path(
            getattr(case, "fixture_key", ""), fixtures_root
        )

        # ── Cache lookup (document path) ─────────────────────────────────
        # Read fixture bytes once for the cache key; the provider closure
        # will re-read them if the graph actually runs (bytes are not held
        # in memory after key derivation).
        _fixture_bytes: bytes = b""
        _cache_key = ""
        if do_read or do_write:
            try:
                _fixture_bytes = fixture_path.read_bytes()
            except OSError:
                # Fixture absent — can't derive a stable key; bypass cache.
                pass
            if _fixture_bytes:
                _cache_key = derive_cache_key(_fixture_bytes)

        if do_read and _cache_key:
            _cached = effective_cache.read(_cache_key)
            if _cached is not None:
                logger.debug(
                    "eval_cache.runner_hit",
                    extra={"case_id": case_id, "branch": "document"},
                )
                return _outcome_from_dict(_cached, case_id)

        async def _file_bytes_provider(_ref: str) -> bytes:
            return fixture_path.read_bytes()

        chart_patient = dict(getattr(case, "chart_patient", {}) or {})

        async def _fhir_patient_provider(_pid: str) -> dict:
            return chart_patient

        if compile_graph_factory is None:
            from graph import compile_graph as _compile  # local import

            compile_graph_factory = _compile

        compiled = compile_graph_factory(
            file_bytes_provider=_file_bytes_provider,
            fhir_patient_provider=_fhir_patient_provider,
        )

        # Build the initial state.
        from graph import make_initial_state  # local import

        initial = make_initial_state(
            request_id=f"eval-{case_id}",
            session_id=f"eval-sess-{case_id}",
            provider_id="eval-provider",
            patient_id=str(chart_patient.get("id") or "eval-patient"),
            file_bytes_ref=f"eval-ref-{case_id}",
            doc_type_hint=getattr(case, "doc_type_hint", None),
        )

        config = {"configurable": {"thread_id": f"eval-thread-{case_id}"}}
        final = await compiled.ainvoke(initial, config=config)

        outcome_doc = RunOutcome(
            case_id=case_id,
            extraction=final.get("extraction"),
            critic_decision=final.get("critic_decision"),
            critic_violations=list(final.get("critic_violations") or []),
            soft_warns=list(final.get("soft_warns") or []),
            captured_logs=list(case_records),
            error=None,
            ocr_layout=list(final.get("ocr_layout") or []) if final.get("ocr_layout") is not None else None,
            observations=None,  # populated by probe_observations() if MySQL is reachable
        )
        if do_write and _cache_key:
            effective_cache.write(_cache_key, _outcome_to_dict(outcome_doc))
        return outcome_doc
    except Exception as exc:  # noqa: BLE001 — runner is policy-free
        logger.exception(
            "eval_run_case_failed",
            extra={"case_id": case_id, "error_type": type(exc).__name__},
        )
        return RunOutcome(
            case_id=case_id,
            extraction=None,
            critic_decision=None,
            critic_violations=[],
            soft_warns=[],
            captured_logs=list(case_records),
            error=f"{type(exc).__name__}",
        )
    finally:
        _case_log_records.reset(token)


# --------------------------------------------------------------------------- #
# Optional MySQL probe — Observation provenance chain (Phase 3)
# --------------------------------------------------------------------------- #


_OBS_CACHE: Dict[str, Dict[str, Any]] = {}


async def probe_observations(
    observation_ids: List[str],
    *,
    mysql_url: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch ``copilot_observations`` rows for the given ids.

    Returns a list of ``{"id", "fhir_resource", "_copilot_citations"}`` dicts
    when the MySQL probe is reachable. Returns ``None`` when MySQL is not
    available — the caller treats that as "skipped".

    Reads ``COPILOT_OBSERVATIONS_MYSQL_URL`` from the env when no URL is
    provided. The URL must be of the shape ``mysql://user:pass@host:port/db``;
    the runner uses ``aiomysql`` which is an optional dep on the host.
    """
    import os
    import json as _json

    if not observation_ids:
        return []

    url = mysql_url or os.environ.get("COPILOT_OBSERVATIONS_MYSQL_URL", "")
    if not url:
        return None

    try:
        import aiomysql  # type: ignore
        import urllib.parse as _u
    except Exception:  # pragma: no cover — optional dep
        return None

    parsed = _u.urlparse(url)
    out: List[Dict[str, Any]] = []
    try:
        conn = await aiomysql.connect(
            host=parsed.hostname,
            port=parsed.port or 3306,
            user=parsed.username,
            password=parsed.password,
            db=(parsed.path or "/openemr").lstrip("/") or "openemr",
            autocommit=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe_observations_connect_failed", extra={"error_type": type(exc).__name__})
        return None

    try:
        cur = await conn.cursor()
        for obs_id in observation_ids:
            if obs_id in _OBS_CACHE:
                out.append(_OBS_CACHE[obs_id])
                continue
            await cur.execute(
                "SELECT id, fhir_resource, citations FROM copilot_observations WHERE id=%s",
                (obs_id,),
            )
            row = await cur.fetchone()
            if not row:
                continue
            try:
                fhir_resource = _json.loads(row[1]) if isinstance(row[1], (str, bytes)) else (row[1] or {})
            except Exception:
                fhir_resource = {}
            try:
                citations = _json.loads(row[2]) if isinstance(row[2], (str, bytes)) else (row[2] or [])
            except Exception:
                citations = []
            entry = {
                "id": row[0],
                "fhir_resource": fhir_resource,
                "_copilot_citations": citations,
            }
            _OBS_CACHE[obs_id] = entry
            out.append(entry)
        return out
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


__all__ = ["RunOutcome", "run_case", "resolve_fixture_path", "probe_observations"]
