"""Slice 5.2 — W2 eval case runner.

Executes the W2 graph for a single ``W2EvalCase`` fixture and captures the
extraction, critic decision, soft warns, and emitted log records into a
policy-free :class:`RunOutcome`. The rubric layer (:mod:`evals.scoring`)
interprets the outcome — the runner just collects.

The fixture set itself is owned by the parallel agent under
``tests.fixtures.w2_eval_cases``; we import lazily so an absent fixture
package does not prevent the rubric/scoring tests from running.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
        self.records.append(
            {
                "level": record.levelname,
                "name": record.name,
                "message": message,
                "extra": extras,
            }
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
    return handler


def _detach_capture(handler: _RecordCaptureHandler) -> None:
    for name in _GRAPH_LOGGERS:
        logging.getLogger(name).removeHandler(handler)
    logging.getLogger().removeHandler(handler)


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
# Public entry point
# --------------------------------------------------------------------------- #


async def run_case(
    case: Any,  # W2EvalCase — typed via duck-typing to avoid hard import
    *,
    fixtures_root: Path,
    compile_graph_factory: Optional[Callable[..., Any]] = None,
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
    """
    case_id = getattr(case, "case_id", "<unknown>")
    handler = _attach_capture()
    try:
        # Resolve fixture bytes lazily; the fixture key may not exist on disk
        # yet for some experimental cases.
        fixture_path = resolve_fixture_path(
            getattr(case, "fixture_key", ""), fixtures_root
        )

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

        return RunOutcome(
            case_id=case_id,
            extraction=final.get("extraction"),
            critic_decision=final.get("critic_decision"),
            critic_violations=list(final.get("critic_violations") or []),
            soft_warns=list(final.get("soft_warns") or []),
            captured_logs=list(handler.records),
            error=None,
        )
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
            captured_logs=list(handler.records),
            error=f"{type(exc).__name__}",
        )
    finally:
        _detach_capture(handler)


__all__ = ["RunOutcome", "run_case", "resolve_fixture_path"]
