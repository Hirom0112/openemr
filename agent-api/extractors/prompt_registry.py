"""Doc-class → (prompt, section-header dict) registry.

Wave 2D: per-class extraction prompts feeding the y-band repointer's
anchor scorer. Per R3 audit, only ``intake_form`` and
``lab_report_tabular`` have enough fixture diversity to justify
per-class prompts; everything narrative collapses to
``other_narrative``. ``non_extractable`` is a deterministic pre-LLM
gate (page count == 0 OR encrypted OR OCR token count < 5) — it has
NO prompt entry here and never reaches an LLM.

Public API:

- :data:`DOC_CLASSES`        — the closed set of LLM-routable classes.
- :data:`NON_EXTRACTABLE`    — sentinel string for the pre-LLM gate.
- :func:`get_prompt`         — return the prompt string for a doc_class.
- :func:`get_section_headers` — return the section-header dict for a doc_class.
- :func:`registered_classes` — listing helper for tests.

Unknown classes fall back to ``other_narrative`` (a generic narrative
prompt with a thin header dict) — this keeps the dispatch always-defined
even when the classifier emits a label that hasn't been promoted to its
own per-class prompt yet.
"""

from __future__ import annotations

from typing import Tuple

from extractors.prompts import intake_form, lab_report_tabular, other_narrative

# Closed set of doc classes that route to an LLM extractor. Keep in sync
# with the Claude classifier's tool-use enum (extractors/classifier.py).
DOC_CLASSES: Tuple[str, ...] = (
    "intake_form",
    "lab_report_tabular",
    "other_narrative",
)

# Sentinel returned by the pre-LLM gate. Not a class in DOC_CLASSES — by
# construction non_extractable docs never reach the prompt registry.
NON_EXTRACTABLE: str = "non_extractable"


_REGISTRY: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {
    "intake_form": (intake_form.PROMPT, intake_form.SECTION_HEADERS),
    "lab_report_tabular": (
        lab_report_tabular.PROMPT,
        lab_report_tabular.SECTION_HEADERS,
    ),
    "other_narrative": (other_narrative.PROMPT, other_narrative.SECTION_HEADERS),
}


def _resolve(doc_class: str) -> tuple[str, dict[str, tuple[str, ...]]]:
    entry = _REGISTRY.get(doc_class)
    if entry is not None:
        return entry
    # Unknown / not-yet-promoted classes fall back to the generic
    # narrative prompt rather than raising — the dispatcher must always
    # have a prompt to send. This is the documented extension point per
    # R3: classes without enough fixture diversity ride other_narrative.
    return _REGISTRY["other_narrative"]


def get_prompt(doc_class: str) -> str:
    """Return the system prompt string for ``doc_class``.

    Falls back to the ``other_narrative`` prompt when ``doc_class`` is
    not registered. Never raises — the LLM call must always have a
    prompt payload.
    """
    return _resolve(doc_class)[0]


def get_section_headers(doc_class: str) -> dict[str, tuple[str, ...]]:
    """Return the section-header dictionary for ``doc_class``.

    The returned mapping has the same shape as
    ``extractors.intake._FIELD_ANCHOR_HINTS`` and feeds the y-band
    repointer's anchor scorer. Falls back to the ``other_narrative``
    headers for unregistered classes.
    """
    return _resolve(doc_class)[1]


def registered_classes() -> Tuple[str, ...]:
    """Listing helper used by tests."""
    return tuple(_REGISTRY.keys())


__all__ = [
    "DOC_CLASSES",
    "NON_EXTRACTABLE",
    "get_prompt",
    "get_section_headers",
    "registered_classes",
]
