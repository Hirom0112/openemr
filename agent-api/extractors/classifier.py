"""Document classifier (W2_ARCHITECTURE.md §5.3 step 2).

Two-tier dispatch:

1. ``classify_keywords`` — the legacy regex fast-path classifier.
   Deterministic, dependency-free, returns one of
   ``{"lab_report", "intake_form", None}`` plus a confidence. Preserved
   verbatim from the pre-Wave-2D implementation so callers that have
   not opted in to ``DOC_CLASSIFIER=claude`` see no behavior change.

2. ``classify_doc`` — Wave 2D dispatch returning a Wave-2D
   :class:`DocClassVerdict` whose ``doc_class`` is one of
   ``{intake_form, lab_report_tabular, other_narrative,
   non_extractable}``. Routes via :data:`config.settings.doc_classifier`
   (``regex`` or ``claude``). Default is ``regex`` — the Claude path is
   gated behind explicit opt-in until Wave 3 evals clear it.

The non_extractable class is a DETERMINISTIC pre-LLM gate run BEFORE
either classifier — page count == 0 OR encrypted OR OCR token count
< 5 short-circuits to ``non_extractable`` so pathological inputs never
burn an LLM call.

Per R3 (Wave 2D), the LLM-routable taxonomy is collapsed: only
``intake_form`` (~54 cases) and ``lab_report_tabular`` (~37 cases)
have enough fixture diversity for a per-class prompt; everything
narrative (consultant_note 27/1, imaging_report 7/1, mixed_content
6/1) routes to ``other_narrative`` with an optional ``sub_kind`` hint.

PSR-3 logging only — no prompt or completion text; no PHI emission.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, List, Literal, Optional

from documents.ocr import LayoutBlock

logger = logging.getLogger(__name__)


# ── Legacy regex tier (preserved verbatim) ───────────────────────────────────

_LAB_RE = re.compile(
    r"\b(LABORATORY|LAB REPORT|LACTATE|LOINC|REFERENCE RANGE|HEMATOLOGY|CHEMISTRY)\b",
    re.IGNORECASE,
)
_INTAKE_RE = re.compile(
    r"\b(ADMISSION|INTAKE|TRIAGE|"
    r"HISTORY OF PRESENT ILLNESS|HPI|"
    r"CHIEF COMPLAINT|CHIEF CONCERN|"
    r"PATIENT DEMOGRAPHICS|"
    r"PROBLEM LIST|PMH|PAST MEDICAL HISTORY|"
    r"SOCIAL HISTORY|FAMILY HISTORY|"
    r"REVIEW OF SYSTEMS|ROS|"
    r"ADVANCE DIRECTIVE|CODE STATUS)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClassifierVerdict:
    """The output of the legacy keyword fast-path classifier."""

    kind: Literal["lab_report", "intake_form", "unknown"]
    confidence: float
    reason: str


# Wave 2D doc-class label set. Mirror of ``prompt_registry.DOC_CLASSES``
# plus the non_extractable sentinel. Kept here as a string Literal so
# downstream type-checkers can narrow on the verdict's ``doc_class``.
DocClass = Literal[
    "intake_form",
    "lab_report_tabular",
    "other_narrative",
    "non_extractable",
]


@dataclass(frozen=True)
class DocClassVerdict:
    """Wave 2D dispatcher verdict.

    ``classifier`` records which path produced the verdict
    (``regex``, ``claude``, ``gate``) for metrics labels. ``sub_kind``
    is an optional R3 hint passed through for ``other_narrative``
    documents (e.g. ``"consultant_note"``) — ignored by the registry
    today but reserved for future per-sub-kind prompt promotion.
    """

    doc_class: DocClass
    confidence: float
    classifier: Literal["regex", "claude", "gate"]
    reason: str
    sub_kind: Optional[str] = None


def _confidence_for(n_matches: int) -> float:
    """0.95 if 2+ matches, 0.80 if exactly 1."""
    if n_matches >= 2:
        return 0.95
    return 0.80


def _count_matches(blocks: Iterable[LayoutBlock], regex: re.Pattern[str]) -> List[str]:
    """Return the list of matched keyword strings across all blocks."""
    found: List[str] = []
    for b in blocks:
        for m in regex.finditer(b.text):
            found.append(m.group(0).upper())
    return found


def classify_keywords(layout: List[LayoutBlock]) -> Optional[ClassifierVerdict]:
    """Run the fast-path keyword classifier over an OCR layout.

    Returns a verdict with ``kind`` of ``"lab_report"`` or ``"intake_form"``
    if high-signal keywords are present, otherwise ``None``.  The caller is
    responsible for the fallback path (LLM classifier or UnknownDocument).
    """
    lab_hits = _count_matches(layout, _LAB_RE)
    intake_hits = _count_matches(layout, _INTAKE_RE)

    # Prefer the kind with more matches; ties go to lab_report (clinically
    # higher value for triage), but only when lab also has at least one hit.
    if lab_hits and len(lab_hits) >= len(intake_hits):
        confidence = _confidence_for(len(lab_hits))
        verdict = ClassifierVerdict(
            kind="lab_report",
            confidence=confidence,
            reason=f"matched {len(lab_hits)} lab keyword(s)",
        )
        logger.info(
            "extractor_classifier_verdict",
            extra={
                "kind": verdict.kind,
                "confidence": verdict.confidence,
                "n_matches": len(lab_hits),
            },
        )
        return verdict

    if intake_hits:
        confidence = _confidence_for(len(intake_hits))
        verdict = ClassifierVerdict(
            kind="intake_form",
            confidence=confidence,
            reason=f"matched {len(intake_hits)} intake keyword(s)",
        )
        logger.info(
            "extractor_classifier_verdict",
            extra={
                "kind": verdict.kind,
                "confidence": verdict.confidence,
                "n_matches": len(intake_hits),
            },
        )
        return verdict

    logger.info(
        "extractor_classifier_verdict",
        extra={"kind": None, "confidence": 0.0, "n_matches": 0},
    )
    return None


# ── Wave 2D dispatch ─────────────────────────────────────────────────────────


# Minimum normalized-whitespace token count below which a document is
# treated as non-extractable. Value chosen empirically from R3: every
# observed all-noise / blank fixture had < 5 tokens after OCR; the
# smallest legitimate intake fixture had ≥ 40.
_NON_EXTRACTABLE_TOKEN_FLOOR = 5


def _is_non_extractable(
    layout: List[LayoutBlock],
    *,
    page_count: Optional[int] = None,
    encrypted: bool = False,
) -> bool:
    """Deterministic pre-LLM gate.

    A document is non_extractable iff ANY of:

    - ``page_count`` is provided and equals 0.
    - ``encrypted`` is True (caller couldn't decrypt the PDF).
    - The OCR layout has fewer than 5 whitespace-delimited tokens
      across all blocks.
    """
    if page_count is not None and page_count == 0:
        return True
    if encrypted:
        return True
    n_tokens = 0
    for b in layout:
        if not b.text:
            continue
        n_tokens += len(b.text.split())
        if n_tokens >= _NON_EXTRACTABLE_TOKEN_FLOOR:
            return False
    return n_tokens < _NON_EXTRACTABLE_TOKEN_FLOOR


def _doc_class_from_regex(layout: List[LayoutBlock]) -> DocClassVerdict:
    """Map the legacy regex verdict into the Wave-2D doc-class label
    set. Lab → ``lab_report_tabular``; intake → ``intake_form``;
    everything else → ``other_narrative`` (since R3 collapses
    consultant_note / imaging_report / mixed_content to that bucket)."""
    legacy = classify_keywords(layout)
    if legacy is None:
        return DocClassVerdict(
            doc_class="other_narrative",
            confidence=0.0,
            classifier="regex",
            reason="no keyword match",
        )
    if legacy.kind == "lab_report":
        return DocClassVerdict(
            doc_class="lab_report_tabular",
            confidence=legacy.confidence,
            classifier="regex",
            reason=legacy.reason,
        )
    if legacy.kind == "intake_form":
        return DocClassVerdict(
            doc_class="intake_form",
            confidence=legacy.confidence,
            classifier="regex",
            reason=legacy.reason,
        )
    return DocClassVerdict(
        doc_class="other_narrative",
        confidence=legacy.confidence,
        classifier="regex",
        reason=legacy.reason,
    )


# Confidence floor below which a Claude classifier verdict is rejected
# and we fall back to the regex result. NOT bimodal — see contract.
_CLAUDE_CONFIDENCE_FLOOR = 0.6


def _doc_class_from_claude(
    layout: List[LayoutBlock],
) -> DocClassVerdict:
    """Claude classifier path.

    Wave 2D ships the dispatch + plumbing only. The actual Claude
    tool-use call is a Wave 3 follow-up — until then the function
    returns a low-confidence verdict so the dispatcher's < 0.6
    fallback unconditionally kicks in. This keeps the env flag wired
    end-to-end (and metric-labelable) without burning LLM budget.
    """
    return DocClassVerdict(
        doc_class="other_narrative",
        confidence=0.0,
        classifier="claude",
        reason="claude classifier not yet implemented (Wave 3)",
    )


def _resolve_classifier_choice() -> Literal["regex", "claude"]:
    """Read ``DOC_CLASSIFIER`` from settings, defaulting to ``regex``.

    Imported lazily to avoid a circular import at module load time
    (config.py loads .env, which can be slow under test collection).
    """
    # Allow env override without going through pydantic-settings — keeps
    # tests cheap (no Settings() reload required to flip the flag).
    env = os.environ.get("DOC_CLASSIFIER")
    if env is not None:
        v = env.strip().lower()
        if v in ("regex", "claude"):
            return v  # type: ignore[return-value]
    try:
        from config import settings  # type: ignore

        v = (settings.doc_classifier or "regex").strip().lower()
        if v in ("regex", "claude"):
            return v  # type: ignore[return-value]
    except Exception:  # pragma: no cover — defensive at boundary
        pass
    return "regex"


def classify_doc(
    layout: List[LayoutBlock],
    *,
    page_count: Optional[int] = None,
    encrypted: bool = False,
) -> DocClassVerdict:
    """Wave 2D dispatch entry point.

    Order of operations:

    1. Pre-LLM gate. If the input is non_extractable (page_count == 0
       OR encrypted OR OCR token count < 5), return immediately with
       ``classifier="gate"`` — no regex, no Claude.
    2. Read ``DOC_CLASSIFIER`` from settings / env. Default ``regex``.
    3. ``regex`` → return the regex verdict directly.
    4. ``claude`` → call the Claude classifier. If its confidence is
       below the 0.6 floor, fall back to the regex verdict (per the
       Wave 2D contract: NOT bimodal — Claude is advisory until eval
       clears it).

    Emits one ``agent_doc_classifier_outcome_total{classifier,
    doc_class}`` counter increment per call.
    """
    # 1. Pre-LLM gate.
    if _is_non_extractable(layout, page_count=page_count, encrypted=encrypted):
        verdict = DocClassVerdict(
            doc_class="non_extractable",
            confidence=1.0,
            classifier="gate",
            reason="page_count==0 OR encrypted OR ocr_tokens<5",
        )
        _emit_metric(verdict)
        logger.info(
            "extractor_doc_class_verdict",
            extra={
                "doc_class": verdict.doc_class,
                "classifier": verdict.classifier,
                "confidence": verdict.confidence,
            },
        )
        return verdict

    # 2-4. Classifier dispatch.
    choice = _resolve_classifier_choice()
    regex_verdict = _doc_class_from_regex(layout)
    if choice == "regex":
        _emit_metric(regex_verdict)
        logger.info(
            "extractor_doc_class_verdict",
            extra={
                "doc_class": regex_verdict.doc_class,
                "classifier": regex_verdict.classifier,
                "confidence": regex_verdict.confidence,
            },
        )
        return regex_verdict

    # choice == "claude"
    claude_verdict = _doc_class_from_claude(layout)
    if claude_verdict.confidence < _CLAUDE_CONFIDENCE_FLOOR:
        # Fall back to regex but preserve the classifier label so the
        # metric attribution shows that Claude was attempted and
        # rejected.
        fallback = DocClassVerdict(
            doc_class=regex_verdict.doc_class,
            confidence=regex_verdict.confidence,
            classifier="claude",
            reason=(
                f"claude conf {claude_verdict.confidence:.2f} < "
                f"{_CLAUDE_CONFIDENCE_FLOOR:.2f}; fell back to regex"
            ),
        )
        _emit_metric(fallback)
        logger.info(
            "extractor_doc_class_verdict",
            extra={
                "doc_class": fallback.doc_class,
                "classifier": fallback.classifier,
                "confidence": fallback.confidence,
                "fallback": True,
            },
        )
        return fallback

    _emit_metric(claude_verdict)
    logger.info(
        "extractor_doc_class_verdict",
        extra={
            "doc_class": claude_verdict.doc_class,
            "classifier": claude_verdict.classifier,
            "confidence": claude_verdict.confidence,
        },
    )
    return claude_verdict


def _emit_metric(verdict: DocClassVerdict) -> None:
    """Best-effort Prometheus counter increment.

    Imported lazily so test code that doesn't bring up the metrics
    registry (or tests that import classifier in isolation) doesn't
    pay the prometheus_client cost.
    """
    try:
        from agent.metrics import agent_doc_classifier_outcome_total  # type: ignore

        agent_doc_classifier_outcome_total.labels(
            classifier=verdict.classifier, doc_class=verdict.doc_class
        ).inc()
    except Exception:  # pragma: no cover — metrics are best-effort
        pass


__all__ = [
    "ClassifierVerdict",
    "DocClass",
    "DocClassVerdict",
    "classify_doc",
    "classify_keywords",
]
