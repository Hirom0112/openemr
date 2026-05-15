"""Wave 2D — prompt registry contract tests.

Covers:

- Per-class prompts are routed correctly.
- Unknown classes fall back to other_narrative.
- Section-header dictionaries have the expected shape.
- intake_form and lab_report_tabular each ship NON-empty headers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractors.prompt_registry import (  # noqa: E402
    DOC_CLASSES,
    NON_EXTRACTABLE,
    get_prompt,
    get_section_headers,
    registered_classes,
)
from extractors.prompts import (  # noqa: E402
    intake_form,
    lab_report_tabular,
    other_narrative,
)


pytestmark = pytest.mark.hard_failure


def test_doc_classes_closed_set() -> None:
    assert set(DOC_CLASSES) == {
        "intake_form",
        "intake_form_prose",
        "lab_report_tabular",
        "other_narrative",
    }
    # non_extractable is NOT a routable class — it's a sentinel.
    assert NON_EXTRACTABLE == "non_extractable"
    assert NON_EXTRACTABLE not in DOC_CLASSES


def test_registered_classes_matches_doc_classes() -> None:
    assert set(registered_classes()) == set(DOC_CLASSES)


def test_intake_prompt_routed() -> None:
    assert get_prompt("intake_form") is intake_form.PROMPT
    headers = get_section_headers("intake_form")
    # Required sections per R3 intake fixture diversity.
    assert "name" in headers
    assert "dob" in headers
    assert "medication" in headers
    assert "allergy" in headers


def test_lab_prompt_routed() -> None:
    assert get_prompt("lab_report_tabular") is lab_report_tabular.PROMPT
    headers = get_section_headers("lab_report_tabular")
    assert "test_name" in headers
    assert "value" in headers
    assert "reference_range" in headers


def test_other_narrative_prompt_routed() -> None:
    assert get_prompt("other_narrative") is other_narrative.PROMPT
    headers = get_section_headers("other_narrative")
    # Coarse cross-sub-kind headers per R3.
    assert "impression" in headers
    assert "findings" in headers


def test_unknown_class_falls_back_to_other_narrative() -> None:
    """Unrecognized classes must fall back to other_narrative — the
    dispatcher must always have a prompt to send."""
    assert get_prompt("not_a_real_class") is other_narrative.PROMPT
    assert get_section_headers("not_a_real_class") is other_narrative.SECTION_HEADERS


def test_section_header_dict_shape() -> None:
    """All registered classes return a dict[str, tuple[str, ...]]."""
    for cls in DOC_CLASSES:
        headers = get_section_headers(cls)
        assert isinstance(headers, dict)
        for k, v in headers.items():
            assert isinstance(k, str)
            assert isinstance(v, tuple)
            for item in v:
                assert isinstance(item, str)


def test_intake_headers_are_uppercase_substrings() -> None:
    """Anchor scorer matches case-insensitively against uppercased
    anchor text. Document the convention so the registry stays
    self-consistent."""
    headers = get_section_headers("intake_form")
    for vals in headers.values():
        for s in vals:
            assert s == s.upper(), f"non-uppercase header substring: {s!r}"


def test_prompts_are_non_empty() -> None:
    for cls in DOC_CLASSES:
        p = get_prompt(cls)
        assert isinstance(p, str)
        assert len(p) > 100, f"{cls} prompt suspiciously short"
